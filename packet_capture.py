# -*- coding: utf-8 -*-
"""网络抓包模块 — TCP 重组、游戏协议解析、事件驱动输出

兼容层说明：
- 保留新版游标化缓冲/TCP 重传保护/Event 停机等实现
- 适配原工程 start_capture(callback) 调用方式
- 内置 PacketReader / zstd_decompress，避免额外依赖 proto_reader/zstd_utils/scapy_minimal
- 对 SyncContainerData 自动解析后继续通过 callback({'v_data': ...}) 传回上层
"""
from __future__ import annotations

import logging
import queue
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional, Callable, Dict, Any

from scapy_minimal import sniff, IP, TCP, Raw  # type: ignore  # 使用最小化包装器，减小打包体积
import zstandard as zstd
from BlueProtobuf_pb2 import SyncContainerData
from notify_dumper import NotifyDumper

log = logging.getLogger(__name__)

_STOP_SENTINEL = object()
_U16 = struct.Struct('>H')
_U32 = struct.Struct('>I')
_U64 = struct.Struct('>Q')

_SERVER_SIG = b"\x00\x63\x33\x53\x42\x00"
_LOGIN_RESP_HEAD = b"\x00\x00\x00\x62\x00\x03\x00\x00\x00\x01"
_LOGIN_RESP_TAIL = b"\x00\x00\x00\x00\x0a\x4e"
_MSG_NOTIFY = 2
_MSG_RESPONSE = 3
_MSG_FRAMEDOWN = 6
_COMPRESS_FLAG = 0x8000
_GAME_SERVICE_UUID = 0x0000000063335342
_SYNC_CONTAINER_DATA_METHOD = 0x00000015


def zstd_decompress(data: bytes, max_output_size: int = 1024 * 1024) -> bytes | None:
    try:
        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(data, max_output_size=max_output_size)
    except Exception:
        return None


class PacketReader:
    __slots__ = ('buf', 'offset', 'end')

    def __init__(self, buf: bytes | bytearray, offset: int = 0, end: int | None = None):
        self.buf = buf
        self.offset = offset
        self.end = len(buf) if end is None else min(end, len(buf))

    @property
    def remaining(self) -> int:
        return self.end - self.offset

    def peek_u32(self) -> int:
        if self.remaining < 4:
            raise ValueError('not enough bytes for u32')
        return _U32.unpack_from(self.buf, self.offset)[0]

    def read_u16(self) -> int:
        if self.remaining < 2:
            raise ValueError('not enough bytes for u16')
        val = _U16.unpack_from(self.buf, self.offset)[0]
        self.offset += 2
        return val

    def read_u32(self) -> int:
        if self.remaining < 4:
            raise ValueError('not enough bytes for u32')
        val = _U32.unpack_from(self.buf, self.offset)[0]
        self.offset += 4
        return val

    def read_u64(self) -> int:
        if self.remaining < 8:
            raise ValueError('not enough bytes for u64')
        val = _U64.unpack_from(self.buf, self.offset)[0]
        self.offset += 8
        return val

    def read_bytes(self, n: int) -> bytes:
        if n < 0 or self.remaining < n:
            raise ValueError('not enough bytes')
        start = self.offset
        self.offset += n
        return bytes(self.buf[start:self.offset])

    def read_remaining(self) -> bytes:
        return self.read_bytes(self.remaining)

    def sub_reader(self, length: int) -> 'PacketReader':
        if length < 0 or self.remaining < length:
            raise ValueError('sub_reader out of bounds')
        start = self.offset
        self.offset += length
        return PacketReader(self.buf, start, start + length)


@dataclass(slots=True)
class PacketEvent:
    kind: str
    stream_id: str = ''
    service_uuid: int = 0
    stub_id: int = 0
    method_id: int = 0
    server_sequence_id: int = 0
    raw_payload: bytes = b''


