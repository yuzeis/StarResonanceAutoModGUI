"""
星痕共鸣模组筛选器 - GUI 界面
基于 PySide6 的图形界面封装
"""

import sys
import os
import time
import math
import threading
import logging
import random
import json
import base64
from typing import List, Optional, Dict

_PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROJ_DIR not in sys.path:
    sys.path.insert(0, _PROJ_DIR)

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QComboBox, QSpinBox, QCheckBox, QTextEdit,
    QScrollArea, QSplitter, QProgressBar,
    QGridLayout, QGroupBox, QSizePolicy, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QDialog, QAbstractItemView,
)
from PySide6.QtCore import Qt, Signal, QThread, QObject, Slot
from PySide6.QtGui import QFont, QTextCursor, QColor, QPalette

# ═══════════════════════════════════════════════════════════
#  调色板
# ═══════════════════════════════════════════════════════════
BG         = "#0d0f14"
BG2        = "#13161e"
BG3        = "#1a1e2a"
PANEL      = "#1e2230"
BORDER     = "#2a3048"
ACCENT     = "#00d4ff"
ACCENT2    = "#0099cc"
ACCENT_DIM = "#003d4d"
TEXT       = "#e8eaf0"
TEXT2      = "#8890aa"
TEXT3      = "#4a5070"
SUCCESS    = "#00e5a0"
WARNING    = "#ffb830"
DANGER     = "#ff4d6a"
CUDA_CLR   = "#76ff88"
OPENCL_CLR = "#ffaa44"
CPU_CLR    = "#6699ff"

# ═══════════════════════════════════════════════════════════
#  属性/类型数据
# ═══════════════════════════════════════════════════════════
BASIC_ATTRS = [
    "力量加持","敏捷加持","智力加持",
    "特攻伤害","精英打击",
    "特攻治疗加持","专精治疗加持",
    "施法专注","攻速专注","暴击专注","幸运专注",
    "抵御魔法","抵御物理",
]
SPECIAL_ATTRS = [
    "极-绝境守护","极-伤害叠加","极-灵活身法",
    "极-生命凝聚","极-急救措施","极-生命波动",
    "极-生命汲取","极-全队幸暴",
]
ALL_ATTRS  = BASIC_ATTRS + SPECIAL_ATTRS
CATEGORIES = ["全部","攻击","守护","辅助"]

# ── 配置码：属性名 ↔ 2字符缩写（用于语义压缩，降低配置码长度）─────────────────
_ATTR_ABBR: dict[str, str] = {
    "力量加持":     "a1", "敏捷加持":     "a2", "智力加持":     "a3",
    "特攻伤害":     "a4", "精英打击":     "a5",
    "特攻治疗加持": "a6", "专精治疗加持": "a7",
    "施法专注":     "a8", "攻速专注":     "a9", "暴击专注":     "aa",
    "幸运专注":     "ab", "抵御魔法":     "ac", "抵御物理":     "ad",
    "极-绝境守护":  "b1", "极-伤害叠加":  "b2", "极-灵活身法":  "b3",
    "极-生命凝聚":  "b4", "极-急救措施":  "b5", "极-生命波动":  "b6",
    "极-生命汲取":  "b7", "极-全队幸暴":  "b8",
}
_ABBR_ATTR: dict[str, str] = {v: k for k, v in _ATTR_ABBR.items()}

# 配置项默认值（导出时跳过与默认值相同的项，减小体积）
_CFG_DEFAULTS: dict = {
    "auto_interface": True, "interface_index": 0, "load_vdata": False,
    "generate_vdata": False, "category": "全部", "attributes": [],
    "exclude_attributes": [], "match_count": 1, "combo_size": 4,
    "enumeration_mode": False, "debug": False, "min_attr_sum": {}, "remark": "",
}

