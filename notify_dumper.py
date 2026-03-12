# -*- coding: utf-8 -*-
"""数据落盘工具 — 纯 I/O 写入，仅在调试/取证时启用

重构要点：
- dump() 改用 `is not None` 判断 decompressed，修复空 bytes 被跳过的 bug
- cleanup() 移除冗余 try-except（rmtree ignore_errors 已足够）
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile

log = logging.getLogger(__name__)


class NotifyDumper:

    __slots__ = ("base_dir", "counter", "enabled")

    def __init__(self, base_dir: str | None = None, *, enabled: bool = False):
        self.base_dir = base_dir or os.path.join(tempfile.gettempdir(), "notify_dump")
        self.counter = 0
        self.enabled = enabled
        if enabled:
            os.makedirs(self.base_dir, exist_ok=True)
            log.info("落盘已启用: %s", self.base_dir)

    def dump(self, raw: bytes, decompressed: bytes | None = None) -> None:
        """落盘原始 + 解压数据（禁用时直接返回，零开销）"""
        if not self.enabled:
            return
        prefix = f"{self.counter:06d}"
        self.counter += 1

        raw_path = os.path.join(self.base_dir, f"{prefix}.raw.bin")
        with open(raw_path, "wb") as f:
            f.write(raw)

        if decompressed is not None:
            dec_path = os.path.join(self.base_dir, f"{prefix}.dec.bin")
            try:
                with open(dec_path, "wb") as f:
                    f.write(decompressed)
            except Exception as e:
                log.debug("落盘解压数据失败 %s: %s", dec_path, e)

    def cleanup(self) -> None:
        shutil.rmtree(self.base_dir, ignore_errors=True)
