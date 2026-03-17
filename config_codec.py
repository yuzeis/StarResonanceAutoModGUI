"""
config_codec — 配置码编解码模块
独立于 GUI，方便单独迭代压缩算法、编写单元测试。

公开 API:
    encode_config(cfg: dict) -> str   配置字典 → 配置码字符串
    decode_config(code: str) -> dict  配置码字符串 → 配置字典

共享常量（GUI 也需要引用）:
    BASIC_ATTRS, SPECIAL_ATTRS, ALL_ATTRS, CATEGORIES, CFG_DEFAULTS
"""

import json
import base64
import struct
import io

# ═══════════════════════════════════════════════════════════
#  属性 / 类型数据（GUI 和编解码共用）
# ═══════════════════════════════════════════════════════════
BASIC_ATTRS = [
    "力量加持", "敏捷加持", "智力加持",
    "特攻伤害", "精英打击",
    "特攻治疗加持", "专精治疗加持",
    "施法专注", "攻速专注", "暴击专注", "幸运专注",
    "抵御魔法", "抵御物理",
]
SPECIAL_ATTRS = [
    "极-绝境守护", "极-伤害叠加", "极-灵活身法",
    "极-生命凝聚", "极-急救措施", "极-生命波动",
    "极-生命汲取", "极-全队幸暴",
]
ALL_ATTRS  = BASIC_ATTRS + SPECIAL_ATTRS   # 共 21 个
CATEGORIES = ["全部", "攻击", "守护", "辅助"]

CFG_DEFAULTS: dict = {
    "auto_interface": True, "interface_index": 0, "load_vdata": False,
    "generate_vdata": False, "category": "全部", "attributes": [],
    "exclude_attributes": [], "match_count": 1, "combo_size": 4,
    "enumeration_mode": False, "debug": False, "min_attr_sum": {}, "remark": "",
}

# ═══════════════════════════════════════════════════════════
#  内部索引映射
# ═══════════════════════════════════════════════════════════
_ATTR_INDEX: dict[str, int] = {a: i for i, a in enumerate(ALL_ATTRS)}
_INDEX_ATTR: dict[int, str] = {i: a for a, i in _ATTR_INDEX.items()}
_CAT_INDEX:  dict[str, int] = {c: i for i, c in enumerate(CATEGORIES)}
_INDEX_CAT:  dict[int, str] = {i: c for i, c in enumerate(CATEGORIES)}

# 旧版 Z4 兼容用缩写映射
_ATTR_ABBR: dict[str, str] = {
    "力量加持": "a1", "敏捷加持": "a2", "智力加持": "a3",
    "特攻伤害": "a4", "精英打击": "a5",
    "特攻治疗加持": "a6", "专精治疗加持": "a7",
    "施法专注": "a8", "攻速专注": "a9", "暴击专注": "aa",
    "幸运专注": "ab", "抵御魔法": "ac", "抵御物理": "ad",
    "极-绝境守护": "b1", "极-伤害叠加": "b2", "极-灵活身法": "b3",
    "极-生命凝聚": "b4", "极-急救措施": "b5", "极-生命波动": "b6",
    "极-生命汲取": "b7", "极-全队幸暴": "b8",
}
_ABBR_ATTR: dict[str, str] = {v: k for k, v in _ATTR_ABBR.items()}


