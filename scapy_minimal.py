# -*- coding: utf-8 -*-
"""
scapy 最小化导入包装器 — 基于 scapy 源码 import 链分析

实际 import 链 (模块级, 不可跳过):
  scapy.sendrecv  → scapy.data, scapy.config, scapy.plist, scapy.packet ...
  scapy.config    → scapy.main (SCAPY_CACHE_FOLDER)
  scapy.data      → scapy.libs.manuf (DATA), scapy.libs.ethertypes (DATA)
  scapy.layers.l2 → scapy.ansmachine (AnsweringMachine)  ← 不能 fake!
  scapy.layers.inet → scapy.ansmachine (AnsweringMachine) ← 不能 fake!

策略:
  1. STUB: 在 import 链上但可以提供最小属性替代的 (scapy.main, scapy.libs.manuf)
  2. FAKE: 完全不在 import 链上的 (scapy.all, scapy.layers.all, 各协议层, contrib)
  3. 不动: 核心模块全部正常加载 (ansmachine, sessions, automaton 等)
  4. 体积优化交给 Nuitka --nofollow-import-to 在编译期处理
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STUB: 在 import 链上, 需要提供特定属性
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_STUB_MODULES: dict[str, dict] = {
    # scapy.config 会 from scapy.main import SCAPY_CACHE_FOLDER
    # scapy.data 的 @scapy_data_cache 用 SCAPY_CACHE_FOLDER / "xxx.pickle"
    # 必须是 Path 对象 (支持 / 运算符)
    "scapy.main": {
        "SCAPY_CACHE_FOLDER": Path(tempfile.gettempdir()) / "scapy",
    },
    # scapy.data.load_manuf() 回退到 DATA.split("\n")
    # 给空字符串: 无 MAC 厂商查找, 不影响抓包功能
    "scapy.libs.manuf": {
        "DATA": "",
    },
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FAKE: 完全不在 import 链上, 注入空壳阻止加载
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_FAKE_MODULES = [
    # --- 触发全部 layers 注册的入口 ---
    "scapy.all",
    "scapy.layers.all",
    # --- 可选扩展 ---
    "scapy.contrib",
    # --- 不需要的协议层 (仅由 scapy.layers.all 加载) ---
    "scapy.layers.bluetooth",
    "scapy.layers.bluetooth4LE",
    "scapy.layers.can",
    "scapy.layers.clns",
    "scapy.layers.dcerpc",
    "scapy.layers.dhcp",
    "scapy.layers.dhcp6",
    "scapy.layers.dns",
    "scapy.layers.dot11",
    "scapy.layers.dot15d4",
    "scapy.layers.eap",
    "scapy.layers.gprs",
    "scapy.layers.gssapi",
    "scapy.layers.hsrp",
    "scapy.layers.http",
    "scapy.layers.inet6",
    "scapy.layers.ipsec",
    "scapy.layers.ir",
    "scapy.layers.isakmp",
    "scapy.layers.kerberos",
    "scapy.layers.l2tp",
    "scapy.layers.ldap",
    "scapy.layers.llmnr",
    "scapy.layers.lltd",
    "scapy.layers.mgcp",
    "scapy.layers.mobileip",
    "scapy.layers.ms_nrtp",
    "scapy.layers.msrpce",
    "scapy.layers.netbios",
    "scapy.layers.netflow",
    "scapy.layers.ntlm",
    "scapy.layers.ntp",
    "scapy.layers.pflog",
    "scapy.layers.ppi",
    "scapy.layers.ppp",
    "scapy.layers.pptp",
    "scapy.layers.quic",
    "scapy.layers.radius",
    "scapy.layers.rip",
    "scapy.layers.rtp",
    "scapy.layers.sctp",
    "scapy.layers.sixlowpan",
    "scapy.layers.skinny",
    "scapy.layers.smb",
    "scapy.layers.smb2",
    "scapy.layers.smbclient",
    "scapy.layers.smbserver",
    "scapy.layers.snmp",
    "scapy.layers.spnego",
    "scapy.layers.ssh",
    "scapy.layers.tftp",
    "scapy.layers.tls",
    "scapy.layers.tpm",
    "scapy.layers.tuntap",
    "scapy.layers.usb",
    "scapy.layers.vrrp",
    "scapy.layers.vxlan",
    "scapy.layers.x509",
    "scapy.layers.zigbee",
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  以下模块 **不 fake** — 它们在核心 import 链上:
#    scapy.ansmachine  ← l2.py / inet.py 模块级 import
#    scapy.sessions    ← sendrecv.py 可能引用
#    scapy.automaton   ← ansmachine 可能引用
#    scapy.autorun     ← 可能被引用
#    scapy.asn1*       ← fields.py 可能引用
#    scapy.pipetool    ← 可能被引用
#    scapy.as_resolvers, scapy.utils6, scapy.route6 等
#  体积优化交给 Nuitka --nofollow-import-to
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _inject() -> None:
    all_names = list(_STUB_MODULES.keys()) + _FAKE_MODULES

    def _make(mod_name: str) -> types.ModuleType:
        m = types.ModuleType(mod_name)
        m.__file__ = __file__
        parts = mod_name.rsplit(".", 1)
        m.__package__ = parts[0] if len(parts) > 1 else mod_name
        # 如果有子模块在列表中, 需要 __path__ 使其可作为 package
        prefix = mod_name + "."
        if any(n.startswith(prefix) for n in all_names):
            m.__path__ = []
        return m

    for mod_name, attrs in _STUB_MODULES.items():
        if mod_name not in sys.modules:
            m = _make(mod_name)
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[mod_name] = m

    for mod_name in _FAKE_MODULES:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = _make(mod_name)


_inject()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  真正需要的最小 scapy 导入
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from scapy.sendrecv import sniff          # noqa: E402
from scapy.layers.inet import IP, TCP     # noqa: E402
from scapy.packet import Raw              # noqa: E402

# NOTE: scapy.layers.l2 (Ether) 已确认不在本工具的抓包路径上，
#       无需导入；若未来需要 L2 层解析再按需添加。

__all__ = ["sniff", "IP", "TCP", "Raw"]