class PacketCapture:
    FRAGMENT_TIMEOUT = 30
    MAX_PACKET_SIZE = 0x0FFFFF
    MAX_CACHE_ENTRIES = 1024
    MAX_FRAMEDOWN_DEPTH = 16

    def __init__(self, interface: str | None = None, *, dump_enabled: bool = False):
        self.interface = interface
        self.is_running = False
        self.callback: Optional[Callable[[Dict[str, Any]], None]] = None
        self.event_queue: queue.Queue = queue.Queue(maxsize=4096)
        self.packet_count = 0
        self.sync_container_count = 0
        self.events_produced = 0
        self.events_dropped = 0
        self.current_server = ''

        self._buf = bytearray()
        self._buf_offset = 0
        self._tcp_cache: dict[int, bytes] = {}
        self._next_seq = -1
        self._last_time = 0.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._dumper = NotifyDumper(enabled=dump_enabled)

    def __enter__(self) -> 'PacketCapture':
        return self

    def __exit__(self, *exc) -> None:
        self.stop_capture()
        self.cleanup()

    def start_capture(self, callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self.callback = callback
        self.is_running = True
        self._stop_event.clear()
        log.info('开始抓包: iface=%s', self.interface)
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._cleanup_loop, daemon=True).start()

    def stop_capture(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self._stop_event.set()
        try:
            self.event_queue.put_nowait(_STOP_SENTINEL)
        except queue.Full:
            pass
        log.info('停止抓包: packets=%d sync=%d events=%d dropped=%d',
                 self.packet_count, self.sync_container_count,
                 self.events_produced, self.events_dropped)

    def cleanup(self) -> None:
        self._dumper.cleanup()

    def get_event(self, timeout: float = 0.2) -> PacketEvent | None:
        try:
            obj = self.event_queue.get(timeout=timeout)
            if obj is _STOP_SENTINEL:
                return None
            return obj
        except queue.Empty:
            return None

    def stats_summary(self) -> str:
        return (f'packets={self.packet_count} sync={self.sync_container_count} '
                f'events={self.events_produced} dropped={self.events_dropped} '
                f'qsize={self.event_queue.qsize()}')

    def _capture_loop(self) -> None:
        try:
            sniff(
                iface=self.interface,
                prn=self._on_packet,
                store=0,
                stop_filter=lambda _: not self.is_running,
            )
        except Exception as e:
            log.error('抓包循环异常退出: %s', e, exc_info=True)

    def _on_packet(self, pkt) -> None:
        if not self.is_running or TCP not in pkt or IP not in pkt or Raw not in pkt:
            return
        self.packet_count += 1
        ip, tcp = pkt[IP], pkt[TCP]
        stream = f'{ip.src}:{tcp.sport} -> {ip.dst}:{tcp.dport}'
        try:
            self._feed_tcp(stream, tcp.seq, bytes(pkt[Raw]))
        except Exception as e:
            log.debug('处理数据包异常: %s', e, exc_info=True)

    def _feed_tcp(self, stream: str, seq: int, payload: bytes) -> None:
        with self._lock:
            if self.current_server != stream:
                if self.current_server:
                    return
                if self._identify_game_server(payload):
                    log.info('识别到游戏服务器: %s', stream)
                    self.current_server = stream
                    self._reset_buf()
                    self._next_seq = (seq + len(payload)) & 0xFFFFFFFF
                return

            if self._next_seq == -1:
                if len(payload) > 4 and _U32.unpack_from(payload, 0)[0] < self.MAX_PACKET_SIZE:
                    self._next_seq = seq
                else:
                    return

            diff = (seq - self._next_seq) & 0xFFFFFFFF
            if diff >= 0x80000000:
                return

            if seq not in self._tcp_cache:
                if len(self._tcp_cache) < self.MAX_CACHE_ENTRIES:
                    self._tcp_cache[seq] = payload
                else:
                    log.debug('TCP cache 已满, 丢弃 seq=%d', seq)

            while self._next_seq in self._tcp_cache:
                chunk = self._tcp_cache.pop(self._next_seq)
                self._buf.extend(chunk)
                self._next_seq = (self._next_seq + len(chunk)) & 0xFFFFFFFF
                self._last_time = time.time()

            self._consume_packets()

    def _reset_buf(self) -> None:
        self._buf.clear()
        self._buf_offset = 0
        self._next_seq = -1
        self._last_time = 0.0
        self._tcp_cache.clear()

    def _consume_packets(self) -> None:
        buf = self._buf
        offset = self._buf_offset
        while len(buf) - offset > 4:
            pkt_size = _U32.unpack_from(buf, offset)[0]
            if pkt_size < 6 or pkt_size > self.MAX_PACKET_SIZE:
                break
            if len(buf) - offset < pkt_size:
                break
            self._parse_game_packet(buf, offset, pkt_size)
            offset += pkt_size
        self._buf_offset = offset
        if self._buf_offset > len(self._buf) // 2:
            del self._buf[:self._buf_offset]
            self._buf_offset = 0

    def _identify_game_server(self, payload: bytes) -> bool:
        if len(payload) < 10:
            return False
        if (len(payload) == 0x62
                and payload[:10] == _LOGIN_RESP_HEAD
                and payload[14:20] == _LOGIN_RESP_TAIL):
            return True
        if payload[4] != 0:
            return False
        try:
            reader = PacketReader(payload, 10)
            while reader.remaining >= 4:
                length = reader.read_u32()
                if length < 4 or length > self.MAX_PACKET_SIZE:
                    return False
                data = reader.read_bytes(length - 4)
                if len(data) >= 11 and data[5:11] == _SERVER_SIG:
                    return True
        except (ValueError, IndexError):
            pass
        return False

    def _parse_game_packet(self, buf: bytearray, start: int, size: int) -> None:
        stack: list[tuple[bytes | bytearray, int, int]] = [(buf, start, size)]
        depth = 0
        while stack and depth < self.MAX_FRAMEDOWN_DEPTH:
            cur_buf, cur_start, cur_size = stack.pop()
            depth += 1
            if cur_size < 6:
                continue
            reader = PacketReader(cur_buf, cur_start, cur_start + cur_size)
            while reader.remaining >= 4:
                pkt_size = reader.peek_u32()
                if pkt_size < 6 or pkt_size > reader.remaining:
                    break
                pr = reader.sub_reader(pkt_size)
                pr.read_u32()
                pkt_type = pr.read_u16()
                compressed = (pkt_type & _COMPRESS_FLAG) != 0
                msg_type = pkt_type & 0x7FFF
                if msg_type in (_MSG_NOTIFY, _MSG_RESPONSE, _MSG_FRAMEDOWN):
                    nested = self._emit_event(pr, msg_type, compressed)
                    if nested is not None:
                        stack.append((nested, 0, len(nested)))

    def _emit_event(self, reader: PacketReader, msg_type: int, compressed: bool) -> bytes | None:
        try:
            if msg_type == _MSG_NOTIFY:
                svc = reader.read_u64()
                stub = reader.read_u32()
                method = reader.read_u32()
                seq_id = 0
            elif msg_type == _MSG_RESPONSE:
                seq_id = reader.read_u32()
                svc = reader.read_u32()
                stub = reader.read_u32()
                method = 0
            else:
                svc = stub = method = 0
                seq_id = reader.read_u32()
                if reader.remaining == 0:
                    return None

            raw = reader.read_remaining()
            final = self._decompress_and_dump(raw, compressed)
            if final is None:
                return None

            kind = 'Notify' if msg_type == _MSG_NOTIFY else ('Response' if msg_type == _MSG_RESPONSE else 'FrameDown')
            evt = PacketEvent(kind=kind, stream_id=self.current_server,
                              service_uuid=svc, stub_id=stub, method_id=method,
                              server_sequence_id=seq_id, raw_payload=final)
            self._push_event(evt)
            if msg_type == _MSG_NOTIFY:
                self._maybe_dispatch_sync_container(evt)
            return final if msg_type == _MSG_FRAMEDOWN else None
        except Exception as e:
            log.debug('解析 msg_type=%d 异常: %s', msg_type, e, exc_info=True)
            return None

    def _decompress_and_dump(self, raw: bytes, compressed: bool) -> bytes | None:
        if compressed:
            dec = zstd_decompress(raw)
            if dec is None:
                log.warning('标记为压缩但解压失败, 丢弃 (%d bytes)', len(raw))
                return None
            self._dumper.dump(raw, dec)
            return dec
        self._dumper.dump(raw)
        return raw

    def _push_event(self, evt: PacketEvent) -> None:
        self.events_produced += 1
        try:
            self.event_queue.put_nowait(evt)
        except queue.Full:
            self.events_dropped += 1
            if self.events_dropped % 100 == 1:
                log.warning('事件队列已满, 累计丢弃 %d 事件', self.events_dropped)

    def _maybe_dispatch_sync_container(self, evt: PacketEvent) -> None:
        if evt.service_uuid != _GAME_SERVICE_UUID or evt.method_id != _SYNC_CONTAINER_DATA_METHOD:
            return
        try:
            sync_data = SyncContainerData()
            sync_data.ParseFromString(evt.raw_payload)
            self.sync_container_count += 1
            if self.callback:
                self.callback({'v_data': sync_data.VData})
        except Exception as e:
            log.debug('解析 SyncContainerData 失败: %s', e, exc_info=True)

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(10):
            log.debug('stats: %s', self.stats_summary())
            with self._lock:
                if self._last_time and time.time() - self._last_time > self.FRAGMENT_TIMEOUT:
                    log.info('TCP 流超时，重置: %s', self.current_server)
                    self.current_server = ''
                    self._reset_buf()
