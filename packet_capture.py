# -*- coding: utf-8 -*-
"""网络抓包模块

修复点：
- 兼容 1.0 的 Notify / FrameDown / SyncContainerData 解析路径
- 支持同时跟踪多个候选游戏 TCP 流，而不是只允许一个 current_server
- 识别到游戏服务器后不会丢弃首个 payload
- TCP 流从错误边界进入时，支持轻量 resync，避免整段缓冲永久卡死
"""
from __future__ import annotations

import io
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, Any

from scapy_minimal import sniff, IP, TCP, Raw  # type: ignore
import zstandard as zstd
from BlueProtobuf_pb2 import SyncContainerData
from logging_config import get_logger

logger = get_logger(__name__)

_U32 = struct.Struct('>I')


class BinaryReader:
    """二进制数据读取器"""

    def __init__(self, buffer: bytes | bytearray, offset: int = 0, end: int | None = None):
        self.buffer = buffer
        self.offset = offset
        self.end = len(buffer) if end is None else min(end, len(buffer))

    def readUInt64(self) -> int:
        if self.remaining() < 8:
            raise ValueError('not enough bytes for uint64')
        value = struct.unpack('>Q', self.buffer[self.offset:self.offset + 8])[0]
        self.offset += 8
        return value

    def readUInt32(self) -> int:
        if self.remaining() < 4:
            raise ValueError('not enough bytes for uint32')
        value = struct.unpack('>I', self.buffer[self.offset:self.offset + 4])[0]
        self.offset += 4
        return value

    def peekUInt32(self) -> int:
        if self.remaining() < 4:
            raise ValueError('not enough bytes for uint32')
        return struct.unpack('>I', self.buffer[self.offset:self.offset + 4])[0]

    def readUInt16(self) -> int:
        if self.remaining() < 2:
            raise ValueError('not enough bytes for uint16')
        value = struct.unpack('>H', self.buffer[self.offset:self.offset + 2])[0]
        self.offset += 2
        return value

    def readBytes(self, length: int) -> bytes:
        if length < 0 or self.remaining() < length:
            raise ValueError('not enough bytes')
        value = self.buffer[self.offset:self.offset + length]
        self.offset += length
        return bytes(value)

    def remaining(self) -> int:
        return self.end - self.offset

    def readRemaining(self) -> bytes:
        value = self.buffer[self.offset:self.end]
        self.offset = self.end
        return bytes(value)


@dataclass
class TCPStreamState:
    stream_id: str
    tcp_cache: Dict[int, bytes] = field(default_factory=dict)
    tcp_next_seq: int = -1
    tcp_last_time: float = 0.0
    data: bytearray = field(default_factory=bytearray)
    packet_count: int = 0
    recognized: bool = False