def _encode_config(cfg: dict) -> str:
    """配置 → 配置码（Z4: 前缀）
    流程: 去默认值 → 属性名缩写 → JSON → zstd(19) → base85
    """
    import zstandard as _zstd
    mini: dict = {}
    for k, v in cfg.items():
        if v == _CFG_DEFAULTS.get(k):
            continue
        if k in ("attributes", "exclude_attributes"):
            v = [_ATTR_ABBR.get(a, a) for a in v]
        elif k == "min_attr_sum":
            v = {_ATTR_ABBR.get(a, a): n for a, n in v.items()}
        mini[k] = v
    json_bytes = json.dumps(mini, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    compressed = _zstd.ZstdCompressor(level=19).compress(json_bytes)
    return "Z4:" + base64.b85encode(compressed).decode('ascii')

def _decode_config(code: str) -> dict:
    """配置码 → 配置字典，兼容 Z4 / Z1 / 旧 base64 三种格式"""
    import zstandard as _zstd
    if code.startswith("Z4:"):
        compressed = base64.b85decode(code[3:].encode('ascii'))
        json_bytes = _zstd.ZstdDecompressor().decompress(compressed)
        mini = json.loads(json_bytes.decode('utf-8'))
        # 还原缩写 + 补全默认值
        cfg = dict(_CFG_DEFAULTS)
        for k, v in mini.items():
            if k in ("attributes", "exclude_attributes"):
                v = [_ABBR_ATTR.get(a, a) for a in v]
            elif k == "min_attr_sum":
                v = {_ABBR_ATTR.get(a, a): n for a, n in v.items()}
            cfg[k] = v
        return cfg
    if code.startswith("Z1:"):
        compressed = base64.b64decode(code[3:].encode('ascii'))
        json_bytes = _zstd.ZstdDecompressor().decompress(compressed)
        return json.loads(json_bytes.decode('utf-8'))
    # 最旧格式：纯 base64(json)
    return json.loads(base64.b64decode(code.encode()).decode())


# ═══════════════════════════════════════════════════════════
#  全局样式表
# ═══════════════════════════════════════════════════════════
QSS = f"""
* {{ font-family: 'Microsoft YaHei UI','PingFang SC','Segoe UI',sans-serif; }}
QMainWindow, QWidget {{ background-color:{BG}; color:{TEXT}; font-size:13px; }}
QScrollBar:vertical {{ background:{BG2}; width:6px; border-radius:3px; }}
QScrollBar::handle:vertical {{ background:{BORDER}; border-radius:3px; min-height:20px; }}
QScrollBar::handle:vertical:hover {{ background:{ACCENT2}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0px; }}
QScrollBar:horizontal {{ background:{BG2}; height:6px; border-radius:3px; }}
QScrollBar::handle:horizontal {{ background:{BORDER}; border-radius:3px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0px; }}
QGroupBox {{
    color:{TEXT2}; font-size:11px; font-weight:600; letter-spacing:2px;
    border:1px solid {BORDER}; border-radius:8px;
    margin-top:16px; padding-top:10px; background:{PANEL};
}}
QGroupBox::title {{ subcontrol-origin:margin; left:12px; padding:0 6px; background:{PANEL}; }}
QPushButton {{
    background:{BG3}; color:{TEXT}; border:1px solid {BORDER};
    border-radius:6px; padding:6px 14px; font-size:13px;
}}
QPushButton:hover {{ background:{BORDER}; border-color:{ACCENT2}; color:{ACCENT}; }}
QPushButton:pressed {{ background:{ACCENT_DIM}; }}
QPushButton:disabled {{ color:{TEXT3}; border-color:{BG3}; background:{BG2}; }}
QPushButton#btn_start {{
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #007a99,stop:1 #005577);
    color:#fff; font-weight:700; font-size:14px; border:none;
    border-radius:8px; padding:10px 32px; letter-spacing:1px;
}}
QPushButton#btn_start:hover {{
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #00aad4,stop:1 #0077aa);
}}
QPushButton#btn_start:disabled {{ background:{BG3}; color:{TEXT3}; }}
QPushButton#btn_stop {{
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #7a0020,stop:1 #550015);
    color:#fff; font-weight:700; font-size:14px; border:none;
    border-radius:8px; padding:10px 32px;
}}
QPushButton#btn_stop:hover {{
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #cc0033,stop:1 #990022);
}}
QPushButton#btn_stop:disabled {{ background:{BG3}; color:{TEXT3}; }}
QComboBox {{
    background:{BG3}; color:{TEXT}; border:1px solid {BORDER};
    border-radius:6px; padding:5px 10px;
}}
QComboBox:hover {{ border-color:{ACCENT2}; }}
QComboBox::drop-down {{ border:none; width:24px; }}
QComboBox QAbstractItemView {{
    background:{BG3}; color:{TEXT}; border:1px solid {BORDER};
    selection-background-color:{ACCENT_DIM}; selection-color:{ACCENT}; outline:none;
}}
QSpinBox {{
    background:{BG3}; color:{TEXT}; border:1px solid {BORDER};
    border-radius:6px; padding:5px 8px;
}}
QSpinBox:hover {{ border-color:{ACCENT2}; }}
QSpinBox::up-button, QSpinBox::down-button {{ background:{BORDER}; border:none; width:18px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background:{ACCENT2}; }}
QCheckBox {{ color:{TEXT}; spacing:8px; }}
QCheckBox::indicator {{ width:16px; height:16px; border-radius:4px; border:1px solid {BORDER}; background:{BG3}; }}
QCheckBox::indicator:checked {{ background:{ACCENT2}; border-color:{ACCENT}; }}
QCheckBox::indicator:hover {{ border-color:{ACCENT}; }}
QTextEdit {{
    background:{BG2}; color:{TEXT}; border:1px solid {BORDER};
    border-radius:6px; font-family:Consolas,'Courier New',monospace;
    font-size:12px; padding:4px; selection-background-color:{ACCENT_DIM};
}}
QProgressBar {{
    background:{BG3}; border:1px solid {BORDER}; border-radius:4px;
    color:transparent; min-height:8px; max-height:8px;
}}
QProgressBar::chunk {{
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {ACCENT2},stop:1 {ACCENT});
    border-radius:4px;
}}
QTabWidget::pane {{
    border:1px solid {BORDER}; border-top:none;
    border-bottom-left-radius:8px; border-bottom-right-radius:8px; background:{PANEL};
}}
QTabBar::tab {{
    background:{BG3}; color:{TEXT2}; border:1px solid {BORDER}; border-bottom:none;
    border-top-left-radius:6px; border-top-right-radius:6px;
    padding:6px 18px; margin-right:2px; font-size:12px;
}}
QTabBar::tab:selected {{ background:{PANEL}; color:{ACCENT}; border-color:{ACCENT2}; }}
QTabBar::tab:hover {{ color:{TEXT}; }}
QTableWidget {{
    background:{BG2}; color:{TEXT}; border:none;
    gridline-color:{BORDER}; selection-background-color:{ACCENT_DIM}; outline:none;
}}
QTableWidget::item {{ padding:6px 8px; border-bottom:1px solid {BORDER}; }}
QTableWidget::item:selected {{ color:{ACCENT}; background:{ACCENT_DIM}; }}
QHeaderView::section {{
    background:{BG3}; color:{TEXT2}; border:none;
    border-right:1px solid {BORDER}; border-bottom:1px solid {BORDER};
    padding:6px 10px; font-size:11px; letter-spacing:1px;
}}
QSplitter::handle {{ background:{BORDER}; width:1px; height:1px; }}
QDialog {{ background:{BG2}; }}
"""

# ═══════════════════════════════════════════════════════════
#  日志处理器
# ═══════════════════════════════════════════════════════════
class QTextEditHandler(logging.Handler, QObject):
    new_record = Signal(str, str)
    LEVEL_COLORS = {"DEBUG":TEXT3,"INFO":TEXT,"WARNING":WARNING,"ERROR":DANGER,"CRITICAL":DANGER}

    def __init__(self):
        logging.Handler.__init__(self)
        QObject.__init__(self)

    def emit(self, record):
        msg = self.format(record)
        level = record.levelname
        color = self.LEVEL_COLORS.get(level, TEXT)
        ts = time.strftime("%H:%M:%S")
        lmap = {"DEBUG":f'<span style="color:{TEXT3}">[DBG]</span>',
                "INFO": f'<span style="color:{ACCENT2}">[INF]</span>',
                "WARNING":f'<span style="color:{WARNING}">[WRN]</span>',
                "ERROR":f'<span style="color:{DANGER}">[ERR]</span>',
                "CRITICAL":f'<span style="color:{DANGER}">[ !! ]</span>'}
        prefix = lmap.get(level, f'[{level}]')
        ts_h = f'<span style="color:{TEXT3}">{ts}</span>'
        msg_h = f'<span style="color:{color}">{self._esc(msg)}</span>'
        self.new_record.emit(f'{ts_h} {prefix} {msg_h}', level)

    @staticmethod
    def _esc(s):
        return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

# ═══════════════════════════════════════════════════════════
#  属性标签多选控件
# ═══════════════════════════════════════════════════════════
class AttrTagSelector(QScrollArea):
    changed = Signal()

    def __init__(self, attrs, color_sel=ACCENT, parent=None):
        super().__init__(parent)
        self._attrs = attrs
        self._selected = set()
        self._btns: Dict[str, QPushButton] = {}
        self._build()

    def _build(self):
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMinimumHeight(160)
        self.setMaximumHeight(220)
        self.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        c = QWidget(); c.setStyleSheet("background:transparent;")
        g = QGridLayout(c); g.setSpacing(5); g.setContentsMargins(4,4,4,4)
        cols = 3
        for i, attr in enumerate(self._attrs):
            btn = QPushButton(attr)
            btn.setCheckable(True)
            btn.setFixedHeight(30)
            btn.setStyleSheet(self._style(False))
            btn.clicked.connect(lambda _, a=attr: self._toggle(a))
            self._btns[attr] = btn
            g.addWidget(btn, i//cols, i%cols)
        self.setWidget(c)

    def _style(self, sel):
        if sel:
            return (f"QPushButton{{background:{ACCENT_DIM};color:{ACCENT};"
                    f"border:1px solid {ACCENT2};border-radius:4px;font-size:12px;padding:2px 6px;}}"
                    f"QPushButton:hover{{background:{ACCENT_DIM};}}")
        return (f"QPushButton{{background:{BG3};color:{TEXT2};"
                f"border:1px solid {BORDER};border-radius:4px;font-size:12px;padding:2px 6px;}}"
                f"QPushButton:hover{{background:{BORDER};color:{TEXT};}}")

    def _toggle(self, attr):
        if attr in self._selected:
            self._selected.discard(attr)
            self._btns[attr].setChecked(False)
            self._btns[attr].setStyleSheet(self._style(False))
        else:
            self._selected.add(attr)
            self._btns[attr].setChecked(True)
            self._btns[attr].setStyleSheet(self._style(True))
        self.changed.emit()

    def get_selected(self):
        return [a for a in self._attrs if a in self._selected]

    def set_selected(self, attrs):
        self.clear_all()
        for a in attrs:
            if a in self._btns:
                self._selected.add(a)
                self._btns[a].setChecked(True)
                self._btns[a].setStyleSheet(self._style(True))
        self.changed.emit()

    def clear_all(self):
        for a in list(self._selected):
            self._btns[a].setChecked(False)
            self._btns[a].setStyleSheet(self._style(False))
        self._selected.clear()
        self.changed.emit()

# ═══════════════════════════════════════════════════════════
#  MIN ATTR SUM — 加号式添加控件
# ═══════════════════════════════════════════════════════════
class MinAttrSumWidget(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._constraints: Dict[str, int] = {}   # attr -> min_value
        self._tag_widgets: Dict[str, QWidget] = {}
        self._build()

    def _build(self):
        self.setStyleSheet("background:transparent;")
        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(6)

        # ── 输入行：下拉选属性 + 数值 + 加号按钮
        input_row = QHBoxLayout(); input_row.setSpacing(6)
        self._attr_combo = QComboBox()
        self._attr_combo.addItems(ALL_ATTRS)
        self._attr_combo.setFixedHeight(30)
        input_row.addWidget(self._attr_combo, 1)

        self._val_spin = QSpinBox()
        self._val_spin.setRange(1, 200)
        self._val_spin.setValue(20)
        self._val_spin.setFixedWidth(64)
        self._val_spin.setFixedHeight(30)
        input_row.addWidget(self._val_spin)

        btn_add = QPushButton("＋")
        btn_add.setFixedSize(30, 30)
        btn_add.setStyleSheet(
            f"QPushButton{{background:{ACCENT_DIM};color:{ACCENT};"
            f"border:1px solid {ACCENT2};border-radius:6px;"
            f"font-size:16px;font-weight:700;padding:0;}}"
            f"QPushButton:hover{{background:{ACCENT2};color:#fff;}}")
        btn_add.clicked.connect(self._add)
        input_row.addWidget(btn_add)
        root.addLayout(input_row)

        # ── 标签流区域（已添加的约束）
        self._tag_scroll = QScrollArea()
        self._tag_scroll.setWidgetResizable(True)
        self._tag_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tag_scroll.setFixedHeight(80)
        self._tag_scroll.setStyleSheet(
            f"QScrollArea{{background:{BG3};border:1px solid {BORDER};"
            f"border-radius:6px;}}")
        self._tag_container = QWidget()
        self._tag_container.setStyleSheet(f"background:{BG3};")
        self._tag_flow = QGridLayout(self._tag_container)
        self._tag_flow.setContentsMargins(6,6,6,6)
        self._tag_flow.setSpacing(5)
        self._tag_flow.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._empty_lbl = QLabel("暂无约束，点击 ＋ 添加")
        self._empty_lbl.setStyleSheet(f"color:{TEXT3};font-size:11px;")
        self._tag_flow.addWidget(self._empty_lbl, 0, 0)
        self._tag_scroll.setWidget(self._tag_container)
        root.addWidget(self._tag_scroll)

    def _add(self):
        attr = self._attr_combo.currentText()
        val  = self._val_spin.value()
        self._constraints[attr] = val
        self._refresh_tags()
        self.changed.emit()

    def _remove(self, attr):
        self._constraints.pop(attr, None)
        self._refresh_tags()
        self.changed.emit()

    def _refresh_tags(self):
        # 清空旧标签
        while self._tag_flow.count():
            item = self._tag_flow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._constraints:
            self._empty_lbl = QLabel("暂无约束，点击 ＋ 添加")
            self._empty_lbl.setStyleSheet(f"color:{TEXT3};font-size:11px;")
            self._tag_flow.addWidget(self._empty_lbl, 0, 0)
            return

        cols = 2
        for i, (attr, val) in enumerate(self._constraints.items()):
            is_sp = attr in set(SPECIAL_ATTRS)
            tag_clr = ACCENT if is_sp else SUCCESS
            tag_bg  = ACCENT_DIM if is_sp else "#003322"
            tag_bdr = ACCENT2 if is_sp else "#006644"

            tag_w = QWidget()
            tag_w.setStyleSheet(
                f"background:{tag_bg};border:1px solid {tag_bdr};"
                f"border-radius:5px;")
            tag_l = QHBoxLayout(tag_w)
            tag_l.setContentsMargins(6,2,4,2); tag_l.setSpacing(4)

            txt = QLabel(f"{attr}  ≥ {val}")
            txt.setStyleSheet(
                f"color:{tag_clr};font-size:12px;font-weight:600;"
                f"background:transparent;border:none;")
            tag_l.addWidget(txt)

            del_btn = QPushButton("✕")
            del_btn.setFixedSize(18, 18)
            del_btn.setStyleSheet(
                f"QPushButton{{background:transparent;color:{TEXT3};"
                f"border:none;font-size:10px;padding:0;}}"
                f"QPushButton:hover{{color:{DANGER};}}")
            del_btn.clicked.connect(lambda _, a=attr: self._remove(a))
            tag_l.addWidget(del_btn)

            self._tag_flow.addWidget(tag_w, i // cols, i % cols)

    def get_constraints(self) -> Dict[str, int]:
        return dict(self._constraints)

    def set_constraints(self, d: Dict[str, int]):
        self._constraints = {k: int(v) for k, v in d.items()}
        self._refresh_tags()
        self.changed.emit()

    def clear_all(self):
        self._constraints.clear()
        self._refresh_tags()
        self.changed.emit()


class ComputeModeBar(QWidget):
    mode_changed = Signal(str)
    _MODES = [("cuda","CUDA",CUDA_CLR),("opencl","OpenCL",OPENCL_CLR),("cpu","CPU",CPU_CLR)]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cur = "cpu"
        self._avail = {"cuda":False,"opencl":False,"cpu":True}
        self._btns: Dict[str,QPushButton] = {}
        self._build()

    def _build(self):
        lay = QHBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(6)
        lbl = QLabel("计算模式"); lbl.setStyleSheet(f"color:{TEXT2};font-size:11px;letter-spacing:1px;")
        lay.addWidget(lbl)
        for mid, label, color in self._MODES:
            btn = QPushButton(f"  {label}")
            btn.setFixedHeight(30); btn.setMinimumWidth(90)
            btn.setCheckable(True)
            self._btns[mid] = btn
            btn.clicked.connect(lambda _, m=mid: self._select(m))
            lay.addWidget(btn)
        lay.addStretch()
        self._refresh(); self._select("cpu")

    def _btn_style(self, mid, active, avail):
        c = {"cuda":CUDA_CLR,"opencl":OPENCL_CLR,"cpu":CPU_CLR}[mid]
        if not avail:
            return f"QPushButton{{background:{BG2};color:{TEXT3};border:1px solid {BG3};border-radius:6px;font-size:12px;}}"
        if active:
            return (f"QPushButton{{background:{BG3};color:{c};border:1px solid {c};"
                    f"border-radius:6px;font-size:12px;font-weight:700;}}"
                    f"QPushButton:hover{{background:{BG3};}}")
        return (f"QPushButton{{background:{BG2};color:{TEXT2};border:1px solid {BORDER};"
                f"border-radius:6px;font-size:12px;}}"
                f"QPushButton:hover{{background:{BG3};color:{c};border-color:{c};}}")

    def _refresh(self):
        for mid, btn in self._btns.items():
            btn.setEnabled(self._avail[mid])
            btn.setStyleSheet(self._btn_style(mid, mid==self._cur, self._avail[mid]))

    def _select(self, mid):
        if not self._avail.get(mid): return
        self._cur = mid; self._refresh(); self.mode_changed.emit(mid)

    def set_availability(self, cuda, opencl):
        self._avail["cuda"]=cuda; self._avail["opencl"]=opencl
        if self._cur=="cuda" and not cuda: self._cur="opencl" if opencl else "cpu"
        if self._cur=="opencl" and not opencl: self._cur="cpu"
        self._refresh(); self.mode_changed.emit(self._cur)

    def current_mode(self): return self._cur

# ═══════════════════════════════════════════════════════════
#  基准测试对话框
# ═══════════════════════════════════════════════════════════
class BenchmarkDialog(QDialog):
    _sig_prog  = Signal(int)
    _sig_log   = Signal(str)
    _sig_speed = Signal(str, float)
    _sig_done  = Signal()

    def __init__(self, avail, parent=None, combo_size=4):
        super().__init__(parent)
        self._avail = avail; self._results = {}; self._running = False
        self._combo_size = max(1, min(10, int(combo_size)))
        self.setWindowTitle("基准测试")
        self.setMinimumWidth(480); self.setMinimumHeight(360)
        self.setStyleSheet(QSS)
        self._build()
        self._sig_prog.connect(self._prog.setValue)
        self._sig_log.connect(self._do_log)
        self._sig_speed.connect(self._do_speed)
        self._sig_done.connect(self._do_done)

    def _build(self):
        lay = QVBoxLayout(self); lay.setSpacing(14); lay.setContentsMargins(20,20,20,20)
        t = QLabel("⚡  计算后端基准测试")
        t.setStyleSheet(f"color:{ACCENT};font-size:16px;font-weight:700;")
        lay.addWidget(t)
        d = QLabel(f"使用模拟数据测试各后端枚举组合速度（当前组合件数：{self._combo_size}）。\n每个后端：1次预热 + 多轮测量取中位数，结果用于预估实际运算时间。")
        d.setStyleSheet(f"color:{TEXT2};font-size:12px;"); d.setWordWrap(True)
        lay.addWidget(d)

        self._table = QTableWidget(3,3)
        self._table.setHorizontalHeaderLabels(["后端","万组合/秒","状态"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setFixedHeight(116)
        lay.addWidget(self._table)

        modes = [("cpu","CPU",CPU_CLR),("cuda","CUDA",CUDA_CLR),("opencl","OpenCL",OPENCL_CLR)]
        for row,(mid,lbl,clr) in enumerate(modes):
            n = QTableWidgetItem(f"  {lbl}")
            n.setForeground(QColor(clr)); n.setFont(QFont("",12,QFont.Bold))
            self._table.setItem(row,0,n)
            s = QTableWidgetItem("—"); s.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row,1,s)
            avail = self._avail.get(mid, mid=="cpu")
            st = QTableWidgetItem("可用" if avail else "不可用")
            st.setForeground(QColor(SUCCESS if avail else TEXT3)); st.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row,2,st)

        self._prog = QProgressBar(); self._prog.setRange(0,100); self._prog.setValue(0)
        lay.addWidget(self._prog)
        self._log_edit = QTextEdit(); self._log_edit.setReadOnly(True); self._log_edit.setFixedHeight(80)
        lay.addWidget(self._log_edit)

        br = QHBoxLayout()
        self._btn_run = QPushButton("▶  开始测试")
        self._btn_run.setStyleSheet(
            f"QPushButton{{background:{BG3};color:{ACCENT};border:1px solid {ACCENT2};"
            f"border-radius:6px;padding:8px 20px;font-weight:600;}}"
            f"QPushButton:hover{{background:{ACCENT_DIM};}}")
        btn_close = QPushButton("关闭")
        br.addWidget(self._btn_run); br.addStretch(); br.addWidget(btn_close)
        lay.addLayout(br)
        self._btn_run.clicked.connect(self._run)
        btn_close.clicked.connect(self.accept)

    @Slot(str)
    def _do_log(self, msg):
        self._log_edit.append(f'<span style="color:{TEXT2};font-size:11px;">{msg}</span>')

    @Slot(str, float)
    def _do_speed(self, mid, ops):
        row_map = {"cpu":0,"cuda":1,"opencl":2}
        row = row_map.get(mid)
        if row is not None:
            self._table.item(row, 1).setText(f"{ops:.1f}")
        self._results[mid] = ops

    @Slot()
    def _do_done(self):
        self._running = False
        self._btn_run.setEnabled(True)

    def _run(self):
        if self._running: return
        self._running = True; self._btn_run.setEnabled(False)
        self._sig_prog.emit(0); self._log_edit.clear()
        self._sig_log.emit("开始测试…")
        threading.Thread(target=self._worker, daemon=True).start()

    @staticmethod
    def _make_bench_modules(n=30):
        """生成n个随机模拟模组，使用真实属性ID，用于基准测试"""
        import random as _rnd
        # 基础属性ID（来自 ModuleAttrType）
        basic_ids  = [1110,1111,1112,1113,1114,1205,1206,1407,1408,1409,1410,1307,1308]
        special_ids= [2104,2105,2204,2205,2404,2405,2406,2304]
        all_ids    = basic_ids + special_ids
        config_ids = [5500101,5500102,5500103,5500104,
                      5500201,5500202,5500203,5500204,
                      5500301,5500302,5500303,5500304]
        try:
            import sys, os
            _proj = os.path.dirname(os.path.abspath(__file__))
            if _proj not in sys.path: sys.path.insert(0, _proj)
            from cpp_extension.module_optimizer_cpp import ModuleInfo, ModulePart
        except Exception as e:
            raise RuntimeError(f"无法导入C++扩展: {e}")
        modules = []
        for i in range(n):
            cfg = _rnd.choice(config_ids)
            # 每个模组4个词条，从真实属性池中随机选取，不重复
            chosen_ids = _rnd.sample(all_ids, 4)
            parts = [ModulePart(aid, str(aid), _rnd.randint(1, 20)) for aid in chosen_ids]
            modules.append(ModuleInfo(f"模组_{i:03d}", cfg, i+1, 4, parts))
        return modules

    @staticmethod
    def _calc_bench_n(combo_size: int) -> int:
        """根据 combo_size 动态计算基准测试所需的模组数 N。

        目标: C(N, k) 在 5000 万 ~ 1.2 亿之间。
        原先目标 200-500 万对 GPU 后端来说太少（CUDA 十几毫秒就跑完，
        kernel launch 和显存传输的固定开销占比过大导致结果波动 ±50%）。
        抬高到 5000 万~1.2 亿后，CPU 约 1~3 秒，CUDA 约 0.3~0.8 秒，
        再配合 warmup + 多轮取中位数即可获得稳定结果。
        """
        import math as _math
        _TARGET_LO = 50_000_000
        _TARGET_HI = 120_000_000
        k = max(1, min(10, combo_size))
        # k<=2 时 C(N,k) 增长慢，直接给大 N
        if k == 1:
            return 80_000_000   # C(N,1) = N，直接给 8000 万
        if k == 2:
            return 15000        # C(15000,2) = 112,492,500
        # 二分搜索满足 C(N,k) ∈ [LO, HI] 的 N
        lo, hi = k, 5000
        best_n = k + 10
        while lo <= hi:
            mid = (lo + hi) // 2
            c = _math.comb(mid, k)
            if c < _TARGET_LO:
                lo = mid + 1
            elif c > _TARGET_HI:
                hi = mid - 1
            else:
                best_n = mid
                break
        else:
            best_n = hi
        return max(best_n, k + 5)

    def _worker(self):
        """子线程：调用真实C++扩展计时，通过Signal回传结果。

        测试流程（每个后端）：
          1. Warmup: 跑 1 次，丢弃结果（让 CPU 缓存/GPU 驱动预热）
          2. Measure: 跑 N 次，取中位数
             - 若单次 < 500ms，则 N=5（短运行波动大，多轮平滑）
             - 若单次 ≥ 500ms，则 N=3（已经够稳定）
        """
        import math as _math, time as _time, sys, os, statistics as _stat
        _proj = os.path.dirname(os.path.abspath(__file__))
        if _proj not in sys.path:
            sys.path.insert(0, _proj)

        modes = [
            ("cpu",    "CPU",    True,
             "strategy_enumeration_cpp"),
            ("cuda",   "CUDA",   self._avail.get("cuda",False),
             "strategy_enumeration_cuda_cpp"),
            ("opencl", "OpenCL", self._avail.get("opencl",False),
             "strategy_enumeration_opencl_cpp"),
        ]

        N_MODULES = self._calc_bench_n(self._combo_size)
        try:
            modules = self._make_bench_modules(N_MODULES)
        except Exception as e:
            self._sig_log.emit(f"[ERR] 构造测试数据失败: {e}")
            self._sig_done.emit(); return

        n = N_MODULES
        k = self._combo_size
        total_combos = _math.comb(n, k)
        self._sig_log.emit(
            f"测试规模：{n} 个模组，C({n},{k}) = {total_combos:,} 组合"
            f"（{total_combos/1e4:.0f} 万）")

        try:
            import cpp_extension.module_optimizer_cpp as _ext
        except Exception as e:
            self._sig_log.emit(f"[ERR] 加载C++扩展失败: {e}")
            self._sig_done.emit(); return

        avail_count = sum(1 for _,_,a,_ in modes if a)
        done_count  = 0

        for idx,(mid,lbl,avail,fn_name) in enumerate(modes):
            if not avail:
                self._sig_log.emit(f"{lbl}：不可用，跳过")
                self._results[mid] = 0.0; continue

            fn = getattr(_ext, fn_name, None)
            if fn is None:
                self._sig_log.emit(f"{lbl}：函数 {fn_name} 不存在，跳过")
                self._results[mid] = 0.0; continue

            self._sig_log.emit(f"测试 {lbl}…")

            try:
                # ── Warmup（预热 CPU 缓存 / GPU 驱动，结果丢弃）──
                self._sig_log.emit(f"  [{lbl}] warmup…")
                fn(modules, set(), set(), {}, 60, 8, k)

                # ── Probe：跑 1 次测实际耗时，决定后续轮数 ──
                t0 = _time.perf_counter()
                fn(modules, set(), set(), {}, 60, 8, k)
                probe_elapsed = _time.perf_counter() - t0

                # 单次 < 500ms → 波动大，多跑几轮；≥ 500ms → 已经稳定，少跑
                measure_rounds = 5 if probe_elapsed < 0.5 else 3
                timings = [probe_elapsed]   # probe 那次也算一轮有效数据

                self._sig_log.emit(
                    f"  [{lbl}] probe {probe_elapsed*1000:.1f}ms → 再跑 {measure_rounds - 1} 轮")

                for r in range(measure_rounds - 1):
                    t0 = _time.perf_counter()
                    fn(modules, set(), set(), {}, 60, 8, k)
                    timings.append(_time.perf_counter() - t0)

                # 取中位数（抗离群值）
                median_elapsed = _stat.median(timings)
                ops = (total_combos / median_elapsed) / 10000.0

                self._sig_speed.emit(mid, ops)
                timing_strs = [f"{t*1000:.1f}" for t in timings]
                self._sig_log.emit(
                    f"  [{lbl}] 各轮: [{', '.join(timing_strs)}] ms"
                    f"  中位数 {median_elapsed*1000:.1f} ms"
                    f"  →  {ops:.1f} 万组合/秒")

            except Exception as e:
                self._sig_log.emit(f"  [ERR] {lbl} 运行失败: {e}")
                self._results[mid] = 0.0

            done_count += 1
            self._sig_prog.emit(int(done_count / avail_count * 90))

        self._sig_prog.emit(100)
        self._sig_log.emit("✓ 完成")
        self._sig_done.emit()

    def get_results(self): return self._results

# ═══════════════════════════════════════════════════════════
#  后台工作线程
# ═══════════════════════════════════════════════════════════
class MonitorWorker(QThread):
    log_signal      = Signal(str)
    done_signal     = Signal()
    error_signal    = Signal(str)
    progress_signal = Signal(int, str)
    module_count_signal = Signal(int)   # 扫描到的模组数量，用于动态计算 C(N,k)

    def __init__(self, cfg):
        super().__init__()
        self._cfg = cfg
        self._stop = threading.Event()
        self._results = None  # 解析后的优化结果列表，供主线程读取

    def stop(self): self._stop.set()

    def run(self):
        cfg = self._cfg
        self.log_signal.emit(f"[INFO] 启动监控器")
        self.log_signal.emit(
            f"[INFO] 模式: {cfg.get('mode','cpu').upper()}  |  "
            f"类型: {cfg.get('category','全部')}  |  "
            f"组合件数: {cfg.get('combo_size',4)}")
        if cfg.get("attributes"):
            self.log_signal.emit(f"[INFO] 包含属性: {', '.join(cfg['attributes'])}")
        if cfg.get("exclude_attributes"):
            self.log_signal.emit(f"[INFO] 排除属性: {', '.join(cfg['exclude_attributes'])}")
        if cfg.get("enumeration_mode"):
            self.log_signal.emit("[INFO] 枚举模式已启用")
        if cfg.get("load_vdata"):
            self.log_signal.emit("[INFO] 离线模式：从 modules.vdata 读取")
        else:
            self.log_signal.emit("[INFO] 在线模式：实时抓包获取模组数据")
            if cfg.get("generate_vdata"):
                self.log_signal.emit("[INFO] ✦ 已启用「生成 vdata」—— 抓包成功后将保存 modules.vdata")

        import subprocess, sys as _sys, os as _os, re as _re
        # 构建 CLI 参数
        # ── 区分编译环境与开发环境 ──────────────────────────────────────────
        # PyInstaller 打包后 sys.executable 指向 .exe 本身，不能再用它来"运行 .py"。
        # 此时改为：exe --run-monitor <args>，入口点会拦截该标志并调用监控器。
        # 开发环境（直接 python gui_main.py）则保持原有行为：python star_railway_monitor.py
        if getattr(_sys, 'frozen', False):
            cli = [_sys.executable, "--run-monitor"]
        else:
            cli = [_sys.executable,
                   _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "star_railway_monitor.py")]
        cli += ["-a"] if cfg.get("auto_interface") else ["-i", str(cfg.get("interface_index",0))]
        if cfg.get("load_vdata"):       cli.append("-lv")            # --load-vdata
        # 生成 vdata：仅在线模式（非 load_vdata）且用户勾选时才传入
        # 双重保险：get_config() 已在离线时强制 False，此处再次校验
        if cfg.get("generate_vdata") and not cfg.get("load_vdata"):
            cli.append("-gv")                                        # --generate-vdata
        if cfg.get("enumeration_mode"): cli.append("-enum")          # --enumeration-mode
        if cfg.get("debug"):            cli.append("-d")             # --debug
        cat = cfg.get("category","全部")
        if cat != "全部":               cli += ["-c", cat]           # --category
        attrs = cfg.get("attributes",[])
        if attrs:                       cli += ["-attr"] + attrs     # --attributes
        excl  = cfg.get("exclude_attributes",[])
        if excl:                        cli += ["-exattr"] + excl    # --exclude-attributes
        mc = cfg.get("match_count", 1)
        cli += ["-mc", str(mc)]                                      # --match-count
        # 组合件数（修复问题2：combo_size 1-10 现在真正生效）
        cs = cfg.get("combo_size", 4)
        cli += ["-cs", str(cs)]                                      # --combo-size
        # 计算模式（修复问题1：计算模式选择现在真正生效）
        compute_mode = cfg.get("mode", "cpu")
        cli += ["-cm", compute_mode]                                 # --compute-mode
        mas = cfg.get("min_attr_sum",{})
        if mas:
            for k,v in mas.items():
                cli += ["-mas", k, str(v)]                           # --min-attr-sum

        self.log_signal.emit(f"[INFO] 执行: {' '.join(cli)}")

        import subprocess, sys as _sys, os as _os, re as _re, queue as _queue, locale as _locale

        # Windows 下子进程控制台输出编码通常是系统代码页（GBK/CP936），
        # 用 locale.getpreferredencoding() 自动取，fallback 到 utf-8
        _enc = _locale.getpreferredencoding(False) or "utf-8"

        try:
            proc = subprocess.Popen(
                cli,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding=_enc, errors="replace",
                bufsize=1,
                # Windows 下用 CREATE_NO_WINDOW 隐藏黑框
                creationflags=subprocess.CREATE_NO_WINDOW if _sys.platform == "win32" else 0,
            )
        except FileNotFoundError:
            self.log_signal.emit("[WARN] 未找到 star_railway_monitor.py，进入演示模式")
            self._results = None
            steps = 20
            for i in range(steps):
                if self._stop.is_set():
                    self.log_signal.emit("[WARN] 用户中止操作")
                    self.done_signal.emit(); return
                time.sleep(0.15)
                pct = int((i+1)/steps*100)
                self.progress_signal.emit(pct, f"处理中 {pct}%")
            self.log_signal.emit("[INFO] ✓ 优化完成（演示数据）")
            self.progress_signal.emit(100,"完成")
            self.done_signal.emit(); return

        # ── 用独立线程读取 stdout，避免阻塞停止检测 ──────────
        line_q: _queue.Queue = _queue.Queue()

        def _reader():
            try:
                for raw in proc.stdout:
                    line_q.put(raw)
            except Exception:
                pass
            finally:
                line_q.put(None)   # 哨兵：结束标记

        import threading as _th
        _th.Thread(target=_reader, daemon=True).start()

        # ── 解析状态机 ────────────────────────────────────────
        results       = []
        cur           = None
        in_modules    = False
        in_attrs      = False
        total_lines   = 0
        progress_step = 0

        def _flush_cur():
            nonlocal cur
            if cur and cur.get("modules"):
                results.append(cur)
            cur = None

        while True:
            # 检查停止信号
            if self._stop.is_set():
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    pass
                # ── 保存已解析的结果（用户点停止时可能已解析到大部分数据）──
                _flush_cur()
                if results:
                    self._results = results
                    self.log_signal.emit(f"[WARN] 用户中止，已保存 {len(results)} 套已解析搭配")
                else:
                    self.log_signal.emit("[WARN] 用户中止操作")
                self.done_signal.emit(); return

            # 非阻塞取行，超时 0.1s 后继续检查停止信号
            try:
                raw = line_q.get(timeout=0.1)
            except _queue.Empty:
                # 子进程还在跑，继续轮询
                if proc.poll() is not None:
                    # 进程已退出但队列暂时为空，等一下再排空
                    time.sleep(0.05)
                continue

            if raw is None:
                break   # 读取线程结束

            line = raw.rstrip()
            total_lines += 1

            # ── 只把关键节点打到 GUI 日志，其余静默 ──────────
            _key = (line.startswith("===") or
                    "优化" in line or "解析" in line or
                    line.startswith("[2") and ("] [INFO]" in line or "] [WARN]" in line or "] [ERR" in line) or
                    "ERROR" in line or "error" in line.lower() and "module" not in line.lower())
            if _key:
                self.log_signal.emit(f"[INFO] {line}" if not line.startswith("[") else line)

            # ── 解析模组总数 N（用于主界面动态预估 C(N,k)）──
            import re as _re2
            _mc = (_re2.search(r"共\s*(\d+)\s*个模组", line) or
                   _re2.search(r"模组总数[：:]\s*(\d+)", line) or
                   _re2.search(r"找到\s*(\d+)\s*个模组", line) or
                   _re2.search(r"加载.*?(\d+)\s*个模组", line) or
                   _re2.search(r"扫描.*?(\d+)\s*个模组", line) or
                   _re2.search(r"(\d+)\s*modules?\s+loaded", line, _re2.IGNORECASE))
            if _mc:
                self.module_count_signal.emit(int(_mc.group(1)))

            # 匹配 "=== 第N名搭配 ==="
            m = _re.match(r"=== 第(\d+)名搭配 ===", line)
            if m:
                _flush_cur()
                rank_num = int(m.group(1))
                cur = {"rank": rank_num, "score": 0, "battle_power": 0,
                       "modules": [], "attr_sum": {}}
                in_modules = False; in_attrs = False
                # 每解析到一套搭配更新进度（假设最多100套，预留5%给收尾）
                pct = min(95, int(rank_num / 100 * 90) + 5)
                self.progress_signal.emit(pct, f"解析搭配 {rank_num}/100…")
                continue

            if cur is None:
                continue

            m = _re.match(r"总属性值:\s*(\d+)", line)
            if m:
                cur["score"] = int(m.group(1)); continue

            m = _re.match(r"战斗力:\s*([\d.]+)", line)
            if m:
                cur["battle_power"] = float(m.group(1)); continue

            if line.strip() == "模组列表:":
                in_modules = True; in_attrs = False; continue

            if line.strip() == "属性分布:":
                in_attrs = True; in_modules = False; continue

            if in_modules:
                m = _re.match(r"\s+\d+\.\s+(.+?)\s+\(品质(\d)\)\s+-\s+(.+)", line)
                if m:
                    mname    = m.group(1).strip()
                    quality  = int(m.group(2))
                    attr_str = m.group(3)
                    attrs_list = []
                    for part in attr_str.split(","):
                        part = part.strip()
                        am = _re.match(r"(.+?)\+(\d+)", part)
                        if am:
                            attrs_list.append((am.group(1).strip(), int(am.group(2))))
                    cur["modules"].append({"name": mname, "quality": quality, "attrs": attrs_list})
                continue

            if in_attrs:
                m = _re.match(r"\s+(.+?):\s+\+(\d+)", line)
                if m:
                    cur["attr_sum"][m.group(1).strip()] = int(m.group(2))
                continue

        _flush_cur()
        rc = proc.wait()

        if rc != 0 and rc is not None:
            self.log_signal.emit(f"[WARN] 子进程退出码: {rc}")

        if results:
            self._results = results
            self.log_signal.emit(f"[INFO] ✓ 解析到 {len(results)} 套搭配")
        else:
            self._results = None
            self.log_signal.emit("[INFO] ✓ 优化完成（结果将以演示数据展示）")

        self.progress_signal.emit(100,"完成")
        self.done_signal.emit()


# ═══════════════════════════════════════════════════════════
#  左侧配置面板
# ═══════════════════════════════════════════════════════════
class ConfigPanel(QWidget):
    config_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(280); self.setMaximumWidth(360)
        self._build()

    def _build(self):
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        outer.addWidget(scroll)
        w = QWidget(); w.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(w); lay.setContentsMargins(12,12,12,12); lay.setSpacing(12)
        scroll.setWidget(w)

        def lbl(t):
            l=QLabel(t); l.setStyleSheet(f"color:{TEXT2};font-size:12px;"); return l

        # ── 配置备注 ──────────────────────────────────────
        g_remark = QGroupBox("REMARK"); g_remark_l = QVBoxLayout(); g_remark_l.setContentsMargins(10,12,10,10); g_remark_l.setSpacing(4); g_remark.setLayout(g_remark_l)
        remark_hint = QLabel("配置备注（将包含在导出的配置码中）")
        remark_hint.setStyleSheet(f"color:{TEXT3};font-size:11px;")
        g_remark_l.addWidget(remark_hint)
        self.remark_edit = QTextEdit()
        self.remark_edit.setPlaceholderText("在此输入备注，例如：适合治疗职业、高暴击流派……")
        self.remark_edit.setFixedHeight(64)
        self.remark_edit.setStyleSheet(
            f"QTextEdit{{background:{BG3};color:{TEXT};border:1px solid {BORDER};"
            f"border-radius:5px;font-size:12px;padding:4px;}}"
            f"QTextEdit:focus{{border-color:{ACCENT2};}}")
        self.remark_edit.textChanged.connect(lambda: self.config_changed.emit())
        g_remark_l.addWidget(self.remark_edit)
        lay.addWidget(g_remark)

        # ── 导入 / 导出配置码 ─────────────────────────────
        g0 = QGroupBox("CONFIG CODE"); g0l = QHBoxLayout(); g0l.setContentsMargins(10,12,10,10); g0l.setSpacing(8); g0.setLayout(g0l)
        btn_exp = QPushButton("⬆  导出配置码")
        btn_imp = QPushButton("⬇  导入配置码")
        for b in (btn_exp, btn_imp):
            b.setFixedHeight(32)
            b.setStyleSheet(
                f"QPushButton{{background:{BG3};color:{TEXT2};"
                f"border:1px solid {BORDER};border-radius:6px;"
                f"font-size:12px;font-weight:600;padding:0 10px;}}"
                f"QPushButton:hover{{color:{ACCENT};border-color:{ACCENT2};}}")
        btn_exp.clicked.connect(self._export_config)
        btn_imp.clicked.connect(self._import_config)
        g0l.addWidget(btn_exp, 1); g0l.addWidget(btn_imp, 1)
        lay.addWidget(g0)

        # ── 网络接口 ─────────────────────────────────────
        g = QGroupBox("NETWORK INTERFACE"); gl = QVBoxLayout(); gl.setSpacing(6); gl.setContentsMargins(10,14,10,10); g.setLayout(gl)
        self.chk_auto = QCheckBox("自动选择接口（推荐）"); self.chk_auto.setChecked(True)
        # 兼容 PySide6 所有版本：int(s)==0 等价于 Qt.Unchecked，不依赖枚举比较
        self.chk_auto.stateChanged.connect(
            lambda s: self.spin_iface.setEnabled(int(s) == 0 and not self.chk_vdata.isChecked())
        )
        gl.addWidget(self.chk_auto)
        row=QHBoxLayout()
        self.spin_iface=QSpinBox(); self.spin_iface.setRange(0,32); self.spin_iface.setValue(0)
        self.spin_iface.setEnabled(False); self.spin_iface.setFixedWidth(70)
        self.btn_list_iface=QPushButton("列出接口"); self.btn_list_iface.setFixedHeight(28)
        self.btn_list_iface.setStyleSheet(f"QPushButton{{background:{BG2};color:{TEXT2};border:1px solid {BORDER};border-radius:5px;font-size:11px;padding:2px 8px;}}QPushButton:hover{{color:{ACCENT};border-color:{ACCENT2};}}")
        self.btn_list_iface.clicked.connect(self._list_ifaces)
        row.addWidget(lbl("接口索引")); row.addWidget(self.spin_iface); row.addStretch(); row.addWidget(self.btn_list_iface)
        gl.addLayout(row)

        # ── 离线 / 在线模式切换 ──────────────────────────
        self.chk_vdata=QCheckBox("离线模式（读取 modules.vdata）"); gl.addWidget(self.chk_vdata)

        # ── 「生成 vdata」—— 仅在线模式下可用 ────────────
        self.chk_generate_vdata = QCheckBox("生成 vdata 文件（在线模式专属）")
        self.chk_generate_vdata.setToolTip(
            "在线模式下抓包成功后，将捕获到的模组数据保存为 modules.vdata。\n"
            "离线模式下此选项自动禁用。")
        gl.addWidget(self.chk_generate_vdata)

        # 离线模式切换时同步更新相关控件状态
        self.chk_vdata.stateChanged.connect(self._on_vdata_mode_changed)

        lay.addWidget(g)

        # ── 模组类型 ─────────────────────────────────────
        g2=QGroupBox("MODULE CATEGORY"); g2l=QVBoxLayout(); g2l.setContentsMargins(10,14,10,10); g2.setLayout(g2l)
        self.combo_cat=QComboBox(); self.combo_cat.addItems(CATEGORIES); self.combo_cat.setCurrentText("全部")
        g2l.addWidget(self.combo_cat); lay.addWidget(g2)

        # ── 包含属性 ─────────────────────────────────────
        g3=QGroupBox("INCLUDE ATTRIBUTES"); g3l=QVBoxLayout(); g3l.setContentsMargins(10,14,10,10); g3l.setSpacing(4); g3.setLayout(g3l)
        self.attr_sel=AttrTagSelector(ALL_ATTRS); self.attr_sel.changed.connect(self.config_changed); g3l.addWidget(self.attr_sel)
        bc=QPushButton("清空所有"); bc.setFixedHeight(22)
        bc.setStyleSheet(f"QPushButton{{background:transparent;color:{TEXT3};border:none;font-size:11px;}}QPushButton:hover{{color:{DANGER};}}")
        bc.clicked.connect(self.attr_sel.clear_all); g3l.addWidget(bc,alignment=Qt.AlignRight)
        lay.addWidget(g3)

        # ── 排除属性 ─────────────────────────────────────
        g4=QGroupBox("EXCLUDE ATTRIBUTES"); g4l=QVBoxLayout(); g4l.setContentsMargins(10,14,10,10); g4l.setSpacing(4); g4.setLayout(g4l)
        self.excl_sel=AttrTagSelector(ALL_ATTRS); self.excl_sel.changed.connect(self.config_changed); g4l.addWidget(self.excl_sel)
        be=QPushButton("清空所有"); be.setFixedHeight(22)
        be.setStyleSheet(f"QPushButton{{background:transparent;color:{TEXT3};border:none;font-size:11px;}}QPushButton:hover{{color:{DANGER};}}")
        be.clicked.connect(self.excl_sel.clear_all); g4l.addWidget(be,alignment=Qt.AlignRight)
        lay.addWidget(g4)

        # ── 高级设置 ─────────────────────────────────────
        g5=QGroupBox("ADVANCED"); adv=QGridLayout(); adv.setContentsMargins(10,14,10,10); adv.setSpacing(8); g5.setLayout(adv)

        adv.addWidget(lbl("匹配属性数"),0,0)
        self.spin_mc=QSpinBox(); self.spin_mc.setRange(1,10); self.spin_mc.setValue(1)
        self.spin_mc.setToolTip("模组需包含的指定属性数量（-mc 参数）"); self.spin_mc.setFixedWidth(70)
        adv.addWidget(self.spin_mc,0,1)

        adv.addWidget(lbl("组合件数"),1,0)
        self.spin_combo=QSpinBox(); self.spin_combo.setRange(1,10); self.spin_combo.setValue(4)
        self.spin_combo.setToolTip("选取几件模组进行组合（1~10，默认 4 件套）"); self.spin_combo.setFixedWidth(70)
        self.spin_combo.valueChanged.connect(self.config_changed)
        adv.addWidget(self.spin_combo,1,1)

        # 件数说明标签
        self.lbl_combo_hint=QLabel("C(N, 4) 件套搭配")
        self.lbl_combo_hint.setStyleSheet(f"color:{TEXT3};font-size:11px;")
        adv.addWidget(self.lbl_combo_hint,1,2)
        self.spin_combo.valueChanged.connect(
            lambda v: self.lbl_combo_hint.setText(f"C(N, {v}) 件套搭配"))

        adv.addWidget(lbl("枚举模式"),2,0)
        self.chk_enum=QCheckBox("启用（推荐配合属性筛选）"); adv.addWidget(self.chk_enum,2,1,1,2)

        adv.addWidget(lbl("调试日志"),3,0)
        self.chk_debug=QCheckBox("启用详细日志输出"); adv.addWidget(self.chk_debug,3,1,1,2)
        adv.setColumnStretch(2,1)
        lay.addWidget(g5)

        # ── 属性总和约束 ─────────────────────────────────
        g6=QGroupBox("MIN ATTR SUM  （件套属性总和约束）"); g6l=QVBoxLayout(); g6l.setContentsMargins(10,14,10,10); g6l.setSpacing(6); g6.setLayout(g6l)
        self.mas_widget = MinAttrSumWidget()
        g6l.addWidget(self.mas_widget); lay.addWidget(g6)

        lay.addStretch()

    def _export_config(self):
        cfg = self.get_config()
        code = _encode_config(cfg)
        dlg = QDialog(self); dlg.setWindowTitle("导出配置码"); dlg.resize(500, 180)
        dlg.setStyleSheet(f"background:{BG2};color:{TEXT};")
        vl = QVBoxLayout(dlg); vl.setSpacing(10); vl.setContentsMargins(16,16,16,16)
        vl.addWidget(QLabel("复制以下配置码，可在任何地方导入还原配置："))
        te = QTextEdit(); te.setPlainText(code); te.setReadOnly(True)
        te.setFixedHeight(60); te.setStyleSheet(f"background:{BG3};color:{ACCENT};border:1px solid {BORDER};border-radius:6px;font-family:monospace;font-size:11px;")
        vl.addWidget(te)
        hl = QHBoxLayout()
        btn_copy = QPushButton("📋  复制到剪贴板"); btn_copy.setFixedHeight(32)
        btn_copy.setStyleSheet(f"QPushButton{{background:{ACCENT_DIM};color:{ACCENT};border:1px solid {ACCENT2};border-radius:6px;font-size:12px;font-weight:600;padding:0 14px;}}QPushButton:hover{{background:{ACCENT2};color:#fff;}}")
        btn_copy.clicked.connect(lambda: (QApplication.clipboard().setText(code), btn_copy.setText("✓  已复制")))
        btn_close = QPushButton("关闭"); btn_close.setFixedHeight(32)
        btn_close.setStyleSheet(f"QPushButton{{background:{BG3};color:{TEXT2};border:1px solid {BORDER};border-radius:6px;font-size:12px;padding:0 14px;}}QPushButton:hover{{color:{TEXT};border-color:{TEXT2};}}")
        btn_close.clicked.connect(dlg.accept)
        hl.addWidget(btn_copy); hl.addStretch(); hl.addWidget(btn_close)
        vl.addLayout(hl)
        dlg.exec()

    def _import_config(self):
        dlg = QDialog(self); dlg.setWindowTitle("导入配置码"); dlg.resize(500, 200)
        dlg.setStyleSheet(f"background:{BG2};color:{TEXT};")
        vl = QVBoxLayout(dlg); vl.setSpacing(10); vl.setContentsMargins(16,16,16,16)
        vl.addWidget(QLabel("粘贴配置码后点击导入："))
        te = QTextEdit(); te.setPlaceholderText("在此粘贴配置码…")
        te.setFixedHeight(70); te.setStyleSheet(f"background:{BG3};color:{TEXT};border:1px solid {BORDER};border-radius:6px;font-family:monospace;font-size:11px;")
        vl.addWidget(te)
        lbl_err = QLabel(""); lbl_err.setStyleSheet(f"color:{DANGER};font-size:11px;")
        vl.addWidget(lbl_err)
        hl = QHBoxLayout()
        btn_ok = QPushButton("⬇  导入"); btn_ok.setFixedHeight(32)
        btn_ok.setStyleSheet(f"QPushButton{{background:{ACCENT_DIM};color:{ACCENT};border:1px solid {ACCENT2};border-radius:6px;font-size:12px;font-weight:600;padding:0 14px;}}QPushButton:hover{{background:{ACCENT2};color:#fff;}}")
        btn_cancel = QPushButton("取消"); btn_cancel.setFixedHeight(32)
        btn_cancel.setStyleSheet(f"QPushButton{{background:{BG3};color:{TEXT2};border:1px solid {BORDER};border-radius:6px;font-size:12px;padding:0 14px;}}QPushButton:hover{{color:{TEXT};border-color:{TEXT2};}}")
        def _do_import():
            code = te.toPlainText().strip()
            try:
                cfg = _decode_config(code)
                self.set_config(cfg)
                dlg.accept()
            except Exception as e:
                lbl_err.setText(f"❌ 解析失败：{e}")
        btn_ok.clicked.connect(_do_import); btn_cancel.clicked.connect(dlg.reject)
        hl.addWidget(btn_ok); hl.addStretch(); hl.addWidget(btn_cancel)
        vl.addLayout(hl)
        dlg.exec()

    def set_config(self, cfg: dict):
        """从配置字典恢复所有控件状态"""
        self.chk_auto.setChecked(bool(cfg.get("auto_interface", True)))
        self.spin_iface.setValue(int(cfg.get("interface_index", 0)))
        self.spin_iface.setEnabled(not bool(cfg.get("auto_interface", True)))

        # 先设 chk_vdata —— 这会触发 _on_vdata_mode_changed，
        # 自动正确地 enable/disable 网络接口控件和 chk_generate_vdata
        is_offline = bool(cfg.get("load_vdata", False))
        self.chk_vdata.setChecked(is_offline)

        # generate_vdata：只有在线模式（非 offline）下才允许为 True
        want_gv = bool(cfg.get("generate_vdata", False)) and not is_offline
        self.chk_generate_vdata.setChecked(want_gv)

        cat = cfg.get("category", "全部")
        idx = self.combo_cat.findText(cat)
        if idx >= 0: self.combo_cat.setCurrentIndex(idx)
        self.attr_sel.set_selected(cfg.get("attributes", []))
        self.excl_sel.set_selected(cfg.get("exclude_attributes", []))
        self.spin_mc.setValue(int(cfg.get("match_count", 1)))
        self.spin_combo.setValue(int(cfg.get("combo_size", 4)))
        self.chk_enum.setChecked(bool(cfg.get("enumeration_mode", False)))
        self.chk_debug.setChecked(bool(cfg.get("debug", False)))
        self.mas_widget.set_constraints(cfg.get("min_attr_sum", {}))
        self.remark_edit.setPlainText(cfg.get("remark", ""))
        self.config_changed.emit()

    def _on_vdata_mode_changed(self, state):
        """离线 / 在线模式切换时，同步更新相关控件的 enable 状态。

        规则（强约束）：
        - 切到离线模式 → 禁用网络接口控件；强制清除并禁用「生成 vdata」
        - 切回在线模式 → 恢复网络接口控件；开放「生成 vdata」

        注意: PySide6 部分版本的 stateChanged 信号传递 int 而非 Qt.CheckState 枚举，
        使用 int() 转换后与常量比较，兼容所有版本。
        """
        # 兼容写法：int(state) 在 PySide6 新旧版本下均可靠
        is_offline = (int(state) == 2)   # Qt.Checked == 2

        # ── 「生成 vdata」仅在线模式有效 ──────────────────
        if is_offline:
            # 强制取消勾选，防止残留状态污染下次在线执行
            self.chk_generate_vdata.setChecked(False)
        self.chk_generate_vdata.setEnabled(not is_offline)

        # ── 网络接口控件：离线模式下无意义，全部禁用 ──────
        self.chk_auto.setEnabled(not is_offline)
        self.btn_list_iface.setEnabled(not is_offline)

        # spin_iface 的状态取决于 is_offline 和 chk_auto 两个维度：
        # - 离线模式下：始终禁用
        # - 在线模式下：chk_auto 勾选时禁用（自动选接口），未勾选时启用（手动选接口）
        if is_offline:
            self.spin_iface.setEnabled(False)
        else:
            self.spin_iface.setEnabled(not self.chk_auto.isChecked())

    def _list_ifaces(self):
        try:
            from network_interface_util import get_network_interfaces
            ifaces=get_network_interfaces()
            lines=["可用网络接口："]
            for i,f in enumerate(ifaces):
                up="✓" if f.get("is_up") else "✗"
                desc=f.get("description",f["name"])
                addrs=[a["addr"] for a in f.get("addresses",[])]
                lines.append(f"  {i:2d}. {up} {desc}")
                if addrs: lines.append(f"       地址: {', '.join(addrs)}")
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self,"网络接口列表","\n".join(lines))
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self,"提示",f"无法获取接口列表：{e}\n（请确认依赖已安装）")

    def get_config(self):
        is_offline = self.chk_vdata.isChecked()
        return {
            "auto_interface":     self.chk_auto.isChecked(),
            "interface_index":    self.spin_iface.value(),
            "load_vdata":         is_offline,
            # 双重保险：即使 chk_generate_vdata 因 bug 保持勾选状态，
            # 离线模式下也强制输出 False，杜绝误生成 vdata
            "generate_vdata":     self.chk_generate_vdata.isChecked() and not is_offline,
            "category":           self.combo_cat.currentText(),
            "attributes":         self.attr_sel.get_selected(),
            "exclude_attributes": self.excl_sel.get_selected(),
            "match_count":        self.spin_mc.value(),
            "combo_size":         self.spin_combo.value(),
            "enumeration_mode":   self.chk_enum.isChecked(),
            "debug":              self.chk_debug.isChecked(),
            "min_attr_sum":       self.mas_widget.get_constraints(),
            "remark":             self.remark_edit.toPlainText(),
        }

# ═══════════════════════════════════════════════════════════
#  右侧输出面板
# ═══════════════════════════════════════════════════════════
class OutputPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self._build()

    def _build(self):
        lay=QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        tabs=QTabWidget(); lay.addWidget(tabs)

        # 日志 Tab
        lw=QWidget(); ll=QVBoxLayout(lw); ll.setContentsMargins(8,8,8,8); ll.setSpacing(4)
        tb=QHBoxLayout()
        lb=QLabel("实时日志"); lb.setStyleSheet(f"color:{TEXT2};font-size:11px;letter-spacing:1px;")
        bc=QPushButton("清空"); bc.setFixedHeight(22)
        bc.setStyleSheet(f"QPushButton{{background:transparent;color:{TEXT3};border:none;font-size:11px;}}QPushButton:hover{{color:{DANGER};}}")
        tb.addWidget(lb); tb.addStretch(); tb.addWidget(bc); ll.addLayout(tb)
        self.log_edit=QTextEdit(); self.log_edit.setReadOnly(True); ll.addWidget(self.log_edit)
        bc.clicked.connect(self.log_edit.clear)
        tabs.addTab(lw,"📋  运行日志")

        # 结果 Tab
        rw=QWidget(); rl=QVBoxLayout(rw); rl.setContentsMargins(8,8,8,8); rl.setSpacing(4)
        rb=QHBoxLayout()
        lr=QLabel("优化结果"); lr.setStyleSheet(f"color:{TEXT2};font-size:11px;letter-spacing:1px;")
        self.lbl_cnt=QLabel("—"); self.lbl_cnt.setStyleSheet(f"color:{ACCENT};font-size:12px;")
        rb.addWidget(lr); rb.addStretch(); rb.addWidget(self.lbl_cnt); rl.addLayout(rb)
        # 滚动区域 — 存放卡片
        self._res_scroll = QScrollArea()
        self._res_scroll.setWidgetResizable(True)
        self._res_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._res_scroll.setStyleSheet(f"QScrollArea{{background:{BG2};border:1px solid {BORDER};border-radius:6px;}}")
        self._res_container = QWidget()
        self._res_container.setStyleSheet(f"background:{BG2};")
        self._res_layout = QVBoxLayout(self._res_container)
        self._res_layout.setContentsMargins(8,8,8,8)
        self._res_layout.setSpacing(8)
        self._res_layout.addStretch()
        self._res_scroll.setWidget(self._res_container)
        rl.addWidget(self._res_scroll)
        tabs.addTab(rw,"🏆  优化结果")

    def append_log(self, html):
        c=self.log_edit.textCursor(); c.movePosition(QTextCursor.End)
        self.log_edit.setTextCursor(c); self.log_edit.insertHtml(html+"<br/>")
        self.log_edit.ensureCursorVisible()

    def append_log_plain(self, text):
        text=text.strip()
        if text.startswith("[INFO]"):   color,tag,msg=TEXT,f'<span style="color:{ACCENT2}">[INF]</span>',text[6:].strip()
        elif text.startswith("[WARN]"):  color,tag,msg=WARNING,f'<span style="color:{WARNING}">[WRN]</span>',text[6:].strip()
        elif "[ERR" in text[:8]:         color,tag,msg=DANGER,f'<span style="color:{DANGER}">[ERR]</span>',text.split("]",1)[-1].strip()
        else:                            color,tag,msg=TEXT,f'<span style="color:{TEXT3}">[LOG]</span>',text
        ts=time.strftime("%H:%M:%S")
        self.append_log(
            f'<span style="color:{TEXT3}">{ts}</span> {tag} '
            f'<span style="color:{color}">{msg}</span>')

    def set_results(self, combo_size, results=None, highlight_attrs=None):
        """
        results: list of dict — 每项格式见 MonitorWorker._results
        highlight_attrs: set/list of str — 用户指定的关注属性，结果中高亮显示
        """
        HL_ATTRS = set(highlight_attrs) if highlight_attrs else set()

        # ── 清空旧卡片（保留末尾 stretch）
        while self._res_layout.count() > 1:
            item = self._res_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if results is None:
            # ── 100 套演示数据 ──────────────────────────────
            import random as _rnd
            _rnd.seed(42)
            _mod_pool = [
                ("卓越辅助",     4, [("极-全队幸暴",10),("施法专注",2),("攻速专注",5)]),
                ("卓越辅助",     4, [("暴击专注",10),("攻速专注",4),("特攻治疗加持",3)]),
                ("高性能守护",   3, [("暴击专注",10),("攻速专注",7)]),
                ("卓越辅助",     4, [("极-全队幸暴",10),("特攻治疗加持",2),("幸运专注",4)]),
                ("高性能治疗",   3, [("暴击专注",10),("攻速专注",4)]),
                ("卓越辅助-优选",4, [("攻速专注",8),("暴击专注",6),("特攻治疗加持",3)]),
                ("高性能攻击",   3, [("暴击专注",10),("特攻伤害",8)]),
                ("卓越辅助",     4, [("暴击专注",10),("幸运专注",4),("特攻治疗加持",3)]),
                ("精英守护",     2, [("暴击专注",8),("攻速专注",6)]),
                ("精英辅助",     2, [("智力加持",6),("暴击专注",6)]),
            ]
            results = []
            base_score = 68
            base_bp    = 1294.0
            for r in range(1, 101):
                chosen = _rnd.sample(_mod_pool, min(combo_size, len(_mod_pool)))
                modules = [{"name":n,"quality":q,"attrs":a} for n,q,a in chosen]
                attr_sum: Dict[str,int] = {}
                for mod in modules:
                    for an, av in mod["attrs"]:
                        attr_sum[an] = attr_sum.get(an, 0) + av
                score = max(20, base_score - (r-1)//5)
                bp    = max(800.0, base_bp - (r-1)*5)
                results.append({
                    "rank": r, "score": score, "battle_power": bp,
                    "modules": modules, "attr_sum": attr_sum,
                })

        RANK_COLORS = {1: "#ffd700", 2: "#c0c0c0", 3: "#cd7f32"}
        # 品质4=金色 品质3=紫色 品质2=蓝色 品质1=灰色
        QUAL_COLORS = {4: "#ffd700", 3: "#b060ff", 2: "#4488ff", 1: TEXT2}
        QUAL_BG     = {4: "#332800", 3: "#220044", 2: "#001433", 1: BG2}
        QUAL_BDR    = {4: "#ffd70044", 3: "#b060ff44", 2: "#4488ff44", 1: BORDER}

        def _chip_style(aname: str):
            """词条chip样式：仅用户选中的属性才高亮（极系蓝色 / 普通绿色），未选中一律灰色"""
            if aname in HL_ATTRS:
                if aname in set(SPECIAL_ATTRS):
                    return ACCENT_DIM, ACCENT, ACCENT2   # 用户选中的极系词条 → 青色
                return "#003322", SUCCESS, "#00aa66"     # 用户选中的普通词条 → 绿色
            return BG2, TEXT2, BORDER                    # 未选中 → 灰色，不论是否为极系

        for res in results:
            rank     = res["rank"]
            score    = res["score"]
            bp       = res.get("battle_power", 0)
            modules  = res["modules"]
            attr_sum = res.get("attr_sum", {})
            rank_clr = RANK_COLORS.get(rank, TEXT2)

            card = QWidget()
            card.setStyleSheet(
                f"QWidget#card{{background:{PANEL};border:1px solid {BORDER};"
                f"border-radius:8px;}}"
                f"QWidget#card:hover{{border-color:{ACCENT2};}}")
            card.setObjectName("card")
            card_lay = QVBoxLayout(card)
            card_lay.setContentsMargins(12,10,12,10); card_lay.setSpacing(6)

            # ── 标题行
            hdr = QHBoxLayout(); hdr.setSpacing(12)
            rank_lbl = QLabel(f"# {rank}")
            rank_lbl.setStyleSheet(
                f"color:{rank_clr};font-size:16px;font-weight:700;min-width:40px;")
            hdr.addWidget(rank_lbl)
            score_lbl = QLabel(f"总属性值  {int(score)}")
            score_lbl.setStyleSheet(f"color:{SUCCESS};font-size:13px;font-weight:600;")
            hdr.addWidget(score_lbl)
            bp_lbl = QLabel(f"战斗力  {bp:,.0f}")
            bp_lbl.setStyleSheet(f"color:{WARNING};font-size:13px;")
            hdr.addWidget(bp_lbl)
            hdr.addStretch()
            card_lay.addLayout(hdr)

            sep = QWidget(); sep.setFixedHeight(1)
            sep.setStyleSheet(f"background:{BORDER};")
            card_lay.addWidget(sep)

            body = QHBoxLayout(); body.setSpacing(12)

            # ── 左：模组列表
            mod_widget = QWidget(); mod_widget.setStyleSheet("background:transparent;")
            mod_lay = QVBoxLayout(mod_widget)
            mod_lay.setContentsMargins(0,0,0,0); mod_lay.setSpacing(4)
            mod_hdr = QLabel("模  组  列  表")
            mod_hdr.setStyleSheet(f"color:{TEXT3};font-size:10px;letter-spacing:3px;")
            mod_lay.addWidget(mod_hdr)

            for idx, mod in enumerate(modules, 1):
                mname   = mod["name"]
                quality = mod["quality"]
                attrs   = mod["attrs"]
                qclr = QUAL_COLORS.get(quality, TEXT2)
                qbg  = QUAL_BG.get(quality, BG2)
                qbdr = QUAL_BDR.get(quality, BORDER)

                row_w = QWidget()
                row_w.setStyleSheet(f"background:{BG3};border-radius:5px;")
                row_l = QHBoxLayout(row_w)
                row_l.setContentsMargins(8,4,8,4); row_l.setSpacing(6)

                idx_lbl = QLabel(f"{idx}.")
                idx_lbl.setStyleSheet(f"color:{TEXT3};font-size:12px;min-width:18px;")
                row_l.addWidget(idx_lbl)

                q_lbl = QLabel(f"品质{quality}")
                q_lbl.setStyleSheet(
                    f"color:{qclr};font-size:11px;font-weight:700;"
                    f"background:{qbg};border:1px solid {qbdr};"
                    f"border-radius:3px;padding:0 5px;")
                row_l.addWidget(q_lbl)

                name_lbl = QLabel(mname)
                name_lbl.setStyleSheet(
                    f"color:{TEXT};font-size:13px;font-weight:600;min-width:100px;")
                row_l.addWidget(name_lbl)

                for aname, aval in attrs:
                    chip_bg, chip_clr, chip_bdr = _chip_style(aname)
                    chip = QLabel(f"{aname}+{aval}")
                    chip.setStyleSheet(
                        f"color:{chip_clr};font-size:11px;"
                        f"background:{chip_bg};border:1px solid {chip_bdr};"
                        f"border-radius:3px;padding:1px 5px;")
                    row_l.addWidget(chip)

                row_l.addStretch()
                mod_lay.addWidget(row_w)

            body.addWidget(mod_widget, 3)

            # ── 右：属性汇总
            sum_widget = QWidget(); sum_widget.setStyleSheet("background:transparent;")
            sum_lay = QVBoxLayout(sum_widget)
            sum_lay.setContentsMargins(0,0,0,0); sum_lay.setSpacing(4)
            sum_hdr = QLabel("属  性  分  布")
            sum_hdr.setStyleSheet(f"color:{TEXT3};font-size:10px;letter-spacing:3px;")
            sum_lay.addWidget(sum_hdr)

            for aname, total in attr_sum.items():
                is_sp = aname in set(SPECIAL_ATTRS)
                is_hl = aname in HL_ATTRS
                if is_hl and is_sp:
                    bar_clr, txt_clr = ACCENT, ACCENT    # 用户选中的极系词条 → 青色
                elif is_hl:
                    bar_clr, txt_clr = SUCCESS, SUCCESS  # 用户选中的普通词条 → 绿色
                else:
                    bar_clr, txt_clr = TEXT2, TEXT2      # 未选中 → 灰色，不论是否为极系

                a_row = QHBoxLayout(); a_row.setSpacing(6)
                name_l = QLabel(aname)
                name_l.setStyleSheet(
                    f"color:{txt_clr};font-size:12px;min-width:110px;"
                    + ("font-weight:600;" if is_hl else ""))
                name_l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                a_row.addWidget(name_l)
                val_l = QLabel(f"+{total}")
                val_l.setStyleSheet(
                    f"color:{bar_clr};font-size:13px;font-weight:700;min-width:36px;")
                a_row.addWidget(val_l)
                a_row.addStretch()
                sum_lay.addLayout(a_row)

            sum_lay.addStretch()
            body.addWidget(sum_widget, 1)

            card_lay.addLayout(body)
            self._res_layout.insertWidget(self._res_layout.count()-1, card)

        self.lbl_cnt.setText(f"共 {len(results)} 套方案  (C(N, {combo_size}))")

# ═══════════════════════════════════════════════════════════
#  底部状态栏（实时进度条）
# ═══════════════════════════════════════════════════════════
class BottomBar(QWidget):
    start_clicked = Signal()
    stop_clicked  = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(64)
        self.setStyleSheet(f"background:{BG2};border-top:1px solid {BORDER};")
        self._running = False
        self._start_time: float = 0.0       # 运行起始时间戳
        self._last_pct: int = 0              # 子进程回报的百分比
        self._estimated_secs: float = 0.0    # 预估总耗时（秒），用于平滑进度条
        self._build()
        from PySide6.QtCore import QTimer
        self._timer = QTimer(self)
        self._timer.setInterval(500)         # 0.5 秒刷新，进度条更丝滑
        self._timer.timeout.connect(self._tick)

    def _build(self):
        lay=QHBoxLayout(self); lay.setContentsMargins(16,8,16,8); lay.setSpacing(12)

        self.lbl_est=QLabel("预估时间：—")
        self.lbl_est.setStyleSheet(f"color:{TEXT2};font-size:12px;min-width:200px;")
        lay.addWidget(self.lbl_est)

        self.lbl_combo=QLabel("组合数：—")
        self.lbl_combo.setStyleSheet(f"color:{TEXT3};font-size:11px;min-width:180px;")
        lay.addWidget(self.lbl_combo)

        lay.addStretch()

        prog_col = QVBoxLayout(); prog_col.setSpacing(2); prog_col.setContentsMargins(0,0,0,0)
        self.prog=QProgressBar(); self.prog.setRange(0,100); self.prog.setValue(0)
        self.prog.setFixedWidth(260); self.prog.setFixedHeight(10)
        prog_col.addWidget(self.prog)

        self.lbl_realtime=QLabel("就绪")
        self.lbl_realtime.setStyleSheet(
            f"color:{TEXT2};font-size:11px;font-family:Consolas,'Courier New',monospace;min-width:260px;")
        prog_col.addWidget(self.lbl_realtime)
        lay.addLayout(prog_col)

        lay.addSpacing(16)
        self.btn_start=QPushButton("▶  开始"); self.btn_start.setObjectName("btn_start"); self.btn_start.setFixedHeight(40)
        self.btn_stop =QPushButton("■  停止"); self.btn_stop.setObjectName("btn_stop");  self.btn_stop.setFixedHeight(40)
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_clicked)
        self.btn_stop.clicked.connect(self.stop_clicked)
        lay.addWidget(self.btn_start); lay.addWidget(self.btn_stop)

    @staticmethod
    def _fmt_duration(s: float) -> str:
        if s < 1:      return "< 1s"
        if s < 60:     return f"{s:.0f}s"
        if s < 3600:   return f"{s/60:.1f}m"
        return f"{s/3600:.1f}h"

    def _tick(self):
        """每 0.5 秒刷新进度条和状态文本。

        进度来源（取最大值）：
          ① 时间推算：elapsed / estimated_secs × 95%（上限 95%，留 5% 给收尾）
          ② 子进程回报：_last_pct（解析到搭配结果时会跳到较高值）
        两者取 max，保证进度条只进不退。
        """
        if not self._running or self._start_time <= 0:
            return
        elapsed = time.time() - self._start_time

        # ── 计算合成进度 ──
        time_pct = 0
        if self._estimated_secs > 0:
            # 上限 95%，剩余 5% 留给"解析结果→完成"阶段
            time_pct = min(95, int(elapsed / self._estimated_secs * 95))
        pct = max(self._last_pct, time_pct, 1)

        if pct >= 100:
            self.prog.setValue(100)
            self.lbl_realtime.setText(f"✓ 完成  耗时 {self._fmt_duration(elapsed)}")
            self._timer.stop()
            return

        self.prog.setValue(pct)

        # ── 文本状态 ──
        parts = [f"{pct}%", f"耗时 {self._fmt_duration(elapsed)}"]
        if self._estimated_secs > 0:
            remaining = max(0, self._estimated_secs - elapsed)
            parts.append(f"剩余 ~{self._fmt_duration(remaining)}")
        elif pct >= 5:
            # 无预估时间时，从已有百分比线性外推
            eta = elapsed / pct * (100 - pct)
            parts.append(f"剩余 ~{self._fmt_duration(eta)}")
        self.lbl_realtime.setText("  |  ".join(parts))

    def set_estimated_secs(self, secs: float):
        """设置本次运行的预估总耗时（秒），用于时间推算进度条。
        由 MainWindow._start 根据基准测试数据计算后调用。
        传 0 表示无预估（回退到纯子进程回报模式）。
        """
        self._estimated_secs = max(0.0, secs)

    def set_running(self, running):
        self._running = running
        self.btn_start.setEnabled(not running); self.btn_stop.setEnabled(running)
        if running:
            self._start_time = time.time()
            self._last_pct = 0
            self.prog.setValue(0)
            self.lbl_realtime.setText("启动中…")
            self._timer.start()
        else:
            self._timer.stop()
            if self._start_time > 0:
                elapsed = time.time() - self._start_time
                self.prog.setValue(100)
                self.lbl_realtime.setText(f"✓ 完成  耗时 {self._fmt_duration(elapsed)}")

    def update_progress(self, v, lbl=""):
        """子进程回报的进度（解析到搭配结果时触发）。"""
        self._last_pct = v
        # 立即同步进度条（取 max 防回退）
        cur = self.prog.value()
        if v > cur:
            self.prog.setValue(v)
        if lbl and not self._running:
            self.lbl_realtime.setText(lbl)

    def update_estimate(self, n, k, mode, bench):
        if not n or n < k:
            self.lbl_est.setText("预估时间：—")
            self.lbl_combo.setText("组合数：需先运行一次扫描")
            return
        try:   total=math.comb(n,k)
        except: total=0
        combo_str=f"{total:,}" if total<1_000_000 else f"{total/1e4:.1f} 万"
        self.lbl_combo.setText(f"C({n}, {k}) = {combo_str}")
        speed=bench.get(mode,0)
        if speed>0 and total>0:
            secs=(total/1e4)/speed
            if secs<1:     est="< 1 秒"
            elif secs<60:  est=f"≈ {secs:.1f} 秒"
            elif secs<3600:est=f"≈ {secs/60:.1f} 分钟"
            else:          est=f"≈ {secs/3600:.1f} 小时"
            mlbl={"cuda":"CUDA","opencl":"OpenCL","cpu":"CPU"}.get(mode,mode)
            self.lbl_est.setText(f"预估时间：{est}  [{mlbl}]")
        else:
            self.lbl_est.setText("预估时间：请先运行基准测试")

# ═══════════════════════════════════════════════════════════
#  顶部标题栏
# ═══════════════════════════════════════════════════════════
class TitleBar(QWidget):
    benchmark_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(56)
        self.setStyleSheet(
            f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 #0d1018,stop:0.5 #111520,stop:1 #0d1018);"
            f"border-bottom:1px solid {BORDER};")
        self._build()

    def _build(self):
        lay=QHBoxLayout(self); lay.setContentsMargins(16,0,16,0); lay.setSpacing(10)
        dot=QLabel("◆"); dot.setStyleSheet(f"color:{ACCENT};font-size:18px;")
        lay.addWidget(dot)
        title=QLabel("星痕共鸣  模组筛选器")
        title.setStyleSheet(f"color:{TEXT};font-size:16px;font-weight:700;letter-spacing:2px;")
        lay.addWidget(title)
        ver=QLabel("v1.6.5")
        ver.setStyleSheet(
            f"color:{TEXT3};font-size:11px;background:{BG3};"
            f"border:1px solid {BORDER};border-radius:3px;padding:1px 6px;")
        lay.addWidget(ver)
        lay.addSpacing(20)
        self.mode_bar=ComputeModeBar(); lay.addWidget(self.mode_bar)
        lay.addStretch()
        btn=QPushButton("⚡  基准测试")
        btn.setStyleSheet(
            f"QPushButton{{background:{BG3};color:{WARNING};"
            f"border:1px solid {WARNING}44;border-radius:6px;"
            f"padding:5px 14px;font-size:12px;font-weight:600;}}"
            f"QPushButton:hover{{background:{WARNING}22;border-color:{WARNING};}}")
        btn.clicked.connect(self.benchmark_clicked)
        lay.addWidget(btn)

# ═══════════════════════════════════════════════════════════
#  主窗口
# ═══════════════════════════════════════════════════════════
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("星痕共鸣 模组筛选器")
        self.resize(1200,780); self.setMinimumSize(900,620)
        self._worker: Optional[MonitorWorker] = None
        self._bench: Dict[str,float] = {}
        self._avail: Dict[str,bool] = {"cuda":False,"opencl":False,"cpu":True}
        self._mode = "cpu"
        self._last_module_n: Optional[int] = None   # 上次扫描到的模组数量，用于 C(N,k) 动态估算
        self._setup_log(); self._build_ui(); self._connect_log_handler(); self._probe_gpu()

    def _build_ui(self):
        self.setStyleSheet(QSS)
        cw=QWidget(); self.setCentralWidget(cw)
        root=QVBoxLayout(cw); root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        self.title_bar=TitleBar()
        self.title_bar.benchmark_clicked.connect(self._open_bench)
        self.title_bar.mode_bar.mode_changed.connect(self._on_mode)
        root.addWidget(self.title_bar)

        spl=QSplitter(Qt.Horizontal); spl.setHandleWidth(1); root.addWidget(spl,1)
        self.cfg=ConfigPanel(); self.cfg.config_changed.connect(self._upd_est)
        spl.addWidget(self.cfg)
        self.out=OutputPanel(); spl.addWidget(self.out)
        spl.setSizes([310,870])

        self.bar=BottomBar()
        self.bar.start_clicked.connect(self._start)
        self.bar.stop_clicked.connect(self._stop)
        root.addWidget(self.bar)

    def _setup_log(self):
        self._log_h=QTextEditHandler()
        fmt=logging.Formatter("%(message)s"); self._log_h.setFormatter(fmt)
        logging.getLogger().addHandler(self._log_h)
        logging.getLogger().setLevel(logging.DEBUG)

    def _connect_log_handler(self):
        """在out面板创建后调用，连接日志handler到UI"""
        self._log_h.new_record.connect(lambda html, _: self.out.append_log(html))

    # Signal用于GPU探针线程安全回调主线程
    _sig_gpu_result = Signal(bool, bool)  # cuda, opencl

    def _probe_gpu(self):
        self._sig_gpu_result.connect(self._on_gpu_result)
        def probe():
            cuda = False
            opencl = False
            try:
                from cpp_extension.module_optimizer_cpp import test_cuda, test_opencl
                cuda = bool(test_cuda())
                opencl = bool(test_opencl())
            except Exception:
                pass
            self._sig_gpu_result.emit(cuda, opencl)
        threading.Thread(target=probe,daemon=True).start()

    @Slot(bool, bool)
    def _on_gpu_result(self, cuda, opencl):
        self._avail={"cuda":cuda,"opencl":opencl,"cpu":True}
        self.title_bar.mode_bar.set_availability(cuda,opencl)
        parts=[]
        if cuda:   parts.append(f'<span style="color:{CUDA_CLR}">CUDA ✓</span>')
        if opencl: parts.append(f'<span style="color:{OPENCL_CLR}">OpenCL ✓</span>')
        parts.append(f'<span style="color:{CPU_CLR}">CPU ✓</span>')
        self.out.append_log(f'<span style="color:{TEXT3}">GPU 探针：</span> {" | ".join(parts)}')

    @Slot(str)
    def _on_mode(self, mode):
        self._mode=mode; self._upd_est()

    def _upd_est(self):
        cfg = self.cfg.get_config()
        k   = cfg.get("combo_size", 4)
        n   = self._last_module_n
        if n is None or n < k:
            # 尚未运行过扫描，显示占位
            self.bar.update_estimate(0, k, self._mode, self._bench)
        else:
            self.bar.update_estimate(n, k, self._mode, self._bench)

    def _open_bench(self):
        combo_size = int(self.cfg.get_config().get("combo_size", 4))
        dlg=BenchmarkDialog(self._avail, self, combo_size=combo_size); dlg.exec()
        r=dlg.get_results()
        if r: self._bench=r; self._upd_est()

    def _start(self):
        cfg=self.cfg.get_config(); cfg["mode"]=self._mode
        # ── 根据基准测试数据计算预估耗时，传给进度条 ──
        est_secs = 0.0
        k = cfg.get("combo_size", 4)
        n = self._last_module_n
        speed = self._bench.get(self._mode, 0)
        if n and n >= k and speed > 0:
            try:
                total = math.comb(n, k)
                est_secs = (total / 1e4) / speed
                # 加 20% 余量（预筛选、结果排序、IO 等开销）
                est_secs *= 1.2
            except Exception:
                pass
        self.bar.set_estimated_secs(est_secs)
        self.bar.set_running(True); self.bar.update_progress(0,"启动中…")
        self.out.log_edit.clear()
        self._worker=MonitorWorker(cfg)
        self._worker.log_signal.connect(self.out.append_log_plain)
        self._worker.progress_signal.connect(lambda v,l: self.bar.update_progress(v,l))
        self._worker.module_count_signal.connect(self._on_module_count)
        self._worker.done_signal.connect(self._on_done)
        self._worker.error_signal.connect(self._on_err)
        self._worker.start()

    @Slot(int)
    def _on_module_count(self, n):
        self._last_module_n = n
        self._upd_est()
        # ── 子进程刚报告了实际模组数 N，重新计算预估耗时推进进度条 ──
        cfg = self.cfg.get_config()
        k = cfg.get("combo_size", 4)
        speed = self._bench.get(self._mode, 0)
        if n >= k and speed > 0:
            try:
                total = math.comb(n, k)
                est_secs = (total / 1e4) / speed * 1.2
                self.bar.set_estimated_secs(est_secs)
            except Exception:
                pass

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self.out.append_log_plain("[WARN] 正在停止…")

    @Slot()
    def _on_done(self):
        self.bar.set_running(False); self.bar.update_progress(100,"完成")
        cfg     = self.cfg.get_config()
        results = getattr(self._worker, '_results', None)
        self.out.set_results(
            cfg.get("combo_size", 4),
            results,
            highlight_attrs=cfg.get("attributes", [])
        )

    @Slot(str)
    def _on_err(self, msg):
        self.bar.set_running(False); self.bar.update_progress(0,"错误")
        self.out.append_log_plain(f"[ERR] {msg}")

    def closeEvent(self, e):
        if self._worker and self._worker.isRunning():
            self._worker.stop(); self._worker.wait(2000)
        super().closeEvent(e)

# ═══════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════
def main():
    app=QApplication(sys.argv); app.setStyle("Fusion")
    pal=QPalette()
    pal.setColor(QPalette.Window,         QColor(BG))
    pal.setColor(QPalette.WindowText,     QColor(TEXT))
    pal.setColor(QPalette.Base,           QColor(BG2))
    pal.setColor(QPalette.AlternateBase,  QColor(BG3))
    pal.setColor(QPalette.Text,           QColor(TEXT))
    pal.setColor(QPalette.Button,         QColor(BG3))
    pal.setColor(QPalette.ButtonText,     QColor(TEXT))
    pal.setColor(QPalette.Highlight,      QColor(ACCENT_DIM))
    pal.setColor(QPalette.HighlightedText,QColor(ACCENT))
    app.setPalette(pal)
    win=MainWindow(); win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    # ── freeze_support 必须在最早期调用，否则 multiprocessing spawn 子进程会再次启动 GUI ──
    import multiprocessing as _mp
    _mp.freeze_support()

    # ── 编译后单二进制调度 ──────────────────────────────────────────────────────────────
    # PyInstaller 打包后只有一个 .exe，子进程无法直接执行 .py 脚本。
    # MonitorWorker 启动监控器时会传入 --run-monitor 标志，在此拦截并转发给监控器，
    # 避免 exe 以无参数模式运行时重新弹出 GUI 窗口。
    if len(sys.argv) > 1 and sys.argv[1] == "--run-monitor":
        # 去掉 --run-monitor，把剩余参数还给 star_railway_monitor 的 argparse
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from star_railway_monitor import main as _monitor_main
        _monitor_main()
    else:
        main()