# ═══════════════════════════════════════════════════════════
#  比特流工具
# ═══════════════════════════════════════════════════════════
class _BitWriter:
    """比特级写入器"""
    __slots__ = ('_buf', '_pos')

    def __init__(self):
        self._buf = 0; self._pos = 0

    def write(self, value: int, bits: int):
        self._buf |= (value & ((1 << bits) - 1)) << self._pos
        self._pos += bits

    def to_bytes(self) -> bytes:
        return self._buf.to_bytes((self._pos + 7) // 8, 'little')


class _BitReader:
    """比特级读取器"""
    __slots__ = ('_buf', '_pos', '_len')

    def __init__(self, data: bytes):
        self._buf = int.from_bytes(data, 'little')
        self._pos = 0; self._len = len(data) * 8

    def read(self, bits: int) -> int:
        val = (self._buf >> self._pos) & ((1 << bits) - 1)
        self._pos += bits; return val

    @property
    def consumed_bytes(self):
        return (self._pos + 7) // 8


# ═══════════════════════════════════════════════════════════
#  编码（当前版本 Z7: 比特流差分）
# ═══════════════════════════════════════════════════════════
def encode_config(cfg: dict) -> str:
    """配置 → 配置码（Z7: 前缀）
    流程: 比特流差分编码 → base85
    · 12-bit presence mask 标记哪些字段与默认值不同
    · 布尔字段仅靠 presence bit 隐含（出现=取反），0 额外比特
    · attributes / exclude_attributes → 21-bit bitmask（仅非空时写入）
    · match_count / combo_size → 4 bit（范围 1-16）
    · min_attr_sum → 5-bit count + (5+8) bits × N
    · remark → 对齐字节后追加 UTF-8
    """
    bw = _BitWriter()

    # ── presence mask (12 bits) ──────────────────────
    presence = 0
    if cfg.get("auto_interface", True)    != True:   presence |= (1 << 0)
    if cfg.get("interface_index", 0)      != 0:      presence |= (1 << 1)
    if cfg.get("load_vdata", False)       != False:  presence |= (1 << 2)
    if cfg.get("generate_vdata", False)   != False:  presence |= (1 << 3)
    if cfg.get("category", "全部")        != "全部":  presence |= (1 << 4)
    if cfg.get("attributes", []):                    presence |= (1 << 5)
    if cfg.get("exclude_attributes", []):            presence |= (1 << 6)
    if cfg.get("match_count", 1)          != 1:      presence |= (1 << 7)
    if cfg.get("combo_size", 4)           != 4:      presence |= (1 << 8)
    if cfg.get("enumeration_mode", False) != False:  presence |= (1 << 9)
    if cfg.get("debug", False)            != False:  presence |= (1 << 10)
    if cfg.get("min_attr_sum", {}):                  presence |= (1 << 11)
    bw.write(presence, 12)

    # ── 各字段数据（仅非默认）───────────────────────
    if presence & (1 << 1):
        bw.write(cfg["interface_index"], 8)
    if presence & (1 << 4):
        bw.write(_CAT_INDEX.get(cfg["category"], 0), 2)
    if presence & (1 << 5):
        mask = 0
        for a in cfg["attributes"]:
            idx = _ATTR_INDEX.get(a)
            if idx is not None: mask |= (1 << idx)
        bw.write(mask, 21)
    if presence & (1 << 6):
        mask = 0
        for a in cfg["exclude_attributes"]:
            idx = _ATTR_INDEX.get(a)
            if idx is not None: mask |= (1 << idx)
        bw.write(mask, 21)
    if presence & (1 << 7):
        bw.write(cfg["match_count"] - 1, 4)
    if presence & (1 << 8):
        bw.write(cfg["combo_size"] - 1, 4)
    if presence & (1 << 11):
        mas = cfg["min_attr_sum"]
        bw.write(len(mas), 5)
        for attr, val in mas.items():
            bw.write(_ATTR_INDEX.get(attr, 0), 5)
            bw.write(min(int(val), 255), 8)

    # ── 转字节 + 追加 remark ────────────────────────
    raw = bw.to_bytes()
    remark = cfg.get("remark", "")
    if remark:
        raw += remark.encode('utf-8')
    return "Z7:" + base64.b85encode(raw).decode('ascii')


# ═══════════════════════════════════════════════════════════
#  解码（兼容 Z7 / Z4 / Z1 / 旧 base64）
# ═══════════════════════════════════════════════════════════
def decode_config(code: str) -> dict:
    """配置码 → 配置字典，兼容所有历史格式"""

    # ── Z7: 比特流差分格式（当前版本）──────────────
    if code.startswith("Z7:"):
        raw = base64.b85decode(code[3:].encode('ascii'))
        cfg = dict(CFG_DEFAULTS)
        br = _BitReader(raw)
        presence = br.read(12)

        if presence & (1 << 0):  cfg["auto_interface"] = False
        if presence & (1 << 2):  cfg["load_vdata"] = True
        if presence & (1 << 3):  cfg["generate_vdata"] = True
        if presence & (1 << 9):  cfg["enumeration_mode"] = True
        if presence & (1 << 10): cfg["debug"] = True

        if presence & (1 << 1):
            cfg["interface_index"] = br.read(8)
        if presence & (1 << 4):
            cfg["category"] = _INDEX_CAT.get(br.read(2), "全部")
        if presence & (1 << 5):
            mask = br.read(21)
            cfg["attributes"] = [_INDEX_ATTR[i] for i in range(21) if mask & (1 << i)]
        if presence & (1 << 6):
            mask = br.read(21)
            cfg["exclude_attributes"] = [_INDEX_ATTR[i] for i in range(21) if mask & (1 << i)]
        if presence & (1 << 7):
            cfg["match_count"] = br.read(4) + 1
        if presence & (1 << 8):
            cfg["combo_size"] = br.read(4) + 1
        if presence & (1 << 11):
            n = br.read(5)
            mas = {}
            for _ in range(n):
                ai = br.read(5); val = br.read(8)
                attr_name = _INDEX_ATTR.get(ai)
                if attr_name: mas[attr_name] = val
            cfg["min_attr_sum"] = mas

        consumed = br.consumed_bytes
        if consumed < len(raw):
            cfg["remark"] = raw[consumed:].decode('utf-8')
        return cfg

    # ── Z4: JSON + zstd + base85（旧版兼容）────────
    if code.startswith("Z4:"):
        import zstandard as _zstd
        compressed = base64.b85decode(code[3:].encode('ascii'))
        json_bytes = _zstd.ZstdDecompressor().decompress(compressed)
        mini = json.loads(json_bytes.decode('utf-8'))
        cfg = dict(CFG_DEFAULTS)
        for k, v in mini.items():
            if k in ("attributes", "exclude_attributes"):
                v = [_ABBR_ATTR.get(a, a) for a in v]
            elif k == "min_attr_sum":
                v = {_ABBR_ATTR.get(a, a): n for a, n in v.items()}
            cfg[k] = v
        return cfg

    # ── Z1: zstd + base64 ────────────────────────
    if code.startswith("Z1:"):
        import zstandard as _zstd
        compressed = base64.b64decode(code[3:].encode('ascii'))
        json_bytes = _zstd.ZstdDecompressor().decompress(compressed)
        return json.loads(json_bytes.decode('utf-8'))

    # ── 最旧：纯 base64(json) ────────────────────
    return json.loads(base64.b64decode(code.encode()).decode())


# ═══════════════════════════════════════════════════════════
#  自测
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    tests = [
        ("全默认", dict(CFG_DEFAULTS)),
        ("典型", {
            **CFG_DEFAULTS,
            "attributes": ["力量加持", "特攻伤害", "精英打击"],
            "match_count": 2,
        }),
        ("中等", {
            **CFG_DEFAULTS,
            "category": "攻击",
            "attributes": ["力量加持", "敏捷加持", "特攻伤害", "精英打击", "暴击专注"],
            "exclude_attributes": ["抵御魔法", "抵御物理"],
            "match_count": 3, "combo_size": 6,
            "min_attr_sum": {"力量加持": 30, "特攻伤害": 25},
        }),
        ("复杂", {
            **CFG_DEFAULTS,
            "auto_interface": False, "interface_index": 2,
            "category": "辅助",
            "attributes": ["力量加持", "敏捷加持", "智力加持", "特攻伤害",
                           "精英打击", "暴击专注", "幸运专注", "极-全队幸暴"],
            "exclude_attributes": ["抵御魔法", "抵御物理", "极-绝境守护", "极-灵活身法"],
            "match_count": 4, "combo_size": 5, "enumeration_mode": True,
            "min_attr_sum": {"力量加持": 30, "暴击专注": 20, "极-全队幸暴": 15},
            "remark": "PVP配置",
        }),
        ("极端", {
            **CFG_DEFAULTS,
            "category": "守护",
            "attributes": ALL_ATTRS[:],
            "match_count": 5, "combo_size": 8,
            "enumeration_mode": True, "debug": True,
            "min_attr_sum": {a: 20 for a in ALL_ATTRS[:10]},
            "remark": "测试极限场景",
        }),
    ]

    print(f"{'场景':<8} {'码长':>4}  配置码")
    print("-" * 70)
    for name, cfg in tests:
        code = encode_config(cfg)
        restored = decode_config(code)
        for k in CFG_DEFAULTS:
            orig = cfg.get(k, CFG_DEFAULTS[k])
            rest = restored.get(k, CFG_DEFAULTS[k])
            assert orig == rest, f"[{name}] 字段 '{k}' 不一致: {orig!r} vs {rest!r}"
        print(f"  {name:<6} {len(code):>4}  {code}")

    print("-" * 70)
    print("✓ 全部往返测试通过")