class PacketCapture:
    """网络数据包抓取器"""

    FRAGMENT_TIMEOUT = 30
    MAX_PACKET_SIZE = 0x0FFFFF
    MAX_CACHE_ENTRIES = 1024
    MAX_STREAMS = 32
    RESYNC_SCAN_LIMIT = 64

    def __init__(self, interface: str = None):
        self.interface = interface
        self.is_running = False
        self.callback = None
        self.packet_count = 0
        self.sync_container_count = 0

        self.tcp_lock = threading.Lock()
        self.streams: Dict[str, TCPStreamState] = {}
        self.module_found = False

    def start_capture(self, callback: Callable[[Dict[str, Any]], None] = None):
        self.callback = callback
        self.is_running = True
        logger.info(f"开始抓包: iface={self.interface or '自动'}")

        capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        capture_thread.start()

        cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        cleanup_thread.start()

    def stop_capture(self):
        self.is_running = False
        logger.info("停止抓包")

    def _capture_loop(self):
        try:
            sniff(
                iface=self.interface,
                prn=self._process_packet,
                store=0,
                stop_filter=lambda _: not self.is_running
            )
        except Exception as e:
            logger.error(f"抓包过程中发生错误: {e}", exc_info=True)

    def _process_packet(self, packet):
        if not self.is_running:
            return

        self.packet_count += 1

        try:
            if TCP in packet and IP in packet and Raw in packet:
                self._process_tcp_packet(packet)
        except Exception as e:
            logger.debug(f"处理数据包时发生错误: {e}", exc_info=True)

    def _process_tcp_packet(self, packet):
        ip_layer = packet[IP]
        tcp_layer = packet[TCP]

        src_addr = ip_layer.src
        dst_addr = ip_layer.dst
        src_port = tcp_layer.sport
        dst_port = tcp_layer.dport
        seq = tcp_layer.seq

        stream_id = f"{src_addr}:{src_port} -> {dst_addr}:{dst_port}"
        payload = bytes(packet[Raw])
        if not payload:
            return

        with self.tcp_lock:
            state = self.streams.get(stream_id)
            if state is None:
                if not self._identify_game_server(payload):
                    return
                if len(self.streams) >= self.MAX_STREAMS:
                    self._evict_oldest_stream_locked()
                state = TCPStreamState(stream_id=stream_id, recognized=True)
                self.streams[stream_id] = state
                logger.info(f"识别到游戏服务器: {stream_id}")

            self._process_tcp_stream(state, seq, payload)

    def _process_tcp_stream(self, state: TCPStreamState, seq: int, payload: bytes):
        state.packet_count += 1
        state.tcp_last_time = time.time()

        if state.tcp_next_seq == -1:
            # 首个包不要丢，直接从当前 seq 开始重组
            state.tcp_next_seq = seq

        # 已经落后于 next_seq，可能是重传或部分重叠旧包
        ahead = (seq - state.tcp_next_seq) & 0xFFFFFFFF
        behind = (state.tcp_next_seq - seq) & 0xFFFFFFFF
        if seq == state.tcp_next_seq:
            pass
        elif behind < 0x80000000 and behind > 0:
            # 重传/重叠：裁掉已经消费过的前缀
            trim = min(len(payload), behind)
            payload = payload[trim:]
            seq = (seq + trim) & 0xFFFFFFFF
            if not payload:
                return
        elif ahead >= 0x80000000:
            # 非法回绕/很旧的数据，忽略
            return

        if seq not in state.tcp_cache:
            if len(state.tcp_cache) >= self.MAX_CACHE_ENTRIES:
                # 丢弃最旧 seq，避免缓存无限膨胀
                oldest_seq = min(state.tcp_cache.keys(), key=lambda x: ((x - state.tcp_next_seq) & 0xFFFFFFFF))
                del state.tcp_cache[oldest_seq]
            state.tcp_cache[seq] = payload

        moved = False
        while state.tcp_next_seq in state.tcp_cache:
            cur_seq = state.tcp_next_seq
            chunk = state.tcp_cache.pop(cur_seq)
            state.data.extend(chunk)
            state.tcp_next_seq = (cur_seq + len(chunk)) & 0xFFFFFFFF
            state.tcp_last_time = time.time()
            moved = True

        if moved:
            self._process_complete_packets(state)

    def _identify_game_server(self, payload: bytes) -> bool:
        if len(payload) < 10:
            return False

        try:
            if payload[4] == 0:
                data = payload[10:]
                if data:
                    signature = b'\x00\x63\x33\x53\x42\x00'
                    stream = io.BytesIO(data)
                    while True:
                        len_buf = stream.read(4)
                        if len(len_buf) < 4:
                            break
                        length = int.from_bytes(len_buf, byteorder='big')
                        if length < 4 or length > self.MAX_PACKET_SIZE:
                            break
                        data1 = stream.read(length - 4)
                        if len(data1) < max(11, length - 4):
                            break
                        if len(data1) >= 11 and data1[5:5 + len(signature)] == signature:
                            return True

            if len(payload) == 0x62:
                signature = b'\x00\x00\x00\x62\x00\x03\x00\x00\x00\x01'
                if payload[:10] == signature and payload[14:20] == b'\x00\x00\x00\x00\x0a\x4e':
                    return True

        except Exception as e:
            logger.debug(f"服务器识别失败: {e}")

        return False

    def _process_complete_packets(self, state: TCPStreamState):
        while len(state.data) >= 4:
            try:
                packet_size = _U32.unpack_from(state.data, 0)[0]

                if packet_size < 6 or packet_size > self.MAX_PACKET_SIZE:
                    resynced = self._resync_buffer(state)
                    if not resynced:
                        break
                    continue

                if len(state.data) < packet_size:
                    break

                packet = bytes(state.data[:packet_size])
                del state.data[:packet_size]
                self._analyze_payload(packet, "TCP", state.stream_id)

                if self.module_found:
                    break

            except Exception as e:
                logger.debug(f"处理完整数据包失败[{state.stream_id}]: {e}", exc_info=True)
                break

    def _resync_buffer(self, state: TCPStreamState) -> bool:
        data_len = len(state.data)
        upper = min(data_len - 3, self.RESYNC_SCAN_LIMIT)
        for i in range(1, upper):
            candidate = _U32.unpack_from(state.data, i)[0]
            if 6 <= candidate <= self.MAX_PACKET_SIZE:
                del state.data[:i]
                logger.debug(f"TCP流重同步成功: {state.stream_id}, 跳过 {i} 字节")
                return True

        if data_len > self.RESYNC_SCAN_LIMIT:
            drop_len = data_len - self.RESYNC_SCAN_LIMIT
            del state.data[:drop_len]
            logger.debug(f"TCP流重同步失败，裁剪旧数据: {state.stream_id}, 删除 {drop_len} 字节")
            return len(state.data) >= 4
        return False

    def _analyze_payload(self, payload: bytes, protocol: str, stream_id: str):
        if len(payload) < 4:
            return

        try:
            parsed_data = self._parse_sync_container_data(payload, stream_id)
            if parsed_data and self.callback:
                self.callback(parsed_data)
                self.module_found = True
        except Exception as e:
            logger.debug(f"解析数据包失败[{stream_id}]: {e}", exc_info=True)

    def _parse_sync_container_data(self, payload: bytes, stream_id: str) -> Optional[Dict[str, Any]]:
        try:
            packets_reader = BinaryReader(payload)

            while packets_reader.remaining() >= 4:
                packet_size = packets_reader.peekUInt32()
                if packet_size < 6 or packet_size > packets_reader.remaining():
                    return None

                packet_data = packets_reader.readBytes(packet_size)
                packet_reader = BinaryReader(packet_data)

                _ = packet_reader.readUInt32()
                packet_type = packet_reader.readUInt16()

                is_zstd_compressed = (packet_type & 0x8000) != 0
                msg_type_id = packet_type & 0x7FFF

                if msg_type_id == 2:  # Notify
                    result = self._process_notify_msg(packet_reader, is_zstd_compressed, stream_id)
                    if result:
                        return result
                elif msg_type_id == 6:  # FrameDown
                    result = self._process_frame_down_msg(packet_reader, is_zstd_compressed, stream_id)
                    if result:
                        return result

        except Exception as e:
            logger.debug(f"解析SyncContainerData失败[{stream_id}]: {e}", exc_info=True)

        return None

    def _process_notify_msg(self, reader: BinaryReader, is_zstd_compressed: bool, stream_id: str) -> Optional[Dict[str, Any]]:
        try:
            service_uuid = reader.readUInt64()
            stub_id = reader.readUInt32()
            method_id = reader.readUInt32()

            GAME_SERVICE_UUID = 0x0000000063335342
            if service_uuid != GAME_SERVICE_UUID:
                return None

            msg_payload = reader.readRemaining()

            if is_zstd_compressed:
                try:
                    dctx = zstd.ZstdDecompressor()
                    msg_payload = dctx.decompress(msg_payload, max_output_size=1024 * 1024)
                except Exception as e:
                    logger.debug(f"Notify zstd解压缩失败[{stream_id}]: {e}")
                    return None

            SYNC_CONTAINER_DATA_METHOD = 0x00000015
            if method_id == SYNC_CONTAINER_DATA_METHOD:
                sync_data = SyncContainerData()
                sync_data.ParseFromString(msg_payload)
                self.sync_container_count += 1
                logger.info(
                    f"发现SyncContainerData数据包 #{self.sync_container_count}: {stream_id} "
                    f"(serviceUuid=0x{service_uuid:016x}, methodId=0x{method_id:08x})"
                )
                return {'v_data': sync_data.VData}

        except Exception as e:
            logger.debug(f"处理Notify消息失败[{stream_id}]: {e}", exc_info=True)

        return None

    def _process_frame_down_msg(self, reader: BinaryReader, is_zstd_compressed: bool, stream_id: str) -> Optional[Dict[str, Any]]:
        try:
            _server_sequence_id = reader.readUInt32()

            if reader.remaining() == 0:
                return None

            nested_packet = reader.readRemaining()

            if is_zstd_compressed:
                try:
                    dctx = zstd.ZstdDecompressor()
                    nested_packet = dctx.decompress(nested_packet, max_output_size=1024 * 1024)
                except Exception as e:
                    logger.debug(f"FrameDown zstd解压缩失败[{stream_id}]: {e}")
                    return None

            return self._parse_sync_container_data(nested_packet, stream_id)

        except Exception as e:
            logger.debug(f"处理FrameDown消息失败[{stream_id}]: {e}", exc_info=True)

        return None

    def _cleanup_loop(self):
        while self.is_running:
            try:
                time.sleep(10)
                self._cleanup_expired_cache()
            except Exception as e:
                logger.debug(f"清理缓存时发生错误: {e}", exc_info=True)

    def _cleanup_expired_cache(self):
        with self.tcp_lock:
            current_time = time.time()
            expired = []
            for stream_id, state in self.streams.items():
                if state.tcp_last_time and current_time - state.tcp_last_time > self.FRAGMENT_TIMEOUT:
                    expired.append(stream_id)

            for stream_id in expired:
                logger.info(f"TCP 流超时，重置: {stream_id}")
                del self.streams[stream_id]

    def _evict_oldest_stream_locked(self):
        if not self.streams:
            return
        oldest_stream = min(self.streams.values(), key=lambda s: s.tcp_last_time or 0.0)
        logger.debug(f"候选流过多，移除最旧流: {oldest_stream.stream_id}")
        self.streams.pop(oldest_stream.stream_id, None)
