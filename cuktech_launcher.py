# CUKTECH control panel native client (PySide6, no webview/browser) — glass/light theme.
# Backend: the project's real ha_server.py executed in a --headless child
# process (main-thread semantics); UI talks to it over 127.0.0.1:8080 only.
#   --headless / CUKTECH_HEADLESS / daily-restart execv -> server only, no UI.
# Close window -> ask: minimize to tray (service keeps running) or exit
# (terminate spawned service child if owned, then quit).
import atexit
import datetime
import ctypes
from ctypes import wintypes
import http.client
import json
import math
import os
import re
import runpy
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEvent, QObject,
    QPoint, QPointF, Property, QPropertyAnimation, QRect, QRectF, QSettings,
    QSize, Qt, QTime, QTimer, QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QConicalGradient, QCursor, QFont,
    QFontDatabase, QFontMetrics, QGradient, QIcon, QImage, QKeySequence, QLinearGradient, QPainter, QPen,
    QPixmap, QRadialGradient, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QHeaderView, QInputDialog, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QSizePolicy, QSpacerItem, QSpinBox, QStackedWidget, QStyle, QSystemTrayIcon, QTabWidget,
    QTableWidget, QTableWidgetItem, QTimeEdit, QVBoxLayout, QWidget, QLayout, QTextBrowser,
    QScrollArea, QListWidget, QListWidgetItem)

import ai_payload       # 1.4.0：AI 出站载荷唯一构造点（字段定义派生 + 软校验）
import ai_service       # 1.5.6：统一 AI 服务层（取数/组装/调用/降级/留痕）
import agent_tools      # 1.5.6：Agent 工具集（能力定义/关键词兜底/参数校验/审计）
import ck_http           # 本机回环 HTTP 单源（make_api 的实现在这里）
import ck_time           # 时间格式化单源（5 个格式串，21 处收口）
import ux_core as ux
import ui_kit as uikit   # 48 px 页内头单源，统一由 ui_kit 提供


class _CkSpinBox(QSpinBox):
    """R09：QSS 下 QSpinBox 的上下按钮只剩底色，三角 border-hack 被绘成实心小方块。
    这里保留按钮命中区（QSS 宽 16px），在 paintEvent 内用矢量三角把箭头画出来。"""

    BTN_W = 16

    def paintEvent(self, ev):
        super().paintEvent(ev)
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        pt.setPen(Qt.NoPen)
        pt.setBrush(QColor('#9fe8ff'))
        cx = self.width() - self.BTN_W / 2.0
        hh = self.height() / 2.0
        up_y = hh / 2.0
        dn_y = hh + hh / 2.0
        pt.drawPolygon(QPolygonF([QPointF(cx - 4, up_y + 2),
                                  QPointF(cx + 4, up_y + 2),
                                  QPointF(cx, up_y - 2)]))
        pt.drawPolygon(QPolygonF([QPointF(cx - 4, dn_y - 2),
                                  QPointF(cx + 4, dn_y - 2),
                                  QPointF(cx, dn_y + 2)]))
        pt.end()


HOST = '127.0.0.1'
FALLBACK_PORT = 8080
# 窗口与端口卡尺寸令牌（唯一来源）：
#   四张卡固定 364×322 两列并排 + 记录页 7 列可完整显示，实测下限就是 995×760。
#   默认窗口不小于最小窗口，首帧不再被 setMinimumSize 悄悄钳制。
WIN_MIN_W, WIN_MIN_H = 995, 760
WIN_W, WIN_H = 1000, 760
PORT_CARD_W, PORT_CARD_H = 364, 322
# 1.5.5：端口卡曲线区最小高度（标题 15 + 图区 ≈ 77）。
# 曲线区常驻：不随充电状态伸缩，空载也占位；卡内富余高度全部给图区，
# 因此同一窗口下四卡等高、同一档位下充电/空载完全一致。
PORT_MINI_H = 92
PANEL_TITLE = '星源智控'
# R48 / C18：独占且稳定的 AUMID（不复用 DoubaoWork.* 前缀）。
# 通知署名、AUMID 注册、Start Menu 快捷方式三处同源。
CK_AUMID_DEFAULT = 'Cuktech.CuktechControlPanel'
CK_AUMID = os.environ.get('CK_AUMID') or CK_AUMID_DEFAULT
CANONICAL_DIR = os.environ.get('CK_BACKEND_DIR', '')  # R28：不再硬编码开发机绝对路径
PORT_NAMES = {'1': ('c1', 'C口 1'), '2': ('c2', 'C口 2'), '3': ('c3', 'C口 3'), '4': ('a', 'USB-A')}
# R042 / C11：统一色板。Qt QSS 没有 CSS 变量，内联样式使用以下字面值；
# 审计以 PALETTE 为唯一 token 源（LIGHT_QSS 中的颜色也是 token 值）。
TEXT = uikit.TOKENS['text']   # 正文白取 ui_kit 令牌（#e8f4ff），引擎内不再留硬编码色
TEXT_SUB = uikit.TOKENS['text_sub']   # 次级文字取令牌（#9fb4d0），全仓旧灰值清零
TEXT_MUTED = '#94a3b8'
TEXT_SOFT = '#cfe0f0'
TEXT_ALT = '#b8c6d6'
OK = '#34d399'
OK_DARK = '#0fa968'
WARN = '#f59e0b'
WARN_SOFT = '#fbbf24'
WARN_BRIGHT = '#ffa42b'
ERR = '#ef4444'
ERR_SOFT = '#ef6a5d'
GRAD_A = '#03a9f4'
GRAD_B = '#7c4dff'
AI_BG = '#0b1220'
AI_LINE = '#3a4658'
AI_BLUE = '#7fd4ff'
AI_BLUE2 = '#8fd6ff'
AI_VIOLET = '#b7a6ff'
AI_TEXT = '#cfe2f5'
AI_TEXT2 = '#eaf4ff'
AI_ERR = '#ff7a7a'
REC_BLUE = '#0ea5e9'
PALETTE = {
    'text': TEXT, 'textSub': TEXT_SUB, 'textMuted': TEXT_MUTED,
    'textSoft': TEXT_SOFT, 'textAlt': TEXT_ALT,
    'ok': OK, 'okDark': OK_DARK, 'warn': WARN, 'warnSoft': WARN_SOFT,
    'warnBright': WARN_BRIGHT, 'err': ERR, 'errSoft': ERR_SOFT,
    'gradA': GRAD_A, 'gradB': GRAD_B,
    'aiBg': AI_BG, 'aiLine': AI_LINE, 'aiBlue': AI_BLUE, 'aiBlue2': AI_BLUE2,
    'aiViolet': AI_VIOLET, 'aiText': AI_TEXT, 'aiText2': AI_TEXT2,
    'aiErr': AI_ERR, 'recBlue': REC_BLUE,
}
# 网页同源配色：C1 青 / C2 紫 / C3 翠绿 / A 琥珀 / Total 白
PORT_COLORS = {'1': '#22d3ee', '2': '#a78bfa', '3': '#34d399', '4': '#fbbf24'}
TOTAL_COLOR = '#ffffff'

TEAL = '#38e2ff'
TEAL_D = '#38e2ff'
ACCENT2 = '#9b6bff'
ACCENT = '#38e2ff'
RATED_POWER = 120.0  # 充电器额定功率（空闲时曲线默认量程）
INK = TEXT   # R042 / C11：与正文同源，消除双白（TOKENS['text'] = #e8f4ff）
SUB = TEXT_SUB   # R042 / C11：与次级文字同源，消除 #9fb4d0/#93a9d1 双灰
BG = '#0d1626'

# 统一动画节奏 token：数字 520ms、卡片 320ms、呼吸 2400ms
DUR_FAST = 160
DUR_MED = 320
DUR_NUM = 520
DUR_SLOW = 2400
# 1.5.5 网络超时 token：控制类 POST（开关/端口/设置）统一 6s；只读查询沿用 api() 默认 8s。
CK_T_CMD = 6
# R23：辉光 A/B 诊断开关（仅取图用，不影响默认运行）。
# =1 时恢复 R22 之前的 QGraphicsDropShadowEffect（hero blur 40 / 'W' blur 22），
# 且 hero 数字走透明绘制（不做抓底自绘、不画手绘描边），用于同构建内视觉等价性对比。
_GLOW_LEGACY = os.environ.get('CK_GLOW_LEGACY') == '1'
# R24-C：辉光拆静态/动态。=1（默认）静态层只烤剪影辉光（key 不含文字/数字/闪光色），
# 文字每帧实时叠加；=0 回退到 R23 的「文字+辉光整图重烤」，仅用于同构建 A/B 取图。
_CK_GLOW_SPLIT = os.environ.get('CK_GLOW_SPLIT') != '0'
_CK_GLOW_R = 40
_CK_GLOW_COL = (3, 169, 244, 150)
# R24-C：静态辉光形状补烤间隔（秒）。0 = 禁用（只随几何/主题/轮廓重建）。
# 默认 3.0：数值稳定后每 ≥3s 用当前字形补烤一次静态层，把「辉光形状滞后」压到
# 肉眼不可见；不随动画帧/闪光帧逐帧重烤（那是旧实现 702–922 次/90s 的来源）。
_CK_GLOW_REFRESH = float(os.environ.get('CK_GLOW_REFRESH') or 3.0)


def aligned_button(text, kind='primary', min_w=96, h=34, parent=None, glyph=None):
    """G1 统一按钮工厂：同组等高、同最小宽、圆角9、字号13、左右内边距14。

    kind: primary(主题渐变)/ghost(透明描边)/danger(低饱和红)；端口单开关等特例
    经 min_w/h 参数纳入（86×30）。
    glyph: C25 起可选 14px 线性图标名（同 _glyph_pm 口径），默认不加。
    """
    b = QPushButton(text, parent)
    b.setFixedHeight(h)
    b.setMinimumWidth(min_w)
    base = ('QPushButton{font-size:13px; padding:0 14px; border-radius:8px;'
            'font-weight:500; text-align:center;}'
            'QPushButton:disabled{color:#5b6b7d; background:rgba(255,255,255,0.04);'
            'border-color:rgba(255,255,255,0.07);}')
    if kind == 'ghost':
        base += ('QPushButton{background:rgba(255,255,255,0.06); color:#e1e1e1;'
                 'border:1px solid rgba(255,255,255,0.14);}'
                 'QPushButton:hover{background:rgba(255,255,255,0.12);'
                 'border-color:rgba(255,255,255,0.32); color:#dce6f0;}'
                 'QPushButton:pressed{background:rgba(56,226,255,0.30);'
                 'border-color:rgba(56,226,255,0.75); color:#ffffff; padding-top:1px;}'
                 'QPushButton:disabled{background:rgba(255,255,255,0.03); color:#5b6b7d;'
                 'border-color:rgba(255,255,255,0.07);}')
    elif kind == 'danger':
        base += ('QPushButton{background:rgba(219,68,55,0.15); color:#ef6a5d;'
                 'border:1px solid rgba(219,68,55,0.30);}'
                 'QPushButton:hover{background:rgba(219,68,55,0.27);}'
                 'QPushButton:pressed{background:rgba(219,68,55,0.42);'
                 'border-color:rgba(219,68,55,0.60); color:#ffd7d2; padding-top:1px;}'
                 'QPushButton:disabled{background:rgba(219,68,55,0.06); color:#8a5a55;'
                 'border-color:rgba(219,68,55,0.12);}')
    else:
        base += ('QPushButton{background:rgba(56,226,255,0.15); color:#a9c3e0;'
                 'border:1px solid rgba(56,226,255,0.30);}'
                 'QPushButton:hover{background:rgba(56,226,255,0.26);'
                 'border-color:rgba(160,185,215,0.55); color:#dce6f0;}'
                 'QPushButton:pressed{background:rgba(56,226,255,0.38);'
                 'border-color:rgba(56,226,255,0.90); color:#ffffff; padding-top:1px;}'
                 'QPushButton:disabled{background:rgba(56,226,255,0.05); color:#5b6b7d;'
                 'border-color:rgba(56,226,255,0.12);}')
    b.setStyleSheet(base)
    if glyph:
        b.setIcon(QIcon(_glyph_pm(glyph, 14, '#a9c3e0' if kind == 'primary' else '#7e97b3')))
        b.setIconSize(QSize(14, 14))
    return b


def dialog_head(title, badge=''):
    """外壳：48 px 页内头（18 px 标题 + 右侧 11 px 徽章），与说明/协议页同规格。

    骨架单源在 ui_kit.page_head（统一实现）；这里只做徽章控件与旧返回契约适配。
    返回 (页头 QWidget, 徽章 QLabel)；badge 为空时徽章隐藏，调用方可后续改文案。
    """
    bd = QLabel(badge)
    bd.setStyleSheet(
        'font-size:11px; color:%s; background:%s; border:1px solid %s;'
        'border-radius:8px; padding:2px 8px;'
        % (ux.TOKENS['text_muted'], ux.TOKENS['surface_soft'], ux.TOKENS['border_soft']))
    bd.setVisible(bool(badge))
    box, _ = uikit.page_head(title, badge=bd)
    return box, bd


def _glyph_pm(kind, size=16, color='#7e97b3'):
    """C20：四页共用线性图标（24 网格绘制后缩放到 size，1.6px 圆头线宽）。"""
    from PySide6.QtGui import QPolygonF
    px = max(8, int(size))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.1, 1.6 * px / 24.0))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    s = px / 24.0

    def _pt(x, y):
        return QPointF(x * s, y * s)

    def _poly(pts, close=False):
        poly = QPolygonF([_pt(x, y) for x, y in pts])
        if close:
            p.drawPolygon(poly)
        else:
            p.drawPolyline(poly)

    if kind == 'vmax':
        _poly([(13, 2), (5, 14), (11, 14), (9, 22), (19, 9), (13, 9), (15, 2)], True)
    elif kind == 'cur':
        _poly([(3, 15), (7, 10), (11, 15), (15, 10), (19, 15)])
    elif kind == 'port':
        p.drawRoundedRect(QRectF(4 * s, 8 * s, 16 * s, 8 * s), 4 * s, 4 * s)
        p.drawLine(_pt(9, 12), _pt(15, 12))
    elif kind == 'load':
        p.drawArc(QRectF(4 * s, 6 * s, 16 * s, 16 * s), 0, 180 * 16)
        p.drawLine(_pt(12, 14), _pt(16, 9))
    elif kind == 'wh':
        p.drawRoundedRect(QRectF(3 * s, 7 * s, 16 * s, 10 * s), 2 * s, 2 * s)
        p.drawLine(_pt(19, 10), _pt(19, 14))
        p.drawLine(_pt(7, 12), _pt(15, 12))
    elif kind == 'sess':
        p.drawRoundedRect(QRectF(4 * s, 5 * s, 16 * s, 4 * s), 2 * s, 2 * s)
        p.drawRoundedRect(QRectF(4 * s, 10 * s, 16 * s, 4 * s), 2 * s, 2 * s)
        p.drawRoundedRect(QRectF(4 * s, 15 * s, 10 * s, 4 * s), 2 * s, 2 * s)
    elif kind == 'avg':
        p.drawLine(_pt(4, 21), _pt(20, 21))
        p.drawLine(_pt(7, 19), _pt(7, 12))
        p.drawLine(_pt(12, 19), _pt(12, 8))
        p.drawLine(_pt(17, 19), _pt(17, 14))
    elif kind == 'peak':
        _poly([(4, 19), (9, 14), (13, 16), (20, 6)])
        p.drawLine(_pt(4, 21), _pt(20, 21))
    elif kind == 'dur':
        p.drawEllipse(QRectF(4 * s, 4 * s, 16 * s, 16 * s))
        p.drawLine(_pt(12, 12), _pt(12, 7))
        p.drawLine(_pt(12, 12), _pt(16, 14))
    elif kind == 'bell':
        p.drawArc(QRectF(6 * s, 6 * s, 12 * s, 12 * s), 0, 180 * 16)
        p.drawLine(_pt(6, 12), _pt(6, 17))
        p.drawLine(_pt(18, 12), _pt(18, 17))
        p.drawLine(_pt(4, 17), _pt(20, 17))
        p.drawLine(_pt(10, 20), _pt(14, 20))
    elif kind == 'shield':
        _poly([(12, 3), (20, 6), (20, 12), (12, 21), (4, 12), (4, 6)], True)
        _poly([(8, 12), (11, 15), (16, 9)])
    elif kind == 'clock':
        p.drawEllipse(QRectF(4 * s, 4 * s, 16 * s, 16 * s))
        p.drawLine(_pt(12, 7), _pt(12, 12))
        p.drawLine(_pt(12, 12), _pt(16, 14))
    # C24：设置/AI/记录页卡片标题与空态的补充线性图标（同一 24 网格口径）
    elif kind == 'ble':
        p.drawLine(_pt(12, 3), _pt(12, 21))
        _poly([(12, 3), (18, 8), (6, 16)])
        _poly([(12, 21), (18, 16), (6, 8)])
    elif kind == 'battery':
        p.drawRoundedRect(QRectF(3 * s, 7 * s, 16 * s, 10 * s), 2 * s, 2 * s)
        p.drawLine(_pt(19, 10), _pt(19, 14))
        p.drawLine(_pt(6, 12), _pt(8, 12))
        p.drawLine(_pt(10, 12), _pt(12, 12))
    elif kind == 'server':
        p.drawRoundedRect(QRectF(4 * s, 5 * s, 16 * s, 5 * s), 1.5 * s, 1.5 * s)
        p.drawRoundedRect(QRectF(4 * s, 14 * s, 16 * s, 5 * s), 1.5 * s, 1.5 * s)
        p.drawPoint(_pt(7, 7.5))
        p.drawPoint(_pt(7, 16.5))
    elif kind == 'sliders':
        p.drawLine(_pt(4, 7), _pt(20, 7))
        p.drawLine(_pt(4, 17), _pt(20, 17))
        p.drawEllipse(QRectF(7 * s, 5 * s, 4 * s, 4 * s))
        p.drawEllipse(QRectF(13 * s, 15 * s, 4 * s, 4 * s))
    elif kind == 'folder':
        _poly([(3, 7), (9, 7), (11, 9), (21, 9), (21, 19), (3, 19)], True)
    elif kind == 'screen':
        p.drawRoundedRect(QRectF(3 * s, 5 * s, 18 * s, 12 * s), 2 * s, 2 * s)
        p.drawLine(_pt(12, 17), _pt(12, 21))
        p.drawLine(_pt(9, 21), _pt(15, 21))
    elif kind == 'gear':
        import math as _m
        p.drawEllipse(QRectF(6 * s, 6 * s, 12 * s, 12 * s))
        p.drawEllipse(QRectF(9.8 * s, 9.8 * s, 4.4 * s, 4.4 * s))
        for _a in range(0, 360, 45):
            _rad = _a * 3.1415926 / 180.0
            p.drawLine(_pt(12 + 6.0 * _m.cos(_rad), 12 + 6.0 * _m.sin(_rad)),
                       _pt(12 + 8.0 * _m.cos(_rad), 12 + 8.0 * _m.sin(_rad)))
    elif kind == 'drop':
        _poly([(12, 3), (17, 10), (16.5, 14), (14, 17), (10, 17), (7.5, 14), (7, 10)], True)
    elif kind == 'scan':
        _poly([(3, 8), (3, 4), (8, 4)])
        _poly([(16, 4), (21, 4), (21, 8)])
        _poly([(21, 16), (21, 20), (16, 20)])
        _poly([(8, 20), (3, 20), (3, 16)])
        p.drawEllipse(QRectF(10.5 * s, 10.5 * s, 3 * s, 3 * s))
    elif kind == 'chat':
        p.drawRoundedRect(QRectF(3 * s, 5 * s, 18 * s, 12 * s), 4 * s, 4 * s)
        _poly([(8, 17), (8, 21), (12, 17)])
    elif kind == 'eye':
        p.drawEllipse(QRectF(3 * s, 8 * s, 18 * s, 9 * s))
        p.drawEllipse(QRectF(10 * s, 10.5 * s, 4 * s, 4 * s))
    elif kind == 'list':
        for _y in (7, 12, 17):
            p.drawPoint(_pt(5, _y))
            p.drawLine(_pt(9, _y), _pt(20, _y))
    elif kind == 'power':
        p.drawArc(QRectF(4 * s, 4 * s, 16 * s, 16 * s), 60 * 16, 300 * 16)
        p.drawLine(_pt(12, 3), _pt(12, 11))
    elif kind == 'off':
        p.drawArc(QRectF(4 * s, 4 * s, 16 * s, 16 * s), 60 * 16, 300 * 16)
        p.drawLine(_pt(12, 3), _pt(12, 11))
        p.drawLine(_pt(5, 4), _pt(19, 20))
    elif kind == 'plug':
        p.drawLine(_pt(8, 3), _pt(8, 9))
        p.drawLine(_pt(16, 3), _pt(16, 9))
        p.drawRoundedRect(QRectF(5 * s, 9 * s, 14 * s, 6 * s), 2 * s, 2 * s)
        p.drawLine(_pt(12, 15), _pt(12, 21))
    # C25：端口卡指标、分页箭头、会话操作按钮的补充画法（同一 24 网格口径）
    elif kind == 'plus':
        p.drawLine(_pt(12, 5), _pt(12, 19))
        p.drawLine(_pt(5, 12), _pt(19, 12))
    elif kind == 'pen':
        _poly([(4, 20), (5.5, 15), (15, 5), (19, 9), (9, 19)], True)
        p.drawLine(_pt(5.5, 15), _pt(9, 19))
    elif kind == 'trash':
        p.drawLine(_pt(4, 7), _pt(20, 7))
        _poly([(9, 7), (9, 4), (15, 4), (15, 7)])
        _poly([(6, 7), (18, 7), (17, 20), (7, 20)], True)
        p.drawLine(_pt(10, 10.5), _pt(10, 17))
        p.drawLine(_pt(14, 10.5), _pt(14, 17))
    elif kind == 'broom':
        p.drawLine(_pt(5, 20), _pt(13, 12))
        _poly([(11.5, 9.5), (16, 5), (20.5, 9.5), (16, 14)], True)
        p.drawLine(_pt(16, 14), _pt(18, 19))
        p.drawLine(_pt(13.5, 13.5), _pt(15, 18.5))
    elif kind == 'arrow_l':
        _poly([(14.5, 5), (7.5, 12), (14.5, 19)])
    elif kind == 'arrow_r':
        _poly([(9.5, 5), (16.5, 12), (9.5, 19)])
    p.end()
    return pm


def _metric_cell(text, glyph):
    """C25：端口卡指标格——14px 线稿 + 原文字标签（返回标签供刷新复用）。"""
    box = QWidget()
    hb = QHBoxLayout(box)
    hb.setContentsMargins(0, 0, 0, 0)
    hb.setSpacing(6)
    ic = QLabel()
    ic.setPixmap(_glyph_pm(glyph, 14, '#5d7891'))
    ic.setFixedSize(14, 14)
    tx = QLabel(text)
    hb.addWidget(ic, 0, Qt.AlignVCenter)
    hb.addWidget(tx, 1)
    return box, tx


def _tab_boost(tw, glyphs):
    """C26：QTabWidget 标签补 14px 线稿；选中深墨、未选中浅蓝，随切换重刷。"""
    tw.setIconSize(QSize(14, 14))

    def _apply(*_a):
        cur = tw.currentIndex()
        for i, g in enumerate(glyphs):
            tw.setTabIcon(i, QIcon(_glyph_pm(g, 14, '#04121f' if i == cur else '#8fa9cc')))

    tw.currentChanged.connect(_apply)
    _apply()
    return _apply


def smooth_xy(xs, ys, sub=2):
    """C9 Catmull-Rom 平滑插值（沿用既有实现）：sub=2，防过冲钳制。

    点数 <3 原样返回；插值点钳制在相邻两点极值之间，避免曲线过冲。
    """
    n = len(ys)
    if n < 3:
        return list(xs), list(ys)
    out_x, out_y = [], []
    for i in range(n - 1):
        p0 = ys[i - 1] if i > 0 else ys[i]
        p1 = ys[i]
        p2 = ys[i + 1]
        p3 = ys[i + 2] if i + 2 < n else ys[i + 1]
        lo, hi = min(p1, p2), max(p1, p2)
        x0, x1 = xs[i], xs[i + 1]
        for j in range(sub):
            t = j / sub
            t2, t3 = t * t, t * t * t
            y = 0.5 * (2 * p1 + (p2 - p0) * t
                       + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                       + (3 * p1 - p0 - 3 * p2 + p3) * t3)
            out_x.append(x0 + (x1 - x0) * t)
            out_y.append(min(max(y, lo), hi))
    out_x.append(xs[-1])
    out_y.append(ys[-1])
    return out_x, out_y

LIGHT_QSS = """
QMainWindow { background: transparent; }
QWidget#root { background: #1c1c1c; }  /* 不透明深色根底（Fusion 跟随系统浅色时不再白窗） */
QLabel { color: %(INK)s; background: transparent; }
QLabel#h1 { font-size: 18px; font-weight: 600; color: #f3f7fc; }
QLabel#sub { color: %(SUB)s; font-size: 12px; }
QLabel#big { font-size: 26px; font-weight: 700; color: %(TEAL_D)s; }
QGroupBox {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(16,28,54,0.60), stop:0.45 rgba(14,26,50,0.68), stop:1 rgba(10,20,40,0.74));
    border: 1px solid rgba(120,190,255,0.18);
    border-top: 1px solid rgba(120,190,255,0.38);
    border-radius: 16px; margin-top: 32px; padding: 16px 12px 12px 12px;
    font-weight: 600; color: %(INK)s;
}
/* C34-UI：卡头 token = 13px/700、无字距。去掉原 1px 字距，与内联卡头
   （14px/700 无字距那批将在批次 3 归并）在字距维度先对齐。 */
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px;
    color: %(INK)s; font-size: 13px; font-weight: 700; }
QGroupBox:hover { border: 1px solid rgba(56,226,255,0.42); }
QPushButton {
    background: rgba(56,226,255,0.08);
    color: %(INK)s; border: 1px solid rgba(120,190,255,0.18); border-radius: 8px;
    padding: 7px 16px; font-weight: 500;
}
QPushButton:hover { background: rgba(56,226,255,0.18); border-color: rgba(56,226,255,0.85); color:#ffffff; }
QPushButton:pressed { background: rgba(56,226,255,0.26); }
QPushButton:disabled { color:#5b6b7d; background:rgba(255,255,255,0.04); border-color:rgba(255,255,255,0.07); }
QPushButton#danger { background: rgba(219,68,55,0.15); color:#ef6a5d;
    border: 1px solid rgba(219,68,55,0.30); }
QPushButton#danger:hover { background: rgba(219,68,55,0.27); }
QPushButton#danger:pressed { background: rgba(219,68,55,0.42); color:#ffd7d2; padding-top:1px; }
QPushButton#ghost { background: rgba(6,14,30,0.42); color: %(INK)s;
    border: 1px solid rgba(120,190,255,0.18); }
QPushButton#ghost:hover { background: rgba(56,226,255,0.12); border-color: rgba(56,226,255,0.55); color:#ffffff; }
QPushButton#ghost:pressed { background: rgba(56,226,255,0.28); border-color: rgba(56,226,255,0.80); color:#ffffff; padding-top:1px; }
QTabWidget::pane { border: 1px solid rgba(120,190,255,0.16); border-radius: 12px;
    background: rgba(10,20,40,0.45); top: -1px; }
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab { background: transparent; color: %(SUB)s; padding: 8px 12px;
    margin-right: 4px; border-radius: 8px; font-size: 13px; font-weight: 500; }
QTabBar::tab:hover { color: #e8f4ff; background: rgba(56,226,255,0.10); }
/* C34-UI：二级 tab 选中态只换底色，不再改字重。原先 500→600 会让标签宽度
   在点击瞬间跳 1~2px（中文 2~3 字最明显），是同页「看起来在抖」的根因。 */
QTabBar::tab:selected { color: #04121f;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #38e2ff, stop:1 #4a8cff); }
QComboBox, QSpinBox, QDoubleSpinBox {
    background: rgba(6,14,30,0.55); color: %(INK)s;
    border: 1px solid rgba(120,190,255,0.18); border-radius: 8px; padding: 5px 8px; }
QComboBox:hover, QSpinBox:hover { border-color: rgba(56,226,255,0.45); }
/* R024（综合审查员 P0-3）：数字/时间控件的上下按钮未主题化，出现浅灰方块并溢出圆角 */
QSpinBox::up-button, QDoubleSpinBox::up-button, QTimeEdit::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button, QTimeEdit::down-button {
    subcontrol-origin: border; width: 16px; background: rgba(255,255,255,0.06);
    border-left: 1px solid rgba(120,190,255,0.20); }
QSpinBox::up-button, QDoubleSpinBox::up-button, QTimeEdit::up-button {
    subcontrol-position: top right; border-top-right-radius: 8px; }
QSpinBox::down-button, QDoubleSpinBox::down-button, QTimeEdit::down-button {
    subcontrol-position: bottom right; border-bottom-right-radius: 8px; }
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover, QTimeEdit::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover, QTimeEdit::down-button:hover {
    background: rgba(56,226,255,0.20); }
/* R09：原三角 border-hack 在 QSS 下被绘成实心小方块（实拍为两个青色块）。
   箭头改由 _CkSpinBox.paintEvent 矢量绘制，这里只把原生箭头关掉、避免叠加。 */
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QTimeEdit::up-arrow,
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QTimeEdit::down-arrow {
    image: none; width: 0; height: 0; }
QComboBox QAbstractItemView { background: #0e1a34; color: %(INK)s;
    selection-background-color: rgba(56,226,255,0.28); border: 1px solid rgba(120,190,255,0.25); }
QPlainTextEdit, QLineEdit {
    background: rgba(6,14,30,0.55); color: %(INK)s;
    border: 1px solid rgba(120,190,255,0.18); border-radius: 8px; padding: 6px;
    selection-background-color: rgba(56,226,255,0.28); }
QTableWidget { background: rgba(8,16,34,0.72); color: %(INK)s;
    border: 1px solid rgba(120,190,255,0.16); border-radius: 12px; gridline-color: rgba(120,190,255,0.10); }
QTableWidget::item { background: transparent; padding: 4px; }
QTableWidget::item:selected { background: rgba(56,226,255,0.28); color:#fff; }
QTableWidget#sessTbl { background: transparent; border: none;
    alternate-background-color: rgba(255,255,255,0.035); }
QTableWidget#sessTbl::item { background: transparent; padding: 2px 6px; }
QTableWidget#sessTbl QHeaderView { background: transparent; border: none; }
QTableWidget#sessTbl QHeaderView::section { background: transparent; color:#cfe6ff;
    border:none; border-bottom:1px solid rgba(255,255,255,0.14); padding:6px; font-weight:600; }
QHeaderView::section { background: rgba(56,226,255,0.10); color: #9fe8ff; border: none;
    padding: 5px; font-weight: 600; }
QFrame#navrail {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 rgba(255,255,255,0.10), stop:0.08 rgba(255,255,255,0.05),
        stop:1 rgba(255,255,255,0.022));
    border: 1px solid rgba(255,255,255,0.12);
    border-top: 1px solid rgba(255,255,255,0.28); border-radius: 16px;
}
QLabel#navbrand { font-size: 16px; font-weight: 700; color: #f3f7fc; background: transparent; }
QLabel#navsub { font-size: 11px; color: %(SUB)s; background: transparent; }
QPushButton#navitem {
    background: transparent; color: %(SUB)s; border: 1px solid transparent;
    border-radius: 8px; text-align: left; padding: 10px 13px;
    font-size: 14px; font-weight: 500;
}
QPushButton#navitem:hover { background: rgba(56,226,255,0.10); color: #e8f4ff; }
/* C34（缺陷 5）：导航项按下态。此前 #navitem 只定义了 hover/checked，id 选择器优先级
   高于通用 QPushButton:pressed，点了没任何视觉反馈；这里补按下态并下方 1px 让位。
   选中项按下时用更亮的渐变，避免「已选中 → 按下无变化」。 */
QPushButton#navitem:pressed { background: rgba(56,226,255,0.30); color:#ffffff; padding-top:11px; }
QPushButton#navitem:checked:pressed {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 rgba(120,240,255,0.98), stop:1 rgba(110,170,255,0.98));
    color:#02101c; padding-top:11px;
}
QPushButton#navitem:checked {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 rgba(56,226,255,0.85), stop:1 rgba(74,140,255,0.85));
    color: #04121f; border: 1px solid rgba(120,220,255,0.75);
}
QMenu { background: #1a2436; color: %(INK)s; border: 1px solid rgba(56,226,255,0.3); border-radius: 8px; }
QMenu::item { padding: 6px 24px; }
QMenu::item:selected { background: #2a4a72; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(56,226,255,0.0); border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: rgba(56,226,255,0.65); }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px; }
QScrollBar::handle:horizontal { background: rgba(56,226,255,0.0); border-radius: 4px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: rgba(120,190,255,0.65); }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QGraphicsView { background: transparent; border: none; }
QGraphicsView QWidget { background: transparent; border: none; }
QDialog { background: #1a2436; }
QTimeEdit { background: rgba(255,255,255,0.075); color: %(INK)s;
    border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; }
QCheckBox { color: %(INK)s; spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid rgba(255,255,255,0.25); background: rgba(255,255,255,0.06); }
QCheckBox::indicator:checked { background: #38e2ff; border-color:#38e2ff; }
/* ===== R023-C 质感精修：焦点态/悬浮反馈/工具提示/消息框 ===== */
QToolTip { background: #1a2436; color: %(INK)s;
    border: 1px solid rgba(56,226,255,0.5); border-radius: 8px;
    padding: 5px 8px; font-size: 12px; }
QMessageBox { background: #1a2436; }
QMessageBox QLabel { color: %(INK)s; font-size: 13px; background: transparent; }
QMessageBox QPushButton { min-width: 88px; min-height: 30px; }
/* R030：系统输入框（新建/重命名会话）同样走暗色主题 */
QInputDialog { background: #1a2436; }
QInputDialog QLabel { color: %(INK)s; font-size: 13px; background: transparent; }
QInputDialog QPushButton { min-width: 88px; min-height: 30px; }
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QLineEdit:focus, QTimeEdit:focus {
    border: 1px solid rgba(56,226,255,0.85); background: rgba(255,255,255,0.10); }
QCheckBox:hover { color: #dce6f0; }
QCheckBox::indicator:hover { border-color: rgba(56,226,255,0.85); }
QScrollBar::handle:vertical:pressed, QScrollBar::handle:horizontal:pressed {
    background: rgba(120,190,255,0.55); }
QComboBox QAbstractItemView { selection-background-color: rgba(56,226,255,0.45); }
""" % {'INK': INK, 'SUB': SUB, 'TEAL': TEAL, 'TEAL_D': TEAL_D}


class Backdrop(QWidget):
    """动态背景：浅色底 + 3 光球漂浮（orbFloat）+ 网格 + 扫描线（scanMove）。"""

    def __init__(self):
        super().__init__()
        self.setObjectName('root')
        self._t = 0.0
        self._static = None
        self._static_size = (0, 0)
        # R007 性能纪律（CK-05）：光球/扫描线已烘焙进静态层，不再 10fps 全窗重绘。
        # 代价：光球停止漂移（视觉差异极小）；收益：整窗空闲零重绘。
        # R020：改为 QImage 缓存（尺寸变化才重建）。

    def _static_bg(self, w, h):
        """静态背景：基底 + 固定辉光 + 网格，只在尺寸变化时重建。"""
        pm = QPixmap(w, h)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(0, 0, w, h, QColor('#1c1c1c'))
        fixed = [
            # R014（领导）：背景对齐网页 body 配色——#1c1c1c 基底 + 三层径向辉光
            # （网页 alpha：rgba(56,226,255,.18) / rgba(124,77,255,.16) / rgba(0,229,255,.10)）
            (0.10, -0.12, 1400, 950, QColor(3, 169, 244, 46)),
            (0.90, -0.10, 1400, 950, QColor(124, 77, 255, 41)),
            (0.50, 1.22, 1150, 1150, QColor(0, 229, 255, 26)),
        ]
        for cxp, cyp, rw, rh, col in fixed:
            cx, cy = w * cxp, h * cyp
            g = QRadialGradient(cx, cy, max(rw, rh))
            g.setColorAt(0.0, col)
            g.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(0, 0, w, h, g)
        # R026（领导）：网格线不再贴边硬切——四周留 16px 边距，线段两端渐变衰减
        step = 46
        edge = 16
        grid_bottom = int(h * 0.7) - 8
        if grid_bottom > edge and w > 2 * edge:
            _c7 = QColor(255, 255, 255, 7)
            _c0 = QColor(255, 255, 255, 0)
            for _x in range(edge, max(edge + 1, w - edge), step):
                lg = QLinearGradient(float(_x), float(edge), float(_x), float(grid_bottom))
                lg.setColorAt(0.0, _c0)
                lg.setColorAt(0.12, _c7)
                lg.setColorAt(0.88, _c7)
                lg.setColorAt(1.0, _c0)
                p.setPen(QPen(QBrush(lg), 1))
                p.drawLine(_x, edge, _x, grid_bottom)
            for _y in range(edge, grid_bottom, step):
                lg = QLinearGradient(float(edge), float(_y), float(w - edge), float(_y))
                lg.setColorAt(0.0, _c0)
                lg.setColorAt(0.12, _c7)
                lg.setColorAt(0.88, _c7)
                lg.setColorAt(1.0, _c0)
                p.setPen(QPen(QBrush(lg), 1))
                p.drawLine(edge, _y, w - edge, _y)
        # 光球烘焙（原 orbFloat 漂移与扫描线已裁撤：全窗每帧重绘代价 > 视觉收益）
        # R015：光球/扫描线改网页同源亮色（原莫兰迪残留使背景发灰）
        for bx, by, br, col in (
                (0.12, 0.10, 0.48, QColor(3, 169, 244, 32)),
                (0.88, 0.40, 0.46, QColor(124, 77, 255, 28)),
                (0.42, 0.88, 0.44, QColor(0, 229, 255, 22))):
            g = QRadialGradient(w * bx, h * by, w * br)
            g.setColorAt(0.0, col)
            g.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(0, 0, w, h, g)
        scan = QRectF(0, h * 0.28, w, 150)
        sg = QLinearGradient(0, scan.top(), 0, scan.bottom())
        sg.setColorAt(0.0, QColor(3, 169, 244, 0))
        sg.setColorAt(0.5, QColor(3, 169, 244, 8))
        sg.setColorAt(1.0, QColor(3, 169, 244, 0))
        p.fillRect(scan, sg)
        p.end()
        self._static = pm

    def paintEvent(self, ev):
        # R020（CK-M2）：背景烘焙为 QImage 缓存，仅尺寸变化重建；paintEvent 只贴图。
        # 修复 QPixmap 路径在本环境无效的问题改用 QImage（已验证可渲染）。
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        if self._static is None or self._static_size != (w, h):
            img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
            img.fill(QColor('#1c1c1c'))
            p = QPainter(img)
            for cxp, cyp, rw, rh, col in (
                    (0.10, -0.12, 1400, 950, QColor(3, 169, 244, 46)),
                    (0.90, -0.10, 1400, 950, QColor(124, 77, 255, 41)),
                    (0.50, 1.22, 1150, 1150, QColor(0, 229, 255, 26))):
                g = QRadialGradient(w * cxp, h * cyp, max(rw, rh))
                g.setColorAt(0.0, col)
                g.setColorAt(1.0, QColor(0, 0, 0, 0))
                p.fillRect(0, 0, w, h, g)
            p.setPen(QPen(QColor(255, 255, 255, 7), 1))
            step = 46
            x = 0.0
            while x <= w:
                p.drawLine(int(x), 0, int(x), int(h * 0.7))
                x += step
            y = 0.0
            while y <= h * 0.7:
                p.drawLine(0, int(y), int(w), int(y))
                y += step
            p.end()
            self._static = img
            self._static_size = (w, h)
        p = QPainter(self)
        p.drawImage(0, 0, self._static)
        p.end()


def _apply_dark_titlebar(win):
    """R007（D-01）：DWM 深色标题栏（20=Win10 1809+/Win11，19=旧版回退）。"""
    try:
        hwnd = int(win.winId())
        for attr in (20, 19):
            val = ctypes.c_int(1)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:411')


def _install_hotkeys(w):
    """R007（D-08）：Ctrl+1..N 切页；F5 立即刷新当前页数据。"""
    try:
        stack = w.findChild(QStackedWidget)
        rail = w.findChild(NavRail)
        if stack is None or rail is None:
            return
        for i in range(stack.count()):
            sc = QShortcut(QKeySequence('Ctrl+%d' % (i + 1)), w)
            idx = i
            sc.activated.connect(lambda idx=idx: (rail.set_selected(idx),
                                                  stack.setCurrentIndex(idx)))
        f5 = QShortcut(QKeySequence('F5'), w)
        f5.activated.connect(lambda: w._refresh_current())
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:429')


def make_brand_footer(app_name, version):
    """品牌声明条（导航底部空白区域）：玻璃渐变卡 + 平台署名 + 版本号。
    参考 DS-Whalegirl guide-footer。"""
    fw = QFrame()
    fw.setObjectName('brandfooter')
    fw.setStyleSheet(
        'QFrame#brandfooter{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,'
        'stop:0 rgba(20,36,68,140),stop:1 rgba(28,20,60,128));'
        'border:1px solid rgba(255,255,255,0.12); border-radius:12px;}')
    fv = QVBoxLayout(fw)
    fv.setContentsMargins(8, 8, 8, 8)
    fv.setSpacing(4)
    l1 = QLabel('Developed by X.H')
    l1.setTextFormat(Qt.RichText)
    l1.setText('<span style="color:#cfe0f0;">Developed by </span>'
               '<span style="color:#5fd0ff; font-weight:700;">X.H</span>')
    l1.setAlignment(Qt.AlignCenter)
    l1.setStyleSheet('font-size:11px; background:transparent;')
    l2 = QLabel('%s v%s · MIT License' % (app_name, version))
    l2.setStyleSheet('color:#8fa3b8; font-size:11px; background:transparent;')
    l2.setAlignment(Qt.AlignCenter)
    l2.setWordWrap(True)
    fv.addWidget(l1)
    fv.addWidget(l2)
    return fw


class _TaskSignal(QObject):
    result = Signal(object)


class _DaemonPool(ThreadPoolExecutor):
    """工作线程为 daemon 的线程池，避免退出后挂住进程。"""

    def _adjust_thread_count(self):
        import concurrent.futures.thread as _cft
        import weakref
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num = len(self._threads)
        if num < self._max_workers:
            name = '%s_%d' % (self._thread_name_prefix or self, num)
            t = threading.Thread(
                name=name, target=_cft._worker,
                args=(weakref.ref(self, weakref_cb),
                      self._create_worker_context(),
                      self._work_queue))
            t.daemon = True
            t.start()
            self._threads.add(t)
            _cft._threads_queues[t] = self._work_queue


WORKER_POOL = _DaemonPool(max_workers=6, thread_name_prefix='ck')


def load_bundled_font():
    """加载打包的 Noto Sans SC（N3），换机不回退；成功返回 True。"""
    candidates = []
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        candidates.append(os.path.join(base, 'fonts', 'NotoSansSC-VF.ttf'))
        candidates.append(os.path.join(os.path.dirname(sys.executable),
                                      'fonts', 'NotoSansSC-VF.ttf'))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, '..', 'assets', 'fonts', 'NotoSansSC-VF.ttf'))
    for c in candidates:
        try:
            if c and os.path.exists(c):
                if QFontDatabase.addApplicationFont(c) >= 0:
                    return True
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:533')
    return False


def ck_icon_path():
    """R47 / C17：品牌图标统一解析（窗口/托盘/AUMID 三处同源）。

    打包态 `_MEIPASS/icons/cuktech.ico` → exe 同级 `icons/` → 源码 `../assets/icons/`。
    源码态不再回退 `SP_ComputerIcon`（C07 记录：托盘/标题栏曾是通用"显示器"图形）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    base = getattr(sys, '_MEIPASS', '') or ''
    if base:
        cands.append(os.path.join(base, 'icons', 'cuktech.ico'))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                              'icons', 'cuktech.ico'))
    cands.append(os.path.join(here, '..', 'assets', 'icons', 'cuktech.ico'))
    for c in cands:
        try:
            if c and os.path.exists(c):
                return c
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:iconpath')
    return cands[0]


def ck_ensure_start_menu_shortcut():
    """R48 / C18：确保 Start Menu 快捷方式绑定同一 AUMID。

    Windows 桌面应用 Toast 的署名/投递依赖带 PKEY_AppUserModel_ID 的快捷方式；
    快捷方式被删后署名会退化为 exe 元数据或旧缓存名。打包态每次启动重建。
    CK_NO_SHORTCUT=1 仅用于“删快捷方式”退化对照实验。
    """
    if os.environ.get('CK_NO_SHORTCUT'):
        print('[shortcut] skipped (CK_NO_SHORTCUT=1)', flush=True)
        return False
    if not getattr(sys, 'frozen', False):
        return False
    try:
        import win32com.client
        from win32com.propsys import propsys, pscon
        exe = sys.executable
        start = os.path.join(os.environ.get('APPDATA', ''),
                             r'Microsoft\Windows\Start Menu\Programs')
        if not os.path.isdir(start):
            os.makedirs(start, exist_ok=True)
        lnk_path = os.path.join(start, PANEL_TITLE + '.lnk')
        sh = win32com.client.Dispatch('WScript.Shell')
        # 仅在快捷方式缺失、或原目标已失效时才改写目标，避免开发实例抢占
        # 已指向安装版的快捷方式；AUMID 每次都绑定，保证 Toast 署名。
        _keep_target = False
        try:
            _cur = sh.CreateShortcut(lnk_path)
            _tgt = str(_cur.TargetPath or '')
            _keep_target = bool(_tgt) and os.path.exists(_tgt)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:shortcut-probe')
        lnk = sh.CreateShortcut(lnk_path)
        if not _keep_target:
            lnk.TargetPath = exe
            lnk.WorkingDirectory = os.path.dirname(exe)
            lnk.IconLocation = exe + ',0'
        lnk.Description = PANEL_TITLE
        lnk.Save()
        store = propsys.SHGetPropertyStoreFromParsingName(
            lnk_path, None, 2, propsys.IID_IPropertyStore)  # GPS_READWRITE
        store.SetValue(pscon.PKEY_AppUserModel_ID,
                       propsys.PROPVARIANTType(CK_AUMID))
        store.Commit()
        got = store.GetValue(pscon.PKEY_AppUserModel_ID)
        try:
            got = got.GetValue()
        except Exception:
            pass
        line = '[shortcut] path=%s aumid=%s readback=%s' % (
            lnk_path, CK_AUMID, got)
        print(line, flush=True)
        _ck_toast_log(line)
        return True
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:shortcut')
        return False


# R47 / C17：角标统一绘制（托盘 32px 画布与 Overlay 共用；圆角 8；字级取 token 16/11）
CK_BADGE_RADIUS = 8
CK_BADGE_FONTS = ['Noto Sans SC', 'Microsoft YaHei UI', 'Segoe UI']


def ck_badge_pixmap(n, size=32, base=None):
    """未读数画进 size 画布右上角，返回新 QPixmap（不改 base）。

    形态：圆角方块（半径 8），与托盘/菜单同族，取代 Overlay 原正圆。
    字级：1–2 位 16pt（token），99+ 11pt（token）——pt16 的 '99+' 实测占宽 37px
    超出 30px 方块会被裁掉 '+'，故三位数降一档保证逐字可辨（见 round_047 量测）。
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    if base is not None and not base.isNull():
        # 高 DPI 屏下 QIcon.pixmap() 返回带 DPR 的位图，直接 drawPixmap 只占 1/DPR 画布，
        # 这里先按像素尺寸缩放并把 DPR 归一，保证底图铺满画布
        _b = base.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        _b.setDevicePixelRatio(1.0)
        p0 = QPainter(pm)
        p0.drawPixmap(0, 0, _b)
        p0.end()
    if n <= 0:
        return pm
    txt = '99+' if n > 99 else str(n)
    d = 30 if size >= 32 else max(8, int(round(size * 30 / 32.0)))
    x, y = size - d - 1, 1
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor('#e5484d'))
    p.drawRoundedRect(x, y, d, d, CK_BADGE_RADIUS, CK_BADGE_RADIUS)
    f = QFont()
    f.setFamilies(list(CK_BADGE_FONTS))
    f.setPointSize(11 if len(txt) > 2 else 16)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor('#ffffff'))
    p.drawText(QRect(x, y, d, d), Qt.AlignCenter, txt)
    p.end()
    return pm


class GlassCard(QGroupBox):
    """玻璃卡片：初始无图形特效（避免常驻 FBO 与切页 FBO 泄漏），
    入场淡入结束后再挂回阴影。"""

    def __init__(self, title):
        super().__init__(title)
        self._accent = QColor(47, 182, 255)
        self._glyph_kind = None
        self._glyph_cache = None

    def set_glyph(self, kind, size=16, color='#7e97b3'):
        """C20：卡标题右侧 16×16 线稿（不占布局高度，零位移）。"""
        self._glyph_kind = kind
        self._glyph_cache = _glyph_pm(kind, size, color)
        self.update()

    def paintEvent(self, ev):
        # R15-A06：标题左侧 3×14 强调竖条（网页 card title-bar 口径）；无标题卡（hero）不画
        # R024-A5：竖条升级为 Whaleglass 青→紫极光渐变
        super().paintEvent(ev)
        if not self.title():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        grad = QLinearGradient(10, 8, 13, 22)
        grad.setColorAt(0, QColor(TEAL))
        grad.setColorAt(1, QColor('#9b6bff'))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(QRectF(10, 8, 3, 14), 1.5, 1.5)
        if self._glyph_kind and self._glyph_cache is not None:
            p.drawPixmap(self.width() - self._glyph_cache.width() - 12, 8,
                         self._glyph_cache)
        p.end()


class KpiCell(QFrame):
    """C20：状态页 KPI 格；右上角 14×14 线性图标绝对定位，不参与布局。"""

    def __init__(self, glyph, parent=None):
        super().__init__(parent)
        self.setObjectName('kpicell')
        self._icon = QLabel(self)
        self._icon.setObjectName('kpiIcon')
        self._icon.setFixedSize(14, 14)
        self._icon.setStyleSheet('background:transparent;')
        self._icon.setPixmap(_glyph_pm(glyph, 14, '#7e97b3'))
        self._icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._icon.move(max(6, self.width() - 20), 8)


class SegmentedSelector(QWidget):
    """分段选择器（QButtonGroup 互斥），用于场景模式/息屏等，原生无 WebView。

    options: [(value, label), ...]；选中态渐变填充，未选中玻璃描边。
    chosen 信号仅在用户点击时发出（set_value 回显不发）。
    """
    chosen = Signal(int)

    def __init__(self, options, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QButtonGroup
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = {}
        n = len(options)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        for i, (val, label) in enumerate(options):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(34)
            b.setStyleSheet(self._seg_qss(i == 0, i == n - 1))
            b.clicked.connect(lambda _c=False, v=val: self.chosen.emit(v))
            self._group.addButton(b, val)
            self._buttons[val] = b
            h.addWidget(b, 1)

    @staticmethod
    def _seg_qss(left, right):
        # R046 / C15：圆角 10 → 12，与全局 {4,8,12,16} 圆角节奏对齐
        rl = 12 if left else 0
        rr = 12 if right else 0
        return (
            'QPushButton{background:rgba(255,255,255,0.05); color:#cfe1f5;'
            'border:1px solid rgba(255,255,255,0.14);'
            'border-top-left-radius:%dpx; border-bottom-left-radius:%dpx;'
            'border-top-right-radius:%dpx; border-bottom-right-radius:%dpx;'
            'font-weight:600; padding:6px 8px;}'
            'QPushButton:hover{background:rgba(47,182,255,0.18); color:#eaf4ff;}'
            'QPushButton:pressed{background:rgba(47,182,255,0.36); color:#ffffff;}'
            'QPushButton:checked{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,'
            'stop:0 #38e2ff, stop:1 #9b6bff); color:#ffffff;'
            'border:1px solid rgba(47,182,255,0.6);}'
        ) % (rl, rl, rr, rr)

    def set_value(self, val):
        if val in self._buttons:
            self._buttons[val].setChecked(True)
        else:
            self._group.setExclusive(False)
            for b in self._buttons.values():
                b.setChecked(False)
            self._group.setExclusive(True)

    def value(self):
        return self._group.checkedId()


class _OpaquePanel(QWidget):
    """R22：透明子控件（pyqtgraph 视口）的底图承接层。

    抓一次卡片底色后自绘不透明底，子控件（曲线/坐标轴）重绘只重画本面板矩形，
    不再沿 GlassCard→StarField→Backdrop→QMainWindow 整条祖先链陪站。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bg = None
        self._bg_lock = False
        self._cap_pending = False
        self._paint_extra = None   # R23：底图之上、子控件之下的自绘钩子
        self._cap_hide = False     # 抓底期间钩子也必须停，否则字会被烤进底图形成残影

    def showEvent(self, ev):
        super().showEvent(ev)
        self._schedule_capture()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._bg is not None:
            self._bg = None
            self._schedule_capture()

    def _schedule_capture(self):
        if self._bg is not None or self._cap_pending:
            return
        self._cap_pending = True

        def _once():
            self._cap_pending = False
            if self._bg is None:
                self._capture_bg()
        QTimer.singleShot(300, _once)

    def _capture_bg(self, _try=0):
        if self._bg_lock or not self.isVisible() or self.width() < 4:
            return
        if not _chain_settled(self):
            if _try < 15:
                QTimer.singleShot(220, lambda: self._capture_bg(_try + 1))
            return
        self._bg_lock = True
        _kids = []
        self._cap_hide = True
        try:
            from PySide6.QtCore import QPoint, QRect
            # 抓底时把子控件临时藏起来，否则曲线/读数会被烤进底图形成残影
            _kids = [k for k in self.findChildren(QWidget) if k.isVisible()]
            for k in _kids:
                k.setVisible(False)
            self.setAttribute(Qt.WA_OpaquePaintEvent, False)
            self.repaint()
            _tl = self.window()
            self._bg = _tl.grab(QRect(self.mapTo(_tl, QPoint(0, 0)), self.size()))
        except Exception as _exc:
            self._bg = None
            ck_swallow(_exc, 'launcher:opaque_panel')
        finally:
            for k in _kids:
                try:
                    k.setVisible(True)
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:opaque_panel_kid')
            self._cap_hide = False
            self._bg_lock = False
        if self._bg is None:
            self._schedule_capture()
            return
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        if self._bg is not None:
            p.drawPixmap(0, 0, self._bg)
        # R23：辉光/数字由面板自绘（与底图同一绘制流程），子控件不再自己画这一块
        # 抓底期间（_cap_hide）必须跳过，否则自绘文字会被烤进底图 → 之后钩子再画一次形成重影
        if callable(self._paint_extra) and not self._cap_hide:
            try:
                self._paint_extra(p)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:panel_extra')


class PowerDistBar(QWidget):
    """功率占比堆叠条：各端口实时功率占比，端口色填充（对齐网页 powerDist）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vals = []
        self.setMinimumHeight(10)
        # R22：抓底自绘——每秒一次 set_values 只重画自身 10px 条，不带动整卡/祖先链
        self._bg = None
        self._bg_lock = False
        self._cap_pending = False

    def showEvent(self, ev):
        super().showEvent(ev)
        if self._bg is not None or self._cap_pending:
            return
        self._cap_pending = True

        def _once():
            self._cap_pending = False
            if self._bg is None:
                self._capture_bg()
        QTimer.singleShot(120, _once)

    def _capture_bg(self, _try=0):
        if self._bg_lock or not self.isVisible():
            return
        if not _chain_settled(self):
            if _try < 15:
                QTimer.singleShot(220, lambda: self._capture_bg(_try + 1))
            return
        self._bg_lock = True
        try:
            from PySide6.QtCore import QPoint, QRect
            self.setAttribute(Qt.WA_OpaquePaintEvent, False)
            self.repaint()
            tl = self.window()
            self._bg = tl.grab(QRect(self.mapTo(tl, QPoint(0, 0)), self.size()))
        except Exception as _exc:
            self._bg = None
            ck_swallow(_exc, 'launcher:powerdist')
        finally:
            self._bg_lock = False
        if self._bg is None:
            return
        # R030：隐藏页首次抓底可能抓到未绘制的白底，会留下白方块。
        # 采样三点，接近纯白即丢弃并限次重试（页面真正绘出后再自绘）。
        try:
            _im = self._bg.toImage()
            _w = max(1, self.width() - 3)
            _h = max(1, self.height() - 3)
            _white = 0
            for _x, _y in ((2, 2), (_w // 2, _h // 2), (_w, _h)):
                _c = _im.pixelColor(_x, _y)
                if _c.red() > 235 and _c.green() > 235 and _c.blue() > 235 and _c.alpha() > 200:
                    _white += 1
            if _white >= 2:
                self._bg = None
                self._retry = getattr(self, '_retry', 0) + 1
                if self._retry <= 6 and self.isVisible():
                    QTimer.singleShot(300, self._capture_bg)
                return
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:bgcapture')
        self._retry = 0
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.update()

    def set_values(self, vals):
        # C22：值未变不重画（空闲时占比条静止，轮询仍会重复下发同一组值）
        _v = list(vals)
        if _v == self._vals:
            return
        self._vals = _v
        self.update()

    def paintEvent(self, ev):
        if self._bg is None:
            return
        p = QPainter(self)
        p.drawPixmap(0, 0, self._bg)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 22))
        p.drawRoundedRect(r, 5, 5)
        total = sum(v for _, v in self._vals)
        if total <= 0:
            return
        x = r.left()
        w = r.width()
        nseg = len(self._vals)
        for i, (col, v) in enumerate(self._vals):
            seg = int(w * v / total)
            if i == nseg - 1:
                seg = r.right() + 1 - x
            if seg <= 0:
                continue
            p.setBrush(QColor(col))
            p.drawRect(x, r.top(), seg, r.height())
            x += seg


class NavRail(QFrame):
    """左侧导航栏（参考荣耀电脑管家）：品牌头 + 导航项（图标+文字），
    选中项为高亮圆角卡片。"""

    def __init__(self, title, subtitle, items, on_select, tips=None,
                 bottom_items=None, bottom_tips=None):
        super().__init__()
        self.setObjectName('navrail')
        self._expanded_w = 208
        self._collapsed_w = 64
        self._collapsed = False
        self._tips = list(tips or ())
        self.setFixedWidth(self._expanded_w)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 16, 12, 12)
        v.setSpacing(8)
        brand = QHBoxLayout()
        self._brand_dot = BreathDot(TEAL, size=10, period=DUR_SLOW)  # R15-C23：动画 token 统一
        brand.addWidget(self._brand_dot)
        self._brand_lbl = QLabel(title)
        self._brand_lbl.setObjectName('navbrand')
        brand.addWidget(self._brand_lbl)
        v.addLayout(brand)
        self._sub_lbl = QLabel(subtitle)
        self._sub_lbl.setObjectName('navsub')
        self._sub_lbl.setWordWrap(True)
        v.addWidget(self._sub_lbl)
        v.addSpacing(8)
        self.buttons = []
        self._glyphs = []
        self._pages = []
        self._all_tips = []
        self._sep = None

        def _add_item(glyph, name, page_idx, tip, put):
            """items 支持 (图标, 名称) 或 (图标, 名称, 页号)：后者用于「按钮顺序
            与 QStackedWidget 页号不一致」的场景（如 AI 入口沉底后设置仍是第 6 页）。"""
            b = QPushButton('%s   %s' % (glyph, name))
            b.setObjectName('navitem')
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(lambda _checked=False, p=page_idx: on_select(p))
            put(b)
            self.buttons.append(b)
            self._glyphs.append((glyph, name))
            self._pages.append(page_idx)
            self._all_tips.append(tip or '')

        for i, it in enumerate(items):
            _add_item(it[0], it[1], int(it[2]) if len(it) > 2 else i,
                      self._tips[i] if i < len(self._tips) else '', v.addWidget)
        v.addStretch(1)
        # 底部辅助入口（与一级导航分隔）：AI 这类"随时可开"的入口不占主导航中段
        if bottom_items:
            self._sep = QFrame()
            self._sep.setFixedHeight(1)
            self._sep.setStyleSheet('background:rgba(255,255,255,0.10); border:none;')
            v.addWidget(self._sep)
            _bt = list(bottom_tips or ())
            for j, it in enumerate(bottom_items):
                _add_item(it[0], it[1], int(it[2]) if len(it) > 2 else len(items) + j,
                          _bt[j] if j < len(_bt) else '', v.addWidget)
        self._bottom_w = None
        self.set_selected(0)

    def set_bottom_widget(self, w):
        """在导航栏底部空白区域放置控件（如设备状态图）。"""
        if self._bottom_w is not None:
            self.layout().removeWidget(self._bottom_w)
            self._bottom_w.setParent(None)
        self._bottom_w = w
        if w is not None:
            self.layout().addWidget(w)

    def set_collapsed(self, collapsed):
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        if collapsed:
            self.setFixedWidth(self._collapsed_w)
            self._brand_lbl.hide()
            self._sub_lbl.hide()
            self.layout().setContentsMargins(8, 16, 8, 12)
            for i, b in enumerate(self.buttons):
                b.setText(self._glyphs[i][0])
                _tp = self._all_tips[i]
                b.setToolTip('%s · %s' % (self._glyphs[i][1], _tp) if _tp else self._glyphs[i][1])
            if self._sep is not None:
                self._sep.hide()
            if self._bottom_w is not None:
                self._bottom_w.hide()
        else:
            self.setFixedWidth(self._expanded_w)
            self._brand_lbl.show()
            self._sub_lbl.show()
            self.layout().setContentsMargins(12, 16, 12, 12)
            for i, b in enumerate(self.buttons):
                b.setText('%s   %s' % self._glyphs[i])
                b.setToolTip(self._all_tips[i])
            if self._sep is not None:
                self._sep.show()
            if self._bottom_w is not None:
                self._bottom_w.show()

    def set_selected(self, idx):
        for b, p in zip(self.buttons, self._pages):
            b.setChecked(p == idx)


def _ck_msg(parent, title, text, warn=False):
    """C16：统一信息/告警弹窗——无系统大图标、中文单按钮、宽度 96。"""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.NoIcon)
    box.setText(text)
    box.setStandardButtons(QMessageBox.NoButton)
    _b = box.addButton('确定', QMessageBox.AcceptRole)
    _b.setFixedWidth(96)
    box.exec()
    return box.clickedButton() is _b


def new_plot(pg):
    """创建透明背景的 PlotWidget：QGraphicsView 视口默认 autoFill 白色，
    必须显式关闭，否则暗色卡片上会出现一块白底。"""
    from PySide6.QtGui import QColor as _QC
    pl = pg.PlotWidget()
    pl.setBackground(None)
    pl.setAutoFillBackground(False)
    pl.setBackgroundRole(pl.palette().ColorRole.NoRole)
    vp = pl.viewport()
    vp.setAutoFillBackground(False)
    # 视口默认 backgroundRole=Window，QWidget 渲染时会按 palette Window 色
    # （#efefef）填充；改为 NoRole 并把 palette 改透明。
    vp.setBackgroundRole(vp.palette().ColorRole.NoRole)
    pal = vp.palette()
    pal.setColor(pal.ColorRole.Window, _QC(0, 0, 0, 0))
    pal.setColor(pal.ColorRole.Base, _QC(0, 0, 0, 0))
    vp.setPalette(pal)
    return pl


# ============================================================
# 动画基础设施（对齐网页版 keyframes：cardIn / pulse / toastIn /
# numFlash / barFlow / modalIn）。动画对象挂在控件自身上防止 GC。
# 关键：动画结束后必须移除透明度特效（setGraphicsEffect(None)），
# 否则 PrintWindow / DWM 会把多个页面的 FBO 残留合成到一起。
def fade_in(widget, duration=420, delay=0, start_opacity=0.0,
            restore_shadow=False):
    """控件淡入（对应网页 cardIn）。结束后移除透明度特效。
    R008（N6）：restore_shadow 已失效（不再挂常驻阴影，防整卡 FBO 重模糊），仅保留参数兼容。"""
    old = getattr(widget, '_fade_anim', None)
    if old is not None:
        try:
            old.stop()
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:774')
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(start_opacity)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b'opacity', widget)
    anim.setStartValue(start_opacity)
    anim.setEndValue(1.0)
    anim.setDuration(duration)
    anim.setEasingCurve(QEasingCurve.OutCubic)

    def cleanup():
        if widget.graphicsEffect() is eff:
            widget.setGraphicsEffect(None)
        # R007 动效纪律（CK-05）：不再为卡片挂常驻 QGraphicsDropShadowEffect——
        # 卡片内任一子控件重绘都会令整卡重进 FBO 重跑模糊，是悬浮动画卡顿的主因。

    anim.finished.connect(cleanup)
    if delay:
        QTimer.singleShot(delay, anim.start)
    else:
        anim.start()
    widget._fade_anim = anim
    widget._fade_eff = eff
    return anim


def pop_in(widget, duration=DUR_MED, delay=0):
    """弹窗弹入（对应网页 modalIn：opacity + 位移）。结束后移除透明度特效。"""
    old = getattr(widget, '_pop_anim', None)
    if old is not None:
        try:
            old.stop()
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:807')
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(0.0)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b'opacity', widget)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setDuration(duration)
    anim.setEasingCurve(QEasingCurve.OutBack)

    def cleanup():
        if widget.graphicsEffect() is eff:
            widget.setGraphicsEffect(None)

    anim.finished.connect(cleanup)
    if delay:
        QTimer.singleShot(delay, anim.start)
    else:
        anim.start()
    widget._pop_anim = anim
    widget._pop_eff = eff
    return anim


# R24：帧率档位（CK_BEAT_MS=150 → 6.67fps；200→5；250→4；500→2；0→停帧）。
# 只影响装饰性连续动画（统一节拍/流光/旋转/产品图浮动），不碰采集与轮询。
_CK_BEAT_MS = int(os.environ.get('CK_BEAT_MS') or 150)
_CK_BEAT_STOPPED = _CK_BEAT_MS <= 0   # 停帧档：装饰动画定时器一律不启动
# R26：可见但不聚焦档——装饰降到 0.67 fps、曲线暂停提交（恢复聚焦立刻补一帧）
_CK_DEG = {'on': False}
# 测量钩子（与 CK_BEAT_MS 同类）：强制降级档，供"副屏可见不聚焦"口径复现（真实触发仍是 isActiveWindow）
_CK_FORCE_DEG = os.environ.get('CK_FORCE_DEG') == '1'
# R26-D：D 案（flash/PSB 挂 UiBeat）成对 A/B 开关；默认开，CK_D_OFF=1 仅用于对照旧动画路径
_CK_D_OFF = {'on': os.environ.get('CK_D_OFF') == '1'}
# R24 单因子对照：CK_ANIM_OFF=uibeat,flow,spin,anim 可单独关掉某一项（其余保持生产值）
_CK_ANIM_OFF = set(x.strip() for x in (os.environ.get('CK_ANIM_OFF') or '').split(',')
                   if x.strip())
# R27：地板阶梯旋钮。CK_DECOR_FPS=0/0.5/1/2 直接改装饰档（0=停帧，等价 CK_BEAT_MS=0）；
#       CK_POLL_MS>0 固定轮询间隔并关闭退避（阶梯用）；CK_POLL_BACKOFF=0 关退避（A/B 用）；
#       CK_NUM_FPS 控制降级态数字上屏节流（默认 2 fps）。
_CK_DECOR_FPS = float(os.environ.get('CK_DECOR_FPS') or 0)
if _CK_DECOR_FPS > 0:
    _CK_BEAT_MS = int(round(1000.0 / _CK_DECOR_FPS))
    _CK_BEAT_STOPPED = False
elif os.environ.get('CK_DECOR_FPS') == '0':
    _CK_BEAT_MS = 0
    _CK_BEAT_STOPPED = True
_CK_POLL_MS = int(os.environ.get('CK_POLL_MS') or 0)
_CK_POLL_BACKOFF = os.environ.get('CK_POLL_BACKOFF', '1') != '0'
_CK_NUM_FPS = float(os.environ.get('CK_NUM_FPS') or 2.0)
# R27：降级态数字上屏节流队列（文本先挂起，由 UiBeat 按 CK_NUM_FPS 统一提交）
_CK_NUMQ = {'pending': {}, 'armed': False, 'last': 0.0, 'min_iv': (1.0 / _CK_NUM_FPS),
            'calls': 0, 'applied': 0, 'defer_all': False}
_CK_SUS_ON = {'on': False}
# C34b：#5 取证用——钉住可见性判定，让 _vis_tick 不再把窗口按 occluded 重新挂起，
# 以便在锁屏/无前台环境下也能观测"恢复后数字是否跟随 plan"。
_CK_SUS_PIN = {'on': os.environ.get('CK_SUS_PIN') == '1'}
# C34d（#11）：无 SOC 时的「已充满」判定状态；时间戳统一 time.monotonic()
_CK_FULL = {'low_since': None, 'high_ts': None, 'is_full': False}
_CK_FULL_HI_W = 5.0      # 充电期高功率门槛（W）
_CK_FULL_LO_W = 0.5      # 低功率门槛（W）
_CK_FULL_HOLD_S = 120.0  # 低功率需持续时长（s）
_CK_FULL_WIN_S = 600.0   # 最近一次高功率的有效窗口（s）


def _ck_full_track(total, now, st=None, soc=None, full_flag=None):
    """C34d（#11）：真实「已充满」判定（无 SOC 时走时间启发式）。

    优先级：设备 full 标志 > SOC ≥ 99% > 启发式。
    启发式：出现过 >5 W 输出后，功率 ≤0.5 W 连续 ≥120 s，且距最近高功率 ≤10 min。
    0.5~5 W 的中间功率会打断低功率累计（抖动不误翻），不改变既有 is_full。
    注：`soc`/`full_flag` 目前无数据源（plan 里没有该字段），仅留协议接口；
    真机接入前一律传 None，不假装能读。
    """
    st = _CK_FULL if st is None else st
    try:
        t = float(total or 0)
    except Exception:
        t = 0.0
    if full_flag is not None:
        st['is_full'] = bool(full_flag)
        return st
    try:
        s = None if soc is None else float(soc)
    except Exception:
        s = None
    if s is not None:
        st['is_full'] = s >= 99.0
        return st
    if t > _CK_FULL_HI_W:
        st['high_ts'] = now
        st['low_since'] = None
        st['is_full'] = False
    elif t <= _CK_FULL_LO_W:
        if st.get('low_since') is None:
            st['low_since'] = now
        _hi = st.get('high_ts')
        st['is_full'] = bool(
            _hi is not None and (now - _hi) <= _CK_FULL_WIN_S
            and (now - st['low_since']) >= _CK_FULL_HOLD_S)
    else:
        st['low_since'] = None
    return st


def _ck_iv(base_ms):
    """动画周期 = max(基准周期, 档位周期)；停帧档周期无意义（因为根本不启动）。"""
    if _CK_BEAT_MS <= 0:
        return int(base_ms)
    return max(int(base_ms), _CK_BEAT_MS)


def _ck_anim_on(name):
    """QVariantAnimation 类动画是否允许运行（同样受停帧档与 CK_ANIM_OFF 约束）。"""
    return (not _CK_BEAT_STOPPED) and (name not in _CK_ANIM_OFF)


def _ck_start(tmr, name=''):
    """按档位启动动画定时器：停帧档 / CK_ANIM_OFF 命中 → **不启动**（90 s 回调 0 次）。

    R24：停帧必须是真停（stop），不能用超长周期——超长周期的定时器照样进事件循环。
    """
    if _CK_BEAT_STOPPED or (name and name in _CK_ANIM_OFF):
        return
    if not tmr.isActive():
        tmr.start()


def _ck_num_flush(force=False):
    """R27：把降级态挂起的数字文本一次性上屏（同拍合并 → 一拍至多一次 update）。"""
    q = _CK_NUMQ['pending']
    if not q:
        # C34b（#5）：队列排空即解除 armed 闩锁。此前 armed 一旦置 True 就再不回落，
        # 节拍器一旦停过（suspend 的 _pause_anims、或订阅者清空导致的 stop），
        # 后续入队不会再 wake()，数字就永久停在旧值——即"徽章变、KPI 冻"的根因之一。
        _CK_NUMQ['armed'] = False
        return 0
    _now = time.monotonic()
    if not force and (_now - _CK_NUMQ['last']) < _CK_NUMQ['min_iv']:
        return 0
    _CK_NUMQ['last'] = _now
    _n = 0
    for lb, s in list(q.items()):
        q.pop(lb, None)
        try:
            lb._show_now(s)
            _n += 1
        except RuntimeError:
            pass
    _CK_NUMQ['applied'] += _n
    if not q:                      # C34b：本拍排空后立刻放开闩锁，下次入队可重新 wake
        _CK_NUMQ['armed'] = False
    return _n


class UiBeat(QObject):
    """R18 统一节拍器：装饰性动画共用 120ms 定时器，一次 tick 批量提交 update()。

    多个独立节拍（LIVE 120ms / 呼吸点 140ms）原本各自唤醒事件循环并在不同时刻
    失效，父级玻璃卡与星空背景因此被拆成多次全窗重绘；合并后同一 tick 内的失效
    被 Qt 合并为一次绘制。
    """
    _inst = None

    @classmethod
    def instance(cls):
        if cls._inst is None:
            cls._inst = UiBeat(QApplication.instance())
        return cls._inst

    def __init__(self, parent=None):
        super().__init__(parent)
        self._subs = []
        self._n = 0          # R26-D②：全局节拍相位（呼吸点/标签同相提交，同拍只投递一次 update）
        self._tmr = QTimer(self)
        self._tmr.setInterval(_ck_iv(150))
        self._tmr.timeout.connect(self._tick)

    def add(self, fn):
        if fn not in self._subs:
            self._subs.append(fn)
        if not _CK_BEAT_STOPPED and 'uibeat' not in _CK_ANIM_OFF and not self._tmr.isActive():
            self._tmr.start()

    def wake(self):
        """R27：只保证节拍器在跑（数字节流用，不注册订阅者，避免重复调用）。"""
        if not _CK_BEAT_STOPPED and 'uibeat' not in _CK_ANIM_OFF and not self._tmr.isActive():
            self._tmr.start()

    def remove(self, fn):
        try:
            self._subs.remove(fn)
        except ValueError as _exc:
            ck_swallow(_exc, 'launcher:863')
        if not self._subs:
            self._tmr.stop()

    def _tick(self):
        self._n += 1
        try:
            _ck_num_flush()      # R27：降级态数字统一在本拍上屏
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:beat-num')
        for fn in tuple(self._subs):
            try:
                fn()
            except RuntimeError:
                self.remove(fn)


class BreathDot(QWidget):
    """呼吸状态点（对应网页 pulse：opacity 1↔0.4，可配周期）。"""

    def __init__(self, color=TEAL, size=10, period=1600, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._size = size
        self._period = period
        self.setFixedSize(size + 6, size + 6)
        self._t = 0.0
        # R18：并入统一节拍器（原 140ms 独立定时器）；步长按 120/140 折算保持呼吸周期
        self._beat = UiBeat.instance()
        self._beat_ok = False
        # R19：抓一次"本控件底下的窗口内容"做自绘背景，声明不透明 → 呼吸帧不再
        #       让 Backdrop/QMainWindow/NavRail 陪着重绘（实测 8 次/s 整窗重绘的来源）
        self._bg = None
        self._bg_lock = False
        if self.isVisible():
            self._start()

    def _start(self):
        if not self._beat_ok:
            self._beat.add(self._tick)
            self._beat_ok = True

    def _stop(self):
        if self._beat_ok:
            self._beat.remove(self._tick)
            self._beat_ok = False

    def showEvent(self, ev):
        super().showEvent(ev)
        self._start()
        QTimer.singleShot(40, self._capture_bg)

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._stop()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._bg = None
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        QTimer.singleShot(40, self._capture_bg)

    def _capture_bg(self):
        """抓底（只在窗口已经画过一轮后调用）：用于自绘背景，避免每帧连带父级重绘。"""
        if self._bg_lock or not self.isVisible():
            return
        self._bg_lock = True
        try:
            from PySide6.QtCore import QPoint, QRect
            tl = self.window()
            pos = self.mapTo(tl, QPoint(0, 0))
            self._bg = tl.grab(QRect(pos, self.size()))
        except Exception:
            self._bg = None
        finally:
            self._bg_lock = False
        if self._bg is None:
            return
        # R030：隐藏页首次抓底可能抓到未绘制的白底，会留下白方块。
        # 采样三点，接近纯白即丢弃并限次重试（页面真正绘出后再自绘）。
        try:
            _im = self._bg.toImage()
            _w = max(1, self.width() - 3)
            _h = max(1, self.height() - 3)
            _white = 0
            for _x, _y in ((2, 2), (_w // 2, _h // 2), (_w, _h)):
                _c = _im.pixelColor(_x, _y)
                if _c.red() > 235 and _c.green() > 235 and _c.blue() > 235 and _c.alpha() > 200:
                    _white += 1
            if _white >= 2:
                self._bg = None
                self._retry = getattr(self, '_retry', 0) + 1
                if self._retry <= 6 and self.isVisible():
                    QTimer.singleShot(300, self._capture_bg)
                return
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:bgcapture')
        self._retry = 0
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.update()

    def set_color(self, hexcol):
        self._color = QColor(hexcol)
        self.update()

    def _tick(self):
        # R22：统一节拍 120→150ms（装饰动画 8.3→6.7 次/s），步长按 150/140 折算保持呼吸周期
        self._t += 0.1 * (150.0 / 140.0)
        # R25：呼吸点是纯装饰，每 5 拍重绘（6.7→1.33 次/s），步长仍每拍推进（周期不变）
        # R26：可见不聚焦档每 6 拍（UiBeat 250ms）→ 0.67 fps（2 fps / 1 fps 实测仍 2.7% / 1.58%）
        # R26-D②：改用全局节拍相位，多个呼吸点同相提交（同拍 update() 合并为一次顶层请求）
        _m = 6 if _CK_DEG['on'] else 5
        if self._beat._n % _m == 0:
            self.update()

    def paintEvent(self, ev):
        import math
        if self._bg is None:
            return          # 首帧不画点：让父级底图先落盘，40ms 后抓底再开始呼吸
        k = 0.5 + 0.5 * math.cos((self._t / (self._period / 1000.0)) * 2 * math.pi)
        alpha = int(102 + 153 * k)
        p = QPainter(self)
        p.drawPixmap(0, 0, self._bg)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        halo = QColor(self._color)
        halo.setAlpha(int(40 + 50 * k))
        p.setBrush(halo)
        p.drawEllipse(3, 3, self._size, self._size)
        core = QColor(self._color)
        core.setAlpha(alpha)
        p.setBrush(core)
        p.drawEllipse(3 + self._size * 0.18, 3 + self._size * 0.18,
                      self._size * 0.64, self._size * 0.64)


class LiveTag(QLabel):
    """R024：网页 hero-live-tag 的桌面实现（脉冲圆点 + 10px/2px 字距胶囊）。"""

    def __init__(self, parent=None):
        super().__init__('', parent)
        self._on = False
        self._ph = 0.0
        # R18：并入统一节拍器（原 120ms 独立定时器）
        self._beat = UiBeat.instance()
        self._beat_ok = False
        self._reduced = False
        # R025（领导）：LIVE 移到数值下方后独立成行，缩小权重（60×22）
        self.setFixedWidth(62)
        self.setFixedHeight(24)
        self.setContentsMargins(0, 0, 0, 0)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # R22：抓底自绘——脉冲只重画本控件，不再连带 GlassCard/祖先链重绘
        self._bg = None
        self._bg_lock = False

    def _dot_rect(self):
        """C22：LIVE 脉冲只改圆点透明度，脏区缩到圆点周围 14px（胶囊/文字不动）。"""
        r = self.contentsRect()
        h = min(r.height(), 18)
        cy = r.top() + r.height() / 2.0
        cx = r.left() + 7 + 2.5
        return QRect(int(cx) - 7, int(cy) - 7, 14, 14)

    def _tick(self):
        # R22：随统一节拍 120→150ms 调整步长，保持 LIVE 脉冲周期不变
        self._ph += 0.08 * (120.0 / 150.0)
        if self._ph > 1.0:
            self._ph -= 1.0
        # R25：LIVE 脉冲同样每 5 拍重绘（6.7→1.33 次/s）；R26：不聚焦档每 2 拍
        # R26-D②：与呼吸点同相（全局节拍相位），同拍 update() 合并
        _m = 6 if _CK_DEG['on'] else 5
        if self._beat._n % _m == 0:
            self.update(self._dot_rect())

    def set_on(self, on, reduced=False):
        self._on = bool(on)
        self._reduced = bool(reduced)
        if self._on and not self._reduced and self.isVisible():
            if not self._beat_ok:
                self._beat.add(self._tick)
                self._beat_ok = True
        else:
            self._stop_beat()
            self._ph = 0.0
        self.update()

    def _stop_beat(self):
        if self._beat_ok:
            self._beat.remove(self._tick)
            self._beat_ok = False

    def showEvent(self, ev):
        super().showEvent(ev)
        if self._on and not self._reduced:
            if not self._beat_ok:
                self._beat.add(self._tick)
                self._beat_ok = True
        self._schedule_capture()

    def _schedule_capture(self):
        if self._bg is not None or getattr(self, '_cap_pending', False):
            return
        self._cap_pending = True

        def _once():
            self._cap_pending = False
            if self._bg is None:
                self._capture_bg()
        QTimer.singleShot(120, _once)

    def changeEvent(self, ev):
        super().changeEvent(ev)
        # 主题/样式切换后底图会变：重新抓底（否则胶囊底下是旧底色）
        if ev.type() in (QEvent.StyleChange, QEvent.PaletteChange) and not self._bg_lock:
            self._bg = None
            self._schedule_capture()

    def _capture_bg(self, _try=0):
        """抓底（窗口画过一轮后调用）：自绘背景，避免每帧让父级重画。"""
        if self._bg_lock or not self.isVisible():
            return
        if not _chain_settled(self):
            if _try < 15:
                QTimer.singleShot(220, lambda: self._capture_bg(_try + 1))
            return
        self._bg_lock = True
        try:
            from PySide6.QtCore import QPoint, QRect
            tl = self.window()
            pos = self.mapTo(tl, QPoint(0, 0))
            self._bg = tl.grab(QRect(pos, self.size()))
        except Exception as _exc:
            self._bg = None
            ck_swallow(_exc, 'launcher:1007')
        finally:
            self._bg_lock = False
        if self._bg is None:
            return
        # R030：隐藏页首次抓底可能抓到未绘制的白底，会留下白方块。
        # 采样三点，接近纯白即丢弃并限次重试（页面真正绘出后再自绘）。
        try:
            _im = self._bg.toImage()
            _w = max(1, self.width() - 3)
            _h = max(1, self.height() - 3)
            _white = 0
            for _x, _y in ((2, 2), (_w // 2, _h // 2), (_w, _h)):
                _c = _im.pixelColor(_x, _y)
                if _c.red() > 235 and _c.green() > 235 and _c.blue() > 235 and _c.alpha() > 200:
                    _white += 1
            if _white >= 2:
                self._bg = None
                self._retry = getattr(self, '_retry', 0) + 1
                if self._retry <= 6 and self.isVisible():
                    QTimer.singleShot(300, self._capture_bg)
                return
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:bgcapture')
        self._retry = 0
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.update()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._stop_beat()

    def paintEvent(self, ev):
        if self._bg is None:
            # 首帧不画：让父级底图先落盘，40ms 后抓底再开始脉冲
            return
        p = QPainter(self)
        p.drawPixmap(0, 0, self._bg)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.contentsRect()
        h = min(r.height(), 18)
        rr = QRectF(r.left(), r.top() + (r.height() - h) / 2.0, r.width() - 1, h - 1)
        fg = QColor(OK) if self._on else QColor(TEXT_MUTED)
        bg = QColor(16, 169, 104, 42) if self._on else QColor(255, 255, 255, 18)
        bd = QColor(52, 211, 153, 96) if self._on else QColor(255, 255, 255, 40)
        p.setPen(QPen(bd, 1))
        p.setBrush(bg)
        p.drawRoundedRect(rr, h / 2.0, h / 2.0)
        # 脉冲圆点（网页 pulse 1.6s；减少动效时静止）
        a = 1.0
        if self._beat_ok:
            a = 0.45 + 0.55 * (0.5 + 0.5 * math.cos(self._ph * 2 * math.pi * 2))
        dc = QColor(fg)
        dc.setAlphaF(a)
        p.setPen(Qt.NoPen)
        p.setBrush(dc)
        p.drawEllipse(QRectF(rr.left() + 7, rr.center().y() - 2.5, 5, 5))
        f = QFont(self.font())
        f.setPixelSize(9)
        f.setBold(True)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.5)
        p.setFont(f)
        p.setPen(fg)
        p.drawText(QRectF(rr.left() + 17, rr.top(), rr.width() - 19, h),
                   Qt.AlignLeft | Qt.AlignVCenter, 'LIVE')
        p.end()


def _chain_settled(w):
    """R22：抓底前置条件——祖先链上不能有未完成的透明度特效（入场/切页淡入）。

    淡入期间整棵子树是半透明的，此时 grab 会把“半透明卡片”录进底图，
    淡入结束恢复不透明后就成了一眼可见的深色矩形补丁（diag6）。
    """
    p = w
    while p is not None:
        try:
            if isinstance(p.graphicsEffect(), QGraphicsOpacityEffect):
                return False
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:_chain_settled')
        p = p.parentWidget()
    return True


def _shadow_pixmap(w, h, draw_src, color, radius, dpr, glow_only=False):
    """R23：用 Qt 自身的阴影特效把「源画面 + 辉光」预渲染成一张本控件尺寸的位图。

    与 QGraphicsDropShadowEffect 走同一个 draw/qt_blurImage 实现，视觉等价；但结果
    只是一张缓存位图，绘制时不外扩脏区（不顶到兄弟控件与祖先链）。只有文字/字体/
    尺寸/闪光色变化时才重算。

    R24-C：``glow_only=True`` 时把源画面整体平移到捕获矩形之外（画布扩为 2w×2h），
    阴影 offset 反向平移回原位，只对 (0,0,w,h) 渲染 → 得到与旧实现同一 blur
    实现的**纯辉光**位图（不含字形本体），供静态层缓存；文字由调用方每帧叠加。

    本体必须**完全**落在捕获矩形之外：用 CompositionMode_Clear 事后抹本体的做法
    （画布保持 1×、单次 ~1.3ms）在字形抗锯齿边缘留下 shadow·α·(1-α) 的残留，
    实测文字区 max 27.6/255、11281 px>6，不可用（见 _r24_step9.md）。
    """
    try:
        from PySide6.QtCore import QRectF as _QRectF
        from PySide6.QtWidgets import (QGraphicsDropShadowEffect, QGraphicsPixmapItem,
                                       QGraphicsScene)
        _col = QColor(*color) if isinstance(color, (tuple, list)) else QColor(color)
        _d = dpr
        iw, ih = max(1, int(w * _d)), max(1, int(h * _d))
        if glow_only:
            cw, ch = iw * 2, ih * 2
            _sx, _sy = w, h
        else:
            cw, ch = iw, ih
            _sx, _sy = 0.0, 0.0
        src = QPixmap(iw, ih)
        src.setDevicePixelRatio(_d)
        src.fill(Qt.transparent)
        _p = QPainter(src)
        _p.setRenderHint(QPainter.Antialiasing, True)
        draw_src(_p)
        _p.end()
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(src)
        eff = QGraphicsDropShadowEffect()
        eff.setBlurRadius(radius)
        eff.setColor(_col)
        eff.setOffset(-_sx, -_sy)   # QGraphicsDropShadowEffect.offset 为逻辑像素
        item.setGraphicsEffect(eff)
        item.setPos(_sx, _sy)
        scene.addItem(item)
        if glow_only:
            scene.setSceneRect(_QRectF(0, 0, 2 * w, 2 * h))
        big = QPixmap(cw, ch)
        big.setDevicePixelRatio(_d)
        big.fill(Qt.transparent)
        _q = QPainter(big)
        _rc = _QRectF(0, 0, cw / _d, ch / _d)
        scene.render(_q, _rc, _rc)
        _q.end()
        scene.clear()
        if glow_only:
            # 只保留左上角捕获区（整画布渲染是 Qt 唯一可靠的出图方式，子矩形会被裁剪裁空）
            out = big.copy(0, 0, iw, ih)
            out.setDevicePixelRatio(_d)
            return out
        return big
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:shadow_pixmap')
        return None


class _HeroGlowPainter:
    """R23：把 hero 数字 + 'W' 的「文字 + Qt 原生辉光」画进 mid_w 的不透明面板底图。

    hero_power / unit_w 在布局里只承担尺寸与字号（paintEvent 直接 return），真正上屏
    的是面板自绘的预渲染位图（由 QGraphicsDropShadowEffect 同一实现生成）。位图比
    文字大一圈，原特效会溢出控件矩形的裙边也能完整画出；脏区止于面板自身，祖先链
    不陪站，且布局零改动。
    """

    PAD = 8
    PAD_U = 6

    def __init__(self, panel, hero, unit):
        self.panel = panel
        self.hero = hero
        self.unit = unit
        self._key = None
        self._hp = None
        self._up = None
        self._baked_txt = ''
        self._bake_t = 0.0

    @staticmethod
    def _outline(lab):
        """轮廓签名：只由字体度量与字符格数构成，不含文字内容/数值/颜色。"""
        fm = lab.fontMetrics()
        t = lab.text() or ''
        return (len(t), t.count('.'), int(fm.horizontalAdvance('0')), int(fm.height()))

    def _key_split(self):
        """R24-C 静态层 key：几何 + 主题 + 半径 + 轮廓（文字/数字/闪光色全部出 key）。"""
        h, u = self.hero, self.unit
        _g = h._grad or (None, None)
        return (h.width(), h.height(), round(h.devicePixelRatioF(), 3),
                None if _g[0] is None else int(_g[0].rgb()),
                None if _g[1] is None else int(_g[1].rgb()),
                _CK_GLOW_R, _CK_GLOW_COL, self._outline(h),
                u.width(), u.height(), round(u.devicePixelRatioF(), 3),
                tuple(u._glow.getRgb()), int(u._blur), self._outline(u))

    def _key_full(self):
        """R23 旧 key（含文字/闪光色），仅 CK_GLOW_SPLIT=0 的 A/B 回退用。"""
        h, u = self.hero, self.unit
        return (h.text(), h.font().toString(), h.width(), h.height(),
                h._flash_color.rgb(), u.text(), u.font().toString(),
                u.width(), u.height(), u.font().toString())

    def _build(self):
        """只烤静态辉光（剪影，不含字形本体）。"""
        h, u, pad, pad2 = self.hero, self.unit, self.PAD, self.PAD_U

        def _hero_src(pp):
            pp.translate(pad, pad)
            h._paint_silhouette(pp)

        self._hp = _shadow_pixmap(h.width() + 2 * pad, h.height() + 2 * pad,
                                  _hero_src, _CK_GLOW_COL, _CK_GLOW_R,
                                  h.devicePixelRatioF(), glow_only=True)

        def _unit_src(pp):
            pp.translate(pad2, pad2)
            u._paint_text(pp)

        self._up = _shadow_pixmap(u.width() + 2 * pad2, u.height() + 2 * pad2,
                                  _unit_src, u._glow.getRgb(), u._blur,
                                  u.devicePixelRatioF(), glow_only=True)

    def _build_full(self):
        """R23 旧实现（文字+辉光整图），仅 A/B 回退用。"""
        h, u, pad, pad2 = self.hero, self.unit, self.PAD, self.PAD_U

        def _hero_src(pp):
            pp.translate(pad, pad)
            h._paint_grad_text(pp, glow=False)

        self._hp = _shadow_pixmap(h.width() + 2 * pad, h.height() + 2 * pad,
                                  _hero_src, _CK_GLOW_COL, _CK_GLOW_R,
                                  h.devicePixelRatioF())

        def _unit_src(pp):
            pp.translate(pad2, pad2)
            u._paint_text(pp)

        self._up = _shadow_pixmap(u.width() + 2 * pad2, u.height() + 2 * pad2,
                                  _unit_src, u._glow.getRgb(), u._blur,
                                  u.devicePixelRatioF())

    def draw(self, p):
        from PySide6.QtCore import QPoint
        h, u = self.hero, self.unit
        key = self._key_split() if _CK_GLOW_SPLIT else self._key_full()
        _need = key != self._key or self._hp is None
        if not _need and _CK_GLOW_SPLIT and _CK_GLOW_REFRESH > 0:
            # 形状补烤（默认禁用）：数值已稳定且距上次 ≥ CK_GLOW_REFRESH 才补一次
            _need = (h.text() != self._baked_txt
                     and h._anim.state() != QAbstractAnimation.Running
                     and time.monotonic() - self._bake_t >= _CK_GLOW_REFRESH)
        if _need:
            self._key = key
            (self._build if _CK_GLOW_SPLIT else self._build_full)()
            self._baked_txt = h.text()
            self._bake_t = time.monotonic()
        if self._hp is not None:
            hp = h.mapTo(self.panel, QPoint(0, 0))
            p.drawPixmap(hp.x() - self.PAD, hp.y() - self.PAD, self._hp)
        if self._up is not None:
            up = u.mapTo(self.panel, QPoint(0, 0))
            p.drawPixmap(up.x() - self.PAD_U, up.y() - self.PAD_U, self._up)
        if _CK_GLOW_SPLIT:
            # 动态层：文字每帧叠加（渐变/闪光色都走这里），不重烤静态辉光
            hp = h.mapTo(self.panel, QPoint(0, 0))
            p.save()
            p.translate(hp)
            h._paint_grad_text(p, glow=False)
            p.restore()
            up = u.mapTo(self.panel, QPoint(0, 0))
            p.save()
            p.translate(up)
            u._paint_text(p)
            p.restore()


class GlowLabel(QLabel):
    """R22：实色小标签 + 手绘辉光（替代 QGraphicsDropShadowEffect）。

    图形特效会把脏区扩到控件矩形之外，每帧连带相邻控件与整条祖先链重绘；
    描边辉光裁剪在自身矩形内，视觉近似、代价可控。
    """

    def __init__(self, text='', parent=None, color=TEAL, glow=(90, 160, 255),
                 blur=22):
        super().__init__(text, parent)
        self._color = QColor(color)
        self._glow = QColor(*glow)
        self._blur = blur
        self._glow_pix = None
        self._glow_key = None
        self._panel_draw = False

    def _base_y(self, fm, r):
        al = self.alignment()
        if al & Qt.AlignBottom:
            return r.bottom() - fm.descent() + 1
        if al & Qt.AlignVCenter:
            return r.top() + (r.height() + fm.ascent() - fm.descent()) // 2
        return r.top() + fm.ascent()

    def _paint_text(self, p):
        """只画实色文字（辉光/面板交给调用方）。"""
        txt = self.text()
        if not txt:
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setFont(self.font())
        fm = p.fontMetrics()
        r = self.contentsRect()
        x = r.left()
        al = self.alignment()
        if al & Qt.AlignHCenter:
            x = r.left() + max(0, (r.width() - fm.horizontalAdvance(txt)) // 2)
        elif al & Qt.AlignRight:
            x = r.right() - fm.horizontalAdvance(txt)
        p.setPen(QPen(self._color, 0))
        p.drawText(x, self._base_y(fm, r), txt)

    def paintEvent(self, ev):
        if self._panel_draw:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setFont(self.font())
        txt = self.text()
        if not txt:
            return
        fm = p.fontMetrics()
        r = self.contentsRect()
        x = r.left()
        al = self.alignment()
        if al & Qt.AlignHCenter:
            x = r.left() + max(0, (r.width() - fm.horizontalAdvance(txt)) // 2)
        elif al & Qt.AlignRight:
            x = r.right() - fm.horizontalAdvance(txt)
        y = self._base_y(fm, r)
        # R23：辉光+文字一次性预渲染（Qt 阴影特效同源），缓存在本控件；
        # 只画自身矩形内的位图，不外扩脏区。
        if _CK_GLOW_SPLIT:
            # R24-C 静态层 key：几何/主题/半径/轮廓，不含文字内容
            _k = (self.width(), self.height(), round(self.devicePixelRatioF(), 3),
                  int(self._blur), tuple(self._glow.getRgb()),
                  (len(txt), txt.count('.'), int(fm.horizontalAdvance('0')),
                   int(fm.height())))
        else:
            _k = (txt, self.font().toString(), self.width(), self.height(), y)
        if self._glow_key != _k or self._glow_pix is None:
            # R26：可见不聚焦 → 不重算辉光位图（看不到；离开降级档后 key 自然不等，下一帧重建）
            if not _CK_DEG['on']:
                self._glow_key = _k
                _f, _xx, _b, _t, _c = self.font(), x, y, txt, QColor(self._color)

                def _src(pp):
                    pp.setFont(_f)
                    pp.setPen(QPen(_c, 0))
                    pp.drawText(_xx, _b, _t)

                self._glow_pix = _shadow_pixmap(
                    self.width(), self.height(), _src,
                    self._glow.getRgb(), self._blur, self.devicePixelRatioF(),
                    glow_only=_CK_GLOW_SPLIT)
        if self._glow_pix is not None and not _CK_DEG['on']:
            p.drawPixmap(0, 0, self._glow_pix)
            if not _CK_GLOW_SPLIT:
                return
        p.setPen(QPen(self._color, 0))
        p.drawText(x, y, txt)


class AnimatedNumberLabel(QLabel):
    """数值平滑变化（对应网页 numFlash：数字过渡 + 变化瞬间放大高亮）。"""
    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self._cur = None
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(DUR_NUM)  # G2 统一数字动画 token
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_value)
        self._anim.finished.connect(self._on_anim_fin)
        self._fmt = lambda v: '%.1f' % v
        self._flash = False
        self._flash_color = QColor(47, 182, 255)
        self._flashing = False               # R22：闪光进行中（自绘上色，不动 QSS）
        self._base_txt_color = QColor(TEXT)  # R22：QSS 基础字色（自绘收尾用）
        self._flash_base_stylesheet = ''
        self._flash_anim = None
        self._flash_ok = False   # R26-D：闪光已挂 UiBeat（不再起独立 60fps 动画）
        self._flash_t0 = 0.0
        self._qss_variants = {}  # R017-CK02：实例级缓存（类级按色值共用会跨实例串样式）
        self._grad = None         # R020：渐变文字（大号总功率）
        self._tgt = None          # R22：上次目标值的显示串（量化去抖用）
        self._glow_pix = None     # R23：预渲染辉光（Qt 模糊位图，缓存在本控件）
        self._glow_key = None
        self._glow_txt = ''
        self._glow_t = 0.0
        # R22：抓底自绘——数字更新只重画自身矩形，不连带祖先链
        self._bg = None
        self._bg_lock = False
        self._cap_hide = False   # 抓底瞬间自绘也必须停（否则把自己的字抓进底图→重影）
        self._panel_draw = False # R23：由父级不透明面板自绘（自己不画，只做布局占位）
        # C29：空值态缩放（0=不启用）。hero 的 '—' 在 84px 下是一条青→紫粗横杠，
        # 首屏第一眼看起来像进度条；空值时按比例缩到小字号并转灰，有值时恢复基准。
        self._empty_scale = 0.0
        self._base_qss = ''
        self._qss_guard = False
        self._base_grad = None
        self._empty_cb = None
        self._c29_empty_hits = 0
        self._c29_value_hits = 0
        # C30：QSS 基准色（自绘路径此前只在闪光时解析 → 首屏空值/首值恒为默认白）
        self._base_color = QColor(TEXT)
        self._probe_tag = ''

    def resizeEvent(self, ev):
        """C30：控件尺寸变化后旧底图尺寸失配——必须作废重抓。

        空值态 → 有值态字号 19→26px、控件高 30→38，若继续用 30 高的 _bg 铺底，
        WA_OpaquePaintEvent 下底部 8px 无人绘制 → 实测出现一条纯黑横条。
        """
        super().resizeEvent(ev)
        if self._bg is not None and self._bg.size() != self.size():
            self.setAttribute(Qt.WA_OpaquePaintEvent, False)
            self._bg = None
            self._schedule_capture()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._schedule_capture()

    def _schedule_capture(self):
        """只安排一次抓底；尺寸/主题不变不重复抓（避免并发抓底互相把 opaque 关掉）。"""
        if self._panel_draw:
            return   # 面板模式：底图与文字都由父级 _OpaquePanel 画
        if _GLOW_LEGACY and self._grad is not None:
            return   # A/B 诊断：legacy 辉光走图形特效，必须保持透明底
        if self._bg is not None or getattr(self, '_cap_pending', False):
            return
        self._cap_pending = True

        def _once():
            self._cap_pending = False
            if self._bg is None:
                self._capture_bg()
        QTimer.singleShot(120, _once)

    def _capture_bg(self, _try=0):
        """抓本控件位置下的窗口底图（抓之前先让父级把该区域重新画成底图）。"""
        if self._bg_lock or not self.isVisible():
            return
        if not _chain_settled(self):
            # 入场/切页淡入还在跑：等特效摘掉再抓（最多 ~3.3s，之后放弃改走透明绘制）
            if _try < 15:
                QTimer.singleShot(220, lambda: self._capture_bg(_try + 1))
            return
        self._bg_lock = True
        try:
            from PySide6.QtCore import QPoint, QRect
            # 关键：抓之前先关掉 opaque 并让父级重画这一块，否则 grab 会把
            # “自己没画任何东西”的空白区域（白/黑）抓进来，变成永久色块。
            self.setAttribute(Qt.WA_OpaquePaintEvent, False)
            self._bg = None
            self._cap_hide = True
            self.repaint()
            tl = self.window()
            pos = self.mapTo(tl, QPoint(0, 0))
            _pm = tl.grab(QRect(pos, self.size()))
            _im = _pm.toImage()
            _c = _im.pixelColor(_im.width() // 2, _im.height() // 2)
            if (_c.red() + _c.green() + _c.blue()) < 12 and _try < 5:
                # 窗口还没画完：稍后重试，不要用未初始化的底图
                self._bg = None
                QTimer.singleShot(120, lambda: self._capture_bg(_try + 1))
                return
            self._bg = _pm
        except Exception as _exc:
            self._bg = None
            ck_swallow(_exc, 'launcher:1112')
        finally:
            self._cap_hide = False
            self._bg_lock = False
        if self._bg is None:
            return
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.update()

    def _show(self, s):
        """统一出口：文本变化时重钉 opaque（Qt 某些重排路径会把它清掉）。"""
        # R27：空载/可见不聚焦/遮挡/最小化 → 文本先挂起，由 UiBeat 按 CK_NUM_FPS 合并提交，
        #      把每拍的 setText→update 收敛成一拍一次；聚焦充电态仍即时上屏（新鲜度不降级）。
        _CK_NUMQ['calls'] += 1
        if _CK_DEG['on'] or _CK_SUS_ON['on'] or _CK_NUMQ['defer_all']:
            _CK_NUMQ['pending'][self] = s
            if not _CK_NUMQ['armed']:
                _CK_NUMQ['armed'] = True
                UiBeat.instance().wake()
            return
        self._show_now(s)

    def _show_now(self, s):
        if s == self.text():
            return
        self.setText(s)
        self._sync_empty_qss()
        if self._bg is not None:
            self.setAttribute(Qt.WA_OpaquePaintEvent, True)

    # C29：空值态样式降级（仅启用了 set_empty_style 的实例生效）
    def setStyleSheet(self, qss):
        """C29/C30：外部传入的样式即“基准样式”；空值态期间内部临时改写，不改基准。

        C30：这里同时解析基准字色。改前 _base_txt_color 只在闪光开始时才从 QSS 解析，
        首屏首次上屏（含 '—'）一律用默认白 TEXT（TOKENS['text']）→ 4 个 KPI 空值是四条白横杠。
        """
        self._base_qss = qss
        if self._qss_guard:
            super().setStyleSheet(qss)
            return
        _empty = bool(self._empty_scale) and self.text() == '—'
        if not self._flashing:
            self._base_color = self._parse_color(qss)
            self._base_txt_color = (QColor('#7d93ab') if _empty else self._base_color)
        super().setStyleSheet(self._empty_style(qss) if _empty else qss)

    @staticmethod
    def _parse_color(qss, default=TEXT):
        _m = re.search(r'color:\s*(#[0-9a-fA-F]{3,8})', qss or '')
        return QColor(_m.group(1)) if _m else QColor(default)

    def set_probe_tag(self, tag):
        """C30：登记到取证表（c30probe 逐项读出）。"""
        self._probe_tag = str(tag or '')
        if self._probe_tag:
            _CK_NUM_PROBE[self._probe_tag] = self

    def set_empty_style(self, scale=0.55):
        self._empty_scale = float(scale or 0)
        self._sync_empty_qss()

    def _empty_style(self, qss):
        """把基准样式里的字号按本实例 scale 缩、字色转静默灰；其余原样保留。"""
        _m = re.search(r'font-size:\s*(\d+)px', qss or '')
        if not _m:
            return qss
        _k = self._empty_scale or 0.55
        _px = max(12, int(round(int(_m.group(1)) * _k)))
        _q = re.sub(r'font-size:\s*\d+px', 'font-size:%dpx' % _px, qss, count=1)
        _q = re.sub(r'color:\s*#[0-9a-fA-F]{3,8}', 'color:#7d93ab', _q, count=1)
        return _q

    def _sync_empty_qss(self):
        if not self._empty_scale:
            return
        _base = self._base_qss or self.styleSheet()
        _empty = self.text() == '—'
        # 空值态也换渐变色：静默灰双端同色 = 实色，避免“小号青紫横杠”仍像进度条
        if _empty and self._base_grad is not None:
            self._grad = (QColor('#6f859c'), QColor('#6f859c'))
        elif not _empty and self._base_grad is not None:
            self._grad = self._base_grad
        # C30：实色自绘路径读的是 _base_txt_color，空值必须同步转灰，否则 QSS 改了也不生效。
        if not self._flashing:
            self._base_color = self._parse_color(_base)
            self._base_txt_color = (QColor('#7d93ab') if _empty else self._base_color)
        _want = self._empty_style(_base) if _empty else _base
        if self.styleSheet() == _want:
            return
        self._qss_guard = True
        try:
            super().setStyleSheet(_want)
        finally:
            self._qss_guard = False
        if self.text() == '—':
            self._c29_empty_hits += 1
            _ck_dbg('C30 num_empty tag=%s hits=%d px=%s'
                    % (self._probe_tag or 'num', self._c29_empty_hits, _want))
        else:
            self._c29_value_hits += 1
            _ck_dbg('C30 num_value tag=%s hits=%d px=%s'
                    % (self._probe_tag or 'num', self._c29_value_hits, _want))
        if callable(self._empty_cb):
            try:
                self._empty_cb(_empty)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:c29_empty_cb')
        self.update()

    def set_formatter(self, fmt):
        self._fmt = fmt

    def set_gradient(self, c1, c2):
        """R020（领导）：大号功率数字用青→蓝→紫渐变（对应网页 hero 主色渐变）。"""
        self._base_grad = (QColor(c1), QColor(c2))
        if not (self._empty_scale and self.text() == '—'):
            self._grad = self._base_grad
        self.update()

    def paintEvent(self, ev):
        if self._panel_draw:
            return   # 面板模式：文字/辉光由父级 _OpaquePanel 自绘
        if _GLOW_LEGACY and self._grad is not None:
            _pl = QPainter(self)   # A/B 诊断：透明底 + 图形特效（与 R22 之前一致）
            self._paint_grad_text(_pl, glow=False)
            _pl.end()
            return
        if self._cap_hide:
            return   # 抓底进行中：自己不画任何东西，让窗口 grab 拿到纯底图
        if self._bg is None:
            # 底图未就绪（淡入中/尚未抓到）：先按原方式透明绘制，避免数字空白
            if self._grad is None:
                return super().paintEvent(ev)
            _pw = QPainter(self)
            self._paint_grad_text(_pw)
            _pw.end()
            return
        if not self.testAttribute(Qt.WA_OpaquePaintEvent):
            self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        _p0 = QPainter(self)
        _p0.drawPixmap(0, 0, self._bg)
        _p0.end()
        if self._grad is None:
            _p1 = QPainter(self)
            self._paint_solid_text(
                _p1, self._flash_color if self._flashing else self._base_txt_color)
            _p1.end()
            return
        # 渐变路径：底色铺好后自绘文字 + 手绘辉光（都在本控件内，不外扩脏区）
        _p2 = QPainter(self)
        self._paint_grad_text(_p2, glow=True)
        _p2.end()

    def _paint_solid_text(self, p, color):
        """R22：实色数字自绘（不再靠 QSS 换色），布局口径与渐变路径一致。"""
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setFont(self.font())
        txt = self.text()
        fm = p.fontMetrics()
        r = self.contentsRect()
        al = self.alignment()
        tw = fm.horizontalAdvance(txt)
        x = r.left()
        if al & Qt.AlignHCenter:
            x = r.left() + max(0, (r.width() - tw) // 2)
        elif al & Qt.AlignRight:
            x = r.right() - tw
        if al & Qt.AlignBottom:
            y = r.bottom() - 2
        elif al & Qt.AlignVCenter:
            y = r.top() + (r.height() + fm.ascent() - fm.descent()) // 2
        else:
            y = r.top() + fm.ascent()
        p.setPen(QPen(QColor(color)))
        p.drawText(x, y, txt)

    def _paint_grad_text(self, p, glow=False):
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setFont(self.font())
        txt = self.text()
        fm = p.fontMetrics()
        r = self.contentsRect()
        tw = fm.horizontalAdvance(txt)
        align = self.alignment()
        x = r.left()
        if align & Qt.AlignHCenter:
            x = r.left() + max(0, (r.width() - tw) // 2)
        elif align & Qt.AlignRight:
            x = r.right() - tw
        if align & Qt.AlignBottom:
            # R026（领导）：数字无降部，基线贴底（去掉 descent 死区），让 LIVE/标题的间距真实生效
            base_y = r.bottom() - 2
        else:
            base_y = r.top() + (r.height() + fm.ascent() - fm.descent()) // 2
        # 闪光期：静止色为 (47,182,255)，白闪从 255 衰减到 47 → t 必须按红通道回升计算
        t = 0.0
        try:
            t = max(0.0, min(1.0, (self._flash_color.red() - 47) / 208.0))
        except Exception:
            t = 0.0
        c1 = QColor(self._grad[0])
        c2 = QColor(self._grad[1])
        if t > 0.01:
            w = QColor(255, 255, 255)
            c1 = QColor(int(c1.red() + (w.red() - c1.red()) * t),
                        int(c1.green() + (w.green() - c1.green()) * t),
                        int(c1.blue() + (w.blue() - c1.blue()) * t))
            c2 = QColor(int(c2.red() + (w.red() - c2.red()) * t),
                        int(c2.green() + (w.green() - c2.green()) * t),
                        int(c2.blue() + (w.blue() - c2.blue()) * t))
        # R022：渐变轴必须水平（原 (x, top)→(x+tw, bottom) 呈 37° 斜向）
        # R023：终点外扩 8px、紫端提前到 0.88（实测右端只到渐变中段 (78,112,251)）
        # R024：用墨迹包围盒而非 advance 宽度，否则紫端落在字形右侧空白
        _br = fm.tightBoundingRect(txt)
        x_ink = x + _br.left()
        w_ink = max(1, _br.width())
        # C30：空值横杠的“真静默”。C29 只把 _grad 两端换灰，但 0.0 停靠点仍硬编码
        # #2fb6ff、辉光层仍用主题青色 → 首屏 '—' 左 1/3 亮青+青色光晕，仍像加载条。
        _empty_dash = bool(self._empty_scale) and txt == '—'
        g = QLinearGradient(x_ink, base_y, x_ink + w_ink, base_y)
        g.setColorAt(0.0, QColor('#6f859c') if _empty_dash else QColor('#2fb6ff'))
        g.setColorAt(0.32, c1)
        g.setColorAt(0.92, c2)
        g.setColorAt(1.0, c2)
        # R23：辉光与文字一次性预渲染（Qt 自身阴影特效，视觉等价 blur 40），
        # 只画在本控件矩形内，不外扩脏区；闪光变色的那几帧才重算。
        if glow and not _empty_dash:
            if _CK_GLOW_SPLIT:
                # R24-C 静态层 key：几何/主题/半径/轮廓，文字与闪光色都不进 key
                _k = (self.width(), self.height(), round(self.devicePixelRatioF(), 3),
                      _CK_GLOW_R, _CK_GLOW_COL,
                      (len(txt), txt.count('.'), int(fm.horizontalAdvance('0')),
                       int(fm.height())))
                _src = lambda pp: self._paint_silhouette(pp)   # noqa: E731
            else:
                _k = (txt, self.font().toString(), self.width(), self.height(),
                      base_y, c1.rgb(), c2.rgb())
                _src = lambda pp: self._paint_grad_text(pp, glow=False)  # noqa: E731
            _need = self._glow_key != _k or self._glow_pix is None
            if not _need and _CK_GLOW_SPLIT and _CK_GLOW_REFRESH > 0:
                _need = (txt != self._glow_txt
                         and self._anim.state() != QAbstractAnimation.Running
                         and time.monotonic() - self._glow_t >= _CK_GLOW_REFRESH)
            if _need:
                # R26：可见不聚焦 → 辉光位图不重算、不绘制（离开降级档后 _need 仍为真，下一帧补上）
                if _CK_DEG['on']:
                    p.setPen(QPen(QBrush(g), 0))
                    p.drawText(x, base_y, txt)
                    return
                self._glow_key = _k
                self._glow_pix = _shadow_pixmap(
                    self.width(), self.height(), _src,
                    _CK_GLOW_COL, _CK_GLOW_R, self.devicePixelRatioF(),
                    glow_only=_CK_GLOW_SPLIT)
                self._glow_txt = txt
                self._glow_t = time.monotonic()
            if self._glow_pix is not None:
                p.drawPixmap(0, 0, self._glow_pix)
                if not _CK_GLOW_SPLIT:
                    return
        p.setPen(QPen(QBrush(g), 0))
        p.drawText(x, base_y, txt)

    def _paint_silhouette(self, p):
        """R24-C：静态辉光剪影——只用当前字形的 alpha（白色实心），不含颜色/闪光。

        与 `_paint_grad_text` 的起笔位置/基线完全对齐；静态层按“轮廓签名”复用，
        数值变化不重烤（辉光形状滞后由轮廓签名控制，见 _r24_step9.md 的实测差异）。
        """
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setFont(self.font())
        txt = self.text()
        if not txt:
            return
        sk = txt
        fm = p.fontMetrics()
        r = self.contentsRect()
        tw = fm.horizontalAdvance(txt)
        align = self.alignment()
        x = r.left()
        if align & Qt.AlignHCenter:
            x = r.left() + max(0, (r.width() - tw) // 2)
        elif align & Qt.AlignRight:
            x = r.right() - tw
        if align & Qt.AlignBottom:
            base_y = r.bottom() - 2
        else:
            base_y = r.top() + (r.height() + fm.ascent() - fm.descent()) // 2
        p.setPen(QPen(QColor(255, 255, 255), 0))
        p.drawText(x, base_y, sk)

    def _on_value(self, v):
        # R22：缓动期间限帧 75ms（≈13fps），把每拍 16ms 的刷新降到一半；
        # 终值由 finished → _on_anim_fin 兜底上屏，不会漏掉最后一帧。
        _now = time.monotonic()
        if (_now - getattr(self, '_pupt', 0.0) < 0.075
                and self._anim.state() == QAbstractAnimation.Running):
            return
        self._pupt = _now
        self._show(self._fmt(v))

    def _on_anim_fin(self):
        if self._anim.endValue() is not None:
            self._show(self._fmt(self._anim.endValue()))

    def set_target(self, value, flash=False):
        if value is None:
            if self._cur is None and self.text() == '—':
                return
            self._show('—')
            self._cur = None
            self._tgt = None
            return
        value = float(value)
        # R22：先按显示精度量化（格式化即量化）——量化后与上次目标相同则不 setText、
        # 不启动缓动；日志/记录/曲线仍用原始值，不受影响。
        _qs = self._fmt(value)
        if self._cur is not None and _qs == getattr(self, '_tgt', None):
            self._cur = value
            return
        self._tgt = _qs
        if self._cur is None:
            self._cur = value
            self._show(_qs)
            return
        # 值未变化（空闲/稳定）则不重启动画，避免每 2s 空转造成卡顿
        _delta = abs(value - self._cur)
        if _delta < 1e-6:
            self._cur = value
            return
        # R06 性能：微小抖动（<0.5 或 <1%）直接上屏，不启动 550ms 过渡。
        if _delta < max(0.5, abs(self._cur) * 0.01):
            self._anim.stop()
            self._cur = value
            self._show(_qs)
            return
        # R22：缓动进行中新目标到达 → 只更新目标值，不重启（避免反复起停与丢帧）
        if self._anim.state() == QAbstractAnimation.Running:
            self._anim.setEndValue(value)
            self._cur = value
            return
        # R26：可见不聚焦 → 数字直接跳变，不起 60fps 过渡（看不到；聚焦恢复后照常渐变）
        if _CK_DEG['on']:
            self._anim.stop()
            self._cur = value
            self._show(_qs)
            return
        self._anim.stop()
        self._anim.setStartValue(self._cur)
        self._anim.setEndValue(value)
        self._anim.start()
        if flash:
            # R017-CK01：先比较（用动画前值）再赋值——原顺序恒 0 致闪光永不触发
            if abs(value - self._cur) > max(0.5, abs(self._cur) * 0.02):
                self._cur = value
                self._do_flash()
                return
        self._cur = value

    # G3：闪光改为颜色/透明度过渡（280ms 淡入淡出），删除 singleShot 硬复位；
    # 闪光色用强调亮青（沿用网页同源色）。
    # R15-C13：起止色改 白→#38e2ff（网页 flashColor 口径），QSS 变体按色值缓存，
    # 动画帧不再逐帧正则拼装（仅首次构建两种变体）。
    def _get_flash_color(self):
        return self._flash_color

    def _set_flash_color(self, c):
        self._flash_color = QColor(c)
        # R22：所有数字标签都改“自绘上色”，不再逐帧 setStyleSheet。
        # 逐帧换 QSS 会触发 StyleChange，把 WA_OpaquePaintEvent 打回 False，
        # 于是每帧又上溯 GlassCard/祖先链——KPI 数字因此把整窗带热。
        # R25：闪光 280 ms 的过渡再限到 10 fps（每 100 ms 一帧），update() 196→约 150/90s。
        # R26：不聚焦档降到 2 fps（500 ms），反正用户看不到。
        _now = time.perf_counter()
        _iv = 0.50 if _CK_DEG['on'] else 0.10
        if _now - getattr(self, '_flash_t', 0.0) >= _iv:
            self._flash_t = _now
            self.update()

    flash_color = Property(QColor, _get_flash_color, _set_flash_color)

    def _do_flash(self):
        # R26-D：闪光挂 UiBeat —— 280ms 过渡由统一节拍插值，同拍 update() 合并，
        #        不再让 QPropertyAnimation 独立 60fps 唤醒事件循环（结构下限 6.4/s 的来源之一）。
        if _CK_D_OFF['on']:
            self._do_flash_anim()
            return
        if self._flashing:
            return
        self._flash_base_stylesheet = self.styleSheet()
        if self._grad is None:
            # R22：解析 QSS 基础字色，闪光结束自绘收尾（不再 setStyleSheet 复位）
            import re as _re
            _m = _re.search(r'color:\s*(#[0-9a-fA-F]{3,8})',
                            self._flash_base_stylesheet or '')
            self._base_txt_color = QColor(_m.group(1)) if _m else QColor(TEXT)
        self._flashing = True
        self._flash_t0 = time.monotonic()
        self._flash_color = QColor(255, 255, 255)
        if not self._flash_ok:
            self._flash_ok = True
            UiBeat.instance().add(self._flash_step)
        self.update()

    def _flash_step(self):
        """UiBeat 每拍推进闪光插值（OutCubic，280ms；150ms 拍 ≈ 2 帧，终值兜底）。"""
        _p = (time.monotonic() - self._flash_t0) / 0.280
        if _p >= 1.0:
            self._flash_ok = False
            UiBeat.instance().remove(self._flash_step)
            self._on_flash_end()
            return
        _e = 1.0 - (1.0 - _p) ** 3
        _a, _b = QColor(255, 255, 255), QColor(47, 182, 255)
        self._flash_color = QColor(
            int(_a.red() + (_b.red() - _a.red()) * _e),
            int(_a.green() + (_b.green() - _a.green()) * _e),
            int(_a.blue() + (_b.blue() - _a.blue()) * _e),
            int(_a.alpha() + (_b.alpha() - _a.alpha()) * _e))
        self.update()

    def _do_flash_anim(self):
        # 旧路径（CK_D_OFF=1 的成对 A/B 对照用）：QPropertyAnimation 60fps + 100ms update 限帧
        if self._flash_anim is not None \
                and self._flash_anim.state() != QAbstractAnimation.State.Stopped:
            return
        self._flash_base_stylesheet = self.styleSheet()
        if self._grad is None:
            import re as _re
            _m = _re.search(r'color:\s*(#[0-9a-fA-F]{3,8})',
                            self._flash_base_stylesheet or '')
            self._base_txt_color = QColor(_m.group(1)) if _m else QColor(TEXT)
        self._flashing = True
        self._flash_color = QColor(47, 182, 255)
        if self._flash_anim is None:
            self._flash_anim = QPropertyAnimation(self, b'flash_color', self)
            self._flash_anim.setDuration(280)
            self._flash_anim.setEasingCurve(QEasingCurve.OutCubic)
            self._flash_anim.finished.connect(self._on_flash_end)
        self._flash_anim.stop()
        self._flash_anim.setStartValue(QColor(255, 255, 255))
        self._flash_anim.setEndValue(QColor(47, 182, 255))
        self._flash_anim.start()

    def _on_flash_end(self):
        # R22：不再 setStyleSheet 复位（StyleChange 会打掉 opaque 属性并触发整窗重绘），
        # 非渐变标签恢复自绘基础色即可。
        self._flashing = False
        if self._grad is None:
            self._flash_color = QColor(self._base_txt_color)
        self.update()


# R15-A05 归档：FlowProgressBar 类（流光算法已被 PowerScaleBar R15-C16 内联复用，类本身未实例化）
class FlowProgressBar(QWidget):
    """带流光的进度条（对应网页 power-bar / barFlow）。"""

    def __init__(self, color=TEAL, color2=TEAL, parent=None):
        super().__init__(parent)
        self._c1 = QColor(color)
        self._c2 = QColor(color2)
        self._ratio = 0.0
        self._t = 0.0
        self.setFixedHeight(10)
        self._timer = QTimer(self)
        self._timer.setInterval(_ck_iv(80))
        self._timer.timeout.connect(self._tick)
        if self.isVisible():
            _ck_start(self._timer, 'flow')

    def showEvent(self, ev):
        super().showEvent(ev)
        _ck_start(self._timer, 'flow')

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._timer.stop()

    def _tick(self):
        self._t += 0.08
        if self._ratio > 0:
            self.update()

    def set_ratio(self, ratio):
        self._ratio = max(0.0, min(1.0, float(ratio)))
        self.update()

    def setValue(self, value):
        self.set_ratio(float(value) / 100.0)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(0, 2, 0, -2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 18))
        p.drawRoundedRect(r, 4, 4)
        if self._ratio > 0:
            fill = QRectF(r.x(), r.y(), r.width() * self._ratio, r.height())
            g = QLinearGradient(fill.x(), 0, fill.right(), 0)
            g.setColorAt(0, self._c1)
            g.setColorAt(1, self._c2)
            p.setBrush(g)
            p.drawRoundedRect(fill, 4, 4)
            phase = (self._t / 2.2) % 1.0
            band_w = fill.width() * 0.35
            bx = fill.x() - band_w + (fill.width() + band_w) * phase
            p.setBrush(QColor(255, 255, 255, 80))
            p.drawRoundedRect(QRectF(bx, fill.y(), band_w, fill.height()), 4, 4)


class StarField(QWidget):
    """低透明度星点背景（固定种子、静态绘制，resize 自适应）。

    星点以相对坐标存储，亮星带十字光芒；卡片间隙透出星空质感。
    """

    def __init__(self, parent=None, count=80, seed=20261029):
        import random
        super().__init__(parent)
        rnd = random.Random(seed)
        self._stars = []
        for _ in range(count):
            self._stars.append((rnd.random(), rnd.random(),
                                rnd.choice((1.0, 1.0, 1.2, 1.8)),
                                rnd.randint(40, 140),
                                rnd.random() < 0.12))
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        # R18：星点静态 → 首次绘制缓存成位图，被父级失效反复触发时只 drawPixmap
        self._pix = None
        self._pix_sz = None

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._pix = None

    def paintEvent(self, ev):
        if self._pix is None or self._pix_sz != self.size():
            self._pix = self._build_pix()
            self._pix_sz = self.size()
        p = QPainter(self)
        if self._pix is not None:
            p.drawPixmap(0, 0, self._pix)
        p.end()

    def _build_pix(self):
        dpr = self.devicePixelRatioF() or 1.0
        cw, ch = self.width(), self.height()
        if cw <= 0 or ch <= 0:
            return None
        pix = QPixmap(int(cw * dpr), int(ch * dpr))
        pix.setDevicePixelRatio(dpr)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)
        cw, ch = self.width(), self.height()
        # R026（领导）：星点四周留边 + 边缘 36px 内线性衰减，消除贴边被裁的半颗星
        mx, my = 14.0, 14.0
        w = max(1.0, cw - 2 * mx)
        h = max(1.0, ch - 2 * my)
        for rx, ry, s, a, bright in self._stars:
            x = mx + rx * w
            y = my + ry * h
            _d = min(x, y, cw - x, ch - y)
            _fade = max(0.0, min(1.0, _d / 36.0))
            a = int(a * _fade)
            if a <= 0:
                continue
            col = QColor(200, 225, 255, a)
            if bright:
                p.setPen(QPen(QColor(190, 220, 255, min(a + 60, 225)), 1))
                p.drawLine(QPointF(x - s * 2.4, y), QPointF(x + s * 2.4, y))
                p.drawLine(QPointF(x, y - s * 2.4), QPointF(x, y + s * 2.4))
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(x, y), s, s)
        p.end()
        return pix


class PowerScaleBar(QWidget):
    """顶部主卡的 0-120W 功率刻度条：填充 + 刻度文字 + 端头辉光（9:38 基线）。"""

    def __init__(self, max_w=120.0, parent=None):
        super().__init__(parent)
        self._max = float(max_w)
        self._val = 0.0
        self._disp = 0.0
        # R020：恢复网页 550ms 过渡 + 12px 轨道（原 8px 偏细）
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(550)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self._anim.finished.connect(self._on_anim_fin)
        self.setFixedHeight(34)
        # R22：抓底自绘——刻度条 550ms 过渡每帧只重画自身，不连带祖先链
        self._bg = None
        self._bg_lock = False
        self._cap_pending = False
        # R26-D：功率条过渡改挂 UiBeat（550ms OutCubic 由统一节拍推进，同拍 update() 合并）
        self._psb_run = False
        self._psb_ok = False
        self._psb_from = 0.0
        self._psb_t0 = 0.0

    def showEvent(self, ev):
        super().showEvent(ev)
        if self._bg is not None or self._cap_pending:
            return
        self._cap_pending = True

        def _once():
            self._cap_pending = False
            if self._bg is None:
                self._capture_bg()
        QTimer.singleShot(120, _once)

    def _capture_bg(self, _try=0):
        if self._bg_lock or not self.isVisible():
            return
        if not _chain_settled(self):
            if _try < 15:
                QTimer.singleShot(220, lambda: self._capture_bg(_try + 1))
            return
        self._bg_lock = True
        try:
            from PySide6.QtCore import QPoint, QRect
            self.setAttribute(Qt.WA_OpaquePaintEvent, False)
            self.repaint()
            tl = self.window()
            _pm = tl.grab(QRect(self.mapTo(tl, QPoint(0, 0)), self.size()))
            _im = _pm.toImage()
            _c = _im.pixelColor(_im.width() // 2, _im.height() // 2)
            if (_c.red() + _c.green() + _c.blue()) < 12 and _try < 5:
                QTimer.singleShot(120, lambda: self._capture_bg(_try + 1))
                return
            self._bg = _pm
        except Exception as _exc:
            self._bg = None
            ck_swallow(_exc, 'launcher:1538')
        finally:
            self._bg_lock = False
        if self._bg is None:
            return
        # R030：隐藏页首次抓底可能抓到未绘制的白底，会留下白方块。
        # 采样三点，接近纯白即丢弃并限次重试（页面真正绘出后再自绘）。
        try:
            _im = self._bg.toImage()
            _w = max(1, self.width() - 3)
            _h = max(1, self.height() - 3)
            _white = 0
            for _x, _y in ((2, 2), (_w // 2, _h // 2), (_w, _h)):
                _c = _im.pixelColor(_x, _y)
                if _c.red() > 235 and _c.green() > 235 and _c.blue() > 235 and _c.alpha() > 200:
                    _white += 1
            if _white >= 2:
                self._bg = None
                self._retry = getattr(self, '_retry', 0) + 1
                if self._retry <= 6 and self.isVisible():
                    QTimer.singleShot(300, self._capture_bg)
                return
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:bgcapture')
        self._retry = 0
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.update()

    def _repaint_span(self, prev):
        """C22：550ms 过渡只重画「填充段 + 端头辉光」矩形，右半轨与刻度不动。
        填充恒从 x=0 起，取新旧两值的较大跨度为脏区；观感与整条重画逐像素一致。"""
        _w = self.width()
        if _w <= 0 or self._bg is None:
            self.update()
            return
        _r = max(float(self._disp), float(prev or 0.0)) / (self._max or 1.0)
        _x = min(_w, int(_w * _r) + 22)   # 端头辉光半径 16 + 抗锯齿余量
        self.update(QRect(0, 0, max(1, _x), self.height()))

    def _on_anim(self, v):
        _prev = self._disp
        try:
            self._disp = float(v)
        except Exception:
            self._disp = 0.0
        _now = time.monotonic()
        # R26：不聚焦档 500 ms 一帧（550 ms 过渡只留 1–2 帧，用户看不到）
        _iv = 0.50 if _CK_DEG['on'] else 0.15
        if (_now - getattr(self, '_pupt', 0.0) < _iv
                and self._anim.state() == QAbstractAnimation.Running):
            return   # R25 限帧：550ms 过渡约 4 帧（原 8fps），终值由 finished 兜底
        self._pupt = _now
        self._repaint_span(_prev)

    def _on_anim_fin(self):
        _prev = self._disp
        self._disp = self._val
        self._repaint_span(_prev)

    def _psb_step(self):
        """UiBeat 每拍推进功率条过渡（OutCubic，550ms；150ms 拍 ≈ 4 帧，与 R25 限帧同观感）。"""
        _p = (time.monotonic() - self._psb_t0) / 0.550
        if _p >= 1.0:
            self._psb_run = self._psb_ok = False
            UiBeat.instance().remove(self._psb_step)
            self._on_anim_fin()
            return
        _e = 1.0 - (1.0 - _p) ** 3
        _prev = self._disp
        self._disp = self._psb_from + (self._val - self._psb_from) * _e
        self._repaint_span(_prev)

    def set_value(self, w):
        _v = max(0.0, min(self._max, float(w or 0)))
        if _CK_D_OFF['on']:
            self._set_value_anim(_v)
            return
        # R06 性能：抖动（<0.5 W）不重启过渡，避免空闲时每秒一段连续重绘
        if abs(_v - self._val) < 0.5 and not self._psb_run:
            self._val = _v
            if abs(_v - self._disp) > 0.01:
                _prev = self._disp
                self._disp = _v
                self._repaint_span(_prev)
            return
        self._val = _v
        _now = time.monotonic()
        if self._psb_run:
            # 过渡中新目标：从当前显示值重新起算（等价于旧路径的 stop+setStartValue）
            self._psb_from = self._disp
            self._psb_t0 = _now
            return
        self._psb_from = self._disp
        self._psb_t0 = _now
        self._psb_run = self._psb_ok = True
        UiBeat.instance().add(self._psb_step)
        self.update()

    def _set_value_anim(self, _v):
        # 旧路径（CK_D_OFF=1 的成对 A/B 对照用）：QVariantAnimation 60fps + 150ms update 限帧
        # R06 性能：抖动（<0.5 W）不重启 550ms 过渡，避免空闲时每秒一段连续重绘
        if (abs(_v - self._val) < 0.5
                and self._anim.state() == QAbstractAnimation.State.Stopped):
            self._val = _v
            if abs(_v - self._disp) > 0.01:
                self._disp = _v
                self.update()
            return
        self._val = _v
        self._anim.stop()
        self._anim.setStartValue(self._disp)
        self._anim.setEndValue(self._val)
        self._anim.start()

    def paintEvent(self, ev):
        if self._bg is None:
            return
        if not self.testAttribute(Qt.WA_OpaquePaintEvent):
            self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        p = QPainter(self)
        p.drawPixmap(0, 0, self._bg)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        track_h = 12.0
        ty = 2.0
        r = QRectF(0, ty, self.width(), track_h)
        # 轨道
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 22))
        p.drawRoundedRect(r, track_h / 2, track_h / 2)
        ratio = self._disp / self._max if self._max else 0
        fw = self.width() * ratio
        if fw > 1:
            fill = QRectF(0, ty, fw, track_h)
            g = QLinearGradient(0, 0, self.width(), 0)
            g.setColorAt(0, QColor(47, 182, 255))
            g.setColorAt(1, QColor(124, 77, 255))
            p.setBrush(g)
            p.drawRoundedRect(fill, track_h / 2, track_h / 2)
            # 端头辉光
            glow = QRadialGradient(fw, ty + track_h / 2, 16)
            glow.setColorAt(0, QColor(120, 200, 255, 200))
            glow.setColorAt(1, QColor(120, 200, 255, 0))
            p.setBrush(glow)
            p.drawEllipse(QPointF(fw, ty + track_h / 2), 16, 16)
        # R32：游标（0W 也可见，替代“只有空灰轨”的观感缺陷）
        _cx = max(7.0, min(float(self.width()) - 7.0, fw))
        p.setPen(QPen(QColor(56, 226, 255, 210), 1.5))
        p.setBrush(QColor(240, 252, 255, 235))
        p.drawEllipse(QPointF(_cx, ty + track_h / 2), 4.5, 4.5)
        # 刻度文字
        p.setPen(QColor(159, 180, 200))
        p.setFont(self.font())
        for i, frac in enumerate((0.0, 0.25, 0.5, 0.75, 1.0)):
            x = self.width() * frac
            label = '%d' % int(round(self._max * frac))
            if i == 0:
                p.drawText(QRectF(x, ty + track_h + 2, 40, 14),
                           Qt.AlignLeft, label)
            elif i == 4:
                p.drawText(QRectF(x - 40, ty + track_h + 2, 40, 14),
                           Qt.AlignRight, label)
            else:
                p.drawText(QRectF(x - 20, ty + track_h + 2, 40, 14),
                           Qt.AlignCenter, label)


class ToastManager(QObject):
    """Toast 提示（对应网页 toastIn：下方弹入，自动消失）。"""

    def __init__(self, parent):
        super().__init__(parent)
        self._parent = parent
        self._items = []

    def show(self, text, kind='info', duration=2600):
        # R024：免打扰真实生效（屏蔽所有 Toast，只留事件日志）
        _np = getattr(self._parent, '_notify_prefs', None) or {}
        if _np.get('dnd'):
            return
        lbl = QLabel(text, self._parent)
        colors = {'info': (TEAL, '#dff1ff'), 'ok': ('#389e3d', '#e3f5e4'),
                  'err': (ERR_SOFT, '#fde7e5'), 'warn': (WARN_BRIGHT, '#fdf0dd')}
        bg, fg = colors.get(kind, colors['info'])
        lbl.setStyleSheet(
            'background:rgba(26,36,54,0.92);color:%s;border:1px solid %s;'
            'border-radius:12px;padding:9px 18px;font-size:13px;'
            'border-left:4px solid %s;' % (fg, bg, bg))
        lbl.setMinimumWidth(220)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.adjustSize()
        lbl.show()
        self._items.append(lbl)
        self._reposition()
        eff = QGraphicsOpacityEffect(lbl)
        eff.setOpacity(0)
        lbl.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b'opacity', lbl)
        anim.setStartValue(0)
        anim.setEndValue(1)
        anim.setDuration(300)
        anim.setEasingCurve(QEasingCurve.OutCubic)

        def add_shadow():
            if lbl.graphicsEffect() is eff:
                sh = QGraphicsDropShadowEffect(lbl)
                sh.setBlurRadius(22)
                sh.setOffset(0, 5)
                sh.setColor(QColor(0, 0, 0, 150))
                lbl.setGraphicsEffect(sh)

        anim.finished.connect(add_shadow)
        anim.start()
        lbl._in_anim = anim
        lbl._in_eff = eff
        QTimer.singleShot(duration, lambda: self._dismiss(lbl))

    def show_timed(self, text, sub='', kind='info', duration=3200):
        """兼容 (text, sub, kind) 调用形态（CK-22：自动化失败提示此前静默）。"""
        self.show(text, kind, duration)

    def _reposition(self):
        pw = self._parent.width()
        ph = self._parent.height()
        for i, lbl in enumerate(self._items):
            # 1.5.5：上移到分页条之上，避免浮层压住「共 N 条 / 上一页」这行文案
            lbl.move((pw - lbl.width()) // 2, ph - 122 - i * 48)

    def _dismiss(self, lbl):
        if lbl not in self._items:
            return
        eff = lbl.graphicsEffect()
        if not isinstance(eff, QGraphicsOpacityEffect):
            eff = QGraphicsOpacityEffect(lbl)
            lbl.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b'opacity', lbl)
        anim.setStartValue(1)
        anim.setEndValue(0)
        anim.setDuration(280)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.start()
        lbl._dismiss_anim = anim

        def done():
            if lbl in self._items:
                self._items.remove(lbl)
            lbl.hide()
            lbl.deleteLater()  # R020/CK-M3：滑出后销毁，避免控件与阴影无限累积
            self._reposition()
        anim.finished.connect(done)


class SpinnerWidget(QWidget):
    """连接中旋转图标（CK-7）：旋转的圆弧，约 1.1s 一圈。"""

    def __init__(self, color=WARN, size=18, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._angle = 0
        self.setFixedSize(size, size)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0)
        self._anim.setEndValue(360)
        self._anim.setDuration(1100)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_angle)
        if _ck_anim_on('spin'):
            self._anim.start()

    def _on_angle(self, v):
        self._angle = int(v)
        self.update()

    # G5：隐藏页动画按可见性启停，控件隐藏后无 tick 进入事件循环
    # 白窗根因修复：show/hide 中的动画异常不得打断绘制链（此前
    # QVariantAnimation.isActive 触发 AttributeError → 启动首帧整窗白底）
    def showEvent(self, ev):
        try:
            if _ck_anim_on('spin') and self._anim.state() != self._anim.State.Running:
                self._anim.start()
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:1789')
        super().showEvent(ev)

    def hideEvent(self, ev):
        try:
            self._anim.stop()
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:1796')
        super().hideEvent(ev)

    def set_color(self, color):
        self._color = QColor(color)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.width() / 2 - 2
        pen = QPen(QColor(self._color.red(), self._color.green(),
                          self._color.blue(), 60), 2.2)
        p.setPen(pen)
        p.drawArc(self.rect().adjusted(2, 2, -2, -2), 0, 360 * 16)
        pen2 = QPen(self._color, 2.2, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen2)
        p.drawArc(self.rect().adjusted(2, 2, -2, -2),
                  -self._angle * 16, 110 * 16)


class ChargerDevice(QWidget):
    """充电器设备可视化：照搬网页 updateDeviceContainer() 三态——
    工作中（任一启用端口 power>0）＝暗色充电器整图 + accent 呼吸辉光 + usb-overlay
    功率行 + 顶部场景角标；已连接待机 / 未连接 ＝ unconnected 产品图（网页同款
    「另一种图标」）。
    """

    SCENE_META = {1: ('ai', 'AI模式'), 2: ('apple', '数码生态'),
                  3: ('single', '单口模式'), 4: ('balance', '均衡模式')}
    # 网页 usb-overlay 行位（相对整图高）：top 25/35/45/55%
    # R08：行高由 14% 收到 10%。原 14% 时第 1 行(25%~39%)与第 2 行(35%~49%)天然重叠，
    # 两个端口同时工作时「≈0.1W」标签叠在一起（实拍可见），收到 10% 后四行互不相压。
    _ROW_TOP = {'1': 0.25, '2': 0.35, '3': 0.45, '4': 0.55}
    _ROW_H = 0.10
    _ROW_KIND = {'1': 'c', '2': 'c', '3': 'c', '4': 'a'}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img_dir = self._pick_img_dir()
        self._phase = 0.0
        self._connected = False
        self._working = False
        self._ports = {}
        self._scene = None
        # 预加载图片（不在 paintEvent 中做磁盘 IO）
        self._pm_unconn = self._load('main_card_ad1204u_unconnected.png')
        self._pm_dark = self._load('main_charger_dark_ad1204_all.png')
        self._usb_rect = {'c': self._load('main_card_usb_c_rectangle.png'),
                          'a': self._load('main_card_usb_a_rectangle.png')}
        self._picon = self._load('main_charger_dark_power_on.png')
        self._scene_icons = {k: self._load('main_card_scene_icon_%s.png' % k)
                             for k in ('ai', 'apple', 'single', 'balance')}
        self.setMinimumSize(120, 140)
        self._val_font = QFont(self.font())
        self._val_font.setPointSize(7)
        self._val_font.setBold(True)
        self._tag_font = QFont(self.font())
        self._tag_font.setPointSize(7)
        self._scaled = {}  # 缩放后图片缓存，resize 时重建（避免每帧缩放）
        # R007（CK-05）：浮动仅 ±2px，QVariantAnimation 60fps 全件重绘过重；
        # 改 33ms（≈30fps）定时器推进相位，画面顺滑度不减，重绘负载减半。
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(_ck_iv(50))   # R06 性能：20fps 足够顺滑，重绘负载再降 1/3
        self._anim_timer.timeout.connect(self._on_tick)
        self._anim_ticks = 0
        if self.isVisible():
            _ck_start(self._anim_timer, 'anim')

    def _on_tick(self):
        self._phase = (self._phase + 0.0691) % (2 * math.pi)  # 3s 周期
        self._anim_ticks += 1
        # R06 性能/动效克制：入场 8s 后静止（装饰性浮动不长期占用主线程）
        if self._anim_ticks > 160:
            self._anim_timer.stop()
            self.update()
            return
        if self.isVisible():
            # R25：产品图浮动是纯装饰，隔拍重绘（20→10 次/s）
            self._rd = (getattr(self, '_rd', 0) + 1) % 2
            if self._rd == 0:
                self.update()

    def _on_phase(self, v):
        self._phase = float(v)
        if self.isVisible():
            self.update()

    def showEvent(self, ev):
        self._anim_ticks = 0
        if not self._anim_timer.isActive():
            _ck_start(self._anim_timer, 'anim')
        super().showEvent(ev)

    def hideEvent(self, ev):
        self._anim_timer.stop()
        super().hideEvent(ev)

    def resizeEvent(self, ev):
        self._scaled = {}
        super().resizeEvent(ev)

    def _pick_img_dir(self):
        """R32：设备图资源目录解析（源码/冻结态/旧归档三路），首个存在的目录胜出。
        冻结态曾因只认 project_dir()/web/static/plugin_imgs 而整块静默丢失。"""
        here = os.path.dirname(os.path.abspath(__file__))
        cands = []
        if getattr(sys, 'frozen', False):
            base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
            cands.append(os.path.join(base, 'assets', 'plugin_imgs'))
            cands.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                      'assets', 'plugin_imgs'))
        cands.append(os.path.join(here, 'assets', 'plugin_imgs'))
        try:
            cands.append(os.path.join(project_dir(), 'web', 'static', 'plugin_imgs'))
        except Exception as _exc:
            ck_swallow(_exc, 'charger:project_dir')
        for c in cands:
            if os.path.isdir(c):
                return c
        ck_swallow(FileNotFoundError('plugin_imgs 目录不存在: %s' % ' | '.join(cands)),
                   'charger:img_dir')
        return cands[0]

    def _load(self, name):
        path = os.path.join(self._img_dir, name)
        pm = QPixmap(path)
        if pm.isNull():
            # R32：静默失败 → errs 点名缺哪个文件（与 aiohttp 漏包同类坑）
            ck_swallow(FileNotFoundError(path), 'charger:img:%s' % name)
        return pm

    def _scaled_pm(self, key, pm, w, h):
        """按当前尺寸缓存缩放后的图片（平滑变换）。"""
        if key not in self._scaled or self._scaled[key][0].isNull():
            sc = min(w / pm.width(), (h - 10) / pm.height())
            dw, dh = pm.width() * sc, pm.height() * sc
            self._scaled[key] = (pm.scaled(
                int(max(1, dw)), int(max(1, dh)),
                Qt.KeepAspectRatio, Qt.SmoothTransformation), dw, dh)
        return self._scaled[key]

    def set_state(self, connected, ports, scene=None):
        self._connected = bool(connected)
        self._ports = ports or {}
        # C10/R014：场景角标（图标 + 场景名，仅已连接显示）
        self._scene = None
        try:
            if self._connected and scene is not None:
                self._scene = self.SCENE_META.get(int(scene))
        except Exception:
            self._scene = None
        # 网页口径：任一启用端口 power>0 即工作中；否则待机 → unconnected 产品图
        self._working = any(
            p.get('enabled') is not False and float(p.get('power') or 0) > 0
            for p in self._ports.values())
        # CK-P3-03：工作中 30fps；未连接/待机降到 20fps，降低 UI 线程争用
        self._anim_timer.setInterval(_ck_iv(33 if self._working else 50))
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()

        # deviceFloat：相位即 3s 周期，上下浮动 2px（浮点，亚像素平滑）
        # R023：减少动效/降级时取消浮动，呼吸光改为恒定弱光（省 CPU）
        float_y = 0 if getattr(self, 'reduced', False) else -2 * (0.5 + 0.5 * math.sin(self._phase))

        # 待机 / 未连接：unconnected 产品图（网页 updateDeviceContainer else 分支）
        # R15-C11：待机分支不再画场景角标（网页口径——角标仅工作中显示，已裁定）
        if not (self._connected and self._working):
            pm = self._pm_unconn
            if not pm.isNull():
                spm, dw, dh = self._scaled_pm('u', pm, w, h)
                tgt = QRectF((w - dw) / 2, (h - dh) / 2 + float_y, dw, dh)
                p.drawPixmap(tgt, spm, QRectF(0, 0, spm.width(), spm.height()))
            return

        # 工作中：暗色充电器整图（保留左侧闪电装饰，与网页一致）+ accent 呼吸辉光
        pm = self._pm_dark
        if pm.isNull():
            return
        # R08：给底部角标预留 30px（角标高 20 + 间距 10），设备图略缩水 1~2px，
        # 换取角标不再压住设备本体且不被控件下边缘裁掉
        sc = min(w / pm.width(), (h - 30) / pm.height())
        dw, dh = pm.width() * sc, pm.height() * sc
        dx = (w - dw) / 2
        dy = 6 + (h - 30 - dh) / 2 + float_y
        breathe = 0.55 if getattr(self, 'reduced', False) else 0.5 + 0.5 * math.sin(self._phase * (3.0 / 1.6))
        glow = QRadialGradient(dx + dw / 2, dy + dh / 2, dh * 0.72)
        glow.setColorAt(0, QColor(3, 169, 244, int(55 + 60 * breathe)))
        glow.setColorAt(0.55, QColor(3, 169, 244, int(16 + 16 * breathe)))
        glow.setColorAt(1, QColor(3, 169, 244, 0))
        p.fillRect(QRectF(dx - 8, dy - 8, dw + 16, dh + 16), glow)
        p.drawPixmap(QRectF(dx, dy, dw, dh), pm, QRectF(0, 0, pm.width(), pm.height()))

        # usb-overlay：仅活动端口显示行（top 25/35/45/55%，网页同位同尺寸）
        s15 = dw / 150.0  # 网页 device-wrap 基准宽 150px
        for pk, rt in self._ROW_TOP.items():
            info = self._ports.get(pk) or {}
            if not (info.get('enabled') is not False and float(info.get('power') or 0) > 0):
                continue
            row = QRectF(dx, dy + dh * rt, dw, dh * self._ROW_H)
            rect = self._usb_rect.get(self._ROW_KIND[pk])
            if rect is not None and not rect.isNull():
                bg_h = min(row.height(), 24 * s15)
                bg_y = row.top() + (row.height() - bg_h) / 2
                self._draw_contained(p, rect, QRectF(row.left() + row.width() * 0.43,
                                                     bg_y, row.width() * 0.57, bg_h))
            if self._picon is not None and not self._picon.isNull():
                ic_h = min(12 * s15, row.height())
                self._draw_contained(p, self._picon,
                                     QRectF(row.left() + row.width() * 0.72,
                                            row.top() + (row.height() - ic_h) / 2,
                                            10 * s15, ic_h))
            # C19-B3：标签加暗底承托，避免 7pt 文字直接压在产品图线条上
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(6, 14, 30, 150))
            p.drawRoundedRect(QRectF(self.width() - 46, row.top() + 1,
                                     44, max(1.0, row.height() - 2)), 5, 5)
            p.setPen(QColor('#64B5F6'))
            p.setFont(self._val_font)
            # R10：原文本区为设备图 0.78~1.0（22% 宽）且 AlignCenter，实测 900 视口
            # widget 仅 88px 宽，文字（约 24px）居中后右半越界被控件裁掉，
            # 「14.6W」显示成「4.6W」（1000 视口 widget 100px，裁 1~2px 不易察觉）。
            # 改为「右缘锚定 + 固定 42px 可绘区」右对齐：双视口都完整，位置与旧版
            # 仅差数 px，不压右侧总功率大数字；不做字体测量，无逐帧额外开销。
            p.drawText(QRectF(self.width() - 44, row.top(), 42, row.height()),
                       Qt.AlignRight | Qt.AlignVCenter,
                       '%.1fW' % float(info.get('power') or 0))
        # R08：角标移到设备图正下方居中（原左下角定位会横跨设备本体，实拍读成「AI AI模式」）
        self._draw_scene_badge(p, dy + dh + 4)

    def _draw_contained(self, p, pm, box):
        """object-fit:contain 等比绘制（网页 usb-pbg/picon 的 object-fit）。"""
        if pm.isNull() or box.width() <= 0 or box.height() <= 0:
            return
        ar = pm.width() / float(pm.height())
        bw, bh = box.width(), box.height()
        if bw / bh > ar:
            dw2 = bh * ar
            tgt = QRectF(box.left() + (bw - dw2) / 2, box.top(), dw2, bh)
        else:
            dh2 = bw / ar
            tgt = QRectF(box.left(), box.top() + (bh - dh2) / 2, bw, dh2)
        p.drawPixmap(tgt, pm, QRectF(0, 0, pm.width(), pm.height()))

    def _draw_scene_badge(self, p, top):
        """设备图正下方的居中角标：胶囊底 + 图标 + 场景名。

        R08：① 定位改为底部居中，不再横跨设备插画；② 'ai' 场景的位图本身就是
        AI 字样，再配文字「AI模式」会读成「AI AI模式」，该场景只画文字。
        """
        if not self._scene:
            return
        key, text = self._scene
        p.setFont(self._tag_font)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        icon_pm = self._scene_icons.get(key)
        use_icon = key != 'ai' and icon_pm is not None and not icon_pm.isNull()
        ic, gap = (14.0, 3.0) if use_icon else (0.0, 0.0)
        pill_w = ic + gap + tw + 12
        px0 = max(1.0, (self.width() - pill_w) / 2.0)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(8, 18, 32, 168))
        p.drawRoundedRect(QRectF(px0, top, pill_w, 20), 6, 6)
        if use_icon:
            p.drawPixmap(QRectF(px0 + 6, top + 3, ic, ic), icon_pm,
                         QRectF(0, 0, icon_pm.width(), icon_pm.height()))
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(px0 + 6 + ic + gap, top, tw + 2, 20),
                   Qt.AlignLeft | Qt.AlignVCenter, text)



class BoxConsDialog(QDialog):
    """单箱 10 维人为区间：每维可单独启停，带来源徽标（查得/推定/人工/自动）与实测 min/中位/最大。"""

    _SRC = {'spec': ('查得', '#34d399'), 'est': ('推定', '#fbbf24'),
            'user': ('人工', '#4ea8ff')}

    def __init__(self, parent, prefs, box, rows, on_saved=None):
        super().__init__(parent)
        self.p = prefs
        self.box = dict(box or {})
        self.rows = rows or []
        self.on_saved = on_saved
        self._old = dict((self.box.get('cons') or {}))
        self.setWindowTitle('维度约束 · %s' % ux.device_box_name(
            self.box.get('id'), self.box.get('name')))
        self.resize(940, 560)
        self._build()
        self._load()

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(8)
        _hint = QLabel('勾选=启用该维硬约束，区间外的会话不会归入本箱；留空/关闭=回退自动中心。'
                       '徽标：查得=官方规格、推定=按规格与经验估算、人工=你改过的值。')
        _hint.setObjectName('sub')
        _hint.setWordWrap(True)
        v.addWidget(_hint)
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(6)
        self._ctl = {}
        for i, (key, label, kind, unit) in enumerate(ux.BOX_CONS_DIMS):
            r, c = i % 5, (i // 5) * 5
            on = QCheckBox(label)
            on.setMinimumWidth(96)
            g.addWidget(on, r, c)
            if kind == 'num':
                lo, hi = QDoubleSpinBox(), QDoubleSpinBox()
                for s in (lo, hi):
                    s.setRange(-1e6, 1e6)
                    s.setDecimals(2)
                    s.setFixedWidth(74)
                g.addWidget(lo, r, c + 1)
                g.addWidget(hi, r, c + 2)
                self._ctl[key] = ('num', on, lo, hi)
            elif kind == 'cat':
                ed = QLineEdit()
                ed.setPlaceholderText('如 PD,PPS；空=不限' if key == 'protocol'
                                      else '如 C1,C2；空=不限')
                ed.setMinimumWidth(120)
                g.addWidget(ed, r, c + 1, 1, 2)
                self._ctl[key] = ('cat', on, ed, None)
            else:
                lo, hi = QSpinBox(), QSpinBox()
                for s in (lo, hi):
                    s.setRange(0, 23)
                    s.setFixedWidth(74)
                g.addWidget(lo, r, c + 1)
                g.addWidget(hi, r, c + 2)
                self._ctl[key] = ('hour', on, lo, hi)
            _badge = QLabel('')
            _badge.setFixedWidth(36)
            _badge.setStyleSheet('font-size:11px; color:%s; background:transparent;'
                                 % TEXT_SUB)
            g.addWidget(_badge, r, c + 3)
            _mea = QLabel('')
            _mea.setMinimumWidth(96)
            _mea.setStyleSheet('font-size:11px; color:%s; background:transparent;'
                               % TEXT_SUB)
            g.addWidget(_mea, r, c + 4)
            self._ctl[key] = self._ctl[key] + (_badge, _mea)
        g.setColumnStretch(4, 1)
        g.setColumnStretch(9, 1)
        v.addLayout(g)
        _legend = QLabel('实测列为该组当前成员的最小/中位/最大；区间明显窄于实测分布时请复核。')
        _legend.setObjectName('sub')
        _legend.setWordWrap(True)
        v.addWidget(_legend)
        v.addStretch(1)
        ft = QHBoxLayout()
        b_auto = QPushButton('恢复自动')
        b_auto.setObjectName('ghost')
        b_auto.clicked.connect(self._reset)
        ft.addWidget(b_auto)
        ft.addStretch(1)
        b_close = QPushButton('取消')
        b_close.setObjectName('ghost')
        b_close.clicked.connect(self.reject)
        b_save = QPushButton('保存并生效')
        b_save.clicked.connect(self._save)
        for _b in (b_close, b_save):
            _b.setMinimumWidth(108)
            ft.addWidget(_b)
        v.addLayout(ft)

    def _load(self):
        cons = (self.box.get('cons') or {})
        for key, _spec in self._ctl.items():
            kind, on = _spec[0], _spec[1]
            c = cons.get(key) or {}
            on.setChecked(bool(c.get('on')))
            if kind == 'num':
                _spec[2].setValue(float(c.get('min') or 0))
                _spec[3].setValue(float(c.get('max') or c.get('min') or 0))
            elif kind == 'cat':
                _spec[2].setText(','.join(c.get('vals') or []))
            else:
                _spec[2].setValue(int(c.get('from') or 0))
                _spec[3].setValue(int(c.get('to') or 0))
            _src = str(c.get('src') or '')
            if on.isChecked() and _src in self._SRC:
                _t, _c = self._SRC[_src]
                _spec[4].setText(_t)
                _spec[4].setStyleSheet('font-size:11px; color:%s; background:transparent;'
                                       % _c)
                _spec[4].setToolTip(str(c.get('note') or ''))
            else:
                _spec[4].setText('自动' if not on.isChecked() else '')
                _spec[4].setStyleSheet('font-size:11px; color:%s; background:transparent;'
                                       % TEXT_SUB)
                _spec[4].setToolTip('')
            _d = ux.box_dim_dist(self.rows, key)
            if _d and kind == 'cat':
                # 常用口是分类维度，实测值是端口名而不是数字
                _spec[5].setText('/'.join(str(x) for x in _d if str(x)))
            elif _d and kind == 'hour':
                _spec[5].setText('%d/%d/%d' % tuple(int(float(x)) for x in _d))
            elif _d:
                _spec[5].setText('%.1f/%.1f/%.1f' % tuple(float(x) for x in _d))
            elif kind == 'cat':
                _ports = sorted({str(s.get('port') or '-') for s in self.rows})
                _spec[5].setText('/'.join(_ports) or '—')
            else:
                _spec[5].setText('—')

    def _value(self, key):
        _spec = self._ctl[key]
        kind = _spec[0]
        if kind == 'num':
            _a, _b = _spec[2].value(), _spec[3].value()
            return {'on': True, 'min': min(_a, _b), 'max': max(_a, _b)}
        if kind == 'cat':
            _vals = [x.strip() for x in _spec[2].text().split(',') if x.strip()]
            return {'on': True, 'vals': _vals} if _vals else None
        return {'on': True, 'from': _spec[2].value(), 'to': _spec[3].value()}

    def _save(self):
        cons = {}
        for key, _spec in self._ctl.items():
            if not _spec[1].isChecked():
                continue
            val = self._value(key)
            if not val:
                continue
            _o = self._old.get(key) or {}
            _same = all(val.get(k) == _o.get(k) for k in val)
            val['src'] = str(_o.get('src') or 'user') if _same else 'user'
            if _same and _o.get('note'):
                val['note'] = _o['note']
            cons[key] = val
        self._commit(cons)

    def _reset(self):
        self._commit(None)

    def _commit(self, cons):
        r = ux.group_mutate(self.p, 'cons', bid=str(self.box.get('id') or ''),
                            cons=cons)
        if not r.get('ok'):
            QMessageBox.warning(self, '未保存', r.get('msg') or '约束保存失败')
            return
        if callable(self.on_saved):
            try:
                self.on_saved()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:boxcons')
        self.accept()


class _ElideLabel(QLabel):
    """固定行数、按宽度省略的标签：内容变化不改几何，悬浮显示全文。"""

    def __init__(self, text='', lines=1, parent=None):
        super().__init__(parent)
        self._lines = max(1, int(lines))
        self._full = ''
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._sync_h()
        self.set_full(text)

    def _sync_h(self):
        fm = self.fontMetrics()
        self.setFixedHeight(fm.lineSpacing() * self._lines + 2)

    def set_font_px(self, px, bold=False):
        f = self.font()
        f.setPixelSize(int(px))
        f.setBold(bool(bold))
        self.setFont(f)
        self._sync_h()
        self._apply()

    def set_full(self, text):
        self._full = str(text or '')
        self.setToolTip(self._full.replace('\n', ' · '))
        self._apply()

    def setText(self, text):        # 统一走省略渲染，避免绕过 tooltip
        self.set_full(text)

    def full_text(self):
        return self._full

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._apply()

    def _apply(self):
        fm = self.fontMetrics()
        w = max(0, self.width() - 2)
        parts = self._full.split('\n')
        _more = len(parts) > self._lines
        parts = parts[:self._lines] + [''] * max(0, self._lines - len(parts))
        out = [fm.elidedText(x, Qt.ElideRight, w) if w > 8 else x for x in parts]
        if _more:
            out[-1] = fm.elidedText(out[-1] + ' …', Qt.ElideRight, w)
        QLabel.setText(self, '\n'.join(out))


_ElideRow = _ElideLabel   # build_ui 作用域里 _ElideLabel 被小源输入提示的局部类遮住，这里留别名


class _FitWrapLabel(QLabel):
    """固定行数、按宽度自动缩字号保证全文显示（不出现省略号）。

    高度只由 lines 决定：数据/文案变化不改几何；字号在 [min_px, px] 内收缩。
    用于设备识别行、分组卡标题这类「必须看全、又不能撑高」的文字。
    """

    def __init__(self, text='', lines=1, px=13, min_px=9, bold=False, parent=None):
        super().__init__(parent)
        self._lines = max(1, int(lines))
        self._px = int(px)
        self._min_px = min(int(min_px), self._px)
        self._bold = bool(bold)
        self._full = ''
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._sync_h()
        self.setText(text)

    def _sync_h(self):
        f = QFont(self.font())
        f.setPixelSize(self._px)
        f.setBold(self._bold)
        self.setFont(f)
        self.setFixedHeight(QFontMetrics(f).lineSpacing() * self._lines + 2)

    def setText(self, text):            # noqa: N802 - 统一走缩字渲染
        self._full = str(text or '')
        self.setToolTip(self._full)
        self._apply()

    def _apply(self):
        w = max(0, self.width() - 2)
        if w <= 8:
            QLabel.setText(self, self._full)
            return
        f = QFont(self.font())
        f.setBold(self._bold)
        px = self._px
        while px > self._min_px:
            f.setPixelSize(px)
            fm = QFontMetrics(f)
            if fm.boundingRect(QRect(0, 0, w, 4000), Qt.TextWordWrap,
                               self._full).height() <= fm.lineSpacing() * self._lines + 1:
                break
            px -= 1
        f.setPixelSize(px)
        self.setFont(f)
        QLabel.setText(self, self._full)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._apply()


class _DevRow(QWidget):
    """智能分析「设备识别」的一行：左名称（缩字看全）+ 右次数（固定宽）。

    行高固定 1 行，0 条/满载/超长名都不改卡片几何；不使用省略号。
    """

    W_META = 48

    def __init__(self, px=13, parent=None):
        super().__init__(parent)
        _h = QHBoxLayout(self)
        _h.setContentsMargins(0, 0, 0, 0)
        _h.setSpacing(8)
        self.name = _FitWrapLabel('', 1, px, 9)
        self.meta = QLabel('')
        self.meta.setObjectName('sub')
        self.meta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.meta.setFixedWidth(self.W_META)
        self.meta.setFixedHeight(self.name.height())
        _h.addWidget(self.name, 1)
        _h.addWidget(self.meta)
        self.setFixedHeight(self.name.height())

    def set(self, name, meta):
        self.name.setText(name)
        self.meta.setText(str(meta or ''))


def _mask_secret(_k):
    """API Key 只回显掩码：sk-****abcd。"""
    _k = str(_k or '')
    return ('%s****%s' % (_k[:3], _k[-4:])) if len(_k) > 8 else '****'


class DevicePickDialog(QDialog):
    """选择设备：带标题栏与边界的独立选择窗（不是下拉浮层，不压同页卡片）。"""

    def __init__(self, parent, items, cur=''):
        super().__init__(parent)
        self.setWindowTitle('选择设备')
        self.setModal(True)
        _v = QVBoxLayout(self)
        _v.setContentsMargins(16, 14, 16, 14)
        _v.setSpacing(10)
        _t = QLabel('共 %d 台设备；固定组即使 0 条也在此列出。' % len(items))
        _t.setObjectName('sub')
        _t.setWordWrap(True)
        _v.addWidget(_t)
        self._keys = [str(k) for k, _x in items]
        self._lw = QListWidget()
        for _k, _x in items:
            self._lw.addItem(QListWidgetItem(_x))
        if cur in self._keys:
            self._lw.setCurrentRow(self._keys.index(cur))
        self._lw.itemDoubleClicked.connect(lambda _i: self.accept())
        _v.addWidget(self._lw, 1)
        _hb = QHBoxLayout()
        _hb.addStretch(1)
        _b0 = aligned_button('取消', 'ghost', min_w=88, h=30)
        _b1 = aligned_button('确定', 'primary', min_w=88, h=30)
        _b0.clicked.connect(self.reject)
        _b1.clicked.connect(self.accept)
        _hb.addWidget(_b0)
        _hb.addWidget(_b1)
        _v.addLayout(_hb)

    def key(self):
        _i = self._lw.currentRow()
        return self._keys[_i] if 0 <= _i < len(self._keys) else ''


class _SegChoice(QWidget):
    """页内三段等宽选择条（替代 QComboBox 下拉）。

    1.5.8：下拉候选是顶层浮层，展开会压住同页卡片；这里改为页内常驻分段，
    高度/圆角/配色与主页面 ghost 按钮一致，几何固定不随文案变化。
    """

    currentIndexChanged = Signal(int)

    def __init__(self, labels, seg_w=56, h=32, parent=None):
        super().__init__(parent)
        self._labels = [str(x) for x in labels]
        self._h = int(h)
        _h = QHBoxLayout(self)
        _h.setContentsMargins(0, 0, 0, 0)
        _h.setSpacing(4)
        self._grp = QButtonGroup(self)
        self._grp.setExclusive(True)
        self._btns = []
        for _i, _t in enumerate(self._labels):
            _b = QPushButton(_t)
            _b.setCheckable(True)
            _b.setFixedSize(int(seg_w), self._h)
            _b.setCursor(Qt.PointingHandCursor)
            _b.setFocusPolicy(Qt.NoFocus)
            self._grp.addButton(_b, _i)
            _h.addWidget(_b)
            self._btns.append(_b)
        self._btns[0].setChecked(True)
        self.setFixedSize(int(seg_w) * len(self._labels) + 4 * (len(self._labels) - 1),
                          self._h)
        self.setStyleSheet(
            'QPushButton{background:rgba(6,14,30,.42); color:#9db4c9;'
            ' border:1px solid rgba(120,190,255,.18); border-radius:8px;'
            ' font-size:12px; padding:0;}'
            'QPushButton:hover{background:rgba(56,226,255,.12); color:#ffffff;}'
            'QPushButton:checked{background:rgba(56,226,255,.20); color:#eaf7ff;'
            ' border-color:rgba(56,226,255,.75);}')
        self._grp.idClicked.connect(self._on_click)

    def _on_click(self, i):
        self.currentIndexChanged.emit(int(i))

    def currentIndex(self):
        for _i, _b in enumerate(self._btns):
            if _b.isChecked():
                return _i
        return 0

    def currentText(self):
        return self._labels[self.currentIndex()] if self._labels else ''

    def setCurrentIndex(self, i):
        _i = max(0, min(len(self._btns) - 1, int(i)))
        if not self._btns[_i].isChecked():
            self._btns[_i].setChecked(True)


class _DevPicker(QFrame):
    """页内设备选项条：‹ 名称 n/N › + 全部。

    不用 QComboBox 下拉：下拉是顶层浮层，展开时会压住同页卡片文字。
    名称固定宽度省略并悬浮全名，条高固定，内容/条数变化不改几何。
    """

    changed = Signal(str)

    W_NAME = 170
    W_TOTAL = 26 + 6 + 170 + 6 + 44 + 6 + 26 + 6 + 72

    def __init__(self, parent=None, h=32):
        super().__init__(parent)
        self.setObjectName('devPick')
        self._items = []
        self._cur = ''
        _h = QHBoxLayout(self)
        _h.setContentsMargins(0, 0, 0, 0)
        _h.setSpacing(6)
        self._prev = QPushButton('<')
        self._next = QPushButton('>')
        for _b in (self._prev, self._next):
            _b.setFixedSize(26, 26)
            _b.setCursor(Qt.PointingHandCursor)
            _b.setAutoDefault(False)
            _b.setFocusPolicy(Qt.NoFocus)
        self._name = _ElideLabel('', 1)
        self._name.setFixedWidth(self.W_NAME)
        self._name.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._name.setStyleSheet('color:#e6f0fa; font-size:12.5px; background:transparent;'
                                 ' padding:0 2px;')
        self._pos = QLabel('0/0')
        self._pos.setFixedWidth(44)
        self._pos.setAlignment(Qt.AlignCenter)
        self._pos.setObjectName('sub')
        self._more = QPushButton('设备列表')
        self._more.setFixedSize(72, 26)
        self._more.setAutoDefault(False)
        self._more.setCursor(Qt.PointingHandCursor)
        self._more.setFocusPolicy(Qt.NoFocus)
        _h.addWidget(self._prev)
        _h.addWidget(self._name)
        _h.addWidget(self._pos)
        _h.addWidget(self._next)
        _h.addWidget(self._more)
        self.setFixedSize(self.W_TOTAL, int(h))
        self.setStyleSheet(
            'QPushButton{background:rgba(6,14,30,.42); color:#cfe3f5;'
            ' border:1px solid rgba(120,190,255,.18); border-radius:8px;'
            ' font-size:12px; padding:0;}'
            'QPushButton:hover{background:rgba(56,226,255,.12); color:#ffffff;'
            ' border-color:rgba(56,226,255,.55);}'
            'QPushButton:pressed{background:rgba(56,226,255,.26);}'
            'QPushButton:disabled{color:#5b6b7d; background:rgba(255,255,255,.03);'
            ' border-color:rgba(255,255,255,.07);}'
            'QLabel#sub{color:#8fa6bd; font-size:11.5px; background:transparent;}')
        self._prev.clicked.connect(lambda _c=False: self._step(-1))
        self._next.clicked.connect(lambda _c=False: self._step(1))
        self._more.clicked.connect(lambda _c=False: self._pick())

    def set_items(self, items, cur=None):
        self._items = [(str(k), str(t)) for k, t in (items or [])]
        _keys = [k for k, _t in self._items]
        self._cur = str(cur) if str(cur) in _keys else (_keys[0] if _keys else '')
        self._render()
        self.setEnabled(bool(_keys))

    def current_key(self):
        return self._cur

    def set_current_key(self, key):
        if str(key) in [k for k, _t in self._items]:
            self._cur = str(key)
            self._render()

    def _render(self):
        _keys = [k for k, _t in self._items]
        _txt = dict(self._items)
        _n = len(_keys)
        _i = _keys.index(self._cur) if self._cur in _keys else -1
        self._name.set_full(_txt.get(self._cur, '') if _n else '暂无设备')
        self._pos.setText(('%d/%d' % (_i + 1, _n)) if _n else '0/0')
        self._prev.setEnabled(_i > 0)
        self._next.setEnabled(0 <= _i < _n - 1)
        self._more.setEnabled(_n > 0)

    def _step(self, _d):
        _keys = [k for k, _t in self._items]
        if not _keys:
            return
        _i = _keys.index(self._cur) if self._cur in _keys else 0
        _j = min(len(_keys) - 1, max(0, _i + _d))
        if _j != _i:
            self._cur = _keys[_j]
            self._render()
            self.changed.emit(self._cur)

    def _pick(self):
        _dlg = DevicePickDialog(self.window(), self._items, self._cur)
        _fit_dialog(_dlg, 440, 500, 380, 360)
        _k = ''
        if _dlg.exec() == QDialog.Accepted:
            _k = _dlg.key()
        _dlg.deleteLater()
        if _k and _k != self._cur:
            self._cur = _k
            self._render()
            self.changed.emit(self._cur)


class GroupSessionsPanel(QWidget):
    """组内会话（内嵌面板，禁止另开顶层窗口）：固定行高分页 + 多选 + 批量迁移。

    列表固定 PAGE 行高、分页条位置固定；0 条与 313 条、设备名超长都不改变几何。
    """

    PAGE = 8
    MIN_ROWS = 3
    MAX_ROWS = 20
    # 2.1.1：最小窗口（995）下右栏仅约 300px，七字段单行必然省略；
    # 窄窗改为两行行式（字号不变）：首行电量/端口/峰值/协议，次行时长/均压/均流。
    ROW_H = 26
    ROW_MAX = 40
    BATCH = 25
    BIG = 200

    def __init__(self, parent, prefs, bid, rows, groups, shapes, model=None,
                 title=None, on_done=None, thr=None, cons=True, compact=True):
        super().__init__(parent)
        # 2.1.1：窄窗（995）用两行行式，宽窗（≥1280）回到单行七字段。
        self.compact = bool(compact)
        self.ROW_H = 40 if self.compact else 26
        self.PAGE = 8 if self.compact else 11
        self.MIN_ROWS = 3 if self.compact else 4
        self.p = prefs
        self.bid = str(bid or '')
        self.rows = [r for r in (rows or []) if isinstance(r, dict)]
        self.groups = list(groups or [])
        self.shapes = shapes or {}
        self.model = model
        self.thr = thr
        self.cons = bool(cons)
        self.on_done = on_done
        self.sel = set()
        self.page = 0
        self._busy = False
        self._queue = []
        self._timer = None
        self._fail_msg = ''
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        _h = QHBoxLayout()
        _h.setSpacing(6)
        self._t = _ElideLabel(title or '组内会话', 1)
        self._t.set_font_px(14, True)
        self._t.setStyleSheet('color:#f3f7fc; background:transparent;')
        self._hint = QLabel('')
        self._hint.setObjectName('sub')
        self._hint.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        _h.addWidget(self._t, 1)
        _h.addWidget(self._hint, 1)
        v.addLayout(_h)
        _tl = QHBoxLayout()
        _tl.setSpacing(6)
        # 2.1.1：工具条按钮用 2 字标签 + tooltip，把右栏最小宽度压回内容所需，
        # 富余宽度让给左侧卡片区（全称保留在 tooltip 里）。
        self._b_cond = aligned_button('条件', 'ghost', min_w=64, h=26)
        self._b_cond.setToolTip('按条件选择会话')
        self._b_cond.clicked.connect(self._cond_select)
        self._b_pg_all = aligned_button('本页', 'ghost', min_w=64, h=26)
        self._b_pg_all.setToolTip('全选本页')
        self._b_pg_all.clicked.connect(self._all_page)
        self._b_all = aligned_button('全部', 'ghost', min_w=64, h=26)
        self._b_all.setToolTip('全选全部会话')
        self._b_all.clicked.connect(self._all_all)
        self._b_none = aligned_button('清空', 'ghost', min_w=64, h=26)
        self._b_none.setToolTip('清空选择')
        self._b_none.clicked.connect(self._none)
        for _w in (self._b_cond, self._b_pg_all, self._b_all, self._b_none):
            _tl.addWidget(_w)
        _tl.addStretch(1)
        v.addLayout(_tl)
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 2.1.1：列表高度按「当前页条数 + 1 行余量」收敛；只有排不下时才出现滚动条。
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.setTextElideMode(Qt.ElideRight)
        self._list.setWordWrap(True)
        self._list.setMinimumHeight(self.ROW_H * 2 + 6)
        self._list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._list.itemSelectionChanged.connect(self._sync_sel)
        v.addWidget(self._list, 0)
        self._room_rows = self.PAGE
        _pg = QHBoxLayout()
        _pg.setSpacing(6)
        self._pg_lb = QLabel('')
        self._pg_lb.setObjectName('sub')
        _bp = aligned_button('上一页', 'ghost', min_w=72, h=26)
        _bp.clicked.connect(lambda: self._flip(-1))
        _bn = aligned_button('下一页', 'ghost', min_w=72, h=26)
        _bn.clicked.connect(lambda: self._flip(1))
        _pg.addWidget(self._pg_lb)
        _pg.addStretch(1)
        _pg.addWidget(_bp)
        _pg.addWidget(_bn)
        v.addLayout(_pg)
        _op1 = QHBoxLayout()
        _op1.setSpacing(6)
        _op1.addWidget(QLabel('迁入到'))
        self._tgt = QComboBox()
        self._tgt.setMinimumWidth(110)
        self._tgt.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._tgt.setMinimumContentsLength(8)
        _op1.addWidget(self._tgt, 1)
        v.addLayout(_op1)
        _op2 = QHBoxLayout()
        _op2.setSpacing(6)
        self._b_move = aligned_button('迁移', 'primary', min_w=76, h=30)
        self._b_move.clicked.connect(self._migrate)
        self._b_out = aligned_button('移出到未归箱', 'ghost', min_w=112, h=30)
        self._b_out.clicked.connect(self._remove)
        self._b_exp = aligned_button('导出所选', 'ghost', min_w=86, h=30)
        self._b_exp.clicked.connect(self._export)
        for _w in (self._b_move, self._b_out, self._b_exp):
            _op2.addWidget(_w)
        _op2.addStretch(1)
        v.addLayout(_op2)
        self._msg = _ElideLabel('', 1)
        self._msg.setStyleSheet('color:%s; background:transparent; font-size:11px;'
                                % TEXT_SUB)
        v.addWidget(self._msg)
        self.set_groups(self.groups)
        self._render()

    # ---- 数据与渲染 ----
    def set_groups(self, groups):
        self.groups = list(groups or [])
        _cur = self._tgt.currentData()
        self._tgt.clear()
        for _g in self.groups:
            _nm = ('　↳ ' if _g.get('parent') else '') + str(_g.get('name') or '')
            self._tgt.addItem(_nm, str(_g.get('id') or ''))
        if _cur:
            _i = self._tgt.findData(_cur)
            if _i >= 0:
                self._tgt.setCurrentIndex(_i)

    def load(self, bid, rows, title=None):
        # 同一分组刷新时保留上一次批量结果（失败条数必须让用户看见）；
        # 切到别的分组/未归箱时才清空提示。
        _same = str(bid or '') == str(self.bid or '')
        self.bid = str(bid or '')
        self.rows = [r for r in (rows or []) if isinstance(r, dict)]
        self.page = 0
        self.sel = set()
        if not _same:
            self._msg.set_full('')
        if title is not None:
            self._t.set_full(title)
        self._render()

    def _page_size(self):
        """每页行数由主体可用高度决定（_fit_body 调 set_room），不随内容变化。"""
        return max(self.MIN_ROWS, min(int(self._room_rows), self.MAX_ROWS))

    def row_width_need(self, rows):
        """两行行式的最宽一行像素宽度（首行含协议短码）。"""
        _fm = self._list.fontMetrics()
        _w = 0
        for _s in (rows or [])[:400]:
            for _ln in self._row_txt(_s).split('\n'):
                _w = max(_w, _fm.horizontalAdvance(_ln))
        return int(_w)

    def chrome_h(self):
        """列表以外的固定高度（标题/工具条/分页/迁移/提示/间距）。"""
        if getattr(self, '_chrome', 0) >= 100:
            return self._chrome
        _si = int(self.sizeHint().height()) - int(self._list.height())
        if _si >= 100:
            self._chrome = _si
            return _si
        return 174

    def set_room(self, room_h):
        """按主体可用高度确定每页行数；返回后可用 content_need() 取自然高度。"""
        # 需要 room ≥ chrome + 16 + (行数+1)*ROW_H（最后一行是余量）
        _can = (int(room_h) - self.chrome_h() - 16) // self.ROW_H - 1
        _rows = max(self.MIN_ROWS, min(self.MAX_ROWS, _can))
        if _rows != self._room_rows:
            self._room_rows = _rows
            self._render()

    def content_need(self):
        """当前页条数的自然高度：chrome + (行数 + 1 行余量)。"""
        _n = min(len(self.rows), self._page_size())
        return self.chrome_h() + 16 + max(2, _n + 1) * self.ROW_H

    def apply_height(self, h):
        """面板被固定到 h（含右栏 8+8 边距）后，把余量摊到行高（上限 40）。"""
        _h = max(3 * self.ROW_H, int(h) - 16)
        self.setFixedHeight(_h)
        _lh = max(2 * self.ROW_H, _h - self.chrome_h())
        self._list.setFixedHeight(_lh)
        _n = self._list.count()
        if not self.rows:
            # 空态：占位项铺满列表并居中，面板余量不留成一块空白
            if _n:
                _it = self._list.item(0)
                _it.setSizeHint(QSize(0, _lh))
                _it.setTextAlignment(Qt.AlignCenter)
            return
        _rh = self.ROW_H
        if _n:
            _rh = max(self.ROW_H, min(self.ROW_MAX, _lh // _n))
        for _i in range(_n):
            self._list.item(_i).setSizeHint(QSize(0, _rh))

    def _vis_keys(self):
        _ps = self._page_size()
        _st = self.page * _ps
        return [ux.session_key(s) for s in self.rows[_st:_st + _ps]]

    def _empty_text(self):
        if self.bid == ux.UNASSIGNED:
            return '未归箱暂无会话：所有会话都已按规则归组。'
        if not self.bid:
            return '点左侧卡片「会话」查看组内会话；也可在此批量迁移。'
        return '该分组暂无会话：可把别组会话迁入，或等待新会话自动归入。'

    @staticmethod
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _row_txt(self, s):
        # 2.1.0：首列由时间改为冲入电量（Wh）；缺值用「—」占位。
        # 行尾不再展示充电器与来源（长字段会被截断），改为平均电压/平均电流。
        # 2.1.1：末尾追加协议短码（PD/PPS/QC/UFCS/5V，未知为「—」），
        # 现有六列口径不变；右栏宽度按整行实测放宽，最小档不省略、不缩字。
        _wh = self._num(s.get('total_wh'))
        _vt = self._num(s.get('avg_voltage'))
        _at = self._num(s.get('avg_current'))
        _pk = self._num(s.get('peak_power_w'))
        _du = self._num(s.get('duration_sec'))
        return ('%sWh·%s·峰%dW·%s\n%d分·%sV·%sA'
                % ('%.1f' % _wh if _wh is not None else '—',
                   ux.port_label(s.get('port')) or '-',
                   0 if _pk is None else int(_pk),
                   ux.proto_short(s.get('protocol')),
                   0 if _du is None else int(_du // 60),
                   '%.1f' % _vt if _vt is not None else '—',
                   '%.2f' % _at if _at is not None else '—')) if self.compact else (
            '%sWh·%s·峰%dW·%d分·%sV·%sA·%s'
            % ('%.1f' % _wh if _wh is not None else '—',
               ux.port_label(s.get('port')) or '-',
               0 if _pk is None else int(_pk),
               0 if _du is None else int(_du // 60),
               '%.1f' % _vt if _vt is not None else '—',
               '%.2f' % _at if _at is not None else '—',
               ux.proto_short(s.get('protocol'))))

    def _render(self):
        _ps = self._page_size()
        _n = len(self.rows)
        _pages = max(1, (_n + _ps - 1) // _ps)
        self.page = max(0, min(self.page, _pages - 1))
        _st = self.page * _ps
        self._list.blockSignals(True)
        self._list.clear()
        _slice = self.rows[_st:_st + _ps]
        if not _slice:
            _it = QListWidgetItem(self._empty_text())
            _it.setFlags(Qt.NoItemFlags)
            _it.setSizeHint(QSize(0, self.ROW_H))
            self._list.addItem(_it)
        for _s in _slice:
            _k = ux.session_key(_s)
            _txt = self._row_txt(_s)
            _it = QListWidgetItem(_txt)
            _it.setData(Qt.UserRole, _k)
            _it.setSizeHint(QSize(0, self.ROW_H))
            _it.setToolTip(_txt.replace('\n', ' · '))
            self._list.addItem(_it)
            if _k in self.sel:
                _it.setSelected(True)
        # 2.1.1：列表高度 = 当前页条数 + 1 行余量，行高固定 ROW_H；不再撑满整块区域。
        # 超出可用高度时由分页/滚动条处理，右侧面板整体高度随之收敛。
        _lh = max(2, min(len(_slice), _ps) + 1) * self.ROW_H
        self._list.setFixedHeight(_lh)
        for _ii in range(self._list.count()):
            self._list.item(_ii).setSizeHint(QSize(0, self.ROW_H))
        if not _slice and self._list.count():
            self._list.item(0).setSizeHint(QSize(0, _lh))
        self._list.setSpacing(0)
        self._list.blockSignals(False)
        self._hint.setText('共 %d 条 · 已选 %d 条' % (_n, len(self.sel)))
        self._pg_lb.setText('第 %d/%d 页' % (self.page + 1, _pages))
        if self.height() > 0:
            self.apply_height(self.height() + 16)  # 翻页/重渲染后行高仍铺满已定高度

    def _sync_sel(self):
        for k in self._vis_keys():
            self.sel.discard(k)
        for it in self._list.selectedItems():
            k = str(it.data(Qt.UserRole) or '')
            if k:
                self.sel.add(k)
        self._hint.setText('共 %d 条 · 已选 %d 条' % (len(self.rows), len(self.sel)))

    def _flip(self, d):
        self.page = max(0, self.page + d)
        self._render()

    def _all_page(self):
        self._list.selectAll()

    def _all_all(self):
        self.sel = {ux.session_key(s) for s in self.rows}
        self._render()

    def _none(self):
        self.sel.clear()
        self._render()

    def _by_key(self):
        return {ux.session_key(s): s for s in self.rows}

    # ---- 条件选择 ----
    def _cond_select(self):
        _d = QDialog(self)
        _d.setWindowTitle('按条件选择')
        _d.setStyleSheet(self.window().styleSheet())
        _v = QVBoxLayout(_d)
        _v.setContentsMargins(16, 14, 16, 12)
        _v.setSpacing(8)
        _f = QFormLayout()
        _f.setSpacing(7)
        _tf = QComboBox()
        for _x in ('全部', '近 7 天', '近 30 天', '自定义'):
            _tf.addItem(_x)
        _from = QLineEdit()
        _from.setPlaceholderText('YYYY-MM-DD')
        _to = QLineEdit()
        _to.setPlaceholderText('YYYY-MM-DD')
        _pf = QComboBox()
        for _x in ('全部', 'C1', 'C2', 'C3', 'A'):
            _pf.addItem(_x)
        _sf = QComboBox()
        for _x in ('全部', '人工指定', '自动判定', '未归箱'):
            _sf.addItem(_x)
        _cf = QSpinBox()
        _cf.setRange(0, 100)
        _cf.setValue(0)
        _cf.setSuffix(' 分以上')
        _f.addRow('时间', _tf)
        _f.addRow('起始', _from)
        _f.addRow('结束', _to)
        _f.addRow('端口', _pf)
        _f.addRow('来源', _sf)
        _f.addRow('置信度', _cf)
        _v.addLayout(_f)
        _br = QHBoxLayout()
        _br.addStretch(1)
        _bc = aligned_button('取消', 'ghost', min_w=80, h=30)
        _bo = aligned_button('选中匹配', 'primary', min_w=96, h=30)
        _bc.clicked.connect(_d.reject)
        _bo.clicked.connect(_d.accept)
        _br.addWidget(_bc)
        _br.addWidget(_bo)
        _v.addLayout(_br)
        _d.resize(360, _d.sizeHint().height())
        if _d.exec() != QDialog.Accepted:
            return

        def _parse(txt):
            try:
                return datetime.datetime.strptime((txt or '').strip(), '%Y-%m-%d').timestamp()
            except Exception:
                return None
        _now = time.time()
        _t0 = _parse(_from.text())
        _t1 = _parse(_to.text())
        if _tf.currentIndex() == 1:
            _t0 = _now - 7 * 86400
        elif _tf.currentIndex() == 2:
            _t0 = _now - 30 * 86400
        _port = _pf.currentText()
        _src = _sf.currentText()
        _cmin = float(_cf.value()) / 100.0
        _learn = ux.learn_load()
        _hit = set()
        for s in self.rows:
            try:
                _st = float(s.get('start_time') or 0)
            except Exception:
                _st = 0.0
            if _t0 is not None and _st < _t0:
                continue
            if _t1 is not None and _st > _t1 + 86400:
                continue
            if _port != '全部' and ux.port_label(s.get('port')) != _port:
                continue
            _bid, _s, _cf_ = ('', '', 0.0)
            if self.model:
                _bid, _s, _cf_ = ux.group_resolve(
                    s, self.model, shapes=self.shapes, thr=self.thr,
                    cons=self.cons, learn=_learn, detail=True)
            if _src == '人工指定' and _s != 'manual':
                continue
            if _src == '自动判定' and _s in ('', 'manual'):
                continue
            if _src == '未归箱' and _bid:
                continue
            if _cf_ < _cmin:
                continue
            _hit.add(ux.session_key(s))
        self.sel = _hit
        self._render()
        self._msg.set_full('按条件选中 %d 条（可继续逐条增减）' % len(_hit))

    # ---- 批量操作 ----
    def _confirm(self, verb, keys, target=None):
        _from = self._t.text() or '组内会话'
        _to = '未归箱'
        if target:
            _i = self._tgt.findData(target)
            _to = (self._tgt.itemText(_i) if _i >= 0 else target).replace('　↳ ', '')
        else:
            _from = self._t.text() or '组内会话'
        return QMessageBox.question(
            self, '确认%s' % verb,
            '将 %d 条会话%s「%s」%s「%s」？\n超过 %d 条会分批执行并显示进度。'
            % (len(keys), '从' if target else '移出', _from,
               '迁移到' if target else '回到', _to, self.BIG)) == QMessageBox.Yes

    def _migrate(self):
        _keys = list(self.sel)
        if not _keys:
            self._msg.set_full('请先勾选要迁移的会话（支持 Ctrl/Shift 多选）')
            return
        _tgt = str(self._tgt.currentData() or '')
        if not _tgt or _tgt == self.bid:
            self._msg.set_full('请选择另一个分组作为迁移目标')
            return
        if not self._confirm('迁移', _keys, _tgt):
            return
        self._start('move', _keys, _tgt)

    def _remove(self):
        _keys = list(self.sel)
        if not _keys:
            self._msg.set_full('请先勾选要移出的会话')
            return
        if not self._confirm('移出', _keys):
            return
        self._start('remove', _keys)

    def _export(self):
        _keys = self.sel or set(ux.session_key(s) for s in self.rows)
        _rows = [s for s in self.rows if ux.session_key(s) in _keys]
        if not _rows:
            self._msg.set_full('没有可导出的会话')
            return
        _path, _ = QFileDialog.getSaveFileName(
            self, '导出所选会话', 'group_sessions.csv', 'CSV (*.csv)')
        if not _path:
            return
        try:
            import csv
            with open(_path, 'w', encoding='utf-8-sig', newline='') as _fh:
                _w = csv.writer(_fh)
                _w.writerow(['会话键', '开始时间', '端口', '峰值W', '时长秒',
                             '充入Wh', '设备名'])
                for _s in _rows:
                    try:
                        _ts = ck_time.fmt(ck_time.MD_HM, datetime.datetime.fromtimestamp(
                            float(_s.get('start_time') or 0)))
                    except Exception:
                        _ts = ''
                    _w.writerow([ux.session_key(_s), _ts,
                                 ux.port_label(_s.get('port')),
                                 _s.get('peak_power_w') or 0,
                                 _s.get('duration_sec') or 0,
                                 _s.get('total_wh') or 0,
                                 _s.get('device_label') or ''])
            self._msg.set_full('已导出 %d 条 → %s' % (len(_rows), _path))
        except Exception as _exc:
            self._msg.set_full('导出失败：%s' % _exc)

    def _start(self, op, keys, target=None):
        _manual = (self.model or {}).get('manual') or {}
        _keys = []
        _skip = 0
        for k in keys:
            _v = str(_manual.get(k) or '')
            if op == 'move' and _v == str(target):
                _skip += 1
                continue
            if op == 'remove' and _v == ux.UNASSIGNED:
                _skip += 1
                continue
            _keys.append(k)
        if not _keys:
            self._msg.set_full('无需操作：所选 %d 条已在该状态（跳过 %d）'
                               % (len(keys), _skip))
            return
        self._op, self._tgt_id = op, target
        self._skip, self._ok, self._fail = _skip, 0, 0
        self._fail_msg = ''
        self._total = len(_keys)
        self._done = 0
        self._queue = list(_keys)
        self._busy = True
        self._set_busy(True)
        _verb = '迁移' if op == 'move' else '移出'
        # 1.5.8：只要超过一个分片就走 0ms 定时器，避免 51–200 条同步阻塞主线程
        # （实测 195 条约 304ms 无任何事件处理，点完界面像卡住）。
        if self._total <= self.BATCH:
            while self._queue:
                self._exec(self._queue[:self.BATCH])
                self._queue = self._queue[self.BATCH:]
                self._done += min(self.BATCH, self._total - self._done)
            self._finish(_verb)
            return
        self._msg.set_full('%s中 0/%d…' % (_verb, self._total))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(0)

    def _tick(self):
        if not self._queue:
            if self._timer is not None:
                self._timer.stop()
                self._timer = None
            self._finish('迁移' if self._op == 'move' else '移出')
            return
        _chunk = self._queue[:self.BATCH]
        self._queue = self._queue[self.BATCH:]
        self._exec(_chunk)
        self._done += len(_chunk)
        self._msg.set_full('%s中 %d/%d…'
                           % ('迁移' if self._op == 'move' else '移出',
                              self._done, self._total))
        QApplication.processEvents()

    def _exec(self, chunk):
        _by = self._by_key()
        _cv = {}
        for k in chunk:
            _s = _by.get(k)
            _sh = self.shapes.get(k)
            if _s and _sh:
                _cv[k] = ux.cluster_vec(_s, _sh)
        try:
            _r = ux.group_mutate(self.p, self._op, keys=chunk,
                                 target=self._tgt_id, cvecs=_cv, n_full=10 ** 9,
                                 rows=self.rows)
        except Exception as _exc:
            self._fail += len(chunk)
            self._fail_msg = self._fail_msg or repr(_exc)
            ck_swallow(_exc, 'launcher:grpbatch')
            return
        if _r.get('ok'):
            self._ok += int(_r.get('affected') or len(chunk))
        else:
            self._fail += len(chunk)
            if not self._fail_msg:
                self._fail_msg = str(_r.get('msg') or '写入失败')

    def _finish(self, verb):
        _t0 = time.time()
        ux.learn_log('BATCH op=%s n=%d ok=%d skip=%d fail=%d target=%s dur=%dms'
                     % (self._op, self._total, self._ok, self._skip, self._fail,
                        self._tgt_id or '-', int((time.time() - _t0) * 1000)))
        self._busy = False
        self._set_busy(False)
        self.sel.clear()
        # 1.5.8：清空选择后必须重绘——否则列表仍整页高亮，再点「全选本页」
        # 不产生选择变化事件，sel 一直为 0，后续批量按钮就“点不动”。
        self._render()
        _txt = ('%s完成：成功 %d · 跳过 %d · 失败 %d'
                % (verb, self._ok, self._skip, self._fail))
        if self._fail and self._fail_msg:
            _txt += '（失败原因：%s）' % self._fail_msg
        self._msg.set_full(_txt)
        # 结果先落屏，再让主线程做重训/整页刷新，避免把最后一段阻塞算进批量操作
        if self._ok > 0:
            QTimer.singleShot(0, lambda: ux.learn_apply([], 'batch', full=True))
        if callable(self.on_done):
            QTimer.singleShot(0, self._emit_done)

    def _emit_done(self):
        try:
            self.on_done()
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:grpdone')

    def _set_busy(self, busy):
        for _w in (self._b_cond, self._b_pg_all, self._b_all, self._b_none,
                   self._b_move, self._b_out, self._b_exp, self._tgt,
                   self._list):
            _w.setEnabled(not busy)


class BoxReviewDialog(QDialog):
    """分箱 AI 复核（1.5.6）：低置信/疑似分错/约束宽窄，建议一键采纳才改数据。"""

    def __init__(self, parent, prefs, rows, model, shapes, notify=None,
                 on_done=None):
        super().__init__(parent)
        self._prefs = prefs
        self._rows = rows or []
        self._model = model or {}
        self._shapes = shapes or {}
        self._notify = notify or (lambda *_a, **_k: None)
        self._on_done = on_done
        self.setWindowTitle('AI 复核 · 分箱建议')
        self.resize(920, 560)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)
        _t = QLabel('AI 复核（本地规则 + 可选在线模型）：建议只是建议，'
                    '勾选后点「采纳所选」才会改数据，采纳留审计。')
        _t.setObjectName('sub')
        _t.setWordWrap(True)
        v.addWidget(_t)
        self._res = ai_service.rule_box_review(self._model, self._rows,
                                               self._shapes)
        self._list = QListWidget()
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.setTextElideMode(Qt.ElideRight)
        self._list.setStyleSheet('QListWidget{background:rgba(10,20,40,0.55);'
                                 'border:1px solid rgba(120,190,255,0.14);'
                                 'border-radius:8px;padding:4px;}')
        _names = {str(b.get('id')): b.get('name')
                  for b in self._model.get('boxes') or []}
        for _i in self._res.get('items') or []:
            if not _i.get('key') or not _i.get('to'):
                continue
            _txt = '%s  「%s」→「%s」  %s' % (
                _i.get('key'), _names.get(str(_i.get('from')), '?'),
                _names.get(str(_i.get('to')), '?'), _i.get('why'))
            _it = QListWidgetItem(_txt)
            _it.setFlags(_it.flags() | Qt.ItemIsUserCheckable)
            _it.setCheckState(Qt.Unchecked)
            _it.setData(Qt.UserRole, _i)
            _it.setToolTip(_txt)
            self._list.addItem(_it)
        for _c in self._res.get('cons') or []:
            _it = QListWidgetItem('约束建议（仅提示）：' + _c.get('text', ''))
            _it.setFlags(Qt.ItemIsEnabled)
            _it.setToolTip(_c.get('text', ''))
            self._list.addItem(_it)
        if self._list.count() == 0:
            self._list.addItem('暂无需要复核的项目（样本不足或分组稳定）。')
        v.addWidget(self._list, 1)
        _brow = QHBoxLayout()
        _brow.addStretch(1)
        _ok = aligned_button('一键采纳全部', 'primary', min_w=128, h=32)
        _sel = aligned_button('采纳所选', 'secondary', min_w=106, h=32)
        _close = aligned_button('关闭', 'ghost', min_w=72, h=32)
        _ok.clicked.connect(self._adopt_all)
        _sel.clicked.connect(self._adopt)
        _close.clicked.connect(self.reject)
        _brow.addWidget(_ok)
        _brow.addWidget(_sel)
        _brow.addWidget(_close)
        v.addLayout(_brow)

    def _allowed(self, all_items):
        out = []
        for _i in range(self._list.count()):
            _it = self._list.item(_i)
            _d = _it.data(Qt.UserRole)
            if not _d or not _d.get('key') or not _d.get('to'):
                continue
            if all_items or _it.checkState() == Qt.Checked:
                out.append(_d)
        return out

    def _adopt(self):
        self._apply(self._allowed(False), '所选')

    def _adopt_all(self):
        self._apply(self._allowed(True), '全部')

    def _apply(self, sel, label):
        if not sel:
            self._notify('请先勾选要采纳的建议' if label == '所选'
                         else '没有可采纳的建议', 'warning')
            return
        _by = {ux.session_key(s): s for s in self._rows}
        _model0 = ux.device_model_load(self._prefs) or self._model or {}
        _man0 = _model0.get('manual') or {}
        _groups = {}
        for _d in sel:
            _groups.setdefault(str(_d.get('to')), []).append(str(_d.get('key')))
        _okn, _bad, _skip = 0, 0, 0
        _t0 = time.time()
        for _to, _keys0 in _groups.items():
            _keys = [k for k in dict.fromkeys(_keys0)
                     if str(_man0.get(k) or '') != _to]
            _skip += len(_keys0) - len(_keys)
            if not _keys:
                continue
            _cv = {}
            for k in _keys:
                _s = _by.get(k)
                _sh = self._shapes.get(k)
                if _s is not None and _sh:
                    _cv[k] = ux.cluster_vec(_s, _sh)
            _r = ux.group_mutate(self._prefs, 'move', target=_to, keys=_keys,
                                 cvecs=_cv, rows=self._rows, n_full=10 ** 9)
            if (_r or {}).get('ok'):
                _okn += int(_r.get('affected') or len(_keys))
            else:
                _bad += len(_keys)
        ai_service.audit('box_review', _t0, adopted=bool(_okn),
                         note='%s adopted %d/%d failed=%d skip=%d'
                              % (label, _okn, len(sel), _bad, _skip))
        _msg = '已采纳%s %d 条（%d 个分组）%s%s' % (
            label, _okn, len(_groups),
            ('，%d 条未成功' % _bad) if _bad else '',
            ('，%d 条本就在目标组' % _skip) if _skip else '')
        self._notify(_msg, 'success')
        if self._on_done:
            try:
                self._on_done({'ok': _okn, 'failed': _bad, 'skipped': _skip,
                               'targets': {k: len(v) for k, v in _groups.items()},
                               'msg': _msg})
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:boxreview')
        self.accept()


class DiagnosisDialog(QDialog):
    """智能分析 · AI 结论（1.5.6）：本地规则结论先出，模型可用时补充，全部带引用。"""

    def __init__(self, parent, prefs, secrets, rows, analysis=None):
        super().__init__(parent)
        self._rows = [r for r in (rows or []) if isinstance(r, dict)]
        self._an = analysis or {}
        self.setWindowTitle('AI 诊断结论')
        self.resize(860, 520)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)
        _t = QLabel('结论只依据本机会话数据，逐条附时间与数值；样本不足会注明条数。')
        _t.setObjectName('sub')
        _t.setWordWrap(True)
        v.addWidget(_t)
        self._txt = QPlainTextEdit()
        self._txt.setReadOnly(True)
        self._txt.setPlainText('\n'.join(ai_service.rule_diagnosis(self._an,
                                                                   self._rows)))
        v.addWidget(self._txt, 1)
        _brow = QHBoxLayout()
        _brow.addStretch(1)
        _close = aligned_button('关闭', 'ghost', min_w=72, h=32)
        _close.clicked.connect(self.reject)
        _brow.addWidget(_close)
        v.addLayout(_brow)
        if (ai_service.scene_on(prefs, 'diagnosis') and self._rows):
            _cfg = ai_service.make_cfg(prefs, secrets, 'diagnosis')
            _st = {'sessions': len(self._rows),
                   'total_wh': round(sum(float(r.get('total_wh') or 0)
                                         for r in self._rows), 1),
                   'avg_power_w': round(sum(float(r.get('avg_power_w') or 0)
                                            for r in self._rows)
                                        / len(self._rows), 1)}
            _rs = {}
            try:
                from collections import Counter
                _c = Counter(str(r.get('label') or '未分类')
                             for r in (self._an.get('reasons') or []))
                _rs = dict(_c)
            except Exception:
                _rs = {}
            ai_service.ask(
                'diagnosis',
                '请给出掉速原因、充满习惯、异常事件与建议充电方式四类结论，'
                '每条附引用会话的时间与数值。',
                {'stats': _st, 'reasons': _rs,
                 'sessions': self._rows[:80]},
                _cfg,
                lambda txt, _deg, _err: self._txt.setPlainText(
                    txt or '\n'.join(ai_service.rule_diagnosis(self._an,
                                                               self._rows))),
                fallback=lambda: '\n'.join(ai_service.rule_diagnosis(self._an,
                                                                     self._rows)))


class EndRulesDialog(QDialog):
    """断开原因规则编辑（1.5.6）：启用/停用、改显示名与匹配条件、恢复默认，落盘即生效。"""

    def __init__(self, parent, notify=None, on_done=None):
        super().__init__(parent)
        self._notify = notify or (lambda *_a, **_k: None)
        self._on_done = on_done
        self.setWindowTitle('断开原因规则')
        self.resize(900, 560)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)
        _t = QLabel('按顺序首个命中的规则生效；显示名与匹配条件可改，改完即生效并留审计。')
        _t.setObjectName('sub')
        _t.setWordWrap(True)
        v.addWidget(_t)
        _sa = QScrollArea()
        _sa.setWidgetResizable(True)
        _sa.setFrameShape(QFrame.NoFrame)
        _host = QWidget()
        _hv = QVBoxLayout(_host)
        _hv.setContentsMargins(0, 0, 0, 0)
        _hv.setSpacing(6)
        self._rows = []
        for _r in ux.end_rules_load(force=True):
            _row = QWidget()
            _rh = QHBoxLayout(_row)
            _rh.setContentsMargins(0, 0, 0, 0)
            _rh.setSpacing(8)
            _on = QCheckBox('')
            _on.setChecked(bool(_r.get('on')))
            _on.setFixedWidth(24)
            _lb = QLineEdit(str(_r.get('label') or ''))
            _lb.setFixedWidth(190)
            _mt = QLineEdit(json.dumps(_r.get('match') or {}, ensure_ascii=False,
                                       sort_keys=True))
            _mt.setToolTip('匹配条件（JSON），留空表示兜底')
            _id = QLabel(str(_r.get('id') or ''))
            _id.setObjectName('sub')
            _id.setFixedWidth(96)
            _id.setTextInteractionFlags(Qt.TextSelectableByMouse)
            _rh.addWidget(_on)
            _rh.addWidget(_id)
            _rh.addWidget(_lb)
            _rh.addWidget(_mt, 1)
            _hv.addWidget(_row)
            self._rows.append((_r, _on, _lb, _mt))
        _hv.addStretch(1)
        _sa.setWidget(_host)
        v.addWidget(_sa, 1)
        _brow = QHBoxLayout()
        _rst = aligned_button('恢复默认', 'ghost', min_w=110, h=32)
        _rst.clicked.connect(self._reset)
        _ok = aligned_button('保存并生效', 'primary', min_w=120, h=32)
        _ok.clicked.connect(self._save)
        _cl = aligned_button('关闭', 'ghost', min_w=72, h=32)
        _cl.clicked.connect(self.reject)
        _brow.addWidget(_rst)
        _brow.addStretch(1)
        _brow.addWidget(_ok)
        _brow.addWidget(_cl)
        v.addLayout(_brow)

    def _reset(self):
        _def = ux.end_rules_reset()
        for (_r0, _on, _lb, _mt), _d in zip(self._rows, _def):
            _on.setChecked(bool(_d.get('on')))
            _lb.setText(str(_d.get('label') or ''))
            _mt.setText(json.dumps(_d.get('match') or {}, ensure_ascii=False,
                                   sort_keys=True))
        self._notify('已恢复默认规则，点「保存并生效」后应用', 'info')

    def _save(self):
        _out = []
        for _r, _on, _lb, _mt in self._rows:
            _txt = _mt.text().strip() or '{}'
            try:
                _m = json.loads(_txt)
                if not isinstance(_m, dict):
                    raise ValueError('match 必须是对象')
            except Exception as _exc:
                self._notify('规则「%s」条件不是合法 JSON：%s' % (
                    _lb.text() or _r.get('id'), _exc), 'error')
                return
            _out.append({'id': _r.get('id'), 'label': _lb.text().strip()
                         or str(_r.get('label') or ''), 'on': _on.isChecked(),
                         'match': _m})
        if ux.end_rules_save(_out):
            self._notify('断开原因规则已保存并生效', 'success')
            if self._on_done:
                try:
                    self._on_done()
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:endrules')
            self.accept()
        else:
            self._notify('规则保存失败，详见日志', 'error')


SCREEN_AVAIL_OVERRIDE = None    # 测试注入：模拟小屏可用区（None = 真实屏幕）


def _fit_dialog(dlg, w, h, min_w=860, min_h=600, margin=24):
    """弹窗按屏幕可用区收敛并居中：任何档位下都完整可见，绝不出屏。

    同时参考父窗口宽度（三档主窗口 995/1280/1600），保证弹窗不会比主窗口更宽；
    屏幕明显小于设计尺寸时按屏幕收缩，内部两栏靠 stretch 自适应。
    """
    scr = dlg.screen() or QApplication.primaryScreen()
    av = SCREEN_AVAIL_OVERRIDE or (scr.availableGeometry() if scr is not None else
                                   QRect(0, 0, w + 2 * margin, h + 2 * margin))
    aw = max(640, av.width() - 2 * margin)
    ah = max(520, av.height() - 2 * margin)
    _pw = dlg.parentWidget()
    if _pw is not None and _pw.window() is not dlg:
        _ww = _pw.window().width()
        if _ww > 0:
            aw = min(aw, max(720, _ww - 8))
    ww, hh = min(int(w), aw), min(int(h), ah)
    dlg.resize(ww, hh)
    dlg.setMinimumSize(min(int(min_w), ww), min(int(min_h), hh))
    dlg.move(av.x() + max(0, (av.width() - ww) // 2),
             av.y() + max(0, (av.height() - hh) // 2))


class _CardViewport(QWidget):
    """列表视口：在内容之上绘制滚动位置指示（不新增子控件）。"""

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner

    def paintEvent(self, ev):
        super().paintEvent(ev)
        self._owner._paint_indicator(self)


class _CardScroll(QScrollArea):
    """分组卡片列表滚动区（1.5.8）。

    - 只有用户滚轮才改位置：不做自动滚动、回弹、惯性/平滑动画，
      刷新数据时保持原位置，不自动跳到某条。
    - 滚轮在列表上时只滚列表，到顶/底也 accept，不带动外层页面。
    - 竖向滚动条不占宽度：关闭系统滚动条，改用 4px 覆盖式细条做位置指示。
    """

    BAR_W = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._keep = 0
        self._src = self.verticalScrollBar()
        self._src.setSingleStep(36)
        self._src.rangeChanged.connect(self._sync)
        self._src.valueChanged.connect(self._sync)
        self.setViewport(_CardViewport(self))

    def _sync(self, *_a):
        self.viewport().update()
        # 2.1.2：刷新/重排时先按旧上限把位置夹小了，等布局完成、上限变大后
        # 再回到用户原来的目标位置，避免点选/切换/刷新后停在上一条。
        _sb = self._src
        if self._keep > _sb.value() and _sb.maximum() >= self._keep:
            _sb.setValue(int(self._keep))

    def _paint_indicator(self, vp):
        """位置指示用绘制实现：不新增子控件、不占宽度、不遮挡卡片。"""
        sb = self._src
        if sb.maximum() <= sb.minimum():
            return
        _h = vp.height()
        _span = sb.maximum() - sb.minimum() + sb.pageStep()
        if _h <= 0 or _span <= 0:
            return
        _th = max(24.0, _h * sb.pageStep() / float(_span))
        _y = (_h - _th) * ((sb.value() - sb.minimum())
                           / float(max(1, sb.maximum() - sb.minimum())))
        pt = QPainter(vp)
        pt.setRenderHint(QPainter.Antialiasing)
        pt.setPen(Qt.NoPen)
        pt.setBrush(QColor(255, 255, 255, 66))
        pt.drawRoundedRect(QRectF(vp.width() - self.BAR_W - 1, _y,
                                  self.BAR_W, _th), 2, 2)
        pt.end()

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if dy == 0:
            ev.ignore()
            return
        sb = self.verticalScrollBar()
        newer = ev.angleDelta().x()
        _d = dy if abs(dy) >= abs(newer) else newer
        _to = int(round(sb.value() - _d * sb.singleStep() / 120.0))
        self._keep = max(sb.minimum(), min(_to, sb.maximum()))
        sb.setValue(self._keep)          # 滚轮后的位置即用户新的目标位置
        ev.accept()

    def scroll_pos(self):
        return int(self.verticalScrollBar().value())

    def hold(self, val):
        self._keep = int(val or 0)

    def restore(self):
        sb = self.verticalScrollBar()
        sb.setValue(max(sb.minimum(), min(int(self._keep), sb.maximum())))


class DeviceGroupsDialog(QDialog):
    """设备分组管理（1.5.6）：一级/二级分组、固定组保护、组内会话单条/批量迁移。

    固定组 = 显式锁定（★，点卡片星标切换）或出厂种子组：重新分组不会改写其
    名称/成员/结构；仍可改名、改约束、迁移成员、确认后删除。新建/改名默认非固定。
    """

    closed = Signal()        # 1.5.8：嵌入二级页时点「返回」发出，由主窗口移除本页

    def __init__(self, parent, prefs, rows, sens=None, on_saved=None,
                 shapes=None, embedded=False):
        super().__init__(parent)
        self.p = prefs
        self._embedded = bool(embedded)      # 1.5.8：嵌入主窗口作为全屏二级页
        self._shapes_in = shapes if isinstance(shapes, dict) else None
        ux.group_seed(prefs)                    # 幂等：首次启动建出 4 个出厂固定组
        self.rows = [r for r in (rows or []) if isinstance(r, dict)]
        self.sens = str(sens or prefs.get('deviceSensitivity') or '均衡')
        self.on_saved = on_saved
        self.model = ux.device_model_load(prefs) or {'boxes': [], 'manual': {}}
        self.setWindowTitle('设备分组管理')
        if self._embedded:
            # 二级页：铺满主窗口可用区（不再按屏幕收敛成小窗）
            self.setWindowFlags(Qt.Widget)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.setMinimumSize(0, 0)
        else:
            _fit_dialog(self, 1080, 760, 900, 620)
        self._sel = set()
        self._cur = ''
        v = QVBoxLayout(self)
        # 二级页与主页面同一套边距（16/16/16/16），弹窗沿用原紧凑边距
        if self._embedded:
            v.setContentsMargins(16, 16, 16, 16)
        else:
            v.setContentsMargins(16, 12, 16, 10)
        v.setSpacing(9)
        # 2.1.1：标题层与状态层分开——标题独占左缘，灵敏度/上限徽章靠右，
        # 操作按钮从下一行开始，三层不再挤在同一条基线上。
        _hdr = QHBoxLayout()
        _hdr.setSpacing(8)
        _t = QLabel('设备分组')
        _t.setObjectName('pdTitle')
        self._badge = QLabel('')
        self._badge.setObjectName('pdBadgeOff')
        # 1.5.9：总数上限必须一眼可见——标题旁常驻「上限 N · 已用 M」
        self._cap = QLabel('')
        self._cap.setObjectName('pdCap')
        _hdr.addWidget(_t)
        _hdr.addStretch(1)
        _hdr.addWidget(self._badge)
        _hdr.addWidget(self._cap)
        v.addLayout(_hdr)
        # 2.1.0：整页共用一张 5 列栅格——第 0 列固定 96（行首文字），后 4 列等宽；
        # 两行操作、未归箱行、提示/计数、页脚按钮全部落在同一列边界，
        # 主体作为跨列行随页面伸展。按钮/控件统一 30 高，行中线一致。
        _ops = QGridLayout()
        _ops.setContentsMargins(0, 0, 0, 0)
        _ops.setHorizontalSpacing(8)
        _ops.setVerticalSpacing(8)
        _ops.setColumnMinimumWidth(0, 96)
        for _c1 in range(1, 5):
            _ops.setColumnStretch(_c1, 1)
            _ops.setColumnMinimumWidth(_c1, 108)
        self._k = QLabel('当前分组 0')
        self._k.setMinimumWidth(96)
        _ops.addWidget(self._k, 0, 0)
        self._b_new1 = aligned_button('新建一级组', 'ghost', min_w=108, h=30)
        self._b_new1.clicked.connect(lambda: self._new_group(''))
        self._b_new2 = aligned_button('新建二级组', 'ghost', min_w=108, h=30)
        self._b_new2.clicked.connect(self._new_sub)
        self._b_build = aligned_button('重新分组', 'primary', min_w=108, h=30)
        self._b_build.setToolTip('用当前模型对已有会话二次分组：固定组（★）条数与成员'
                                 '一律不动，只在非固定组和未归箱范围内重新分配。')
        self._b_build.clicked.connect(self._rebuild)
        # 1.5.9：按曲线形态聚类看分箱结果（只预览，命名后才落库）
        self._b_curve = aligned_button('按曲线看分箱', 'ghost', min_w=108, h=30)
        self._b_curve.setToolTip('按曲线形态聚类查看现有分箱与候选新设备；'
                                 '只有命名后才会写入分组。')
        self._b_curve.clicked.connect(self._open_curve_bins)
        for _c1, _b in enumerate((self._b_new1, self._b_new2,
                                  self._b_build, self._b_curve)):
            _b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            _ops.addWidget(_b, 0, _c1 + 1)
        # 第二行：选择态 + 合并/约束/复核/撤销（按钮文案随选择数变，矩形不变）
        self._sel_lb = QLabel('已选 0 个')
        self._sel_lb.setObjectName('sub')
        self._sel_lb.setMinimumWidth(96)
        _ops.addWidget(self._sel_lb, 1, 0)
        self._b_merge = aligned_button('合并所选箱', 'ghost', min_w=108, h=30)
        self._b_merge.clicked.connect(self._merge_boxes)
        self._b_cons = aligned_button('维度约束', 'ghost', min_w=108, h=30)
        self._b_cons.clicked.connect(self._edit_cons)
        self._b_ai = aligned_button('AI 复核', 'ghost', min_w=108, h=30)
        self._b_ai.setToolTip('列出置信度低、疑似分错的会话与约束宽窄建议；'
                              '一键采纳后才改数据，采纳留审计。')
        self._b_ai.clicked.connect(self._ai_review)
        self._b_undo = aligned_button('撤销合并', 'ghost', min_w=108, h=30)
        self._b_undo.setToolTip('恢复到最近一次合并前的分组与成员（仅保留一步）。')
        self._b_undo.clicked.connect(self._undo_merge)
        for _c1, _b in enumerate((self._b_merge, self._b_cons,
                                  self._b_ai, self._b_undo)):
            _b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            _ops.addWidget(_b, 1, _c1 + 1)
        # 2.1.0：摘要并入同一张栅格（跨 5 列），左缘与第 0 列文字严格一致；
        # 最小默认尺寸（995）下两行仍会把备注尾部截断，因此计数一行、备注按「；」折行。
        self._sum = _ElideLabel('', 3)
        self._sum.setStyleSheet('color:%s; font-size:12px; background:transparent;'
                                % TEXT_SUB)
        _ops.addWidget(self._sum, 2, 0, 1, 5)
        # 主体：左=分组卡片（仅竖向滚动、禁横向），右=组内会话内嵌面板（禁止独立窗口）
        _body = QHBoxLayout()
        _body.setSpacing(10)
        _left = QWidget()
        # 2.1.1：左列给足卡片所需的最小宽度（5 个功能按钮 + 标题单行），
        # 右列只吃 410~520 的定宽区间，不再把左列压到放不下文字。
        _left.setMinimumWidth(360)
        _lv = QVBoxLayout(_left)
        _lv.setContentsMargins(0, 0, 0, 0)
        _lv.setSpacing(0)
        # 1.5.8：单列纵向列表；滚动条覆盖在列表内侧不占宽，滚轮只滚列表。
        self._cards_scroll = _CardScroll()
        self._cards_scroll.setObjectName('cardsScroll')
        self._cards_scroll.setStyleSheet(
            'QScrollArea{background:rgba(255,255,255,0.02);'
            ' border:1px solid rgba(120,190,255,0.18); border-radius:12px;}'
            'QScrollArea>QWidget>QWidget{background:transparent;}')
        self._cards_scroll.viewport().setAutoFillBackground(False)
        self._cards_host = QWidget()
        self._cards = QGridLayout(self._cards_host)
        self._cards.setContentsMargins(10, 10, 10, 10)
        self._cards.setHorizontalSpacing(10)
        self._cards.setVerticalSpacing(8)
        self._cards_scroll.setWidget(self._cards_host)
        _lv.addWidget(self._cards_scroll)
        self._right = QFrame()
        self._right.setObjectName('grpSide')
        # 2.1.1：右栏宽度按会话行文字实际需要给（最宽一条 + 内边距），
        # 不再按 3:2 比例均分；富余宽度全部让给左侧卡片区。
        self._right.setMinimumWidth(410)
        self._right.setMaximumWidth(520)
        _rv = QVBoxLayout(self._right)
        _rv.setContentsMargins(10, 8, 10, 8)
        _rv.setSpacing(0)
        # 2.1.1：窄窗用两行行式（字号不变、不省略），宽窗单行七字段。
        _compact = int(self.window().width() or 995) < 1180
        self._panel = GroupSessionsPanel(
            self, self.p, '', [], self._groups_for_combo(), {}, model=self.model,
            on_done=self._after_panel,
            thr=float(self.p.get('devShapeThr') or 16) / 10.0,
            cons=bool(self.p.get('devHourGuard')), compact=_compact)
        _rv.addWidget(self._panel)
        # 用整库会话算一次最宽行文本，宽度在本页生命周期内固定（切组/三档都不跳）
        _rw = max(self._panel.minimumSizeHint().width() + 20,
                  self._panel.row_width_need(rows) + 34)
        _rw = max(300, min(480, _rw))
        self._right_w = _rw
        self._right.setMinimumWidth(_rw)
        self._right.setMaximumWidth(_rw)
        _body.addWidget(_left, 1)
        _body.addWidget(self._right, 0)
        _body.setAlignment(_left, Qt.AlignTop)
        _body.setAlignment(self._right, Qt.AlignTop)
        self._left = _left
        self._body_h = 0
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self._fit_body)
        # 主体作为栅格第 3 行（跨 5 列）：左右缘与上下各行严格同一条边
        _ops.addLayout(_body, 3, 0, 1, 5)
        _ops.setRowStretch(3, 1)
        # 2.1.1：未归箱只留一个入口——行首按钮本身带条数并可直接在右侧查看，
        # 不再同时出现「未归箱」标签和「未归箱会话」按钮。
        self._b_pend = aligned_button('未归箱', 'ghost', min_w=96, h=30)
        self._b_pend.setToolTip('在右侧面板查看未归箱会话，可多选后批量迁移。')
        self._b_pend.clicked.connect(self._open_unassigned)
        _ops.addWidget(self._b_pend, 4, 0)
        self._pend = QComboBox()
        # 宽度由栅格列决定（不再另设最小/最大宽），数据与档位变化都不改列边界
        self._pend.setFixedHeight(30)
        self._pend.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._pend.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._pend.setMinimumContentsLength(10)
        _ops.addWidget(self._pend, 4, 1, 1, 2)
        self._pbox = QComboBox()
        self._pbox.setFixedHeight(30)
        self._pbox.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._pbox.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._pbox.setMinimumContentsLength(6)
        _ops.addWidget(self._pbox, 4, 3)
        self._b_assign = aligned_button('归入', 'ghost', min_w=108, h=30)
        self._b_assign.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._b_assign.clicked.connect(self._manual_assign)
        _ops.addWidget(self._b_assign, 4, 4)
        # 提示整行跨 5 列（未归/人工计数已并入行首按钮与摘要行，不重复展示）
        self._hint = _ElideLabel('', 1)
        self._hint.setStyleSheet('color:%s; font-size:11px; background:transparent;'
                                 % TEXT_SUB)
        _ops.addWidget(self._hint, 5, 0, 1, 5)
        # 页脚按钮整行跨 5 列、右对齐：右缘与主体/计数同一条边
        _ft = QHBoxLayout()
        _ft.setContentsMargins(0, 0, 0, 0)
        _ft.setSpacing(8)
        _ft.addStretch(1)
        _b_close = aligned_button('返回' if self._embedded else '关闭', 'ghost',
                                  min_w=96, h=32)
        _b_close.clicked.connect(self._back)
        self._b_save = aligned_button('保存并返回' if self._embedded else '确认并关闭',
                                      'primary', min_w=120, h=32)
        self._b_save.clicked.connect(self._confirm)
        _ft.addWidget(_b_close)
        _ft.addWidget(self._b_save)
        _ops.addLayout(_ft, 6, 0, 1, 5)
        self._ops = _ops
        v.addLayout(_ops, 1)
        self.setStyleSheet(
            'QLabel#pdTitle { font-size:18px; font-weight:700; color:#f3f7fc; }'
            'QLabel#pdBadgeOff { font-size:11px; color:' + TEXT_SUB + ';'
            ' background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.12);'
            ' border-radius:9px; padding:2px 8px; }'
            'QLabel#pdCap { font-size:11px; color:#8fd6ff; font-weight:600;'
            ' background:rgba(56,226,255,0.10); border:1px solid rgba(56,226,255,0.45);'
            ' border-radius:9px; padding:2px 9px; }'
            'QFrame#grpSide { background:rgba(255,255,255,0.025);'
            ' border:1px solid rgba(255,255,255,0.09); border-radius:10px; }')
        self._ensure_model()
        self._reload()

    # ---- 数据 ----
    def _kcount(self):
        n = len(((self.model or {}).get('boxes') or []))
        return max(2, min(8, n or 3))

    def _ensure_model(self):
        if self.model and (self.model.get('boxes') or []):
            return
        built = ux.device_build(self.rows, self._kcount(), self.sens, None)
        if built.get('ok'):
            self.model = built
            ux.device_save(self.p, built)
        self._tip = '' if self.model else built.get('msg', '')

    def _tree(self):
        return ux.group_tree(self.model)

    def _groups_for_combo(self):
        out = []
        for _b, _d in self._tree():
            out.append({'id': str(_b.get('id') or ''),
                        'name': ux.device_box_name(_b.get('id'), _b.get('name')),
                        'parent': _d})
        return out

    def _reload(self, force_panel=False):
        eps = ux.DEV_EPS.get(self.sens, ux.DEV_EPS['均衡'])
        self._badge.setText('灵敏度 %s' % self.sens)
        self._badge.setToolTip('聚类阈值 ε=%.2f（越小越严格）' % eps)
        self.model = ux.device_model_load(self.p) or {'boxes': [], 'manual': {}}
        boxes = (self.model or {}).get('boxes') or []
        self._k.setText('当前分组 %d' % len(boxes))
        if not hasattr(self, '_shapes'):
            if self._shapes_in:
                # 主窗口已算过同一批会话的形态，直接复用（省掉 450 ms 重读）
                self._shapes = dict(self._shapes_in)
            else:
                _sh = load_shapes([s.get('id') for s in self.rows])
                self._shapes = {ux.session_key(s): _sh[s['id']] for s in self.rows
                                if s.get('id') in _sh}
        self._panel.shapes = self._shapes
        self._panel.model = self.model
        thr = float(self.p.get('devShapeThr') or 16) / 10.0
        cons = bool(self.p.get('devHourGuard'))
        live = (ux.device_assign(self.rows, self.model, self.sens,
                                 shapes=self._shapes, thr=thr, cons=cons)
                if boxes else None)
        self._render_cards(boxes, live)
        self._cards_args = (boxes, live)
        _un = int((live or {}).get('unassigned', 0))
        _done = max(0, len(self.rows) - _un)
        _fx = sum(1 for b in boxes if ux.group_is_fixed(b))
        _note = str((self.model or {}).get('note') or '')
        _ver = int((ux.learn_load() or {}).get('version') or 0)
        _cap = ux.device_cap(self.p)['cap']
        if boxes:
            self._cap.setText('上限 %d · 已用 %d' % (_cap, len(boxes)))
            self._cap.setToolTip('分组总数上限 %d；已用 %d（固定组 %d · 自建组 %d）。'
                                 % (_cap, len(boxes), _fx, len(boxes) - _fx))
            self._sum.set_full(
                '会话 %d 条 · 已归 %d · 未归 %d · 固定组 %d · 模型 v%d%s'
                % (len(self.rows), _done, _un, _fx, _ver,
                   ('\n' + _note.replace('；', '\n', 1)) if _note else ''))
        else:
            self._cap.setText('上限 %d · 已用 0' % _cap)
            self._cap.setToolTip('分组总数上限 %d' % _cap)
            self._sum.set_full(getattr(self, '_tip', '')
                               or '暂无分组：点「重新分组」按曲线生成。')
        pend = self._pending_sessions() if boxes else []
        self._pend.clear()
        for s in pend:
            try:
                _t = datetime.datetime.fromtimestamp(float(s.get('start_time') or 0))
                _ts = ck_time.fmt(ck_time.MD_HM, _t)
            except Exception:
                _ts = '--'
            self._pend.addItem('%s · %s · 峰 %d W · %d 分'
                               % (_ts, ux.port_label(s.get('port')),
                                  int(float(s.get('peak_power_w') or 0)),
                                  int(float(s.get('duration_sec') or 0) // 60)))
        # 常驻：0 条时禁用而不是隐藏，保证底部行几何不随数据变化
        _can = bool(pend)
        self._pend.setEnabled(_can)
        self._b_assign.setEnabled(_can)
        # 2.1.1：唯一入口直接带条数，打开右侧未归箱会话；0 条也可打开看空态说明。
        self._b_pend.setText('未归箱 %d ›' % len(pend))
        self._b_pend.setToolTip('在右侧面板查看全部未归箱会话（%d 条），可多选后批量迁移。'
                                % len(pend))
        self._pend.setToolTip(self._pend.currentText() if _can
                              else '当前没有未归箱会话')
        self._pbox.clear()
        for b in self._tree():
            _b, _d = b
            self._pbox.addItem(('　↳ ' if _d else '')
                               + ux.device_box_name(_b.get('id'), _b.get('name')),
                               str(_b.get('id') or ''))
        self._pbox.setToolTip(self._pbox.currentText() or '选择迁入的目标分组')
        self._panel.set_groups(self._groups_for_combo())
        # 1.5.8：未归箱不是真实箱，不能被「箱已不存在」的清理逻辑清掉
        _ids = {str(b.get('id')) for b in boxes} | {ux.UNASSIGNED}
        if self._cur and self._cur not in _ids:
            self._cur = ''
        # 2.1.1：默认展示第一个分组，右栏不再以「组内会话 0 条」的空占位开场。
        if not self._cur and boxes:
            self._cur = str(boxes[0].get('id') or '')
        self._show_group(self._cur, force=force_panel)
        self._sel_lb.setText('已选 %d 个' % len(self._sel))
        self._b_merge.setText('合并所选 %d 个箱' % len(self._sel)
                              if len(self._sel) > 1 else '合并所选箱')
        self._b_undo.setEnabled(isinstance((self.model or {}).get('merge_undo'), dict))
        _rev = [ux.device_box_name(b.get('id'), b.get('name')) for b in boxes
                if (b.get('id') in ((self.model or {}).get('review') or []))]
        _moved = any(float(b.get('move') or 0) > 0 for b in boxes)
        if not _rev:
            self._hint.set_full('★ 固定组不会被重新分组改写；选中卡片可在右侧查看并迁移会话。')
        elif _moved:
            self._hint.set_full('待确认：%s —— 由最近中心迁移而来，可点「改名」调整。'
                                % '、'.join(_rev))
        else:
            self._hint.set_full('待确认：%s —— 首次分组，请核对名称。'
                                % '、'.join(_rev))

    def _show_group(self, bid, force=False):
        """右栏切换到指定分组（内嵌，不新开窗口）。

        1.5.8 修复：未归箱（ux.UNASSIGNED）不是真实箱 id，必须先分支，
        否则会走「箱不存在 → 空面板」提前返回，入口点了没反应。
        """
        bid = str(bid or '')
        if not bid:
            self._cur = ''
            self._panel.load('', [], '组内会话')
            return
        _b = None
        if bid != ux.UNASSIGNED:
            _b = next((x for x in ((self.model or {}).get('boxes') or [])
                       if str(x.get('id')) == bid), None)
            if _b is None:
                self._cur = ''
                self._panel.load('', [], '组内会话')
                return
        if not force and self._panel.bid == bid:
            return
        self._cur = bid
        _thr = float(self.p.get('devShapeThr') or 16) / 10.0
        _cons = bool(self.p.get('devHourGuard'))
        if bid == ux.UNASSIGNED:
            mem = self._pending_sessions()
            _nm = '未归箱会话'
        else:
            mem = ux.group_members(self.model, bid, self.rows, self._shapes,
                                   _thr, _cons)
            _nm = ux.device_box_name(bid, _b.get('name'))
        self._panel.load(bid, mem, _nm)
        self._fit_deferred()

    def _after_panel(self):
        self._reload(True)

    # ---- 结构操作（统一走 group_mutate） ----
    def _after(self, r, what):
        if not r.get('ok'):
            QMessageBox.warning(self, what, r.get('msg') or (what + '失败'))
            return False
        if callable(self.on_saved):
            try:
                self.on_saved()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:groups')
        self._reload(True)   # 结构变更后右栏强制重读，避免标题/列表停留在旧状态
        return True

    def _new_group(self, parent=''):
        name, ok = QInputDialog.getText(self, '新建分组', '分组名称')
        name = (name or '').strip()
        if not ok or not name:
            return
        self._after(ux.group_mutate(self.p, 'create', name=name, parent=parent),
                    '新建分组')

    def _new_sub(self):
        _idx = self._sel_rows()
        boxes = (self.model or {}).get('boxes') or []
        if len(_idx) != 1 or _idx[0] >= len(boxes):
            QMessageBox.information(self, '新建二级组', '请先选中一个一级分组，再点「新建二级组」。')
            return
        _p = boxes[_idx[0]]
        if str(_p.get('parent') or ''):
            QMessageBox.information(self, '新建二级组', '二级分组下不能再建子组（最多两级）。')
            return
        self._new_group(str(_p.get('id') or ''))

    def _sel_rows(self):
        return [i for i, b in enumerate((self.model or {}).get('boxes') or [])
                if str(b.get('id')) in self._sel]

    def _merge_boxes(self):
        """多箱合并（结构操作）：成员全部并入、条数守恒，可指定名称，保留一步撤销。

        合并只改结构，不重新判定既有会话：参与合并各箱的成员逐条钉到保留组；
        中心重算只用于之后新会话的判定。人工/自动不一致的冲突按
        「人工 > 模型 > 规则 > 未归箱」保留人工归属，并在确认框里列出。
        """
        _idx = self._sel_rows()
        boxes = (self.model or {}).get('boxes') or []
        if len(_idx) < 2:
            self._sum.set_full('请先在卡片上选中 2 个及以上分组再合并。')
            return
        _selb = [boxes[i] for i in _idx if 0 <= i < len(boxes)]
        _ids = [str(b.get('id')) for b in _selb]
        _idset = set(_ids)
        _names = [ux.device_box_name(b.get('id'), b.get('name')) for b in _selb]
        _thr = float(self.p.get('devShapeThr') or 16) / 10.0
        _cons = bool(self.p.get('devHourGuard'))
        _live = ux.device_assign(self.rows, self.model, self.sens,
                                 shapes=self._shapes, thr=_thr, cons=_cons)
        _cnt = {str(g['id']): int(g.get('n') or 0) for g in _live.get('groups', [])}
        _un0 = int(_live.get('unassigned') or 0)
        _mem, _keys, _src, _conf = [], [], {}, []
        _nm_of = {str(b.get('id')): ux.device_box_name(b.get('id'), b.get('name'))
                  for b in boxes}
        for _s in self.rows:
            _bid, _ssrc, _ = ux.group_resolve(
                _s, self.model, self.sens, shapes=self._shapes, thr=_thr,
                cons=_cons, detail=True)
            if str(_bid) not in _idset:
                continue
            _mem.append(_s)
            _keys.append(ux.session_key(_s))
            _src[_ssrc] = int(_src.get(_ssrc) or 0) + 1
            if _ssrc == 'manual':       # 人工与自动判定不一致 → 冲突，人工优先
                _ab, _asrc, _ = ux.group_resolve(
                    _s, self.model, self.sens, manual={}, shapes=self._shapes,
                    thr=_thr, cons=_cons, detail=True)
                if str(_ab) != str(_bid):
                    _conf.append((ux.session_key(_s), str(_bid), str(_ab) or '',
                                  _asrc or '未归箱'))
        _total = len(_keys)
        _def = _names[0]
        _nm, ok = QInputDialog.getText(
            self, '合并分组', '合并后名称（留空沿用「%s」）' % _def,
            QLineEdit.Normal, _def)
        if not ok:
            return
        _nm = (_nm or '').strip() or _def
        _warn = ('\n含固定组（★）：合并后该组保持固定保护。'
                 if any(ux.group_is_fixed(b) for b in _selb) else '')
        _srctxt = '、'.join(
            '%s %d' % ({'manual': '人工', 'shape': '形态', 'learn': '学习',
                        'rule': '规则', 'cons': '约束'}.get(_k, _k), _v)
            for _k, _v in _src.items()) or '0'
        if _conf:
            _ex = '；'.join(
                '%s 人工保留「%s」/ 自动判「%s」'
                % (_k, _nm_of.get(_b, _b), _nm_of.get(_a, _a) or '未归箱')
                for _k, _b, _a, _x in _conf[:3])
            _ctxt = ('冲突 %d 条（人工与自动判定不一致）：按「人工 > 模型 > 规则 > 未归箱」'
                     '保留人工归属并全部并入本组，不静默改判。示例：%s%s'
                     % (len(_conf), _ex, ' 等' if len(_conf) > 3 else ''))
        else:
            _ctxt = '冲突 0 条（没有人工与自动判定不一致的会话）。'
        if QMessageBox.question(
                self, '确认合并',
                '把 %d 个分组合并为「%s」：\n%s\n成员 %d 条将全部并入（%s），'
                '条数守恒，其它箱与未归箱不变。\n%s\n'
                '中心按这批成员重算，只影响之后的新会话，不回写已有归属；'
                '来源标 manual。%s\n合并后可点「撤销合并」恢复一次。'
                % (len(_ids), _nm, '、'.join(_names), _total, _srctxt,
                   _ctxt, _warn)) != QMessageBox.Yes:
            return
        self._sel.clear()
        if self._after(ux.group_mutate(self.p, 'merge', bids=_ids, name=_nm,
                                       target='', keys=_keys,
                                       n_full=30, n_total=_total),
                       '合并分组'):
            _rc = self._recenter(_ids, _mem)
            self.model = ux.device_model_load(self.p) or self.model
            _live2 = ux.device_assign(self.rows, self.model or {}, self.sens,
                                      shapes=self._shapes, thr=_thr, cons=_cons)
            _cnt2 = {str(g['id']): int(g.get('n') or 0)
                     for g in _live2.get('groups', [])}
            _un1 = int(_live2.get('unassigned') or 0)
            _keep_n = _cnt2.get(_ids[0], 0)
            _other = ['「%s」%d→%d' % (_nm_of.get(_b, _b), _a, _c)
                      for _b, _a, _c in
                      ((b, _cnt.get(b, 0), _cnt2.get(b, 0)) for b in _cnt
                       if b not in _idset and _cnt.get(b, 0) != _cnt2.get(b, 0))]
            _chk = ('其它箱与未归箱不变' if not _other and _un1 == _un0 else
                    '其它变化：%s%s' % ('、'.join(_other) or '',
                                       ('' if _un1 == _un0
                                        else '未归箱 %d→%d' % (_un0, _un1))))
            _cf = ('；冲突 %d 条已按人工优先保留' % len(_conf) if _conf else '')
            self._sum.set_full(
                '已合并 %d 个箱为「%s」：成员 %d 条全部并入，本箱现 %d 条（条数守恒）；'
                '%s；来源 manual%s%s，可撤销一次。'
                % (len(_ids), _nm, _total, _keep_n, _chk, _cf, _rc))

    def _recenter(self, bids, members=None):
        """合并后按合并前的成员重算保留组中心（只服务后续新会话，不改既有归属）。"""
        _vecs = [ux.dev_vec(s) for s in (members or []) if isinstance(s, dict)]
        _c = ux.dev_center(_vecs)
        if not _c:
            return ''
        _m = ux.device_model_load(self.p) or {}
        _kid = str(bids[0])
        for _b in _m.get('boxes') or []:
            if str(_b.get('id')) == _kid:
                _b['center'] = _c
        ux.device_save(self.p, _m)
        ux.learn_log('GROUP op=recenter bid=%s keys=%d' % (_kid, len(_vecs)))
        return '、中心已按 %d 条成员重算（仅影响之后的新会话）' % len(_vecs)

    def _undo_merge(self):
        _u = (self.model or {}).get('merge_undo')
        if not isinstance(_u, dict) or not _u.get('boxes'):
            self._sum.set_full('没有可撤销的合并')
            return
        _names = '、'.join(ux.device_box_name(b.get('id'), b.get('name'))
                          for b in (_u.get('boxes') or []))
        if QMessageBox.question(
                self, '撤销合并',
                '恢复合并前的 %d 个分组：%s？' % (len(_u.get('boxes') or []),
                                                _names)) != QMessageBox.Yes:
            return
        _ids = [str(b.get('id')) for b in (_u.get('boxes') or [])]
        self._sel.clear()
        if self._after(ux.group_mutate(self.p, 'merge_undo'), '撤销合并'):
            _thr = float(self.p.get('devShapeThr') or 16) / 10.0
            _live = ux.device_assign(self.rows, self.model or {}, self.sens,
                                     shapes=self._shapes, thr=_thr,
                                     cons=bool(self.p.get('devHourGuard')))
            _cnt = {str(g['id']): int(g.get('n') or 0)
                    for g in _live.get('groups', [])}
            _nm_of = {str(b.get('id')): ux.device_box_name(b.get('id'), b.get('name'))
                      for b in ((self.model or {}).get('boxes') or [])}
            self._sum.set_full(
                '已撤销合并，恢复：%s；未归箱 %d 条（成员已按原归属还原，条数守恒）。'
                % ('、'.join('「%s」%d 条' % (_nm_of.get(i, i), _cnt.get(i, 0))
                             for i in _ids),
                   int(_live.get('unassigned') or 0)))

    def _ai_review(self):
        """AI 复核：建议先预览；「一键采纳全部」立即写入分组、重训并刷新。"""
        def _done(res=None):
            self._reload(True)
            if res:
                _txt = str(res.get('msg') or '已采纳')
                self._sum.set_full(_txt)
                self._sum.setToolTip(_txt)
        BoxReviewDialog(self, self.p, self.rows, self.model,
                        getattr(self, '_shapes', None) or {},
                        notify=lambda _m, _lv='info': self._sum.setText(_m),
                        on_done=_done).exec()

    def _edit_cons(self):
        _idx = self._sel_rows()
        boxes = (self.model or {}).get('boxes') or []
        if len(_idx) != 1 or _idx[0] >= len(boxes):
            self._sum.setText('请先选中一个分组，再编辑维度约束。')
            return
        b = boxes[_idx[0]]
        _bid = str(b.get('id'))
        _manual = (self.model or {}).get('manual') or {}
        _mem = [s for s in self.rows
                if str(_manual.get(ux.session_key(s)) or '') == _bid]
        if not _mem:
            _mem = ux.group_members(self.model, _bid, self.rows, self._shapes,
                                    float(self.p.get('devShapeThr') or 16) / 10.0,
                                    bool(self.p.get('devHourGuard')))
        BoxConsDialog(self, self.p, b, _mem, on_saved=self._reload).exec()

    def _pending_sessions(self):
        boxes = (self.model or {}).get('boxes') or []
        if not boxes:
            return []
        _learn = ux.learn_load()
        _thr = float(self.p.get('devShapeThr') or 16) / 10.0
        _cons = bool(self.p.get('devHourGuard'))
        return [s for s in self.rows
                if not ux.group_resolve(s, self.model, self.sens,
                                        shapes=self._shapes, thr=_thr,
                                        cons=_cons, learn=_learn)]

    def _manual_assign(self):
        pend = self._pending_sessions()
        r = self._pend.currentIndex()
        if not (0 <= r < len(pend)) or not self.model:
            return
        bid = self._pbox.currentData()
        if not bid:
            return
        _k = ux.session_key(pend[r])
        _sh = self._shapes.get(_k)
        cv = {_k: ux.cluster_vec(pend[r], _sh)} if _sh else {}
        self._after(ux.group_mutate(self.p, 'move', keys=[_k], target=str(bid),
                                    cvecs=cv, rows=self.rows,
                                    n_full=int(self.p.get('devFullTrainN') or 30)),
                    '归入分组')

    def _open_curve_bins(self):
        """按曲线形态聚类看分箱结果（只预览；命名后落库再刷新卡片）。"""
        items, err = load_cluster_items()
        if err or not items:
            self._sum.setText('曲线数据不可用：%s' % (err or '暂无可聚类的充电会话'))
            return
        BinClusterDialog(self, self.p, items=items,
                         on_applied=lambda: self._reload(True)).exec()

    def _rebuild(self):
        """重新分组（唯一入口）：固定组只增不改，非固定组重排，偏离/无法判定单列。"""
        _cap = ux.device_cap(self.p)['cap']
        _thr = float(self.p.get('devShapeThr') or 16) / 10.0
        _cons = bool(self.p.get('devHourGuard'))
        built = ux.device_build(self.rows, self._kcount(), self.sens, self.model,
                                max_boxes=_cap, shapes=self._shapes,
                                thr=_thr, cons=_cons)
        if not built.get('ok'):
            self._tip = built.get('msg', '')
            self._sum.set_full(self._tip)
            QMessageBox.warning(self, '重新分组', self._tip)
            return
        self.model = built
        ux.device_save(self.p, built)
        _sm = built.get('summary') or {}
        _fx = _sm.get('fixed') or {}
        _lr = _sm.get('learn') or {}
        ux.learn_log('REGROUP k=%s sens=%s boxes=%d fixed=%d cap=%d '
                     'fixed_removed=%s added_fixed=%s new_boxes=%s merged=%s '
                     'outliers=%s undecided=%s conf=%s→%s'
                     % (built.get('k'), self.sens,
                        len(built.get('boxes') or []), int(_fx.get('boxes') or 0),
                        _cap, _fx.get('removed'), _sm.get('added_fixed'),
                        len(_sm.get('new_boxes') or []),
                        len(_sm.get('merged_boxes') or []),
                        _sm.get('outliers'), _sm.get('undecided'),
                        _lr.get('conf_before'), _lr.get('conf_after')))
        self._sel.clear()
        self._reload()
        self._last_regroup = _sm
        _txt = str(built.get('note') or '重新分组完成')
        self._sum.set_full(_txt)
        self._sum.setToolTip(_txt)
        if _fx and not _fx.get('intact', True):
            self._sum.set_full('重新分组完成，但固定组有 %d 条成员被改动，请核对审计。'
                               % int(_fx.get('removed') or 0))

    # ---- 卡片 ----
    def _members(self, boxes):
        _thr = float(self.p.get('devShapeThr') or 16) / 10.0
        _cons = bool(self.p.get('devHourGuard'))
        return {str(b.get('id')): ux.group_members(self.model, str(b.get('id')),
                                                  self.rows, self._shapes,
                                                  _thr, _cons)
                for b in boxes}

    @staticmethod
    def _rng(d, nd=1):
        if not d:
            return '—'
        return ('%.' + str(nd) + 'f–%.' + str(nd) + 'f') % (float(d[0]), float(d[2]))

    def _dims_txt(self, mem):
        g = {k: ux.box_dim_dist(mem, k) for k, _l, _kd, _u in ux.BOX_CONS_DIMS}
        _ports = sorted({str(s.get('port') or '-') for s in mem})
        _hr = g.get('hour')
        if not mem:
            return '暂无会话'
        # 2.1.1：第三行末尾补「主协议 X N%」（仅展示，用于人工校正与分组判断）。
        return ('峰 %sW · 均 %sW · 电量 %sWh\n'
                '时长 %s分 · 均压 %sV · 均流 %sA\n'
                '峰均 %s× · 口 %s · 时段 %s点 · 主协议 %s'
                % (self._rng(g.get('peak_power_w'), 0),
                   self._rng(g.get('avg_power_w'), 1),
                   self._rng(g.get('total_wh'), 1),
                   self._rng(g.get('duration_min'), 0),
                   self._rng(g.get('avg_voltage'), 1),
                   self._rng(g.get('avg_current'), 2),
                   self._rng(g.get('ratio'), 1),
                   '/'.join(_ports) or '—',
                   ('%d–%d' % (int(_hr[0]), int(_hr[2]))) if _hr else '—',
                   self._main_proto(mem)))

    @staticmethod
    def _main_proto(mem):
        """组内占比最高的协议（短码口径），返回「PD 3.0 62%」；空组返回「—」。"""
        _c = {}
        for _s in mem or []:
            _p = ux.proto_short(_s.get('protocol'))
            _c[_p] = _c.get(_p, 0) + 1
        _tot = sum(_c.values())
        if not _tot:
            return '—'
        _k = max(_c, key=lambda x: (_c[x], x != '—'))
        _nm = '未知' if _k == '—' else ux.proto_full(_k).split('（')[0]
        return '%s %d%%' % (_nm, int(round(_c[_k] * 100.0 / _tot)))

    _SRC_LABEL = {'preset': '预置', 'user': '自建', 'manual': '自建',
                  'cluster': '聚类', 'auto': '自动'}

    def _tag_txt(self, b, n):
        """卡片底行：条数 · 类型 · 来源（+ 子组数），统一措辞，无技术字段。"""
        _kid = len(ux.group_children(self.model, str(b.get('id') or '')))
        _t = '%d 条 · %s · 来源 %s' % (
            int(n), '固定组' if ux.group_is_fixed(b) else '普通组',
            self._SRC_LABEL.get(str(b.get('src') or ''), '自建'))
        return _t + (' · 子组 %d' % _kid if _kid else '')

    def _card(self, b, n_by_id, mem_by_id, depth, h, fs):
        bid = str(b.get('id') or '')
        on = bid in self._sel
        cur = bid == self._cur
        _fix = ux.group_is_fixed(b)
        wd = QFrame()
        wd.setObjectName('binCard')
        wd.setCursor(Qt.PointingHandCursor)
        wd.setToolTip('固定组：重新分组不会改写名称与成员；可改名、改约束、迁移成员。'
                      if _fix else '普通组：重新分组可能重写名称与结构；点 ☆ 可锁定为固定组。')
        wd.setStyleSheet(
            'QFrame#binCard { background:%s; border:1px solid %s; border-left:%s;'
            ' border-radius:10px; }'
            % ('rgba(78,168,255,0.13)' if on else 'rgba(255,255,255,0.035)',
               '#4ea8ff' if on else ('#3f7fbf' if cur else 'rgba(255,255,255,0.10)'),
               ('3px solid #fbbf24' if _fix else
                ('3px solid rgba(255,255,255,0.14)' if depth else '1px solid transparent'))))
        g = QGridLayout(wd)
        g.setContentsMargins(10 + (12 if depth else 0), 6, 8, 5)
        g.setHorizontalSpacing(5)
        g.setVerticalSpacing(2)
        _sh = list(((b.get('sc') or {}).get('mean') or [])[:ux.CLUSTER_N])
        # 1.5.9：标题口径改为「标题占 col0-3，曲线缩略图 col4，星标 col5」，
        # 与下方三行同起于 col0，左缘对齐、右缘对齐。
        g.addWidget(_CurveThumb(_sh, w=92, h=28), 0, 4, 1, 1)
        _nm = ('↳ ' if depth else '') + ux.device_box_name(bid, b.get('name'))
        if _fix:
            _nm = '★ ' + _nm
        _n = int(n_by_id.get(bid, b.get('n') or 0))
        # 2.1.1：分组名固定单行完整显示（按宽度缩字，最小 10px），不再折成两行。
        _nm_lb = _FitWrapLabel(_nm, 1, 13, 10, bold=True)
        _nm_lb.setStyleSheet('color:#f3f7fc; background:transparent;')
        g.addWidget(_nm_lb, 0, 0, 1, 4)
        _lk = aligned_button('★' if _fix else '☆', 'ghost', min_w=26, h=22)
        _lk.setToolTip('固定组（点击解锁）' if _fix else '普通组（点击锁定为固定组）')
        _lk.clicked.connect(lambda: self._toggle_lock(bid))
        g.addWidget(_lk, 0, 5)
        _dim = _FitWrapLabel(self._dims_txt(mem_by_id.get(bid) or []), 3, fs, 8)
        _dim.setStyleSheet('color:%s; background:transparent;' % TEXT_SUB)
        g.addWidget(_dim, 1, 0, 1, 6)
        _chips = QHBoxLayout()
        _chips.setContentsMargins(0, 0, 0, 0)
        _chips.setSpacing(5)
        for _t, _fn in (('改名', lambda: self._rename_box(bid)),
                        ('约束', lambda: self._cons_for(bid)),
                        ('会话', lambda: self._open_group(bid)),
                        ('子组', lambda: self._new_sub_for(bid)),
                        ('删除', lambda: self._del_box(bid))):
            # 2.1.1：最小宽按文字实际需要给足（2 字 + 左右内边距 = 56），
            # 不再允许压到 44 把文字裁掉；多出的宽度均分，五个按钮同一基线。
            _bt = aligned_button(_t, 'ghost', min_w=56, h=22)
            _bt.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            _bt.clicked.connect(_fn)
            _chips.addWidget(_bt, 1)
        g.addLayout(_chips, 2, 0, 1, 6)
        _tag = _FitWrapLabel(self._tag_txt(b, _n), 1, 11, 8)
        _tag.setStyleSheet('color:%s; background:transparent;' % TEXT_SUB)
        g.addWidget(_tag, 3, 0, 1, 6)
        g.setColumnStretch(1, 1)
        wd.mousePressEvent = lambda ev, _b=bid: self._pick(_b)
        # 高度由内容结构决定（3 行形态 + 1 行标签），与条数/文字长度无关
        wd.setFixedHeight(wd.sizeHint().height())
        return wd

    def _render_cards(self, boxes, live):
        # 1.5.8：刷新只换内容，滚动位置沿用用户当前值（没有自动定位/回弹）
        _pos = self._cards_scroll.scroll_pos() if hasattr(self, '_cards_scroll') else 0
        while self._cards.count():
            it = self._cards.takeAt(0)
            _w = it.widget()
            if _w is not None:
                _w.setParent(None)
                _w.deleteLater()
        self._sel &= {str(b.get('id')) for b in boxes}
        if not boxes:
            _e = QLabel('还没有分组：点上方「重新分组」按真实曲线分，或「新建一级组」手工建。')
            _e.setObjectName('sub')
            self._cards.addWidget(_e, 0, 0, 1, 2)
            self._keep_scroll(_pos)
            return
        mem = self._members(boxes)
        n_by_id = {str(g['id']): g['n'] for g in (live or {}).get('groups', [])}
        _tree = self._tree()
        # 1.5.8：分箱列表固定单列纵向排列，箱数多时只在列表内竖向滚动；
        # 不再铺多列或换行堆叠（与主页面整页无滚动条的规则区分）。
        for i, (_b, _d) in enumerate(_tree):
            self._cards.addWidget(self._card(_b, n_by_id, mem, _d, 0, 10), i, 0)
        self._cards.setColumnStretch(0, 1)
        # 2.1.1：不再用尾行 stretch 把空白留在列表底部；卡间距由 _fit_cards
        # 按主体可用高度均分，卡片正好铺到与右列同一条底边。
        self._cards.setRowStretch(len(_tree), 0)
        self._keep_scroll(_pos)
        self._fit_deferred()

    def _keep_scroll(self, pos):
        """布局刷新后回到用户原来的滚动位置，不做任何自动定位/动画。"""
        if not hasattr(self, '_cards_scroll'):
            return
        self._cards_scroll.hold(pos)
        self._cards_scroll.restore()
        QTimer.singleShot(0, self._cards_scroll.restore)

    # ---- 主体高度：右列按内容收敛，左列用间距铺满同一条底边 (2.1.1) ----
    def _fit_deferred(self):
        """布局稳定后再量一次（show/重建卡片/切组都会改变自然高度）。"""
        if hasattr(self, '_fit_timer'):
            self._fit_timer.start()

    def _fit_body(self):
        if not self.isVisible() or not hasattr(self, '_panel'):
            return
        _room = int(self._ops.cellRect(3, 0).height())
        if _room < 80:
            return
        _need_l = self._cards_need()
        # 行数由主体可用高度决定（不留空白）；再与左列自然高取大，两列同高同底。
        self._panel.set_room(_room)
        _need_r = int(self._panel.content_need())
        _h = min(_room, max(_need_r, _need_l))
        self._body_h = _h
        self._left.setFixedHeight(_h)
        self._right.setFixedHeight(_h)
        self._panel.apply_height(_h)
        self._fit_cards(_h)
        # 2.1.2：点选/切换/刷新只重排内容，不重置滚动位置；hold 里保存的是
        # 用户的目标位置，布局未完成时 restore 会被旧上限临时夹住，
        # 上限恢复后由 _CardScroll._sync 自动补回目标位置。
        self._cards_scroll.restore()
        QTimer.singleShot(0, self._cards_scroll.restore)

    def _cards_need(self):
        _ws = [self._cards.itemAt(i).widget() for i in range(self._cards.count())]
        _ws = [w for w in _ws if w is not None]
        if not _ws:
            return 0
        return 22 + sum(int(w.height()) for w in _ws) + 8 * (len(_ws) - 1)

    def _fit_cards(self, h):
        """把卡间距/上下留白按 h 均分：卡片正好铺到底边，不在列表底部留空。"""
        _ws = [self._cards.itemAt(i).widget() for i in range(self._cards.count())]
        _ws = [w for w in _ws if w is not None]
        _n = len(_ws)
        if not _n:
            return
        _sum = sum(int(w.height()) for w in _ws)
        _avail = int(h) - _sum
        if _n == 1:
            _top = max(10, (int(h) - _sum) // 2)
            self._cards.setVerticalSpacing(8)
            self._cards.setContentsMargins(10, _top, 10, 10)
            return
        if _avail >= 20 + 8 * (_n - 1):
            _m = 10
            _gap = (_avail - 20) // (_n - 1)
        elif _avail >= 8 + 4 * (_n - 1):
            _m = 4
            _gap = (_avail - 8) // (_n - 1)
        else:
            # 放不下：维持可读间距并允许纵向滚动，滚动位置由 _fit_body 按原值保持
            _m = 10
            _gap = 8
        _rest = max(0, _avail - 2 * _m - _gap * (_n - 1))
        self._cards.setVerticalSpacing(_gap)
        self._cards.setContentsMargins(10, _m + _rest // 2, 10,
                                       _m + _rest - _rest // 2)

    def _pick(self, bid):
        if bid in self._sel:
            self._sel.discard(bid)
        else:
            self._sel.add(bid)
        self._reload()

    def _cons_for(self, bid):
        self._sel = {bid}
        self._reload()
        self._edit_cons()

    def _new_sub_for(self, bid):
        self._sel = {bid}
        self._reload()
        self._new_sub()

    def _open_group(self, bid):
        """卡片「会话」：在右侧内嵌面板查看（不新开窗口）。"""
        self._show_group(bid, force=True)

    def _open_unassigned(self):
        """未归箱会话：同样在右侧面板内呈现。"""
        self._show_group(ux.UNASSIGNED, force=True)

    def _toggle_lock(self, bid):
        b = next((x for x in ((self.model or {}).get('boxes') or [])
                  if str(x.get('id')) == bid), None)
        if b is None:
            return
        if ux.group_is_fixed(b):
            if b.get('preset'):
                _msg = ('「%s」是出厂固定组，解锁后重新分组可能改写其成员。\n'
                        '确定解锁？' % ux.device_box_name(bid, b.get('name')))
            else:
                _msg = ('「%s」是固定组，解锁后重新分组可能改写其名称与成员。\n'
                        '确定解锁？' % ux.device_box_name(bid, b.get('name')))
            if QMessageBox.question(self, '解锁固定组', _msg) != QMessageBox.Yes:
                return
            self._after(ux.group_mutate(self.p, 'unlock', bid=bid, confirm=True), '解锁')
        else:
            self._after(ux.group_mutate(self.p, 'lock', bid=bid), '锁定')

    def _rename_box(self, bid):
        boxes = (self.model or {}).get('boxes') or []
        b = next((x for x in boxes if str(x.get('id')) == bid), None)
        if b is None:
            return
        old = ux.device_box_name(bid, b.get('name'))
        name, ok = QInputDialog.getText(
            self, '组改名', '分组名称（改名不改变固定/非固定状态）',
            QLineEdit.Normal, old)
        name = (name or '').strip()
        if not ok or not name or name == old:
            return
        self._after(ux.group_mutate(self.p, 'rename', bid=bid, name=name), '改名')

    def _del_box(self, bid):
        b = next((x for x in ((self.model or {}).get('boxes') or [])
                  if str(x.get('id')) == bid), None)
        if b is None:
            return
        _nm = ux.device_box_name(bid, b.get('name'))
        _n = len(ux.group_members(self.model, bid, self.rows, self._shapes,
                                  float(self.p.get('devShapeThr') or 16) / 10.0,
                                  bool(self.p.get('devHourGuard'))))
        _kids = ux.group_children(self.model, bid)
        _msg = ('删除分组「%s」？\n组内 %d 条会话将回到未归箱；%s'
                '删除后立即重算中心并触发全量重训。'
                % (_nm, _n, ('其下 %d 个二级组会一并删除；' % len(_kids)) if _kids else ''))
        if ux.group_is_fixed(b):
            _msg += '\n注意：该组为固定组（★），删除后不再受保护。'
        if QMessageBox.question(self, '确认删除', _msg) != QMessageBox.Yes:
            return
        self._after(ux.group_mutate(self.p, 'delete', bid=bid, confirm=True), '删除')

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit_deferred()

    def _back(self):
        """关闭/返回：嵌入二级页时由主窗口收回本页，弹窗模式走 reject。"""
        if self._embedded:
            self.closed.emit()
        else:
            self.reject()

    def _confirm(self):
        if self.model:
            self.model['review'] = []
            ux.device_save(self.p, self.model)
        if callable(self.on_saved):
            try:
                self.on_saved()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:c40dev')
        if self._embedded:
            self.closed.emit()
        else:
            self.accept()


def project_dir():
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(os.path.abspath(sys.executable))
        # R28：后端源码随包，只认包内位置；不再回退到开发机绝对路径
        candidates = [os.path.join(base, 'backend'),
                      os.path.join(base, '_internal', 'backend'),
                      base]
        for c in candidates:
            if os.path.exists(os.path.join(c, 'ha_server.py')):
                return c
        raise RuntimeError('backend 未随包：找不到 ha_server.py（%s）' % candidates)
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (os.path.join(here, 'backend'), here):
        if os.path.exists(os.path.join(c, 'ha_server.py')):
            return c
    if CANONICAL_DIR and os.path.exists(os.path.join(CANONICAL_DIR, 'ha_server.py')):
        return CANONICAL_DIR
    raise RuntimeError('backend 未找到：%s 下无 ha_server.py' % here)


def app_data_dir():
    """持久化数据目录（数据库/运行态），应用更新（app 目录被覆盖）不丢失。
    - 独立打包：%LOCALAPPDATA%\\CuktechPanel\\data
    - 开发模式：后端项目目录
    """
    if getattr(sys, 'frozen', False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "CuktechPanel", "data")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            return project_dir()
    return project_dir()


def _db_session_count(path):
    """只读统计 charge_sessions 行数；失败返回 -1。"""
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        n = c.execute("SELECT COUNT(*) FROM charge_sessions").fetchone()[0]
        c.close()
        return n
    except sqlite3.Error:
        return -1


def _history_db_path():
    """历史库路径：优先后端注入的 CUKTECH_HISTORY_DB_PATH，否则持久化目录。"""
    p = os.environ.get('CUKTECH_HISTORY_DB_PATH')
    if p and os.path.exists(p):
        return p
    return os.path.join(app_data_dir(), 'port_history.db')


def load_cluster_items(limit=4000):
    """只读会话 + 曲线 → [{key, session, shape}]，供「按曲线聚类」预览。"""
    import sqlite3
    path = _history_db_path()
    try:
        con = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)
        con.row_factory = sqlite3.Row
        ss = [dict(r) for r in con.execute(
            "SELECT id, port, start_time, end_time, duration_sec, total_wh,"
            " avg_power_w, avg_voltage, avg_current, peak_power_w"
            " FROM charge_sessions WHERE end_time IS NOT NULL AND duration_sec >= 60"
            " ORDER BY start_time DESC LIMIT %d" % int(limit))]
        pts = {}
        for r in con.execute("SELECT session_id, timestamp, power FROM charge_points"
                             " ORDER BY session_id, timestamp"):
            pts.setdefault(r['session_id'], []).append((r['timestamp'], r['power'] or 0.0))
        con.close()
    except Exception as exc:
        return [], str(exc)
    items = []
    for s in ss:
        sh = ux.curve_shape(pts.get(s['id']) or [])
        if sh:
            items.append({'key': ux.session_key(s), 'session': s, 'shape': sh})
    return items, ''


def load_shapes(session_ids):
    """只读指定会话的 32 点归一化形态（改归属时同步箱中心用）。

    1.5.8：加两级缓存，避免每次都在主线程重读 40 万点（实测 410–450 ms）。
      · 进程内：{sid: (点数, 最后时刻, 形态)}，库文件 (mtime, size) 未变时直接命中；
      · 跨进程：%LOCALAPPDATA%\\CuktechPanel\\shape_cache.json，重启后首屏同样命中；
      · 库有写入时先查每会话 (count, max(ts))（实测 ~75 ms），只重算变化的会话。
    """
    import sqlite3
    ids = []
    for _x in (session_ids or []):
        try:
            ids.append(int(_x))
        except Exception:
            pass
    if not ids:
        return {}
    global _shape_cache_loaded
    if not _shape_cache_loaded:
        _shape_cache_loaded = True
        _shape_cache_load()
    path = _history_db_path()
    fp = _db_fingerprint(path)
    cache = _SHAPE_CACHE['by_id']
    if _SHAPE_CACHE['fp'] == fp and all(_i in cache for _i in ids):
        return {_i: cache[_i][2] for _i in ids if _i in cache}
    try:
        con = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)
        try:
            q = ('SELECT session_id, COUNT(*), MAX(timestamp) FROM charge_points'
                 ' WHERE session_id IN (%s) GROUP BY session_id'
                 % ','.join('?' * len(ids)))
            sig = {int(r[0]): (int(r[1]), int(r[2] or 0))
                   for r in con.execute(q, ids)}
            stale = [_i for _i in ids
                     if _i not in cache or tuple(cache[_i][:2]) != sig.get(_i)]
            if stale:
                pts = {}
                q2 = ('SELECT session_id, timestamp, power FROM charge_points'
                      ' WHERE session_id IN (%s) ORDER BY session_id, timestamp'
                      % ','.join('?' * len(stale)))
                for r in con.execute(q2, stale):
                    pts.setdefault(r[0], []).append((r[1], r[2] or 0.0))
                for _i in stale:
                    _sh = ux.curve_shape(pts.get(_i) or [])
                    _s = sig.get(_i)
                    if _sh and _s:
                        cache[_i] = (_s[0], _s[1], _sh)
                    else:
                        cache.pop(_i, None)
        finally:
            con.close()
    except Exception:
        return {}
    _SHAPE_CACHE['fp'] = fp
    if len(cache) > 5000:                     # 只留最近的会话，缓存文件不无限增长
        for _i in sorted(cache)[:-4000]:
            cache.pop(_i, None)
    _shape_cache_save(fp)
    return {_i: cache[_i][2] for _i in ids if _i in cache}


_SHAPE_CACHE = {'fp': None, 'by_id': {}}
_shape_cache_loaded = False


def _db_fingerprint(path):
    try:
        st = os.stat(path)
        return (path, int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (path, 0, 0)


def _shape_cache_file():
    return os.path.join(app_data_dir(), 'shape_cache.json')


def _shape_cache_load():
    try:
        with open(_shape_cache_file(), encoding='utf-8') as f:
            d = json.load(f)
        fp = tuple(d.get('fp') or ())
        if len(fp) == 3 and fp == _db_fingerprint(_history_db_path()):
            _SHAPE_CACHE['fp'] = fp
            _SHAPE_CACHE['by_id'] = {int(k): (int(v[0]), int(v[1]), list(v[2]))
                                     for k, v in (d.get('s') or {}).items()}
    except (OSError, ValueError, TypeError, IndexError):
        pass


def _shape_cache_save(fp):
    try:
        _d = app_data_dir()
        os.makedirs(_d, exist_ok=True)
        with open(_shape_cache_file(), 'w', encoding='utf-8') as f:
            json.dump({'fp': list(fp),
                       's': {str(k): [v[0], v[1], v[2]]
                             for k, v in _SHAPE_CACHE['by_id'].items()}}, f)
    except (OSError, TypeError, ValueError):
        pass


class _CurveThumb(QWidget):
    """簇代表曲线缩略图（32 点归一化形态，纯绘制，不加图层）。"""

    def __init__(self, shape, parent=None, w=132, h=42):
        super().__init__(parent)
        self._sh = list(shape or [])
        self.setFixedSize(w, h)

    def paintEvent(self, ev):
        from PySide6.QtGui import QPolygonF
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(2, 4, -2, -4)
        p.setPen(QPen(QColor(120, 190, 255, 70), 1))
        p.drawLine(r.left(), r.bottom(), r.right(), r.bottom())
        if len(self._sh) < 2:
            return
        p.setPen(QPen(QColor(120, 190, 255), 1.6))
        n = len(self._sh)
        pts = [QPointF(r.left() + r.width() * i / (n - 1.0),
                       r.bottom() - r.height() * max(0.0, min(1.0, v)))
               for i, v in enumerate(self._sh)]
        p.drawPolyline(QPolygonF(pts))


class BinClusterDialog(QDialog):
    """分箱即簇的形态预览 → 候选命名建箱；不点命名不改任何归属。

    上半：每个箱一行（代表曲线 + 条数 + 时长/Wh 区间 + 箱内平均距离 + 最近匹配率）。
    下半：未归箱会话里 k-means 聚出的候选新设备，命名后即成为正式分箱。
    """

    def __init__(self, parent, prefs, items=None, on_applied=None, audit=None):
        super().__init__(parent)
        self.setWindowTitle('按曲线看分箱形态')
        self.p = prefs
        self.items = items or []
        self.by_key = {it['key']: it for it in self.items}
        self.on_applied = on_applied
        self.audit = audit
        self._model = {}
        self._build()
        self._run()
        # 1.5.9：高度随内容收敛（不固定 700 留大片空白），宽度保持两列对齐所需
        _h = self.sizeHint().height() + 8
        self.resize(max(960, self.width()), max(430, min(_h, 760)))

    # ---- 构建 ----
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 12)
        v.setSpacing(10)
        # 1.5.9：行内固定两列信息，给足宽度，避免文字折行压行
        self.setMinimumWidth(900)
        self.resize(960, 700)
        head = QHBoxLayout()
        self.sum_lb = QLabel('正在统计…')
        self.sum_lb.setObjectName('sub')
        head.addWidget(self.sum_lb, 1)
        head.addWidget(QLabel('候选簇数'))
        self.k_cb = QComboBox()
        for _k in range(2, 7):
            self.k_cb.addItem(str(_k), _k)
        self.k_cb.setFixedWidth(64)
        head.addWidget(self.k_cb)
        re_b = QPushButton('重新统计')
        re_b.setMinimumWidth(96)
        re_b.clicked.connect(self._run)
        head.addWidget(re_b)
        v.addLayout(head)
        self.box_lb = QLabel('分箱形态簇')
        self.box_lb.setObjectName('sub')
        v.addWidget(self.box_lb)
        self.box_body = QVBoxLayout()
        self.box_body.setSpacing(6)
        v.addLayout(self.box_body)
        self.cand_lb = QLabel('未归箱会话里的候选新设备')
        self.cand_lb.setObjectName('sub')
        v.addWidget(self.cand_lb)
        self.cand_body = QVBoxLayout()
        self.cand_body.setSpacing(6)
        v.addLayout(self.cand_body)
        v.addStretch(1)
        foot = QHBoxLayout()
        self.undo_b = QPushButton('撤销上一批簇命名')
        self.undo_b.setObjectName('ghost')
        self.undo_b.clicked.connect(self._undo)
        foot.addWidget(self.undo_b)
        foot.addStretch(1)
        close_b = QPushButton('关闭')
        close_b.setMinimumWidth(96)
        close_b.clicked.connect(self.reject)
        foot.addWidget(close_b)
        v.addLayout(foot)

    @staticmethod
    def _clear(lay):
        while lay.count():
            it = lay.takeAt(0)
            wd = it.widget()
            if wd is not None:
                wd.setParent(None)
                wd.deleteLater()

    @staticmethod
    def _range(members, key, div=1.0, nd=0):
        vs = [float(m['session'].get(key) or 0) / div for m in members]
        if not vs:
            return '—'
        return ('%.0f–%.0f' if nd == 0 else '%.' + str(nd) + 'f–%.' + str(nd) + 'f') % (
            min(vs), max(vs))

    # ---- 统计（只预览，不落库） ----
    def _run(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            m = ux.device_model_load(self.p) or {}
            boxes = [b for b in (m.get('boxes') or []) if isinstance(b, dict)]
            manual = dict(m.get('manual') or {})
            self._thr = float(self.p.get('devShapeThr') or 16) / 10.0
            self._cand_min = max(2, int(self.p.get('devCandMin') or 8))
            _full = str(self.p.get('devCenterMode') or '增量') == '全量'
            calc = (ux.box_shape_rebuild(
                {'boxes': [dict(b) for b in boxes], 'manual': manual},
                self.items, manual) if _full else
                {'boxes': [dict(b) for b in boxes], 'manual': manual})
            by_id = {str(b.get('id')): b for b in (calc.get('boxes') or [])}
            members = {}
            for it in self.items:
                _b = str(manual.get(it['key']) or '')
                if _b in by_id:
                    members.setdefault(_b, []).append(it)
            free = [it for it in self.items if str(manual.get(it['key']) or '') not in by_id]
            # 解析统计：箱内平均距离 + 最近中心命中率
            stats = []
            for b in boxes:
                bid = str(b.get('id') or '')
                cb = by_id.get(bid) or {}
                mem = members.get(bid) or []
                hit = 0
                _cvs = []
                for it in mem:
                    _cv = ux.cluster_vec(it['session'], it['shape'])
                    _cvs.append(_cv)
                    _nb, _nd, _thr = ux.box_nearest(_cv, calc, it['session'], self._thr)
                    if _nb == bid:
                        hit += 1
                _ctr = list((cb.get('sc') or {}).get('mean') or [])
                _dist = 0.0
                if _cvs and len(_ctr) == len(_cvs[0]):
                    _dist = sum(ux.shape_dist(v, _ctr) for v in _cvs) / len(_cvs)
                stats.append({
                    'bid': bid, 'name': ux.device_box_name(bid, b.get('name')),
                    'n': len(mem), 'spread': _dist,
                    'match': (hit / len(mem)) if mem else 0.0,
                    'shape': _ctr[:ux.CLUSTER_N],
                    'dur': self._range(mem, 'duration_sec', 60.0),
                    'wh': self._range(mem, 'total_wh', 1.0, 1)})
            self._model = m
            self._calc = calc
            self._stats = stats
            self._free = free
            self.res = ux.cluster_sessions(free, None) if len(free) >= 8 else None
            _dirty = False
            for b in boxes:                      # 派生数据：箱中心随成员重算后落盘
                _nb = by_id.get(str(b.get('id')))
                if (_nb or {}).get('sc') != b.get('sc'):
                    b['sc'] = (_nb or {}).get('sc')
                    _dirty = True
            if _dirty:
                m['boxes'] = boxes
                ux.device_save(self.p, m)
        finally:
            QApplication.restoreOverrideCursor()
        self._render()

    def _render(self):
        self._clear(self.box_body)
        self._clear(self.cand_body)
        st = getattr(self, '_stats', []) or []
        _loose = [s['name'] for s in st if s['n'] and (s['spread'] > 0.9 or s['match'] < 0.9)]
        self.sum_lb.setText('会话 %d 条（有曲线）· 已归箱 %d 条 · 未归箱 %d 条%s'
                            % (len(self.items),
                               sum(s['n'] for s in st), len(self._free),
                               (' · 形态较散待复核：' + '、'.join(_loose)) if _loose else ''))
        if not st:
            _e = QLabel('还没有分箱：先在下方候选里命名，或到记录页右键「添加新分箱」。')
            _e.setObjectName('sub')
            self.box_body.addWidget(_e)
        for s in st:
            self.box_body.addWidget(self._box_row(s))
        _r = self.res or {}
        self.cand_lb.setText(
            '未归箱会话里的候选新设备（k=%s · 轮廓系数 %s · 覆盖 %s 条 · 小簇并入未分类 %s 条）'
            % (_r.get('k', '—'), ('%.3f' % _r['sil']) if _r.get('ok') else '—',
               _r.get('covered', 0), _r.get('small', 0)))
        if not _r.get('ok'):
            _e = QLabel('未归箱会话不足，暂无候选（%s）' % (_r.get('note') or '—'))
            _e.setObjectName('sub')
            self.cand_body.addWidget(_e)
            return
        for c in _r.get('clusters') or []:
            self.cand_body.addWidget(self._cand_row(c))

    def _box_row(self, s):
        # 1.5.9：行内左（缩略图+两行文案）右（指标）两端对齐，文字单行缩字号，
        # 不再折行压到下一行。
        wd = QWidget()
        wd.setFixedHeight(46)
        h = QHBoxLayout(wd)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        h.addWidget(_CurveThumb(s['shape'], w=120, h=40), 0, Qt.AlignVCenter)
        _col = QVBoxLayout()
        _col.setContentsMargins(0, 0, 0, 0)
        _col.setSpacing(2)
        _nm = _FitWrapLabel(s['name'], 1, 12, 9)
        _nm.setStyleSheet('color:#e8eef6; background:transparent;')
        _col.addWidget(_nm)
        _sub = _FitWrapLabel('共 %d 条 · 时长 %s 分钟 · %s Wh'
                             % (s['n'], s['dur'], s['wh']), 1, 11, 9)
        _sub.setStyleSheet('color:#8fa3b8; background:transparent;')
        _col.addWidget(_sub)
        _col.addStretch(1)
        h.addLayout(_col, 1)
        _bad = bool(s['n']) and (s['spread'] > 0.9 or s['match'] < 0.9)
        _tag = _FitWrapLabel('箱内平均距离 %.3f · 最近匹配 %.0f%%%s'
                             % (s['spread'], s['match'] * 100,
                                ' · 建议复核' if _bad else ''), 1, 11, 9)
        _tag.setFixedWidth(260)
        # _FitWrapLabel 默认 Ignored 横宽（用于自适应行内文字），
        # 这里要固定宽度贴右缘，必须切成 Fixed，否则布局不预留这 260 px。
        _tag.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        _tag.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        _tag.setStyleSheet('color:%s; background:transparent;'
                           % ('#f0b45a' if _bad else '#8fa3b8'))
        h.addWidget(_tag)
        return wd

    def _cand_row(self, c):
        wd = QWidget()
        wd.setFixedHeight(52)
        h = QHBoxLayout(wd)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        h.addWidget(_CurveThumb(c.get('shape'), w=120, h=40), 0, Qt.AlignVCenter)
        _col = QVBoxLayout()
        _col.setContentsMargins(0, 0, 0, 0)
        _col.setSpacing(2)
        _t1 = _FitWrapLabel('候选 %s · %d 条' % (c['cid'], c['n']), 1, 12, 9)
        _t1.setStyleSheet('color:#e8eef6; background:transparent;')
        _col.addWidget(_t1)
        _dur = c.get('dur') or [0, 0]
        _wh = c.get('wh') or [0, 0]
        _t2 = _FitWrapLabel('时长 %d–%d 分钟 · %s–%s Wh'
                            % (int(_dur[0] // 60), int(_dur[1] // 60), _wh[0], _wh[1]),
                            1, 11, 9)
        _t2.setStyleSheet('color:#8fa3b8; background:transparent;')
        _col.addWidget(_t2)
        _col.addStretch(1)
        h.addLayout(_col, 1)
        name = QLineEdit()
        name.setFixedWidth(150)
        name.setPlaceholderText('命名后即成为正式分箱')
        h.addWidget(name, 0, Qt.AlignVCenter)
        b = QPushButton('建箱并归入')
        b.setFixedWidth(104)
        b.clicked.connect(lambda _c=False, cc=c, nn=name: self._name(cc, nn.text()))
        h.addWidget(b, 0, Qt.AlignVCenter)
        return wd

    # ---- 落库（命名才写） ----
    def _name(self, c, name):
        name = (name or '').strip()
        if not name:
            return
        m = ux.device_model_load(self.p) or {'boxes': []}
        boxes = list(m.get('boxes') or [])
        bid = next((str(b.get('id')) for b in boxes if str(b.get('name')) == name), '')
        at = time.strftime('%Y-%m-%d %H:%M')
        batch = time.strftime('%Y%m%d%H%M%S')
        keys = [k for k in (c.get('keys') or []) if k in self.by_key]
        recs, changes, lines = [], [], []
        feats, cvecs = [], []
        for k in keys:
            it = self.by_key[k]
            feats.append(ux.learn_feats(it['session']))
            cvecs.append(ux.cluster_vec(it['session'], it['shape']))
        if not bid:
            used = {str(b.get('id')) for b in boxes}
            n = 1
            while ('DEV-%04d' % n) in used:
                n += 1
            bid = 'DEV-%04d' % n
            center = ({k: round(sum(f[k] for f in feats) / len(feats), 4)
                       for k in ux.LEARN_DIMS} if feats else {})
            nb = {'id': bid, 'name': name, 'center': center, 'n': 0,
                  'review': False, 'move': 0.0, 'prev': '', 'at': at,
                  'src': 'cluster'}
            for cv in cvecs:
                ux.box_shape_add(nb, cv)
            boxes.append(nb)
        else:
            for b in boxes:
                if str(b.get('id')) == bid:
                    for cv in cvecs:
                        ux.box_shape_add(b, cv)
        manual = dict(m.get('manual') or {})
        meta = dict(m.get('manual_meta') or {})
        for i, k in enumerate(keys):
            old = str(manual.get(k) or '')
            manual[k] = bid
            meta[k] = {'at': at, 'by': '本机用户', 'src': 'cluster', 'batch': batch,
                       'from': old, 'to': bid, 'session': self.by_key[k]['session'].get('id')}
            recs.append({'key': k, 'prev': old,
                         'vec': feats[i] if i < len(feats) else {},
                         'cvec': cvecs[i] if i < len(cvecs) else []})
            changes.append((k, bid, feats[i] if i < len(feats) else {},
                            cvecs[i] if i < len(cvecs) else [], 0.6))
            lines.append('CLUSTER key=%s session=%s from=%s to=%s src=cluster batch=%s'
                         % (k, self.by_key[k]['session'].get('id'), old or 'auto', name, batch))
        m['boxes'] = boxes
        m['manual'] = manual
        m['manual_meta'] = meta
        batches = dict(m.get('cluster_batches') or {})
        batches[batch] = {'name': name, 'bid': bid, 'at': at, 'items': recs}
        m['cluster_batches'] = batches
        ux.device_save(self.p, m)
        _tm = ux.learn_apply(changes, 'cluster')
        if self.audit:
            self.audit(lines + ['CLUSTER-BATCH batch=%s name=%s n=%d src=cluster v%s dur=%sms'
                                % (batch, name, len(recs), _tm.get('version'),
                                   _tm.get('dur_ms', '—'))])
        if self.on_applied:
            self.on_applied()
        self._run()

    def _undo(self):
        m = ux.device_model_load(self.p) or {}
        batches = dict(m.get('cluster_batches') or {})
        if not batches:
            return
        batch = max(batches)
        rec = batches.pop(batch)
        manual = dict(m.get('manual') or {})
        meta = dict(m.get('manual_meta') or {})
        boxes = [b for b in (m.get('boxes') or []) if isinstance(b, dict)]
        by_id = {str(b.get('id')): b for b in boxes}
        at = time.strftime('%Y-%m-%d %H:%M')
        changes, lines = [], []
        for r in rec.get('items') or []:
            k = r['key']
            if r.get('prev'):
                manual[k] = r['prev']
                meta[k] = {'at': at, 'by': '本机用户', 'src': 'manual',
                           'batch': batch, 'to': r['prev']}
            else:
                manual.pop(k, None)
                meta.pop(k, None)
            _cv = r.get('cvec') or []
            for _b in by_id.values():           # 中心跟着回退：从原箱减掉该条
                if _cv and str(_b.get('id')) == str(rec.get('bid')):
                    ux.box_shape_del(_b, _cv)
            changes.append((k, r.get('prev') or None, r.get('vec') or {}, _cv, 1.0))
            lines.append('CLUSTER-UNDO key=%s to=%s batch=%s'
                         % (k, r.get('prev') or 'auto', batch))
        _bid = str(rec.get('bid') or '')
        if _bid and _bid in by_id and str(by_id[_bid].get('src') or '') == 'cluster' \
                and not any(str(v) == _bid for v in manual.values()):
            boxes = [b for b in boxes if str(b.get('id')) != _bid]   # 批次建的箱一并撤掉
        m['manual'] = manual
        m['manual_meta'] = meta
        m['cluster_batches'] = batches
        m['boxes'] = boxes
        ux.device_save(self.p, m)
        _tm = ux.learn_apply(changes, 'undo')
        if self.audit:
            self.audit(lines + ['CLUSTER-UNDO-BATCH batch=%s name=%s n=%d v%s dur=%sms'
                                % (batch, rec.get('name'), len(changes),
                                   _tm.get('version'), _tm.get('dur_ms', '—'))])
        if self.on_applied:
            self.on_applied()
        self._run()


def _db_last_samples(path):
    """只读每个端口最后一条采样的 unix 时间；读不到返回 {}。

    「离线 · 最后更新」的唯一数据源就是这条：断链后不推断、不伪造实时值。
    走 idx_port_history_port_time 索引定位，单次 <1ms；只在刚断链/启动即离线时调一次。
    """
    import sqlite3
    out = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        for p in (1, 2, 3, 4):
            row = c.execute(
                'SELECT MAX(timestamp) FROM port_history WHERE port=?', (p,)).fetchone()
            if row and row[0]:
                out[str(p)] = float(row[0])
        c.close()
    except sqlite3.Error:
        return {}
    return out


def _online_backup(src, target):
    """用 SQLite 在线 backup API 把 src 完整一致地复制到 target。

    即使源库后端正在运行、数据在 WAL 中，也能得到一致副本；
    不复制活跃 WAL/SHM（避免拷贝到不一致的 WAL 被新进程丢弃）。
    返回 True/False。
    """
    import sqlite3
    s = t = None
    try:
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        t = sqlite3.connect(target)
        s.backup(t)
        return True
    except sqlite3.Error:
        return False
    finally:
        if t is not None:
            t.close()
        if s is not None:
            s.close()


def _migrate_history_db(data_d):
    """首次以独立包运行时，把旧位置的历史数据库迁到持久化目录（仅一次）。

    覆盖开发机场景：旧库在 CANONICAL_DIR\\port_history.db（约 80MB），
    而 backend 目录新建的是空库。使用 SQLite 在线 backup API 保证一致性。
    迁移标记落 data\\.db_migrated。
    """
    flag = os.path.join(data_d, '.db_migrated')
    target = os.path.join(data_d, 'port_history.db')
    # 候选旧库：仅当显式给了 CK_BACKEND_DIR 才查开发目录（不再硬编码绝对路径）
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(data_d)), 'port_history.db'),
    ]
    if CANONICAL_DIR:
        candidates.append(os.path.join(CANONICAL_DIR, 'port_history.db'))

    # 选定一个数据最全的源
    best_src = None
    best_n = -1
    for src in candidates:
        try:
            if os.path.exists(src) and os.path.getsize(src) > 100000:
                n = _db_session_count(src)
                if n > best_n:
                    best_n, best_src = n, src
        except OSError as _exc:
            ck_swallow(_exc, 'launcher:2298')
    if best_src is None:
        # 没有可迁移的源，仅写标记，避免反复尝试
        try:
            with open(flag, 'w', encoding='utf-8') as fh:
                fh.write('ok')
        except OSError as _exc:
            ck_swallow(_exc, 'launcher:2305')
        return

    # 是否需要迁移：未迁移，或目标库会话数明显少于源（< 源的 90%，容忍小幅时间差）
    need = (not os.path.exists(flag)) or (not os.path.exists(target))
    if not need:
        tn = _db_session_count(target)
        if tn < 0 or tn < best_n * 0.9:
            need = True
    if not need:
        return

    # 备份到临时文件再原子替换，避免中途失败留下半截库
    tmp_target = target + '.baknew'
    for ext in ('-wal', '-shm'):
        if os.path.exists(tmp_target + ext):
            try:
                os.remove(tmp_target + ext)
            except OSError as _exc:
                ck_swallow(_exc, 'launcher:2324')
    if _online_backup(best_src, tmp_target):
        try:
            if os.path.exists(target):
                os.replace(tmp_target, target)
            else:
                os.replace(tmp_target, target)
        except OSError:
            return
        # 目标库以全新主文件为准，清掉可能残留的旧 WAL/SHM
        for ext in ('-wal', '-shm'):
            if os.path.exists(target + ext):
                try:
                    os.remove(target + ext)
                except OSError as _exc:
                    ck_swallow(_exc, 'launcher:2339')
    try:
        with open(flag, 'w', encoding='utf-8') as fh:
            fh.write('ok')
    except OSError as _exc:
        ck_swallow(_exc, 'launcher:2344')


def port_up(port):
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect((HOST, port))
        s.close()
        return True
    except OSError:
        return False


def _wait_for_port(port, ms=60000):
    """非阻塞等待端口就绪（QEventLoop 轮询，供 QProcess 启动后使用）。"""
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    t = QTimer()
    t.setInterval(100)
    state = {'elapsed': 0}

    def tick():
        state['elapsed'] += 100
        if port_up(port) or state['elapsed'] >= ms:
            t.stop()
            loop.quit()
    t.timeout.connect(tick)
    t.start()
    loop.exec()
    return port_up(port)


def make_api(port):
    def _read_token():
        # 与 ha_server 的运行态文件一致（本机 token，仅本机可读）
        try:
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            f = os.path.join(base, "CuktechPanel", "runtime.json")
            with open(f, encoding="utf-8") as fh:
                return json.load(fh).get("token", "")
        except Exception:
            return ""
    # 连接复用（R19）/ 命名管道 / GET 单次重试已整体搬到 ck_http.Loopback。
    # 主机固定 v4（取证：_verify/c46_b1/host_probe.json，服务端只监听 127.0.0.1:8080），
    # 原 R25 的 ::1 探测与相关探测结论落盘一并删除——它每次都只会走回落分支。
    _lb = ck_http.Loopback(
        port, host=HOST, token=_read_token(),
        pipe_name=os.environ.get('CK_LIVE_PIPE') or (r'\\.\pipe\ck_live_%d' % port),
        pipe_enabled=os.environ.get('CK_LIVE_TRANSPORT', 'auto') == 'pipe',
        swallow=ck_swallow)
    api = _lb.api   # R19：二元返回签名不变，api.api_meta 由 ck_http 挂好
    return api, _lb.get_bytes


_CRASH_DIR = os.path.join(os.environ.get('LOCALAPPDATA', '.'), 'CuktechPanel')
_CRASH_FILE = os.path.join(_CRASH_DIR, 'crash.log')
_crash_fh = None
# crash.log 轮转。超 8 MB 切成 crash.log.1.gz 并顺延，保留 5 份；
# 历史一律 gzip 归档，不做裸删除；轮转自身只写 crash_rotate.log（不回调 crash_log）。
_CRASH_MAX_BYTES = 8 * 1024 * 1024
_CRASH_KEEP = 5
_CRASH_LOCK = threading.Lock()
_CRASH_FAULT = {'on': False}
_CRASH_SKIP = {'t': 0.0}


def _ck_append(path, text, kind=ck_time.HMS, sep=' '):
    """日志落盘唯一写手（只合并写盘动作；文件、前缀、异常处理仍归调用方）。

    kind=None 时不加时间戳前缀（launcher_job.log 是 JSONL，自带 ts 字段）；
    sep 保留 crash.log 历史上的两空格分隔，其余站点为单空格。
    """
    if kind is not None:
        text = '%s%s%s' % (ck_time.fmt(kind), sep, text)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(text + '\n')


def _crash_note(line):
    """轮转/归档自身的记录：独立小文件，避免与 crash_log 递归。"""
    try:
        os.makedirs(_CRASH_DIR, exist_ok=True)
        _ck_append(os.path.join(_CRASH_DIR, 'crash_rotate.log'), line,
                   ck_time.FULL, sep='  ')
    except BaseException:
        return


def _crash_size():
    try:
        return os.path.getsize(_CRASH_FILE)
    except OSError:
        return 0


def _crash_reopen():
    """轮转后重建句柄并重新挂 faulthandler（旧 fd 必须换到新文件）。"""
    global _crash_fh
    if not _CRASH_FAULT['on']:
        return
    try:
        import faulthandler
        _crash_fh = open(_CRASH_FILE, 'a', encoding='utf-8')
        faulthandler.enable(_crash_fh, all_threads=True)
    except Exception as _exc:
        _crash_note('reopen failed: %r' % (_exc,))


def _crash_rotate():
    """超阈值把 crash.log 压成 .1.gz 并顺延旧归档；返回耗时（秒），未轮转返回 0。

    先压到临时文件、再删原文件、最后顺延归档。任一实例仍持有 crash.log
    句柄时 Windows 会拒绝删除 —— 此时整轮放弃（不截断、不丢内容），60 s 后再试。
    """
    global _crash_fh
    if _crash_size() <= _CRASH_MAX_BYTES:
        return 0.0
    if time.time() - _CRASH_SKIP['t'] < 60:
        return 0.0
    import gzip
    import shutil
    with _CRASH_LOCK:
        if _crash_size() <= _CRASH_MAX_BYTES:
            return 0.0
        _t0 = time.perf_counter()
        _tmp = _CRASH_FILE + '.rotating.gz'
        # faulthandler 钉在旧句柄上：先关，稍后统一重建
        if _crash_fh is not None:
            try:
                _crash_fh.close()
            finally:
                _crash_fh = None
        try:
            with open(_CRASH_FILE, 'rb') as _fi, \
                    gzip.open(_tmp, 'wb', compresslevel=1) as _fo:
                shutil.copyfileobj(_fi, _fo, 1024 * 1024)
        except OSError as _exc:
            _crash_note('compress failed: %r' % (_exc,))
            _CRASH_SKIP['t'] = time.time()
            _crash_reopen()
            return 0.0
        try:
            os.remove(_CRASH_FILE)
        except OSError as _exc:
            _crash_note('rotate skipped (held by another instance): %r' % (_exc,))
            try:
                os.remove(_tmp)
            except OSError as _e2:
                _crash_note('temp cleanup failed: %r' % (_e2,))
            _CRASH_SKIP['t'] = time.time()
            _crash_reopen()
            return 0.0
        try:
            try:
                os.remove('%s.%d.gz' % (_CRASH_FILE, _CRASH_KEEP))
            except FileNotFoundError:
                pass
            for _i in range(_CRASH_KEEP - 1, 0, -1):
                _src = '%s.%d.gz' % (_CRASH_FILE, _i)
                if os.path.exists(_src):
                    os.replace(_src, '%s.%d.gz' % (_CRASH_FILE, _i + 1))
            os.replace(_tmp, _CRASH_FILE + '.1.gz')
        except OSError as _exc:
            _crash_note('archive shift failed: %r' % (_exc,))
        _crash_reopen()
        _dt = time.perf_counter() - _t0
        _crash_note('rotated in %.2fs -> crash.log.1.gz (keep=%d)'
                    % (_dt, _CRASH_KEEP))
        return _dt


def crash_log(msg):
    """全局崩溃/异常日志（本地落盘，可在设置里导出诊断）。轮转 + gzip 归档。"""
    try:
        os.makedirs(_CRASH_DIR, exist_ok=True)
        _crash_rotate()
        _ck_append(_CRASH_FILE, str(msg).rstrip(), ck_time.FULL, sep='  ')
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:2469')


_TRACE_FH = None


# R22：被捕获异常的全局台账。仓库纪律：except 不许空写——每个站点都过 ck_swallow，
# 它做三件事：①按「异常种类:消息」累计次数；②记录首次出现的站点与堆栈；
# ③同站点每分钟最多一条带栈日志（限流，避免刷屏）。台账可通过 `errs` 自检命令导出。
_ERR_STATS = {}       # key -> {'n','first','where','why','tb'}
_ERR_LASTLOG = {}     # where -> 上次落日志的 ts
_ERR_TOTAL = 0
_ERR_LOCK = threading.Lock()
_ERR_INSIDE = threading.local()
_ERRS_FILE = os.path.join(_CRASH_DIR, 'errs.json')
# 已知良性（环境性/可选依赖）：errs 里标 known，不要求额外解释
_ERRS_WHITELIST = {
    'OSError': '可选文件/系统调用失败可忽略',
    'ImportError': '可选依赖缺失',
    'ModuleNotFoundError': '可选依赖缺失',
    'FileNotFoundError': '可选文件不存在',
    'PermissionError': '可选路径权限不足',
    'RuntimeError': 'Qt/线程对象已析构时的收尾失败',
}


def ck_err_key(exc):
    """异常台账键：种类 + 首行消息（去掉易变的上下文）。"""
    s = str(exc).split('\n', 1)[0][:80]
    return type(exc).__name__ + (':' + s if s else '')


def ck_errs_flush():
    """把异常台账快照落盘，供 `errs` 自检命令读取（原子替换）。"""
    try:
        os.makedirs(_CRASH_DIR, exist_ok=True)
        with _ERR_LOCK:
            snap = {'total': _ERR_TOTAL, 'kinds': dict(_ERR_STATS),
                    'ts': round(time.time(), 2)}
        tmp = _ERRS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _ERRS_FILE)
    except BaseException:
        return


def ck_swallow(exc, where, why=''):
    """R22：捕获异常的唯一出口（替代 except: pass）。计数 + 首次堆栈 + 限流日志。"""
    global _ERR_TOTAL
    if getattr(_ERR_INSIDE, 'busy', False):
        return
    _ERR_INSIDE.busy = True
    try:
        key = ck_err_key(exc)
        now = time.time()
        logit = False
        with _ERR_LOCK:
            rec = _ERR_STATS.get(key)
            if rec is None:
                rec = _ERR_STATS[key] = {
                    'n': 0, 'first': round(now, 2), 'where': where, 'why': why,
                    'tb': traceback.format_exc()[-1200:]}
            rec['n'] += 1
            _ERR_TOTAL += 1
            if now - _ERR_LASTLOG.get(where, 0) >= 60:
                _ERR_LASTLOG[where] = now
                logit = True
        if logit:
            crash_log('[swallowed] %s @%s n=%d%s\n%s'
                      % (key, where, rec['n'], ('  # ' + why) if why else '', rec['tb']))
        if _ERR_TOTAL % 50 == 0:
            ck_errs_flush()
    except BaseException:
        _ERR_TOTAL += 1
    finally:
        _ERR_INSIDE.busy = False


# C37：Agent 搭建（小源 的 Agent Studio）——模型 / 人设与边界 / 能力挂载 / 试运行 / 保存删除
AGENT_BOUNDARY = ('【硬性边界】不能发送短信、不能拨打电话、不能修改系统或设备设置、'
                  '不能主动推送通知；只能读取已挂载能力范围内的本机数据；'
                  '没有数据就明确说没有，禁止编造数值，禁止声称已完成任何未实际执行的动作。')
AGENT_MODELS = ('跟随 AI 配置', 'deepseek-chat', 'deepseek-reasoner',
                'qwen2.5-7b-instruct', '本地端点默认')
AGENT_CAPS = (('status', '端口状态', '只读 /api/status 的端口与功率'),
              ('energy', '今日能量', '只读 /api/energy/stats?period=today'),
              ('report', '充电日报', '复用本地规则引擎 ux.rule_report'),
              ('records', '记录页会话摘要', '复用记录页已算好的统计与断开原因'))


class AgentStudio(QWidget):
    """Agent 搭建（最小可用）：五要素齐备，依赖全部注入以便离屏验证。

    prefs / secrets 与「AI 配置」共用；ready_fn() = aiEnabled and consent_ok；
    cfg_fn() 返回 ux.ai_config 结果（含 system）；snap_fn(caps) 只用现有数据源。
    """

    _tool_done = Signal(object)

    def __init__(self, parent, prefs, secrets, ready_fn, cfg_fn, snap_fn, notify,
                 tool_fn=None):
        super().__init__(parent)
        self._prefs = prefs
        self._secrets = secrets
        self._ready = ready_fn
        self._cfg_fn = cfg_fn
        self._snap_fn = snap_fn
        self._notify = notify or (lambda *_a, **_k: None)
        self._tool_fn = tool_fn
        self._pending_tool = None
        self._tool_queue = []
        self._queue_text = ''
        self._last_box = ''
        self._tools = {}
        self._store = QSettings('DoubaoWork', 'CuktechPanel')
        self._list = []
        self._idx = 0
        self._busy = False
        self._load()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        # ---- 门禁页：未开启 AI / 未同意隐私说明时整页挡住 ----
        self._gate = QWidget()
        _gv = QVBoxLayout(self._gate)
        _gv.setContentsMargins(12, 12, 12, 12)
        _gv.setSpacing(8)
        _gg = GlassCard('Agent 搭建暂不可用')
        _gg.set_glyph('shield')
        _ggv = QVBoxLayout(_gg)
        _ggv.setContentsMargins(12, 16, 12, 12)
        _ggv.setSpacing(8)
        self._gate_reason = QLabel('')
        self._gate_reason.setWordWrap(True)
        _ggv.addWidget(self._gate_reason)
        self._gate_hint = QLabel('')
        self._gate_hint.setObjectName('sub')
        self._gate_hint.setWordWrap(True)
        _ggv.addWidget(self._gate_hint)
        _gv.addWidget(_gg)
        _gv.addStretch(1)
        self._stack.addWidget(self._gate)

        # ---- 搭建页 ----
        # 1.5.7：删除「系统提示词」整卡（提示词只在「AI 配置」维护一处）。
        # 页面改 3 行 2 列固定栅格：列表 | 基础 / 可挂载能力 | 试运行 / 工具能力(整行)。
        # 每张卡声明最小高度，子控件按顺序占位，禁止负间距与绝对定位堆叠。
        self._body = QWidget()
        _bv = QGridLayout(self._body)
        _bv.setContentsMargins(0, 0, 0, 0)
        _bv.setHorizontalSpacing(12)
        _bv.setVerticalSpacing(8)
        _bv.setColumnStretch(0, 2)
        _bv.setColumnStretch(1, 3)

        def _card(title, glyph, row, col, rowspan=1, colspan=1, min_h=0):
            _c = GlassCard(title)
            _c.set_glyph(glyph)
            _c.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
            if min_h:
                _c.setMinimumHeight(int(min_h))
            _f = QVBoxLayout(_c)
            _f.setContentsMargins(12, 16, 12, 12)
            _f.setSpacing(8)
            _bv.addWidget(_c, row, col, rowspan, colspan)
            return _f

        # 三行等高（不再让某行独吞余量导致其余行被压到控件重叠）
        _bv.setRowStretch(0, 1)
        _bv.setRowStretch(1, 1)
        _bv.setRowStretch(2, 1)

        # 1) 列表与保存/删除（控件纵向顺序占位，卡最小高度固定，不再压住状态行）
        _f1 = _card('Agent 列表', 'list', 0, 0, min_h=148)
        self._combo = QComboBox()
        self._combo.setMinimumHeight(30)
        self._combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._combo.setMinimumContentsLength(8)
        _f1.addWidget(self._combo)
        _brow = QHBoxLayout()
        _brow.setSpacing(8)
        self._b_new = aligned_button('新建', 'ghost', min_w=72, h=30)
        self._b_save = aligned_button('保存', 'primary', min_w=72, h=30)
        self._b_del = aligned_button('删除', 'ghost', min_w=72, h=30)
        for _b in (self._b_new, self._b_save, self._b_del):
            _b.setAutoDefault(False)
            _brow.addWidget(_b)
        _brow.addStretch(1)
        _f1.addLayout(_brow)
        self._save_state = _ElideRow('', 1)
        self._save_state.setObjectName('sub')
        self._save_state.setFixedHeight(20)
        _f1.addWidget(self._save_state)
        _f1.addStretch(1)

        # 2) 基础：名称 / 模型 两行纵向（禁止两个输入框并排挤压、占位文字穿透）
        _f2 = _card('基础', 'gear', 0, 1, min_h=148)
        self._name = QLineEdit()
        self._name.setMinimumHeight(30)
        self._name.setPlaceholderText('给这个 Agent 起个名字')
        self._model = QComboBox()
        self._model.setMinimumHeight(30)
        self._model.setEditable(True)
        self._model.addItems(list(AGENT_MODELS))
        _g2 = QFormLayout()
        _g2.setContentsMargins(0, 0, 0, 0)
        _g2.setHorizontalSpacing(12)
        _g2.setVerticalSpacing(8)
        _g2.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        _g2.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        _g2.addRow('名称', self._name)
        _g2.addRow('模型选择', self._model)
        _f2.addLayout(_g2)
        _mh = QLabel('「跟随 AI 配置」使用 AI 配置页的通道与模型；也可直接输入自定义模型名。')
        _mh.setObjectName('sub')
        _mh.setWordWrap(True)
        _f2.addWidget(_mh)
        _f2.addStretch(1)

        # 3) 能力挂载
        _f3 = _card('可挂载能力（只用现有数据源）', 'plug', 1, 0, min_h=168)
        self._caps = {}
        for _k, _n, _d in AGENT_CAPS:
            _cb = QCheckBox(_n)
            _cb.setToolTip(_d)
            _cb.setMinimumHeight(26)
            _cb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self._caps[_k] = _cb
            _f3.addWidget(_cb)
        _f3.addStretch(1)   # 余高均分在卡内底部，checklist 保持顶部基线

        # 4) 系统提示词卡已移除（1.5.7）：提示词统一在「AI 配置 → 系统提示词」维护，
        #    固定边界说明并入那张卡下方的独立区块，避免两处各存一份。

        # 5) 试运行（与左列「可挂载能力」同带等高）
        _f5 = _card('试运行', 'chat', 1, 1, min_h=168)
        _irow = QHBoxLayout()
        _irow.setSpacing(8)
        self._try_in = QLineEdit()
        self._try_in.setMinimumHeight(30)
        self._try_in.setPlaceholderText('试运行问题，例如「现在充电情况怎么样」')
        self._try_btn = aligned_button('运行', 'primary', min_w=72, h=30)
        _irow.addWidget(self._try_in, 1)
        _irow.addWidget(self._try_btn)
        _f5.addLayout(_irow)
        # 状态行放在结果框上方：结果区可滚动，状态始终可见
        self._try_state = QLabel('未运行')
        self._try_state.setObjectName('sub')
        self._try_state.setWordWrap(True)
        _f5.addWidget(self._try_state)
        self._try_out = QTextBrowser()
        self._try_out.setMinimumHeight(60)
        self._try_out.setPlaceholderText('运行后在此显示结果：命中的能力、取到的数据、模型回复。')
        self._try_out.setStyleSheet('border-radius:10px; padding:8px;'
                                    ' background:rgba(10,20,40,0.55);'
                                    ' color:' + TEXT + '; border:1px solid '
                                    'rgba(120,190,255,0.14);')
        # 1.5.6：动作类能力的确认卡（与对话同区，点确认才执行）
        self._confirm = QFrame()
        self._confirm.setObjectName('agentConfirm')
        self._confirm.setStyleSheet(
            'QFrame#agentConfirm{background:rgba(255,190,90,0.10);'
            'border:1px solid rgba(255,190,90,0.38);border-radius:8px;}')
        _cv = QHBoxLayout(self._confirm)
        _cv.setContentsMargins(8, 6, 8, 6)
        _cv.setSpacing(8)
        self._confirm_lbl = QLabel('')
        self._confirm_lbl.setWordWrap(True)
        _cv.addWidget(self._confirm_lbl, 1)
        self._b_yes = aligned_button('确认执行', 'primary', min_w=84, h=28)
        self._b_no = aligned_button('取消', 'ghost', min_w=60, h=28)
        _cv.addWidget(self._b_yes)
        _cv.addWidget(self._b_no)
        self._confirm.hide()
        _f5.addWidget(self._confirm)
        _f5.addWidget(self._try_out, 1)   # 结果区吃满卡内余高，不留整块空白

        # 6) 工具能力（占满一行，逐个可启停；默认开查询与单口开关）
        _f6 = _card('工具能力（可直接接管操作）', 'bolt', 2, 0, 1, 2, min_h=132)
        _hint = QLabel('勾选后可用自然语言直接执行；动作类执行前会弹确认卡，'
                       '单条迁移免确认。任何执行都写 agent_action.log。')
        _hint.setObjectName('sub')
        _hint.setWordWrap(True)
        _f6.addWidget(_hint)
        _tg = QGridLayout()
        _tg.setContentsMargins(0, 0, 0, 0)
        _tg.setHorizontalSpacing(12)
        _tg.setVerticalSpacing(4)
        for _i, (_tk, _tv) in enumerate(agent_tools.TOOLS.items()):
            _cb = QCheckBox(agent_tools.short_name(_tk))
            _cb.setMinimumHeight(26)
            _cb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            _cb.setToolTip('%s\n%s\n风险：%s｜%s' % (
                _tv['name'], _tv['desc'],
                {'query': '查询（免确认）', 'action': '动作（需确认）',
                 'batch': '批量（需确认）'}.get(_tv['risk'], ''),
                '默认开启' if _tv.get('default') else '默认关闭'))
            self._tools[_tk] = _cb
            _tg.addWidget(_cb, _i // 5, _i % 5)
        _f6.addLayout(_tg)
        _f6.addStretch(1)
        # R1.5.5-E：去掉外层 QScrollArea（纵向滚动条来源），三档窗口都由网格吸收高度
        self._stack.addWidget(self._body)

        self._b_new.clicked.connect(self._new)
        self._b_save.clicked.connect(self._save)
        self._b_del.clicked.connect(self._delete)
        self._try_btn.clicked.connect(self._run)
        self._try_in.returnPressed.connect(self._run)
        self._combo.currentIndexChanged.connect(self._sync_form)
        self._name.textChanged.connect(lambda _t: self._save_state.setText('有未保存修改'))
        self._b_yes.clicked.connect(lambda: self._exec_pending(True))
        self._b_no.clicked.connect(self._cancel_pending)
        self._tool_done.connect(self._on_tool_done)

        self._refresh_list()
        self.refresh_gate()

    # ---------- 存储 ----------
    def _load(self):
        try:
            _raw = self._store.value('ai/agents', '')
            _v = json.loads(_raw) if _raw else []
            self._list = [a for a in _v if isinstance(a, dict) and a.get('name')]
        except Exception:
            self._list = []
        if not self._list:
            self._list = [self._default_agent()]

    def _default_agent(self):
        return {'name': '新 Agent', 'model': '跟随 AI 配置',
                'caps': ['status', 'energy'],
                'tools': agent_tools.defaults()}

    def _persist(self):
        try:
            self._store.setValue('ai/agents',
                                 json.dumps(self._list, ensure_ascii=False))
            return True
        except Exception as _e:
            self._notify('保存失败：%s' % _e, 'warning')
            return False

    # ---------- 门禁 ----------
    def refresh_gate(self):
        """未开启对话或未同意隐私说明 → 整页挡住（不只灰按钮）。"""
        _on = bool(self._prefs.get('aiEnabled', False))
        _ok = bool(self._ready())
        if _ok:
            self._stack.setCurrentIndex(1)
            return
        if not _on:
            self._gate_reason.setText('尚未开启对话能力，Agent 搭建不可用。')
            self._gate_hint.setText('请到「小源 → AI 配置 → 能力开关」勾选「对话」，'
                                    '并确认已同意隐私说明。')
        else:
            self._gate_reason.setText('尚未同意隐私说明，Agent 搭建不可用。')
            self._gate_hint.setText('请到「设置 → 进阶配置 → 隐私与协议」勾选同意后再使用，'
                                    '该页可查看《隐私说明》全文。')
        self._stack.setCurrentIndex(0)

    # ---------- 列表 / 表单 ----------
    def _refresh_list(self, sel=None):
        self._combo.blockSignals(True)
        self._combo.clear()
        for _a in self._list:
            self._combo.addItem(str(_a.get('name') or '未命名'))
        self._idx = max(0, min(sel if sel is not None else self._idx,
                               len(self._list) - 1))
        self._combo.setCurrentIndex(self._idx)
        self._combo.blockSignals(False)
        self._sync_form()

    def _sync_form(self, *_a):
        _i = self._combo.currentIndex()
        if _i < 0 or _i >= len(self._list):
            return
        self._idx = _i
        _a = self._list[_i]
        self._name.setText(str(_a.get('name') or ''))
        _m = str(_a.get('model') or '跟随 AI 配置')
        if self._model.findText(_m) < 0:
            self._model.addItem(_m)
        self._model.setCurrentText(_m)
        _caps = set(_a.get('caps') or [])
        for _k, _cb in self._caps.items():
            _cb.setChecked(_k in _caps)
        _tl = _a.get('tools')
        if not isinstance(_tl, dict):
            _tl = agent_tools.defaults()
        for _k, _cb in self._tools.items():
            _cb.setChecked(bool(_tl.get(_k, (agent_tools.TOOLS.get(_k) or {})
                                      .get('default'))))
        self._save_state.setText('已保存 · %s' % _a.get('name'))

    def _form_agent(self):
        return {'name': self._name.text().strip(),
                'model': self._model.currentText().strip() or '跟随 AI 配置',
                'caps': [_k for _k, _cb in self._caps.items() if _cb.isChecked()],
                'tools': {_k: _cb.isChecked() for _k, _cb in self._tools.items()}}

    # ---------- 新建 / 保存 / 删除 ----------
    def _new(self):
        if not self._ready():
            self.refresh_gate()
            return
        _n = '新 Agent %d' % (len(self._list) + 1)
        self._list.append({'name': _n, 'model': '跟随 AI 配置',
                           'caps': ['status', 'energy'],
                           'tools': agent_tools.defaults()})
        self._persist()
        self._refresh_list(len(self._list) - 1)
        self._notify('已新建「%s」，编辑后点「保存」' % _n, 'info')

    def _save(self):
        if not self._ready():
            self.refresh_gate()
            return False
        _a = self._form_agent()
        if not _a['name']:
            self._notify('名称不能为空', 'warning')
            return False
        if not _a['caps']:
            self._notify('至少挂载一项能力', 'warning')
            return False
        self._list[self._idx] = _a
        if not self._persist():
            return False
        self._refresh_list(self._idx)
        self._save_state.setText('已保存 · %s' % _a['name'])
        self._notify('已保存 Agent「%s」' % _a['name'], 'success')
        return True

    def _delete(self):
        if not self._ready():
            self.refresh_gate()
            return
        _nm = self._list[self._idx].get('name')
        self._list.pop(self._idx)
        if not self._list:
            self._list = [self._default_agent()]
        self._persist()
        self._refresh_list(min(self._idx, len(self._list) - 1))
        self._notify('已删除「%s」' % _nm, 'info')

    # ---------- 试运行 / 工具执行 ----------
    def _build_payload(self, q):
        _a = self._form_agent()
        _f = {}
        try:
            _f = dict(self._snap_fn(_a['caps']) or {})
        except Exception as _e:
            _f = {'notes': ['（数据快照失败：%s）' % _e]}
        return ai_service.payload('agent', q, _f)

    def _enabled_caps(self):
        return [_k for _k, _cb in self._caps.items() if _cb.isChecked()]

    def _tool_on(self, tool):
        _cb = self._tools.get(tool)
        return bool(_cb and _cb.isChecked())

    def _rule_text(self):
        """模型不可用时的规则版结论：只用现有快照，标注来源。"""
        try:
            _f = dict(self._snap_fn(self._enabled_caps()) or {})
        except Exception:
            _f = {}
        _ln = ['（本地规则结论：未调用在线模型）']
        _e = _f.get('energy') or {}
        if isinstance(_e, dict) and _e:
            _ln.append('能量：充入 %s Wh，会话 %s 次。' % (
                _e.get('total_wh', '—'), _e.get('session_count', '—')))
        _s = _f.get('status') or {}
        if isinstance(_s, dict) and _s:
            _ln.append('状态：%s 个端口，%s 个开启，总功率 %s W。' % (
                _s.get('port_count', '—'), _s.get('on_count', '—'),
                _s.get('total_power_w', '—')))
        _r = _f.get('records') or {}
        if isinstance(_r, dict) and _r:
            _ln.append('记录：会话 %s 条，断开原因 %s 类，设备组 %s 个。' % (
                _r.get('session_count', '—'), _r.get('reason_count', '—'),
                _r.get('device_count', '—')))
        if len(_ln) == 1:
            _ln.append('当前没有可用的数据快照，请先在「数据授权」开启相应能力。')
        return '\n'.join(_ln)

    def _describe(self, tool, params):
        _p = []
        for _k, _v in params.items():
            if _k == 'port':
                _p.append(agent_tools.PORT_NAMES.get(str(_v), str(_v)))
            elif _k == 'action':
                _p.append('开启' if _v == 'on' else '关闭')
            elif _k == 'minutes':
                _p.append('%s 分钟' % _v)
            elif _k == 'count':
                _p.append('最近 %s 条' % _v)
            elif _k == 'period':
                _p.append({'today': '今天', 'week': '本周',
                           'month': '本月'}.get(_v, _v))
            elif _k == 'on':
                _p.append('开启' if _v else '关闭')
            elif _v not in ('', None):
                _p.append('「%s」' % _v)
        return '；'.join(_p) or '无参数'

    def _ask_tool(self, text, tool, params):
        self._pending_tool = (text, tool, params)
        _name = agent_tools.TOOLS[tool]['name']
        if agent_tools.needs_confirm(tool, params):
            self._confirm_lbl.setText('将执行：%s\n参数：%s' % (
                _name, self._describe(tool, params)))
            self._confirm.show()
            self._try_state.setText('等待确认 · %s' % _name)
        else:
            self._exec_pending(False)

    def _next_tool(self):
        """按队列执行复合指令；后续动作若未写目标箱，沿用刚新建的箱。"""
        while self._tool_queue:
            _tool, _params = self._tool_queue.pop(0)
            if _tool == 'move_sessions' and not _params.get('box') \
                    and _params.get('action') == 'move' and self._last_box:
                _params = dict(_params, box=self._last_box)
            _ok, _clean, _err = agent_tools.validate(_tool, _params)
            if not _ok:
                self._try_state.setText('未执行 · ' + _err)
                self._try_out.setPlainText('没能执行：%s' % _err)
                continue
            self._ask_tool(self._queue_text, _tool, _clean)
            return
        self._try_state.setText('全部完成')

    def _cancel_pending(self):
        self._pending_tool = None
        self._tool_queue = []
        self._confirm.hide()
        self._try_state.setText('已取消')

    def _exec_pending(self, confirmed):
        if not self._pending_tool:
            return
        _q, _tool, _params = self._pending_tool
        self._pending_tool = None
        self._confirm.hide()
        if not self._tool_on(_tool):
            self._try_state.setText('该能力已停用')
            return
        self._busy = True
        self._try_btn.setEnabled(False)
        self._try_state.setText('执行中 · %s' % agent_tools.TOOLS[_tool]['name'])

        def _work():
            try:
                if self._tool_fn:
                    _r = self._tool_fn(_tool, _params)
                else:
                    _r = (False, '未接入执行器', {})
            except Exception as _e:
                _r = (False, '执行异常：%s' % _e, {})
            self._tool_done.emit((_q, _tool, _params, _r, confirmed))

        threading.Thread(target=_work, daemon=True).start()

    def _on_tool_done(self, obj):
        _q, _tool, _params, _r, _confirmed = obj
        _ok, _msg = bool(_r[0]), str(_r[1] or '')
        self._busy = False
        self._try_btn.setEnabled(True)
        agent_tools.log_action(_q, _tool, _params, _msg, _confirmed, _ok)
        self._try_out.setPlainText('%s\n\n（已写入 agent_action.log）'
                                   % (('执行回执：' if _ok else '未完成：') + _msg))
        self._try_state.setText('%s · %s' % ('已执行' if _ok else '未执行',
                                             agent_tools.TOOLS[_tool]['name']))
        if _ok and _tool == 'create_box':
            self._last_box = str(_params.get('name') or '')
        if self._tool_queue:
            QTimer.singleShot(0, self._next_tool)

    def _run(self):
        if not self._ready():
            self.refresh_gate()
            return
        _q = self._try_in.text().strip() or '请介绍你能做什么'
        if self._busy:
            self._notify('正在运行，请稍候', 'warning')
            return
        if not self._save():
            return
        self._confirm.hide()
        # 1) 关键词兜底：断网也能执行的简单命令（支持复合指令，按序执行）
        _ints = [i for i in agent_tools.parse_all(_q) if self._tool_on(i[0])]
        if _ints:
            self._queue_text = _q
            self._tool_queue = list(_ints)
            self._next_tool()
            return
        # 2) 大模型 function calling
        _a = self._form_agent()
        _cf = dict(self._cfg_fn() or {})
        if _a['model'] and _a['model'] != '跟随 AI 配置':
            _cf['model'] = _a['model']
        # 1.5.7：人设/提示词统一取「AI 配置 → 系统提示词」，Agent 页不再单独维护
        _cf['system'] = (str(self._prefs.get('aiSystemPrompt') or '')
                         or ai_service.DEFAULT_SYSTEM) + '\n\n' + AGENT_BOUNDARY
        try:
            _snap = dict(self._snap_fn(self._enabled_caps()) or {})
        except Exception:
            _snap = {}
        _prompt = '当前数据快照（只读）：%s\n\n用户指令：%s' % (
            json.dumps(_snap, ensure_ascii=False)[:1200], _q)
        self._busy = True
        self._try_btn.setEnabled(False)
        self._try_state.setText('运行中 · %s · %s' % (_cf.get('provider'),
                                                  _cf.get('model')))
        self._try_out.setPlainText('')

        def _done(msg, err):
            self._busy = False
            self._try_btn.setEnabled(True)
            if err or not msg:
                ai_service.audit('agent', _t0, degraded=True, ok=True,
                                 note=str(err or '')[:120])
            else:
                ai_service.audit('agent', _t0, ok=True)
            if isinstance(msg, dict) and msg.get('tool_calls'):
                _fn = (msg['tool_calls'][0] or {}).get('function') or {}
                _name = str(_fn.get('name') or '')
                try:
                    _args = json.loads(_fn.get('arguments') or '{}')
                except Exception:
                    _args = {}
                if not self._tool_on(_name):
                    self._try_state.setText('该能力已停用')
                    self._try_out.setPlainText(self._rule_text())
                    return
                _ok, _clean, _err = agent_tools.validate(_name, _args)
                if not _ok:
                    self._try_state.setText('未执行 · ' + _err)
                    return
                self._ask_tool(_q, _name, _clean)
                return
            if isinstance(msg, dict) and msg.get('content'):
                self._try_out.setPlainText(str(msg['content']))
                self._try_state.setText('运行成功 · %s · %s' % (
                    _cf.get('provider'), _cf.get('model')))
                return
            self._try_out.setPlainText(self._rule_text())
            self._try_state.setText('在线模型不可用，已给本地规则结论')

        _t0 = time.time()
        ux.ai_tool(_cf, _prompt, agent_tools.openai_tools(), _done)


# R35 / C05：WinRT Toast（进入通知中心留痕 + 点击激活）——失败时由调用方回退 tray.showMessage。
_CK_AUMID = CK_AUMID  # R48 / C18：单点定义（见 CK_AUMID_DEFAULT）
_ck_toast_keep = []
_ck_toast_activate = {'cb': None, 'bridge': None}


class _CkToastBridge(QObject):
    """WinRT 激活回调在非 Qt 线程触发，必须切回主线程再动 UI/定时器。"""
    fired = Signal(object)


def _ck_toast_bridge():
    b = _ck_toast_activate.get('bridge')
    if b is None:
        b = _CkToastBridge()
        b.fired.connect(_ck_toast_dispatch, Qt.QueuedConnection)
        _ck_toast_activate['bridge'] = b
    return b


def _ck_toast_dispatch(pg):
    try:
        cb = _ck_toast_activate.get('cb')
        if cb:
            cb(pg)
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:toast-dispatch')


def _ck_toast_log(line):
    try:
        _f = os.path.join(os.environ.get('LOCALAPPDATA', ''),
                          'CuktechPanel', 'socket_debug.log')
        _ck_append(_f, line, ck_time.HMS)
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:toastlog')


# R48 / C18：通知时效埋点。event→submit→history 两段耗时 + 主线程心跳 gap + 状态标签。
_CK_BEAT = {'t': 0.0, 'gap_ms': 0.0, 'max_gap_ms': 0.0}
_CK_NOTIFY_CTX = {'state': 'fg'}
# C33：静音状态提到模块级——所有系统通知在 _ck_toast_send 收口处判断，
# 旧实现只在 notifytest 调试命令里判断，真实告警（充电开始/结束、越界）静音无效。
_CK_MUTE = {'on': False}
# C34f：静音期的未读计数（不弹系统横幅，只累计角标；解除静音后补发摘要）。
# fn 由窗口初始化时注册为 _set_unread，模块级调用也能刷新托盘角标。
_CK_UNREAD = {'n': 0, 'fn': None}
# C34g：托盘激活取证（右键无菜单回归排查用）。n=累计激活次数，last=最近 6 次原因。
_CK_TRAY = {'n': 0, 'last': []}


def _ck_mute_count(kind='plain'):
    """静音期只计数、不弹任何东西；返回累计未读数。"""
    _p = ux.ck_notify_plan(True, _CK_UNREAD.get('n', 0), kind)
    _CK_UNREAD['n'] = _p['unread']
    _fn = _CK_UNREAD.get('fn')
    if _fn:
        try:
            _fn(_CK_UNREAD['n'])
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:unread-badge')
    return _CK_UNREAD['n']
# C33（缺陷 1）：曲线实时尾提交开关（默认开；`cfast 0` 用于同构建 A/B 对照）
_CK_CFAST = {'on': os.environ.get('CK_CURVE_FAST', '1') != '0'}
_CK_CFAST_TRACE = os.environ.get('CK_CFAST_TRACE', '')


def _ck_ble_badge(connected, ble_ok):
    """C34（缺陷 1）：蓝牙**认证**状态的单一文案口径。

    三处（状态页横带 / 端口页 / 设置页）都调这里，不再各写一套词汇。
    注意语义：connected = BLE 链路已建立（充电器在线）；ble_ok = 已完成认证（数据可读）。
    「已连接充电器」只说明链路，是否可读由本函数返回的徽章承担。
    """
    if ble_ok:
        return '蓝牙 已认证', '#0fa968'
    if connected:
        return '蓝牙 未认证', WARN_SOFT
    return '蓝牙 未连接', TEXT_MUTED


# 1.5.5：巴法云 MQTT 状态（总览行第二枚徽章）。全局 2s 轮询写入，首页/设置同源。
_CK_MQTT = {'state': '', 'host': '', 'port': '', 'reason': ''}
_CK_MQTT_FAILS = {'n': 0}
# 1.5.5：端口卡「本次充电设备名」——活跃会话 + device_key→分箱映射（记录页回填）
_CK_ACTIVE = {}
_CK_BOX_HINT = {}
_CK_PORT_PID = {'c1': 1, 'c2': 2, 'c3': 3, 'a': 4}
_CK_RULE_EVENTS = []   # /api/auto_rules/log 最近事件（智能分析页展示自动切换留痕）
_CK_HINT_ASKED = False  # 端口页首次遇到未映射设备时，触发一次记录页拉取填充箱名映射


def _ck_active_dev_name(sess):
    """本次充电设备名：与记录页同一取名口径；未命名才 DEV 编号。"""
    if not sess:
        return ''
    _key = str(sess.get('device_key') or '')
    _bid, _nm = _CK_BOX_HINT.get(_key, ('', ''))
    if _bid:
        return ux.device_box_name(_bid, _nm)
    return ''


def _ck_mqtt_state_text():
    """MQTT 状态词与颜色（首页徽章 / 连接设置状态行共用）。

    state 由 /api/mqtt/status 写入：空=尚未回读，disabled=未启用，
    connecting/failed/disconnected/connected 与后端同名。
    """
    _st = (_CK_MQTT or {}).get('state') or ''
    _lab = {'connected': '已连接', 'connecting': '连接中',
            'failed': '连接失败', 'disconnected': '未连接',
            'disabled': '未启用'}.get(_st, '')
    _col = {'connected': '#0fa968', 'connecting': '#d97706',
            'failed': '#ef4444'}.get(_st, TEXT_MUTED)
    return _lab, _col


def _ck_mqtt_badge():
    """巴法云连接状态只出短状态词；地址/原因等细节放悬停提示与功能说明。"""
    _lab, _col = _ck_mqtt_state_text()
    if not _lab:
        return '巴法云 状态未知', _col
    return '巴法云 ' + _lab, _col


def _ck_mqtt_tip():
    """巴法云状态细节（悬停可见）：地址、失败原因、未启用时的入口。"""
    _d = _CK_MQTT or {}
    _st = _d.get('state') or ''
    if not _st:
        return '暂时读不到巴法云状态；服务恢复后会自动刷新。'
    _host = str(_d.get('host') or '')
    _port = str(_d.get('port') or '')
    _tip = []
    if _host:
        _tip.append('地址 %s:%s' % (_host, _port or '?'))
    if _d.get('reason'):
        _tip.append('原因：%s' % _d['reason'])
    if _st == 'disabled':
        _tip.append('到「设置 → 连接设置」填写巴法云配置并保存后启用。')
    return '；'.join(_tip)


class _EqualPortWidths(QObject):
    """四卡严格等宽。

    2×2 网格在窗口宽度为奇数时会各分到 366/365 这类 1 px 差；这里在页面尺寸
    变化后按「可用宽度 // 2」统一固定宽度，余数留给右外边距。只锁宽度，
    高度仍由网格两行等分拉伸（因此卡仍随窗口等比缩放）。
    """

    def __init__(self, page, grid, cards):
        super().__init__(page)
        self._page = page
        self._grid = grid
        self._cards = cards
        self._t = QTimer(self)
        self._t.setSingleShot(True)
        self._t.setInterval(40)
        self._t.timeout.connect(self._apply)
        page.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if obj is self._page and ev.type() == QEvent.Resize:
            self._t.start()
        return False

    def _apply(self):
        m = self._grid.contentsMargins()
        avail = self._page.width() - m.left() - m.right() - self._grid.horizontalSpacing()
        cw = max(120, avail // 2)
        for item in self._cards.values():
            item[0].setFixedWidth(cw)


def _ck_chg_badge(plan, mode=None, full=False):
    """C34（缺陷 1）：把「充电器在位 / 充电中」从 BLE 连接状态里拆出来。

    `connected` 只代表 BLE 链路，不能当充电器结论（用户点名的语义错误）。
    充电侧单独一枚徽章，按端口功率给结论：
    - 充电中 <W>：有端口在输出；
    - 充电器在位 · 未充电：链路在、可读，但所有口 0W；
    - 充电器 状态未知：链路不在，读不到设备（不臆断在位/不在位）。
    mode 为 `devstate` 注入态（unconn/idle/charging/full）时优先，用于四态截图。
    `full` 只是注入近似，真实「已充满」判定仍需真机 BLE。
    """
    _m = str(mode or '')
    if _m == 'charging':
        return '充电器在位 · 充电中', '#0fa968'
    if _m == 'full':
        return '充电器在位 · 已充满', '#0fa968'
    if _m == 'idle':
        return '充电器在位 · 未充电', WARN_SOFT
    if _m == 'unconn':
        return '充电器 状态未知', TEXT_MUTED
    _p = plan or {}
    if not _p.get('connected'):
        return '充电器 状态未知', TEXT_MUTED
    _tot = float(_p.get('total') or 0)
    if _tot > 0.5:
        return '充电中 %.1fW' % _tot, '#0fa968'
    if not _p.get('ble_ok'):
        return '充电器在位 · 数据不可读', WARN_SOFT
    if full:
        return '充电器在位 · 已充满', '#0fa968'
    return '充电器在位 · 未充电', WARN_SOFT


def _ck_dev_plan(mode, base=None):
    """C34（缺陷 2）：把 `devstate <mode>` 注入成与 `_plan_status` 同形的 plan。

    R32 的 devstate 只改设备图，端口卡/横幅/线稿图标仍走真实轮询 → 四态截图自相矛盾。
    这里让四态（unconn / idle / charging / full）贯穿整页可见元素；`full` 的真实充满
    判定仍需要真机 BLE（mock 只能给「使能 + 本次充电：已充满」的近似）。
    """
    base = base or {}
    conn = mode in ('idle', 'charging', 'full')
    pw_map = {'charging': {'1': 65.4, '2': 30.2}}
    pins, raws = {}, {}
    total = 0.0
    for _k in ('1', '2', '3', '4'):
        _pw = float(pw_map.get(mode, {}).get(_k, 0.0))
        _en = bool(conn)
        _active = _pw > 0
        _v = 20.0 if _pw else 0.0
        _c = round(_pw / _v, 3) if _v else 0.0
        if mode == 'full':
            _dur = '本次充电：已充满'
        elif _active:
            _dur = '本次充电：00:01:20'
        else:
            _dur = '本次充电：未充电'
        pins[_k] = {'v': _v, 'c': _c, 'pw': _pw,
                    'vt': '电压 %.2f V' % _v, 'ct': '电流 %.2f A' % _c,
                    'pt': '功率 %.1f W' % _pw,
                    'proto': '协议 ' + ('PD' if _pw else 'idle'),
                    'en': _en, 'active': _active, 'tmo': None,
                    'dur': _dur, 'cd': None}
        raws[_k] = {'power': _pw, 'voltage': _v, 'current': _c,
                    'enabled': _en, 'active': _active}
        total += _pw
    p = dict(base)
    p.update({'connected': conn, 'ble_ok': conn, 'ble_error': '',
              'stg': base.get('stg') if isinstance(base.get('stg'), dict) else {},
              'stg15': None, 'ports': pins, 'raw': raws,
              'total': round(total, 2),
              'vmax': max([x['v'] for x in pins.values()] or [0.0]),
              'cur_sum': round(sum(x['c'] for x in pins.values()), 3),
              'n_active': sum(1 for x in pins.values() if x['active']),
              'load': round(total / 140.0 * 100.0, 3),
              'scene': 1 if conn else None})
    return p


# C33（缺陷 4）：启动分段埋点。CK_BOOT_TRACE=<file> 时逐段落盘，
# 用于定位「启动到可用 ~5s」的主导段（导入 / 后端派生 / build_ui / show）。
_CK_T0 = time.perf_counter()


def _ck_boot_trace(stage):
    _p = os.environ.get('CK_BOOT_TRACE', '')
    if not _p:
        return
    try:
        with open(_p, 'a', encoding='utf-8') as _f:
            # C34h：第三列 epoch，供外部 harness 把「进程启动 → _CK_T0」的引导段算进来
            _f.write('%s\t%.3f\t%.3f\n' % (stage, time.perf_counter() - _CK_T0, time.time()))
    except Exception:
        pass


# C34h：pyqtgraph 延迟初始化。导入本身 215 ms（冷启动/新构建后 450 ms）+ 建图 100 ms，
# 全部推到首帧之后再付；首屏用骨架占位，不留空白。
_CK_PG = {'m': None}


def _ck_pg():
    m = _CK_PG['m']
    if m is None:
        import pyqtgraph as _m
        _m.setConfigOptions(background=None, foreground='#cfe0f0', antialias=True)
        _CK_PG['m'] = _m
        m = _m
    return m


def _ck_notify_log(rec):
    try:
        d = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'CuktechPanel')
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'notify_latency.jsonl'), 'a',
                  encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:notifylog')


def _ck_app_state():
    try:
        return int(QApplication.applicationState())
    except Exception:
        return -1


def _ck_history_snapshot():
    """R48：通知中心留痕。空历史 WinRT 抛 -2147023728（找不到元素），按 0 条记。"""
    try:
        from winrt.windows.ui.notifications import ToastNotificationManager
        try:
            entries = ToastNotificationManager.history.get_history_with_id(CK_AUMID)
        except Exception:
            entries = ToastNotificationManager.history.get_history()
        out = []
        for e in entries:
            try:
                txt = [t.inner_text for t in
                       e.content.get_elements_by_tag_name('text')]
            except Exception:
                txt = []
            out.append({'tag': getattr(e, 'tag', None),
                        'group': getattr(e, 'group', None),
                        'text': txt})
        return out
    except Exception:
        return []


def _ck_history_probe(tag, t_event, t_submit):
    """后台线程轮询通知中心，记录同 tag 条目出现时间（不阻塞投递）。"""
    def _run():
        t_seen = None
        try:
            for _ in range(40):
                if any(x.get('tag') == tag for x in _ck_history_snapshot()):
                    t_seen = time.perf_counter()
                    break
                time.sleep(0.05)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:histprobe')
        try:
            _ck_notify_log({
                'ts': ck_time.fmt(),
                'evt': 'history', 'tag': tag,
                'state': _CK_NOTIFY_CTX.get('state', 'fg'),
                'event_to_submit_ms': round((t_submit - t_event) * 1000.0, 1),
                'submit_to_history_ms': (round((t_seen - t_submit) * 1000.0, 1)
                                         if t_seen else None),
                'event_to_history_ms': (round((t_seen - t_event) * 1000.0, 1)
                                        if t_seen else None),
                'history_found': bool(t_seen)})
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:histlog')
    threading.Thread(target=_run, daemon=True).start()


def _ck_toast_send(title, body, kind='plain', page=None, tag=None, t_event=None):
    """R35：WinRT Toast。成功返回 True；任何失败只吞并返回 False（调用方回退）。

    C33：静音收口——返回 True 表示「已按用户意愿处理（抑制）」，调用方不得回退到
    tray.showMessage；否则静音后仍会从回退通路弹出来（缺陷 8 的实际链路）。
    """
    if _CK_MUTE.get('on'):
        _n = _ck_mute_count(kind)
        _ck_toast_log('toast muted+counted kind=%s page=%s unread=%d'
                      % (kind, page, _n))
        return True
    t_evt = t_event if t_event is not None else time.perf_counter()
    try:
        from winrt.windows.ui.notifications import (
            ToastNotificationManager, ToastNotification, ToastTemplateType)
        xml = ToastNotificationManager.get_template_content(ToastTemplateType.TOAST_TEXT02)
        texts = xml.get_elements_by_tag_name('text')
        texts[0].append_child(xml.create_text_node(str(title)))
        texts[1].append_child(xml.create_text_node(str(body)))
        if page is not None:
            xml.document_element.set_attribute('launch', 'ck://page/%d' % int(page))
        note = ToastNotification(xml)
        note.tag = tag or ('ck-%s-%d' % (kind, int(time.time() * 1000)))

        def _on_activated(_sender, args):
            try:
                arg = getattr(args, 'arguments', '') or ''
                _ck_toast_log('toast activated tag=%s page=%s args=%s'
                              % (note.tag, page, arg))
                _pg = page
                if _pg is None:
                    _m = re.match(r'ck://page/(\d+)', arg)
                    if _m:
                        _pg = int(_m.group(1))
                # 激活回调在 WinRT 线程：切回 Qt 主线程后再操作 UI
                _ck_toast_bridge().fired.emit(_pg)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:toast-act')

        note.add_activated(_on_activated)
        notifier = ToastNotificationManager.create_toast_notifier_with_id(_CK_AUMID)
        notifier.show(note)
        t_submit = time.perf_counter()
        _ck_notify_log({
            'ts': ck_time.fmt(),
            'evt': 'submit', 'kind': kind, 'tag': note.tag, 'page': page,
            'state': _CK_NOTIFY_CTX.get('state', 'fg'),
            'event_to_submit_ms': round((t_submit - t_evt) * 1000.0, 1),
            'loop_gap_ms': round(_CK_BEAT.get('gap_ms', 0.0), 1),
            'max_gap_ms': round(_CK_BEAT.get('max_gap_ms', 0.0), 1),
            'app_state': _ck_app_state()})
        _ck_history_probe(note.tag, t_evt, t_submit)
        _ck_toast_keep.append(note)
        if len(_ck_toast_keep) > 8:
            del _ck_toast_keep[:-8]
        _ck_toast_log('winrt toast kind=%s tag=%s page=%s' % (kind, note.tag, page))
        return True
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:toast-winrt', 'fallback:tray.showMessage')
        return False


def ck_errs_report():
    """当前进程内的异常台账（探针/自检用）。"""
    with _ERR_LOCK:
        kinds = {}
        for k, v in _ERR_STATS.items():
            kinds[k] = {'n': v['n'], 'where': v['where'], 'first': v['first'],
                        'why': v['why'], 'tb': v['tb'],
                        'known': _ERRS_WHITELIST.get(k.split(':')[0], '')}
        total = _ERR_TOTAL
    unexplained = sorted(k for k, v in kinds.items() if not v['known'] and not v['why'])
    return {'total': total, 'kinds': kinds, 'unexplained': unexplained}


def ck_errs_load():
    """读取上次运行落盘的台账（`errs` 命令用）。"""
    try:
        with open(_ERRS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except BaseException:
        return {'total': 0, 'kinds': {}, 'note': '无 errs.json：本次运行无异常或尚未落盘'}


# R24 顶级规则：CPU 口径固化。任何 CPU 数字只允许用生产 QApplication.exec()
# 事件循环测得；自旋采样器（processEvents + sleep(4ms)）作废，只能作
# "内部横向比较"并注明"偏低下界"。改口径必须同步改这里的版本号。
_CK_CPU_PROBE_VERSION = 'ck-cpu-caliber/1 (R24)'

# R24：严格参数表。未知参数只打印 usage 并 exit=2，**永不 fallback 到 GUI**
# （旧行为：调试参数 `--zzz` 会直接拉起 GUI、留下实例占单实例锁，污染 CPU 测量）。
_CK_USAGE = """用法: 星源智控 [参数]
  (无参数)            启动 GUI
  --tray              托盘启动
  --headless          仅服务进程（无窗口）
  errs | --errs       导出异常台账（上次运行被捕获异常的种类/次数/首次站点）
  cpu口径 | --cpu-caliber   打印 CPU 口径与探针版本（含分母/闭合规则）
  --selftest-only     只跑自检（不建窗口）：cpu口径 + errs + 运行环境
  --selftest-theme    主题矩阵只读自检（offscreen，无窗口）：四段打点落 JSONL
                      （N=CK_MATRIX_N，默认 6；输出=CK_SELFTEST_OUT）
  --selftest-aihead   AI 区逐 tab 抓图（并排对照用，真窗 1280x860 整窗 PNG +
                      aihead.json 量 48 px 条高）。输出=CK_SELFTEST_OUT；
                      默认真窗（离屏渲染不出表单），需离屏设 CK_SELFTEST_OFFSCREEN=1
  --selftest-scroll   大数据滚动只读自检（offscreen）：2000 行定时 / 120 会话渲染与切换
                      （N=CK_SCHED_N / CK_SESS_N；输出=CK_SELFTEST_OUT）
  --selftest-smoke    构建门禁冒烟（offscreen）：建面板 + 逐页/逐 AI tab 切换，
                      槽内异常即 SMOKE_FAIL（输出=CK_SELFTEST_OUT，可选）
  --selftest-portui   端口动作「不切页」即时刷新自证（offscreen）：关/开后 300 ms 内
                      行文本与按钮变目标态，打印 PORTUI_OK（含实测 ms）
  --selftest-autoff   无充电 10 分钟自动关端口自证（后端纯逻辑）：599s/600s/断链/归零/
                      端口关闭/A 口豁免名单 七场景，打印 AUTOOFF_OK 7/7
  --selftest-settings 连接设置页布局实测（offscreen）：输出页面可用高度与两张卡
                      需求高度 JSON（SETTINGS_METRICS），供出包报告引用
  --selftest-port     端口卡布局实测（offscreen）：强制显示迷你曲线后输出卡片几何
                      JSON（PORT_METRICS），验证不拉伸、不裁切
  --selftest-merge    合并守恒自证（offscreen）：真实库三箱合并 → 校验 272→272 /
                      其它箱与未归箱不变 / 撤销还原，结果落 CK_MERGE_OUT
  --version | -V      打印版本号（读 VERSION 单源，与页脚同源；构建断言用）
未知参数：打印本用法并以 exit=2 退出。"""


def ck_cpu_caliber():
    """`cpu口径` 自检命令：打印当前 CPU 口径、探针版本与必需字段，防止口径混用。"""
    try:
        logical = os.cpu_count() or 0
    except BaseException:
        logical = 0
    logical = max(int(logical), 1)
    return {
        'caliber': 'exec',
        'loop': 'QApplication.exec()（生产事件循环）',
        'probe_version': _CK_CPU_PROBE_VERSION,
        'deprecated': 'spin(processEvents+sleep(4ms))：作废，仅可作内部横向比较且必须注明"偏低下界"',
        'denominator': {'logical_cpus': logical,
                        'single_core_pct': 'CPU秒 / 墙钟秒 × 100（不除核数）',
                        'machine_pct': '单核%% / %d' % logical},
        'required_fields': ['exec_single_core_pct', 'cpu_seconds_90s',
                            'thread_sum_vs_process_diff'],
        'window': '90 s（25 s 预热之后）',
        'closure_rule': '线程 CPU 和与进程 CPU 和之差 ≤0.01 s 视为闭合；不闭合该次数字作废',
    }


atexit.register(ck_errs_flush)   # 正常退出时把异常台账落盘（errs 命令读它）


def ck_trace(seg, **kw):
    """R21：端到端追迹（仅 CK_TRACE=1 时生效，默认零开销）。"""
    global _TRACE_FH
    if not os.environ.get('CK_TRACE'):
        return
    try:
        if _TRACE_FH is None:
            _TRACE_FH = open(os.environ.get('CK_TRACE_FILE') or 'ck_trace.jsonl',
                             'a', encoding='utf-8', buffering=1)
        kw.update(seg=seg, t=round(time.time(), 3))
        _TRACE_FH.write(json.dumps(kw, ensure_ascii=False) + '\n')
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:2585')


def _ck_dbg(line):
    """模块级调试落盘。build_ui 内的 _dbg_log 是闭包变量，模块级类（如
    AnimatedNumberLabel）拿不到，故单独提供一份同口径写手。"""
    try:
        _f = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          "CuktechPanel", "socket_debug.log")
        _ck_append(_f, line, ck_time.HMS)
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:ck_dbg')


# C30：首屏数值控件的取证登记表（hero + 4 KPI）。只登记带 probe_tag 的实例，
# 供 c30probe 管道命令逐项读出「文本 / 字号 / 实际绘制色 / 空值命中」。
_CK_NUM_PROBE = {}


def _ck_fmt_dur(sec):
    """C30：时长量纲归一。>=60 秒进位到分、>=60 分进位到时、>=24 时进位到天。

    改前记录页会出现「179分23秒」「545分29秒」「308时46分」——分钟/小时分别越界，
    属量纲越界；这里按物理量级逐级进位，不做四舍五入猜测（负值/脏值返回「—」）。
    """
    try:
        s = int(round(float(sec)))
    except Exception:
        return '—'
    if s < 0:
        return '—'
    if s < 60:
        return '%d秒' % s
    if s < 3600:
        return '%d分%02d秒' % (s // 60, s % 60)
    if s < 86400:
        return '%d时%02d分' % (s // 3600, (s % 3600) // 60)
    return '%d天%02d时' % (s // 86400, (s % 86400) // 3600)


# ===== C51：无充电 10 分钟自动关闭端口（判定与断开都在后端 backend/auto_off.py） =====
# 界面不显示无充电计时/倒计时，只把开关偏好同步给后端。


# C32 条件自证：电量进位分支命中计数（c30probe 前后可读）
_CK_EN_STAT = {'n': 0, 'kwh': 0, 'bad': 0}


def _ck_fmt_energy(wh):
    """C32：电量量纲归一。>=999.5 Wh 进位 kWh（2 位），否则 Wh；缺失/脏值 → 「—」。

    改前记录页统计卡直接印 `2542.50`（单位只在小字 cap 里），首屏最大数字要读者心算换算；
    详情摘要用 `%.2f Wh` 且 `or 0` 把缺失猜成 0。这里统一成一个函数：单位随值、缺失不猜。
    真 0 仍输出 `0.00 Wh`，与「—」（未知）可区分。
    """
    _CK_EN_STAT['n'] += 1
    try:
        v = float(wh)
    except (TypeError, ValueError):
        _CK_EN_STAT['bad'] += 1
        return '—'
    if v < 0:
        _CK_EN_STAT['bad'] += 1
        return '—'
    if v >= 999.5:
        _CK_EN_STAT['kwh'] += 1
        return '%.2f\u00A0kWh' % (v / 1000.0)
    return '%.2f\u00A0Wh' % v


def install_crash_logging():
    """R025：全局异常捕获 + faulthandler + Qt 消息落盘，单页出错不拖垮主进程。"""
    global _crash_fh
    try:
        import faulthandler
        os.makedirs(_CRASH_DIR, exist_ok=True)
        _crash_rotate()   # 先轮转再开新会话，避免旧日志无限增长
        # 不再写「==== session ====」分隔行 —— 每次启动固定写盘会让
        # 「启动 N 次增量 0 B」无法成立；时间戳信息由每条日志自带。
        _crash_fh = open(_CRASH_FILE, 'a', encoding='utf-8')
        _crash_fh.flush()
        faulthandler.enable(_crash_fh, all_threads=True)
        _CRASH_FAULT['on'] = True
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:2600')

    def _hook(t, v, b):
        crash_log(''.join(traceback.format_exception(t, v, b)))
    sys.excepthook = _hook
    try:
        def _thook(a):
            crash_log('thread %s\n%s' % (
                getattr(getattr(a, 'thread', None), 'name', '?'),
                ''.join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback))))
        threading.excepthook = _thook
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:2612')
    try:
        from PySide6.QtCore import qInstallMessageHandler

        def _qtmsg(mode, ctx, msg):
            crash_log('Qt[%s] %s' % (mode, msg))
        qInstallMessageHandler(_qtmsg)
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:2620')


# ===== headless 子进程 Job Object（GUI 退出即带走后端）=====
# 规则：GUI 建匿名 job（KILL_ON_JOB_CLOSE），句柄只由 GUI 持有、不 inherit；
# 后端派生后立刻 assign；GUI 自己不入 job；失败只警告不阻断（不重试、不弹窗）。
_JOB = {'h': None, 'ok': False, 'stage': 'none', 'err': 0, 'backend_pid': 0}
_JOB_KILL_ON_CLOSE = 0x2000


class _JOB_BASIC(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD)]


class _JOB_IO(ctypes.Structure):
    _fields_ = [('ReadOperationCount', ctypes.c_ulonglong),
                ('WriteOperationCount', ctypes.c_ulonglong),
                ('OtherOperationCount', ctypes.c_ulonglong),
                ('ReadTransferCount', ctypes.c_ulonglong),
                ('WriteTransferCount', ctypes.c_ulonglong),
                ('OtherTransferCount', ctypes.c_ulonglong)]


class _JOB_EXT(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _JOB_BASIC),
                ('IoInfo', _JOB_IO),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t)]


def _job_log(rec):
    """结构化日志：%LOCALAPPDATA%\\CuktechPanel\\launcher_job.log（一行一条 JSON）。"""
    try:
        os.makedirs(_CRASH_DIR, exist_ok=True)
        _rec = {'ts': round(time.time(), 3), 'pid': os.getpid()}
        _rec.update(rec)
        _ck_append(os.path.join(_CRASH_DIR, 'launcher_job.log'),
                   json.dumps(_rec, ensure_ascii=False), kind=None)
    except OSError as _exc:
        ck_swallow(_exc, 'launcher:job-log')


def _job_state(rec):
    """job 终态并入状态文件（原子替换），供自检/看门狗复核。"""
    try:
        os.makedirs(_CRASH_DIR, exist_ok=True)
        _rec = {'ts': round(time.time(), 3), 'pid': os.getpid()}
        _rec.update(rec)
        _tmp = os.path.join(_CRASH_DIR, 'launcher_job_state.json.tmp')
        with open(_tmp, 'w', encoding='utf-8') as f:
            json.dump(_rec, f, ensure_ascii=False)
        os.replace(_tmp, os.path.join(_CRASH_DIR, 'launcher_job_state.json'))
    except OSError as _exc:
        ck_swallow(_exc, 'launcher:job-state')


def _kernel32():
    _k = ctypes.WinDLL('kernel32', use_last_error=True)
    _k.CreateJobObjectW.restype = wintypes.HANDLE
    _k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k.SetInformationJobObject.restype = wintypes.BOOL
    _k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                           wintypes.LPVOID, wintypes.DWORD]
    _k.OpenProcess.restype = wintypes.HANDLE
    _k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k.AssignProcessToJobObject.restype = wintypes.BOOL
    _k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k.CloseHandle.restype = wintypes.BOOL
    _k.CloseHandle.argtypes = [wintypes.HANDLE]
    return _k


def ck_job_create():
    """建匿名 job + KILL_ON_JOB_CLOSE。失败只警告，不阻断启动。"""
    if os.environ.get('CK_NO_JOB') == '1':
        _JOB.update(ok=False, stage='disabled', err=0)
        _job_log({'job': 'off', 'stage': 'disabled', 'backend_pid': 0, 'err': 0})
        _job_state({'job': 'off', 'stage': 'disabled', 'backend_pid': 0, 'err': 0})
        return None
    try:
        _k = _kernel32()
        _h = _k.CreateJobObjectW(None, None)
        if not _h:
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError as _exc:
        _e = getattr(_exc, 'winerror', 0) or 0
        _JOB.update(h=None, ok=False, stage='create', err=_e)
        _job_log({'job': 'off', 'stage': 'create', 'backend_pid': 0, 'err': _e})
        _job_state({'job': 'off', 'stage': 'create', 'backend_pid': 0, 'err': _e})
        print('[job] create failed err=%s — 仅兜底失效，继续启动' % _e, flush=True)
        return None
    try:
        _info = _JOB_EXT()
        _info.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
        if not _k.SetInformationJobObject(_h, 9, ctypes.byref(_info),
                                          ctypes.sizeof(_info)):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError as _exc:
        _e = getattr(_exc, 'winerror', 0) or 0
        try:
            _k.CloseHandle(_h)
        except Exception as _e2:
            ck_swallow(_e2, 'launcher:job-close-fail')
        _JOB.update(h=None, ok=False, stage='setinfo', err=_e)
        _job_log({'job': 'off', 'stage': 'setinfo', 'backend_pid': 0, 'err': _e})
        _job_state({'job': 'off', 'stage': 'setinfo', 'backend_pid': 0, 'err': _e})
        print('[job] setinfo failed err=%s — 仅兜底失效，继续启动' % _e, flush=True)
        return None
    _JOB.update(h=_h, ok=True, stage='ok', err=0)
    _job_log({'job': 'on', 'stage': 'create', 'backend_pid': 0, 'err': 0})
    _job_state({'job': 'on', 'stage': 'create', 'backend_pid': 0, 'err': 0})
    return _h


def ck_job_assign(pid):
    """把后端进程放进 job；返回最终 stage（ok / assign / create / setinfo / disabled）。"""
    _h = _JOB.get('h')
    _job = 'on' if _JOB.get('ok') else 'off'
    _p = int(pid or 0)
    if not _h:
        _job_log({'job': _job, 'stage': _JOB.get('stage') or 'create',
                  'backend_pid': _p, 'err': _JOB.get('err') or 0})
        return _JOB.get('stage')
    try:
        _k = _kernel32()
        _hp = _k.OpenProcess(0x0100 | 0x0001, False, _p)   # SET_QUOTA | TERMINATE
        if not _hp:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _k.AssignProcessToJobObject(_h, _hp):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _k.CloseHandle(_hp)
    except OSError as _exc:
        _e = getattr(_exc, 'winerror', 0) or 0
        _JOB.update(stage='assign', err=_e)
        _job_log({'job': _job, 'stage': 'assign', 'backend_pid': _p, 'err': _e})
        _job_state({'job': _job, 'stage': 'assign', 'backend_pid': _p, 'err': _e})
        print('[job] assign pid=%s failed err=%s — 仅兜底失效' % (_p, _e), flush=True)
        return 'assign'
    _JOB.update(backend_pid=_p)
    _job_log({'job': 'on', 'stage': 'ok', 'backend_pid': _p, 'err': 0})
    _job_state({'job': 'on', 'stage': 'ok', 'backend_pid': _p, 'err': 0})
    return 'ok'


def ck_job_close():
    """GUI 退出时关句柄：内核随即终止 job 内所有进程（KILL_ON_JOB_CLOSE）。"""
    _h = _JOB.get('h')
    if not _h:
        return
    _JOB['h'] = None
    _JOB['ok'] = False
    try:
        _kernel32().CloseHandle(_h)
        _job_log({'job': 'off', 'stage': 'closed',
                  'backend_pid': _JOB.get('backend_pid') or 0, 'err': 0})
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:job-close')


def run_server_mainthread(d, port_override):
    """Run ha_server.py on the MAIN thread of this process.

    ha_server's asyncio/executor stack misbehaves when driven from a secondary
    thread (status/chart/sessions/energy handlers 500 silently), so the
    service process must block here — same as the production pythonw run.
    """
    sys.path.insert(0, d)
    # 工作目录切到后端目录：config.yaml 中的相对路径才会落在后端目录。
    try:
        os.chdir(d)
    except OSError as _exc:
        ck_swallow(_exc, 'launcher:2635')
    # 持久化数据目录：数据库放 %LOCALAPPDATA%，应用更新不丢失；首次运行迁移旧库。
    # 必须在 ha_server 加载 config 之前设置 CUKTECH_HISTORY_DB_PATH（绝对路径）。
    data_d = app_data_dir()
    _migrate_history_db(data_d)
    os.environ['CUKTECH_HISTORY_DB_PATH'] = os.path.join(data_d, 'port_history.db')
    if port_override:
        import config as config_mod
        _orig_load = config_mod.load_config

        def _load_with_override(*a, **k):
            c = _orig_load(*a, **k)
            c.server.port = int(port_override)
            return c

        config_mod.load_config = _load_with_override
    runpy.run_path(os.path.join(d, 'ha_server.py'), run_name='__main__')


_CHILD = None


# R06：主线程帧心跳（仅 CK_FRAME_PROBE 非空时启用）——16 ms 定时器记录相邻回调间隔，
# 作为「最长帧间隔」硬指标；只累加，不触发重绘。
CK_FRAME_PROBE = os.environ.get('CK_FRAME_PROBE', '')
# C33：原始相邻帧间隔（ms）落盘，用于给 P50/P95/max 分布，而不只是累计均值。
CK_FRAME_RAW = os.environ.get('CK_FRAME_RAW', '')
_ck_frame = {'last': 0.0, 'n': 0, 'sum': 0.0, 'max': 0.0, 'fh': None}
# R39：正常退出留痕（供看门狗区分“手动退出”与“崩溃”）；崩溃/被杀走不到写文件这一步。
_CK_EXIT_HOW = {'v': 'window_close'}


def _ck_frame_tick():
    _now = time.perf_counter()
    _dt = (_now - _ck_frame['last']) * 1000 if _ck_frame['last'] else 0.0
    _ck_frame['last'] = _now
    if _dt <= 0:
        return
    _ck_frame['n'] += 1
    _ck_frame['sum'] += _dt
    _ck_frame['max'] = max(_ck_frame['max'], _dt)
    if CK_FRAME_RAW:
        _ck_frame.setdefault('rawbuf', []).append(_dt)
    if CK_FRAME_PROBE and _ck_frame['n'] % 60 == 0:
        # R16：句柄只开一次（行缓冲），不再每行 open/close。
        # 旧写法每 60 帧在 GUI 线程里 open+write+close，关句柄会触发杀毒扫描，
        # 探针自己的 max 会被自己的磁盘 I/O 抬高（R15 的秒级峰值的来源之一）。
        try:
            if _ck_frame['fh'] is None:
                _ck_frame['fh'] = open(CK_FRAME_PROBE, 'a', encoding='utf-8',
                                       buffering=1)
            _ck_frame['fh'].write('frame n=%d avg=%.1fms max=%.1fms\n'
                                  % (_ck_frame['n'], _ck_frame['sum'] / _ck_frame['n'],
                                     _ck_frame['max']))
        except Exception:
            _ck_frame['fh'] = None
    if CK_FRAME_RAW and _ck_frame['n'] % 60 == 0 and _ck_frame.get('rawbuf'):
        try:
            if _ck_frame.get('rfh') is None:
                _ck_frame['rfh'] = open(CK_FRAME_RAW, 'a', encoding='utf-8', buffering=1)
            _ck_frame['rfh'].write(' '.join('%.2f' % _v for _v in _ck_frame['rawbuf']) + '\n')
            _ck_frame['rawbuf'] = []
        except Exception:
            _ck_frame['rfh'] = None
            _ck_frame['rawbuf'] = []


def _ck_selftest_shutdown(app, widget, tag):
    """自检进程有界收尾。

    窗口还活着就 os._exit 会跳过 Py_Finalize，ExitProcess 随即对所有 DLL 发
    DLL_PROCESS_DETACH；此时 PySide 绑定状态未收尾、ck_* 工作线程还在跑，
    实测 pyside6.abi3.dll+0x1c91e 访问违例（0xC0000005）。改为先销毁窗口、
    再等线程池收完，让解释器自己走完退出流程，退出码稳定为 0。
    """
    try:
        if widget is not None:
            widget.hide()
            widget.deleteLater()
        for _ in range(3):
            app.processEvents()
        app.quit()
        app.processEvents()
        WORKER_POOL.shutdown(wait=True)
    except Exception as _exc:
        ck_swallow(_exc, tag)


def _ck_selftest_merge157(app):
    """1.5.7 合并守恒自证（源码/构建产物/安装版通用）：真实库三箱合并 → 校验 → 撤销。

    输出：CK_MERGE_OUT 指定的 JSON（窗口态 stdout 不可靠），同时打印 MERGE_SELFTEST 行。
    只读用户数据：调用方需把 LOCALAPPDATA / CUKTECH_HISTORY_DB_PATH 指到测试目录。
    """
    out = {'version': ux.APP_VERSION, 'ok': False}
    host = None
    _q = _g = None
    try:
        p = ux.Prefs('DoubaoWorkQC', 'CuktechPanelQC')
        p.reset()
        ux.group_seed(p)
        items, err = load_cluster_items()
        rows = [it['session'] for it in items]
        out.update({'rows': len(rows), 'err': err})
        ux.device_save(p, ux.device_build(rows, 4, '均衡', ux.device_model_load(p)))
        _m0 = ux.device_model_load(p)
        _l0 = ux.device_assign(rows, _m0, '均衡', thr=1.6)
        _top = sorted([g for g in _l0.get('groups', []) if int(g.get('n') or 0)],
                      key=lambda g: -int(g.get('n') or 0))[:3]
        _ids = [str(g['id']) for g in _top]
        _before = {str(g['name']): int(g.get('n') or 0) for g in _top}
        _idset = set(_ids)
        _others0 = {str(g['id']): int(g.get('n') or 0)
                    for g in _l0.get('groups', []) if str(g['id']) not in _idset}
        host = QWidget()
        host.resize(995, 760)
        host.show()
        dlg = DeviceGroupsDialog(host, p, rows, '均衡', None)
        dlg.show()
        for _ in range(3):
            app.processEvents()
        dlg._sel = set(_ids)
        _q, _g = QMessageBox.question, QInputDialog.getText
        QInputDialog.getText = staticmethod(lambda *a, **k: ('合并守恒自证', True))
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        try:
            dlg._merge_boxes()
            _txt = dlg._sum.full_text()
            _m1 = ux.device_model_load(p)
            _mid = next((str(b['id']) for b in (_m1.get('boxes') or [])
                         if str(b.get('name')) == '合并守恒自证'), '')
            _l1 = ux.device_assign(rows, _m1, '均衡', thr=1.6)
            _c1 = {str(g['id']): int(g.get('n') or 0) for g in _l1.get('groups', [])}
            _others1 = {k: _c1.get(k, 0) for k in _others0}
            _bx = (_m1.get('merge_undo') or {}).get('boxes') or []
            dlg._undo_merge()
            _m2 = ux.device_model_load(p)
            _l2 = ux.device_assign(rows, _m2, '均衡', thr=1.6)
            _c2 = {str(g['id']): int(g.get('n') or 0) for g in _l2.get('groups', [])}
            _undo = {str(b.get('name')): _c2.get(str(b.get('id')), 0) for b in _bx}
        finally:
            QMessageBox.question, QInputDialog.getText = _q, _g
        out.update({
            'before': _before, 'before_sum': sum(_before.values()),
            'after_keep': _c1.get(_mid, 0), 'others_before': _others0,
            'others_after': _others1,
            'unassigned': [int(_l0.get('unassigned') or 0),
                           int(_l1.get('unassigned') or 0)],
            'undo': _undo, 'merge_text': _txt})
        try:
            _lp = ux.learn_log_path()
            if _lp and os.path.exists(_lp):
                with open(_lp, 'r', encoding='utf-8') as _lf:
                    _ls = [x.rstrip() for x in _lf if 'op=merge' in x]
                out['merge_log'] = _ls[-2:]
        except OSError as _exc:
            ck_swallow(_exc, 'launcher:merge-log')
        out['ok'] = (out['after_keep'] == out['before_sum']
                     and _others0 == _others1
                     and out['unassigned'][0] == out['unassigned'][1]
                     and all(_undo.get(k) == v for k, v in _before.items()))
    except Exception as _exc:
        out['err'] = repr(_exc)
    _p = os.environ.get('CK_MERGE_OUT', '')
    if _p:
        try:
            with open(_p, 'w', encoding='utf-8') as _f:
                json.dump(out, _f, ensure_ascii=False, indent=1)
        except OSError as _exc:
            ck_swallow(_exc, 'launcher:merge-out')
    try:
        print('MERGE_SELFTEST ' + json.dumps(out, ensure_ascii=False), flush=True)
    except OSError:
        pass
    _ck_selftest_shutdown(app, host, 'launcher:merge-teardown')
    return out


def main():
    global _CHILD
    install_crash_logging()
    # 1.5.6：AI 载荷自检（CUKTECH_SELFCHECK=1）——只记录/打印，失败不阻断启动。
    if os.environ.get('CUKTECH_SELFCHECK') == '1':
        try:
            _scp = ai_payload.self_check()
            print('[ai_payload.self_check] %s'
                  % ('PASS' if not _scp else 'FAIL: ' + '; '.join(_scp)),
                  flush=True)
        except Exception as _exc:
            print('[ai_payload.self_check] ERROR %r' % (_exc,), flush=True)
    argv = sys.argv[1:]
    argv1 = argv[0] if argv else ''
    _KNOWN_ARGS = {'', '--tray', '--headless', 'errs', '--errs', 'cpu口径',
                   '--cpu-caliber', 'cpu-caliber', '--selftest-only',
                   '--selftest-theme', '主题矩阵', '--selftest-aihead',
                   '--selftest-scroll', '--selftest-smoke', '--selftest-portui',
                   '--selftest-autoff', '--selftest-settings', '--selftest-port', '--shot',
                   '--demo', '--size', '--version', '-V', '--selftest-merge'}
    _allow = set(_KNOWN_ARGS)
    if '--shot' in argv:
        _i = argv.index('--shot')
        if _i + 1 < len(argv):
            _allow.add(argv[_i + 1])
    if '--size' in argv:
        _i = argv.index('--size')
        if _i + 1 < len(argv):
            _allow.add(argv[_i + 1])
    _bad = [a for a in argv if a not in _allow and not a.endswith('ha_server.py')]
    if _bad:
        print(_CK_USAGE, file=sys.stderr, flush=True)
        print('unknown arg(s): %s' % ' '.join(_bad), file=sys.stderr, flush=True)
        print('panel not started (exit=2)', file=sys.stderr, flush=True)
        sys.exit(2)
    if argv1 == '--selftest-only':
        # R24：无窗口自检入口（与生产启动彻底分开，不建 QApplication、不占单实例锁）。
        out = {'cpu': ck_cpu_caliber(), 'errs': ck_errs_load(),
               'runtime': {'pid': os.getpid(), 'argv': argv, 'cwd': os.getcwd(),
                           'python': sys.executable, 'frozen': bool(getattr(sys, 'frozen', False)),
                           'ck_beat_ms': _CK_BEAT_MS, 'started': ck_time.fmt()}}
        try:
            from PySide6.QtCore import __version__ as _qtver
            out['runtime']['qt'] = _qtver
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:selftest_qtver')
        # Qt API 能力探测表：PySide 不暴露的方法必须先 hasattr 再调用
        #（教训：setUpdateInterval 不存在，曾让 9 次 90s 连跑在 boot 阶段全废）。
        _caps = {}
        try:
            from PySide6.QtCore import QVariantAnimation as _QVA
            for _n in ('setUpdateInterval', 'updateInterval', 'setDuration',
                       'setLoopCount', 'setEasingCurve'):
                _caps['QVariantAnimation.' + _n] = hasattr(_QVA, _n)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:selftest_caps')
        out['runtime']['caps'] = _caps
        # R28：打包环境逐项 import 验证（源码环境通过不算数），并落盘
        _mods = ('aiohttp', 'aiohttp.web', 'multidict', 'yarl', 'frozenlist',
                 'attr', 'bleak', 'yaml', 'requests',
                 'cryptography', 'Cryptodome')
        _imp = {}
        for _m in _mods:
            try:
                __import__(_m)
                _imp[_m] = 'OK'
            except Exception as _e:
                _imp[_m] = 'FAIL(%r)' % (_e,)
        out['imports'] = _imp
        try:
            _op = (os.environ.get('CK_SELFTEST_OUT')
                   or os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                   'selftest.txt'))
            with open(_op, 'w', encoding='utf-8') as _f:
                _f.write(json.dumps(out, ensure_ascii=False, indent=1) + '\n')
        except Exception:
            pass
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    if argv1 in ('errs', '--errs'):
        # R22 自检命令：导出上次运行被捕获异常的「种类 / 次数 / 首次站点与堆栈」。
        # 非空且存在 unexplained 项 -> 验收不通过，必须解释或修掉。
        print(json.dumps(ck_errs_load(), ensure_ascii=False, indent=1))
        return
    if argv1 in ('cpu口径', '--cpu-caliber', 'cpu-caliber'):
        # R24 自检命令：打印当前 CPU 口径与探针版本（口径混用即验收不通过）。
        print(json.dumps(ck_cpu_caliber(), ensure_ascii=False, indent=1))
        return
    if argv1 == '--selftest-autoff':
        # C51：判定已移到后端（backend/auto_off.py）——直接跑后端纯逻辑自证。
        # 判据：600s 内不触发、到点触发、断链暂停不误断、中途恢复充电归零、端口关闭清零、
        # A 口默认豁免（不累计/不写日志/不断电），且豁免名单可配置（置空后 A 口照常到点断电）。
        import importlib.util
        # project_dir() 返回的是含 ha_server.py 的目录（开发机即 backend/）。
        _src = os.path.join(project_dir(), 'auto_off.py')
        if not os.path.isfile(_src):
            _src = os.path.join(project_dir(), 'backend', 'auto_off.py')
        _spec = importlib.util.spec_from_file_location('_ck_auto_off', _src)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _ok, _passed, _total = _mod._selftest()
        _aout = os.environ.get('CK_SELFTEST_OUT')
        if _aout:
            try:
                with open(_aout, 'w', encoding='utf-8') as _af:
                    json.dump({'selftest': 'backend.auto_off', 'ok': bool(_ok)}, _af,
                              ensure_ascii=False, indent=1)
            except OSError as _exc:
                ck_swallow(_exc, 'launcher:autoff-out')
        if not _ok:
            raise SystemExit(1)
        print('AUTOOFF_OK %d/%d (backend)' % (_passed, _total), flush=True)
        return 0

    if argv1 == '--selftest-settings':
        # 1.5.1 布局实测：连接设置页可用高度 vs 蓝牙/MQTT 两张卡实际需求高度。
        # 不连后端、不连蓝牙；只建 UI 量几何，输出 JSON 供报告引用。
        from PySide6.QtWidgets import QApplication as _QApp
        _app = _QApp.instance() or _QApp(sys.argv[:1])
        _api, _gb = make_api(FALLBACK_PORT)
        _w = build_ui(FALLBACK_PORT, _api, _gb, None, None)
        _out = []
        try:
            for _ww, _hh in ((WIN_MIN_W, WIN_MIN_H), (1100, 800), (1280, 860)):
                _w.resize(_ww, _hh)
                _w.show()
                _pg = getattr(_w, '_ck_conn_page', None)
                _tabs = getattr(_w, '_ck_settings_tabs', None)
                if _tabs is not None and _pg is not None:
                    from PySide6.QtWidgets import QStackedWidget as _QStack
                    _node = _tabs
                    while _node is not None:
                        _par = _node.parentWidget()
                        if isinstance(_par, _QStack):
                            _par.setCurrentWidget(_node)
                            break
                        _node = _par
                    _tabs.setCurrentWidget(_pg)
                for _ in range(12):
                    _app.processEvents()
                if _w.layout() is not None:
                    _w.layout().activate()
                _app.processEvents()
                _m = _w._ck_conn_metrics()
                _m['window'] = [_ww, _hh]
                _m['mode'] = 'generic'
                _out.append(_m)
                # 巴法云模式多出四路主题行：同一窗口尺寸下再量一次
                _combo = getattr(_w, '_ck_mq_mode_combo', None)
                if _combo is not None:
                    _bi = _combo.findData('bemfa')
                    if _bi >= 0:
                        _combo.setCurrentIndex(_bi)
                        for _ in range(6):
                            _app.processEvents()
                        if _w.layout() is not None:
                            _w.layout().activate()
                        _app.processEvents()
                        _m2 = _w._ck_conn_metrics()
                        _m2['window'] = [_ww, _hh]
                        _m2['mode'] = 'bemfa'
                        _out.append(_m2)
                        _gi = _combo.findData('generic')
                        if _gi >= 0:
                            _combo.setCurrentIndex(_gi)
                            for _ in range(4):
                                _app.processEvents()
        finally:
            _ck_selftest_shutdown(_app, _w, 'launcher:settings-metrics')
        print('SETTINGS_METRICS ' + json.dumps(_out, ensure_ascii=False), flush=True)
        return 0
    if argv1 == '--selftest-port':
        # 1.5.2 端口卡实测：离屏量几何；带 --shot 时接后端跑 20s 截一张真图。
        from PySide6.QtWidgets import QApplication as _QApp
        _shot = ''
        if '--shot' in sys.argv:
            _si = sys.argv.index('--shot')
            if _si + 1 < len(sys.argv):
                _shot = sys.argv[_si + 1]
        _sz = (WIN_W, WIN_H)
        if '--size' in sys.argv:
            _zi = sys.argv.index('--size')
            if _zi + 1 < len(sys.argv):
                try:
                    _zw, _zh = sys.argv[_zi + 1].lower().split('x')
                    _sz = (int(_zw), int(_zh))
                except Exception:
                    _sz = (WIN_W, WIN_H)
        _port = int(os.environ.get('CUKTECH_PANEL_PORT') or FALLBACK_PORT)
        _app = _QApp.instance() or _QApp(sys.argv[:1])
        _api, _gb = make_api(_port)
        _w = build_ui(_port, _api, _gb, None, None)
        try:
            _w._ck_build_charts()   # 正常路径由 QTimer(60ms) 触发，自检里直接建
        except Exception:
            pass
        _out = []
        try:
            if _shot:
                _w.show()
                # 端口页必须切到前台，否则 QStackedWidget 不给隐藏页布局，
                # 卡片量到的是默认几何（480 高），截图也会是假的。
                _pp = getattr(_w, '_ck_port_page', None)
                _stk = getattr(_w, '_ck_stack', None)
                if _stk is not None and _pp is not None:
                    _stk.setCurrentWidget(_pp)
                for _ in range(8):
                    _app.processEvents()
                _w.resize(_sz[0], _sz[1])   # show 会恢复上次几何，必须在 show 之后 resize
                _deadline = time.time() + 20.0
                while time.time() < _deadline:
                    _app.processEvents()
                    time.sleep(0.02)
                # 合成运行环境拿不到焦点，节拍器会自我降级；截图前强制补一帧。
                try:
                    _w._ck_chart_commit(force=True)
                except Exception:
                    pass
                for _ in range(6):
                    _app.processEvents()
                if '--demo' in sys.argv:
                    # 视觉取证用：喂一组固定 36 点曲线，验证固定高度下不变形/不裁切。
                    # 放在最后一次事件循环之后，避免轮询把空载迷你图重新隐藏。
                    # 1.5.5：_paint_mini 以 live_pw 判定「有负载」，注入数据时必须同源写一份，
                    # 否则下一次重绘会按真实 0 W 清成空网格。
                    _demo = {}
                    for _pk in _w._ck_port_cards:
                        _demo[_pk] = {'xs': list(range(36)),
                                      'ys': [12.0 + 6.0 * ((i * 7) % 9) / 8.0
                                             for i in range(36)],
                                      'max': 18.0, 'm': 36}
                    _cs_d = getattr(_w, '_ck_conn_state', None)
                    if isinstance(_cs_d, dict):
                        _cs_d['ever_ok'] = True
                        _cs_d['last_connected'] = True
                    _cst = getattr(_w, '_ck_chart_state', None)
                    if isinstance(_cst, dict):
                        _cst['mini_last'] = _demo
                        _cst['live_pw'] = {str(_k): 18.0 for _k in _w._ck_port_cards}
                    try:
                        _w._ck_paint_mini(_demo)
                    except Exception:
                        pass
                _w.grab().save(_shot)
                _first = next(iter(_w._ck_port_cards.values()), None)
                if _first is not None:
                    _pg = _first[0].parentWidget()
                    if _pg is not None:
                        _pg.grab().save(_shot.replace('.png', '_cards.png'))
                _m = _w._ck_port_metrics()
                _m['window'] = list(_sz)
                _m['shot'] = _shot
                _cs = getattr(_w, '_ck_chart_state', {})
                _m['mini_last_keys'] = sorted((_cs.get('mini_last') or {}).keys())
                _m['live_pw'] = _cs.get('live_pw')
                _out.append(_m)
            else:
                _w.show()
                _pp = getattr(_w, '_ck_port_page', None)
                _stk = getattr(_w, '_ck_stack', None)
                if _stk is not None and _pp is not None:
                    _stk.setCurrentWidget(_pp)   # 隐藏页不布局，必须先切到前台
                for _ in range(8):
                    _app.processEvents()
                for _ww, _hh in ((WIN_MIN_W, WIN_MIN_H), (1100, 800), (1280, 860)):
                    _w.resize(_ww, _hh)
                    for _ in range(30):
                        _app.processEvents()
                        time.sleep(0.02)
                    if _pp is not None and _pp.layout() is not None:
                        _pp.layout().activate()
                    for _pk, _tup in _w._ck_port_cards.items():
                        _box, _curve = _tup[8], _tup[9]
                        if _curve is None:
                            continue
                        _box.show()
                        _plot = getattr(_box, '_plot', None)
                        if _plot is not None:
                            _plot.show()
                        _curve.setData(list(range(36)), [12.0] * 36)
                    for _ in range(12):
                        _app.processEvents()
                    if _pp is not None and _pp.layout() is not None:
                        _pp.layout().activate()
                    for _ in range(6):
                        _app.processEvents()
                    _m = _w._ck_port_metrics()
                    _m['window'] = [_ww, _hh]
                    _out.append(_m)
        finally:
            _ck_selftest_shutdown(_app, _w, 'launcher:port-metrics')
        print('PORT_METRICS ' + json.dumps(_out, ensure_ascii=False), flush=True)
        return 0
    headless = bool(os.environ.get('CUKTECH_HEADLESS')) or argv1 == '--headless' \
        or argv1.endswith('ha_server.py')
    # C1: UI 以 QProcess 派生并带标记的 headless 子进程是合法服务进程，
    # 跳过单实例探测；其余独立 headless 与 GUI 共用同一探测。
    spawned_child = bool(os.environ.get('CUKTECH_SPAWNED_BY_UI'))
    tray_start = argv1 == '--tray'
    port_override = os.environ.get('CUKTECH_PANEL_PORT')
    d = project_dir()
    port = int(port_override) if port_override else FALLBACK_PORT
    if headless:
        if not spawned_child:
            # C1 致命修复：headless 分支在单实例探测之前直接 run_server 的绕过
            # 已堵死——独立 `exe --headless` 先探测，已存在实例即退出（5s 内）。
            from PySide6.QtCore import QCoreApplication
            from PySide6.QtNetwork import QLocalSocket
            _capp = QCoreApplication(sys.argv)
            probe = QLocalSocket()
            probe.connectToServer('CuktechPanelSingleInstance_v2')
            if probe.waitForConnected(200):
                probe.disconnectFromServer()
                print('single-instance: CuktechPanel already running; exit', flush=True)
                sys.exit(0)
        # 纯服务进程（被 UI 以 QProcess 托管，或独立启动且无既有实例）：主线程常驻。
        run_server_mainthread(d, port_override)
        return
    from PySide6.QtWidgets import QApplication
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtCore import QProcess, QProcessEnvironment
    # R28：Windows Toast 的硬性前提——进程未设 AppUserModelID 时，
    # QSystemTrayIcon.showMessage() 的系统通知会被静默丢弃（不报错、不弹窗、通知中心无痕）。
    # 必须早于 QApplication 创建设置。CK_NO_AUMID=1 仅用于根因对照实验。
    # R33：AUMID 去掉 DoubaoWork 前缀（否则通知会被归到“豆包 Work”名下），
    # 并在启动时注册 HKCU\Software\Classes\AppUserModelId\<AUMID> 的显示名与图标。
    _AUMID = CK_AUMID  # R48 / C18：独占稳定 AUMID
    try:
        if os.environ.get('CK_NO_AUMID'):
            print('[aumid] skipped (CK_NO_AUMID=1)', flush=True)
        else:
            import ctypes as _ct
            _rc = _ct.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_AUMID)
            _ico = ''
            try:
                import winreg as _wr
                if getattr(sys, 'frozen', False):
                    _ico = sys.executable
                else:
                    # R47 / C17：源码态也指向品牌图（原 assets/icons 路径不存在时回退 python.exe）
                    _cand = ck_icon_path()
                    _ico = _cand if os.path.exists(_cand) else sys.executable
                _k = _wr.CreateKey(_wr.HKEY_CURRENT_USER,
                                   r'Software\Classes\AppUserModelId\%s' % _AUMID)
                _wr.SetValueEx(_k, 'DisplayName', 0, _wr.REG_SZ, PANEL_TITLE)
                _wr.SetValueEx(_k, 'IconUri', 0, _wr.REG_SZ, _ico)
                _wr.CloseKey(_k)
            except Exception as _e2:
                ck_swallow(_e2, 'launcher:aumid-reg')
            print('[aumid] set=%s rc=%s icon=%s' % (_AUMID, _rc, _ico), flush=True)
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:aumid')
    # R48 / C18：启动时重建 Start Menu 快捷方式（PKEY_AppUserModel_ID 绑定同一 AUMID）。
    # C34g（#3）：WScript.Shell + 属性库写入实测占用冷启动关键路径 ~0.5-1 s，且它只影响
    # Toast 署名、不影响首帧——改到 app 建好后再延时做（见下方 app 创建之后）。
    if argv1 in ('--selftest-theme', '主题矩阵', '--selftest-aihead',
                 '--selftest-scroll', '--selftest-smoke', '--selftest-portui',
                 '--selftest-settings', '--selftest-merge'):
        # 只读自检：不占单实例锁、不写任何配置。
        # --selftest-aihead 是给「两个应用 AI 段并排对照」拍图的，必须真窗——
        # 离屏下子页表单不渲染，抓出来是空白。需要离屏时设 CK_SELFTEST_OFFSCREEN=1。
        if argv1 == '--selftest-aihead' and not os.environ.get('CK_SELFTEST_OFFSCREEN'):
            os.environ.pop('QT_QPA_PLATFORM', None)
        else:
            os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    if argv1 in ('--version', '-V'):
        # 版本单源：与页脚同读 ux.APP_VERSION；构建断言比对（窗口态 stdout 不可靠，落文件）。
        _vtxt = 'CuktechPanel %s' % ux.APP_VERSION
        _vout = os.environ.get('CK_VERSION_OUT')
        if _vout:
            try:
                with open(_vout, 'w', encoding='utf-8') as _vf:
                    _vf.write(_vtxt + '\n')
            except OSError as _exc:
                ck_swallow(_exc, 'launcher:version-out')
        print(_vtxt, flush=True)
        os._exit(0)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    _ck_boot_trace('qapp')
    if argv1 == '--selftest-merge':
        _ck_selftest_merge157(app)
        return 0
    if argv1 in ('--selftest-theme', '主题矩阵'):
        # 只触发主题矩阵切换并四段打点，结果落 JSONL + summary，然后直接退出。
        _mout = (os.environ.get('CK_SELFTEST_OUT')
                 or os.path.join(os.getcwd(), 'matrix_selftest.jsonl'))
        _mn = int(os.environ.get('CK_MATRIX_N') or 6)
        _mapi, _mgb = make_api(port)
        _mw = build_ui(port, _mapi, _mgb, None, None)
        _msum = _mw._ck_matrix_probe(_mn, _mout)
        try:
            with open(_mout + '.summary.json', 'w', encoding='utf-8') as _mf:
                json.dump(_msum, _mf, ensure_ascii=False, indent=1)
        except OSError as _exc:
            ck_swallow(_exc, 'launcher:matrix-sum-cli')
        try:
            print(json.dumps(_msum, ensure_ascii=False), flush=True)
        except OSError:
            pass   # --windowed 构建没有 stdout；结果以 summary 文件为准
        # 与 aihead 同因。原先这里直接 os._exit，跳过 Py_Finalize 后
        # pyside6.abi3.dll 在 DLL_PROCESS_DETACH 期访问违例（0xC0000005）。
        _ck_selftest_shutdown(app, _mw, 'launcher:matrix-teardown')
        return 0
    if argv1 == '--selftest-aihead':
        # 只读自检：AI 区逐 tab 离屏抓图 + 量页内 48 px 条高度，不启真窗、不写配置。
        _aout = (os.environ.get('CK_SELFTEST_OUT')
                 or os.path.join(os.getcwd(), 'aihead_selftest'))
        try:
            os.makedirs(_aout, exist_ok=True)
        except OSError as _exc:
            ck_swallow(_exc, 'launcher:aihead-mkdir')
        _amapi, _amgb = make_api(port)
        _aw = build_ui(port, _amapi, _amgb, None, None)
        # 先把窗口尺寸定死再抓图。子页在离屏下要等布局才拿到几何，
        # 直接 grab 子页会得到未布局的空白图（并排对照拍不出来）。
        _aw.resize(1280, 860)
        _aw.show()
        for _ in range(6):
            app.processEvents()
        _aw._ck_show_ai()
        for _ in range(4):
            app.processEvents()
        _at = _aw._ck_ai_tabs
        _rows = []
        for _i in range(_at.count()):
            _at.setCurrentIndex(_i)
            for _ in range(4):
                app.processEvents()
            _p = _at.currentWidget()
            _bar = _p.findChild(QWidget, 'aiBar')
            _txt = []
            if _bar is not None:
                _txt = [_x.text() for _x in _bar.findChildren(QLabel) if _x.text()]
            _pf = os.path.join(_aout, 'aihead_%d.png' % _i)
            _rows.append({'i': _i, 'tab': _at.tabText(_i),
                          'bar_h': (_bar.height() if _bar is not None else 0),
                          'bar_w': (_bar.width() if _bar is not None else 0),
                          'bar_text': _txt,
                          'page_min_h': _p.minimumSizeHint().height(),
                          'page_size': [_p.width(), _p.height()],
                          'png': _pf, 'saved': bool(_aw.grab().save(_pf))})
        try:
            with open(os.path.join(_aout, 'aihead.json'), 'w', encoding='utf-8') as _af:
                json.dump(_rows, _af, ensure_ascii=False, indent=1)
            print(json.dumps(_rows, ensure_ascii=False), flush=True)
        except (OSError, TypeError) as _exc:
            ck_swallow(_exc, 'launcher:aihead-json')
        # 真窗自检收尾。窗口还活着就 os._exit，ExitProcess 会在 PySide
        # (pyside6.abi3.dll) 尚未完成析构时卸载 DLL，实测 0xC0000005。
        # 改为有界正常收尾：销毁窗口 → 停工作线程 → 正常返回，让解释器自己走完。
        _ck_selftest_shutdown(app, _aw, 'launcher:aihead-teardown')
        return 0
    if argv1 == '--selftest-scroll':
        # 只读自检：2000 行定时 / 120 会话的渲染与滚动耗时（离屏，不写后端/配置）。
        _sout = (os.environ.get('CK_SELFTEST_OUT')
                 or os.path.join(os.getcwd(), 'scroll_selftest.json'))
        _smapi, _smgb = make_api(port)
        _sw = build_ui(port, _smapi, _smgb, None, None)
        _sw.show()
        for _ in range(3):
            app.processEvents()
        _srec = _sw._ck_scroll_probe(int(os.environ.get('CK_SCHED_N') or 2000),
                                     int(os.environ.get('CK_SESS_N') or 120))
        try:
            with open(_sout, 'w', encoding='utf-8') as _sf:
                json.dump(_srec, _sf, ensure_ascii=False, indent=1)
            print(json.dumps(_srec, ensure_ascii=False), flush=True)
        except (OSError, TypeError) as _exc:
            ck_swallow(_exc, 'launcher:scroll-json')
        # 与 matrix/aihead 同因，统一走有界正常收尾。
        _ck_selftest_shutdown(app, _sw, 'launcher:scroll-teardown')
        return 0
    if argv1 == '--selftest-smoke':
        # 构建门禁：模块可导入 + build_ui 可构造 + 逐页/逐 AI tab 切换无异常。
        # Qt 槽内异常不会让进程失败退出，必须看 errors 而不是只看退出码。
        _kmapi, _kmgb = make_api(port)
        _kw = None
        try:
            _kw = build_ui(port, _kmapi, _kmgb, None, None)
            _krec = _kw._ck_smoke()
        except Exception:
            _krec = {'pages': 0, 'ai_tabs': 0,
                     'errors': ['build_ui: %s' % traceback.format_exc(limit=6)]}
        _kout = os.environ.get('CK_SELFTEST_OUT')
        if _kout:
            try:
                with open(_kout, 'w', encoding='utf-8') as _kf:
                    json.dump(_krec, _kf, ensure_ascii=False, indent=1)
            except OSError as _exc:
                ck_swallow(_exc, 'launcher:smoke-out')
        if _krec.get('errors'):
            print('SMOKE_FAIL ' + json.dumps(_krec, ensure_ascii=False), flush=True)
            _ck_selftest_shutdown(app, _kw, 'launcher:smoke-teardown')
            # 模块底部是 main() 而不是 sys.exit(main())，return 1 会被丢弃成
            # 退出码 0；门禁不能只靠 stdout 判失败，这里显式抛出以保证非 0。
            raise SystemExit(1)
        print('SMOKE_OK ' + json.dumps(_krec, ensure_ascii=False), flush=True)
        _ck_selftest_shutdown(app, _kw, 'launcher:smoke-teardown')
        return 0
    if argv1 == '--selftest-portui':
        # 缺陷自证：动作成功后「不切页」，端口行文本/按钮在 300 ms 内变为目标态。
        # 只走 UI 链路（_ck_port_after_action 与 on_toggle 成功分支同一个函数），不碰网络。
        _puapi, _pugb = make_api(port)
        _pw = None
        _rows = []
        try:
            _pw = build_ui(port, _puapi, _pugb, None, None)
            _pw.resize(1280, 860)
            for _t in (getattr(_pw, '_r19_timers', ()) or ()):
                _t.stop()
            _pw.show()
            for _ in range(4):
                app.processEvents()
            _pw._ck_port_after_action('c1', True)   # 预热：先置开，确保有真实变化可测
            app.processEvents()
            for _want, _label in ((False, 'off'), (True, 'on')):
                _t0 = time.perf_counter()
                _ms = _pw._ck_port_after_action('c1', _want)
                app.processEvents()
                _wall = round((time.perf_counter() - _t0) * 1000.0, 2)
                _v = _pw._ck_port_visual('1')
                _expect = '● 关闭' if not _want else '● 使能'
                _ok = (_v['status'] == _expect) and (_v['checked'] == _want) \
                    and _wall <= 300.0
                _rows.append({'dir': _label, 'apply_ms': _ms, 'wall_ms': _wall,
                              'status': _v['status'], 'btn': _v['btn'],
                              'checked': _v['checked'], 'expect': _expect,
                              'ok': bool(_ok)})
        except Exception:
            _rows.append({'error': traceback.format_exc(limit=6)})
        _pout = os.environ.get('CK_SELFTEST_OUT')
        if _pout:
            try:
                with open(_pout, 'w', encoding='utf-8') as _pf:
                    json.dump(_rows, _pf, ensure_ascii=False, indent=1)
            except OSError as _exc:
                ck_swallow(_exc, 'launcher:portui-out')
        _pok = bool(_rows) and all(r.get('ok') for r in _rows)
        if not _pok:
            print('PORTUI_FAIL ' + json.dumps(_rows, ensure_ascii=False), flush=True)
            _ck_selftest_shutdown(app, _pw, 'launcher:portui-teardown')
            raise SystemExit(1)
        print('PORTUI_OK ' + json.dumps(_rows, ensure_ascii=False), flush=True)
        _ck_selftest_shutdown(app, _pw, 'launcher:portui-teardown')
        return 0
    try:
        QTimer.singleShot(2500, ck_ensure_start_menu_shortcut)
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:shortcut-defer')
    # R48 / C18：主线程 200ms 心跳，量化页面冻结/卡顿对通知投递的影响。
    try:
        _ck_beat_prev = {'t': time.perf_counter()}

        def _ck_beat_tick():
            _now = time.perf_counter()
            _gap = (_now - _ck_beat_prev['t']) * 1000.0
            _ck_beat_prev['t'] = _now
            _CK_BEAT['gap_ms'] = _gap
            if _gap > _CK_BEAT.get('max_gap_ms', 0.0):
                _CK_BEAT['max_gap_ms'] = _gap

        _ck_bt = QTimer()
        _ck_bt.setInterval(200)
        _ck_bt.timeout.connect(_ck_beat_tick)
        _ck_bt.start()
        app._ck_beat = _ck_bt
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:beat')
    try:
        from PySide6.QtCore import QTranslator, QLibraryInfo
        _ck_tr = QTranslator(app)
        if _ck_tr.load('qtbase_zh_CN', QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
            app.installTranslator(_ck_tr)
            app._ck_tr = _ck_tr
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:i18n')
    app.setApplicationName(PANEL_TITLE)
    if CK_FRAME_PROBE:
        try:
            _ft = QTimer()
            _ft.setInterval(16)
            _ft.timeout.connect(_ck_frame_tick)
            _ft.start()
            app._ck_ft = _ft
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:2738')
    app.setQuitOnLastWindowClosed(False)

    # 单实例锁：多开时激活已有窗口后退出（架构 A01）
    LOCK_NAME = 'CuktechPanelSingleInstance_v2'
    probe = QLocalSocket()
    probe.connectToServer(LOCK_NAME)
    _probe_conn = probe.waitForConnected(200)
    if _probe_conn:
        probe.write(b'show')
        probe.flush()
        probe.waitForBytesWritten(200)
        probe.disconnectFromServer()
        return
    QLocalServer.removeServer(LOCK_NAME)
    # 单实例/自检命令通道：show=激活窗口；shot N PATH=切到第N页并截图到PATH；
    # quit=退出。具体 dispatch 在 build_ui 后注入（需 on_select/w）。
    cmd_holder = {"dispatch": None}
    single = QLocalServer()
    single.listen(LOCK_NAME)
    single.cmd_holder = cmd_holder

    def _activate_existing():
        for wgt in app.topLevelWidgets():
            if wgt.isVisible():
                wgt.showNormal()
                wgt.activateWindow()

    def _on_single_conn():
        c = single.nextPendingConnection()
        if c is None:
            return
        _st = {"done": False, "gone": False}

        def _rd():
            try:
                msg = bytes(c.readAll()).decode("utf-8", "replace").strip()
            except Exception:
                msg = ""
            if msg:
                _st["done"] = True
                if cmd_holder["dispatch"] is not None:
                    cmd_holder["dispatch"](msg)
                elif msg == "show":
                    _activate_existing()
        c.readyRead.connect(_rd)
        _rd()

        def _alive():
            """C++ 侧是否还活着。_rd() 里派发 quit/close 后 socket 可能已被析构。"""
            try:
                from shiboken6 import isValid
                return bool(isValid(c))
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:reap-valid')
                return True

        def _reap():
            # C23：客户端（_r28_ctl）是「写完即关」，断开瞬间最后一包可能还在
            # 读缓冲里——此类实现曾出现同型竞态。所以不照搬固定延时，而是
            # 「先有界排空再删」：处理过消息直接删；没处理过就补读一次，仍空再
            # 等 80ms 重读一次，确认没有数据才回收。
            # 崩溃根因不是排空不够，而是排空过程中命令（quit/close）把对象带走，
            # 之后仍去 disconnect/deleteLater，抛 shiboken RuntimeError。逐段验活即可。
            if _st["gone"]:
                return
            if not _alive():
                _st["gone"] = True
                return
            if not _st["done"]:
                _rd()
                if not _st["done"] and _alive():
                    try:
                        if c.waitForReadyRead(80):
                            _rd()
                    except RuntimeError:
                        _st["gone"] = True
                        return
            if not _alive():
                _st["gone"] = True
                return
            _st["gone"] = True
            try:
                c.readyRead.disconnect(_rd)
            except (RuntimeError, TypeError) as _exc:
                ck_trace('reap_disconnect_skip', err=type(_exc).__name__)
            if _alive():
                c.deleteLater()

        c.disconnected.connect(_reap)
        # 兜底：客户端既不断开也不发命令时 4s 回收，避免 socket 永久驻留
        QTimer.singleShot(4000, _reap)
    single.newConnection.connect(_on_single_conn)
    _ck_boot_trace('lock')

    # 托管 headless 子进程（QProcess 父子绑定）：随主程序启动而启动
    # GUI 建 job 并独占句柄；atexit 关闭句柄 = 内核带走 job 内所有进程。
    ck_job_create()
    atexit.register(ck_job_close)
    headless_proc = None

    def _spawn_headless():
        # C1：统一派生（env 标记让子进程跳过单实例探测——它是合法服务进程）
        nonlocal headless_proc
        if headless_proc is not None and headless_proc.state() != QProcess.ProcessState.NotRunning:
            return
        headless_proc = QProcess()
        headless_proc.setProcessChannelMode(QProcess.ForwardedOutputChannel)
        _qenv = QProcessEnvironment.systemEnvironment()
        _qenv.insert('CUKTECH_SPAWNED_BY_UI', '1')
        headless_proc.setProcessEnvironment(_qenv)
        if getattr(sys, 'frozen', False):
            headless_proc.start(sys.executable, ['--headless'])
        else:
            headless_proc.start(sys.executable,
                                [os.path.abspath(sys.argv[0]), '--headless'])
        # 派生后立刻入 job（早于等端口/健康检查）；失败只记日志，不影响启动。
        if headless_proc.waitForStarted(3000):
            ck_job_assign(int(headless_proc.processId() or 0))
        _wait_for_port(port, 60000)

    _ck_boot_trace('pre_spawn')
    if not port_up(port):
        _spawn_headless()
    if not port_up(port):
        return
    _ck_boot_trace('spawn_chk')
    # C1 BLE 自愈：后端进程意外退出后，GUI 每 8s 检测一次，退避重连（上限 6 次，
    # 成功即计数归零），避免服务挂掉后数据永久停摆。
    _heal_attempts = {'n': 0}

    def _heal_check():
        if port_up(port):
            _heal_attempts['n'] = 0
            return
        if _heal_attempts['n'] >= 6:
            return
        _heal_attempts['n'] += 1
        _spawn_headless()

    heal_timer = QTimer()
    heal_timer.setInterval(8000)
    heal_timer.timeout.connect(_heal_check)
    heal_timer.start()
    _ck_boot_trace('pg_deferred')
    # 1.4.0：首次启动先征得同意（拒绝只停用 AI，离线功能照常）。
    # 托盘静默启动不弹模态：未同意时 AI 保持停用，用户下次正常启动再看。
    if not tray_start:
        try:
            _pf = ux.Prefs('DoubaoWork', 'CuktechPanel')
            if _pf.consent_ok():
                _ck_dbg('consent: 已同意 %s，跳过首启卡' % ux.CONSENT_VERSION)
            elif _pf.consent_decided():
                _ck_dbg('consent: 已拒绝 %s，跳过首启卡（AI 保持停用）' % ux.CONSENT_VERSION)
            else:
                _ck_dbg('consent: 未问过（当前 %s），弹首启卡' % ux.CONSENT_VERSION)
                ux.ensure_consent(None, _pf)
                _ck_dbg('consent: 首启卡结束 accepted=%r' % _pf.consent_ok())
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:consent-first-run')
    else:
        _ck_dbg('consent: 托盘静默启动，跳过首启卡')
    api, get_bytes = make_api(port)
    _ck_boot_trace('build_start')
    w = build_ui(port, api, get_bytes, headless_proc, single)
    _ck_boot_trace('build_done')
    w._single_server = single
    w._headless_proc = headless_proc

    def _cascade_kill():
        proc = getattr(w, '_headless_proc', None)
        if proc is not None:
            try:
                proc.kill()
                proc.waitForFinished(3000)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:2839')
    app.aboutToQuit.connect(_cascade_kill)

    if tray_start:
        w.hide()
    elif getattr(w, '_default_maximized', False):
        w.showMaximized()
    else:
        w.show()
    _apply_dark_titlebar(w)
    _install_hotkeys(w)
    _ck_boot_trace('shown')
    # C34-UI：AI 入口只剩导航底部一处（hero 横带与充电记录页的两处已移除），
    # 首次打开给一次轻提示；看过即写偏好，之后不再出现（不是常驻入口）。
    if not tray_start and not getattr(w, '_ck_tray_only', False):
        try:
            _hint = QSettings('DoubaoWork', 'CuktechPanel')
            if not _hint.value('ai/entryHintSeen', False, type=bool):
                def _ai_hint():
                    try:
                        w._toast.show('小源 已移到左下角，随时可以问它', 'info', 3600)
                    except Exception as _exc:
                        ck_swallow(_exc, 'launcher:entry-hint')
                QTimer.singleShot(1500, _ai_hint)
                _hint.setValue('ai/entryHintSeen', True)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:entry-hint-init')
    # C34h：图表（pyqtgraph）在首帧之后构建，可交互时间不含导入/建图
    _ckbc = getattr(w, '_ck_build_charts', None)
    if _ckbc is not None and os.environ.get('CK_NO_CHARTS') != '1':
        # 1.5.8：建图实测 486 ms（pyqtgraph 导入 + 主图/4 迷你图），原先启动 60 ms
        # 后无条件执行，首屏必卡半秒。改为「首次进入含图页时才建」：
        # 默认落在状态页时启动零成本；已在该页时仍延后 60 ms 让首帧先出来。
        _stk_c = getattr(w, '_ck_stack', None)
        _port_pg = getattr(w, '_ck_port_page', None)
        # 2.1.2：主图在状态页，迷你图在端口页——旧逻辑只认端口页，默认停在状态页时
        # 永不建图，曲线位一直是「图表加载中…」骨架（用户报「曲线没了」的真因）。
        _chart_pg = None
        try:
            _q = w.findChild(QWidget, 'chartSkeleton')
            while _q is not None:
                _pp = _q.parentWidget()
                if isinstance(_pp, QStackedWidget):
                    _chart_pg = _q
                    break
                _q = _pp
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:chart-page')
        _ckbc_done = {'n': 0}

        def _ckbc_kick():
            if _ckbc_done['n']:
                return
            _ckbc_done['n'] = 1
            QTimer.singleShot(0, _ckbc)

        def _ckbc_need(_cur):
            return _cur is not None and (_cur is _port_pg or _cur is _chart_pg)

        if _stk_c is not None and (_port_pg is not None or _chart_pg is not None):
            def _ckbc_on_page(_i=0):
                try:
                    if _ckbc_need(_stk_c.currentWidget()):
                        _ckbc_kick()
                except RuntimeError:
                    pass
            _stk_c.currentChanged.connect(_ckbc_on_page)
        if _stk_c is None or _ckbc_need(_stk_c.currentWidget()):
            QTimer.singleShot(60, _ckbc_kick)
    _rc = app.exec()
    try:
        # 正常退出留痕：菜单/托盘/关闭按钮/命令通道都汇聚到这里；崩溃（被杀）不会写
        _d = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'CuktechPanel')
        os.makedirs(_d, exist_ok=True)
        with open(os.path.join(_d, '_graceful_exit.json'), 'w', encoding='utf-8') as _f:
            json.dump({"line": "cuktech", "ts": time.time(),
                       "at": ck_time.fmt(),
                       "how": _CK_EXIT_HOW['v'], "rc": _rc, "pid": os.getpid()},
                      _f, ensure_ascii=False)
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:graceful-exit')
    sys.exit(_rc)


# 自检/IPC 命令分发从 build_ui 内搬出（命令名/调用点不变）。
# 67 个名字是原闭包自由变量，全部为只读（0 个被赋值/nonlocal），
# 由 build_ui 末尾的同名薄封装 _dispatch 在调用时按值传入。
def _ck_ui_dispatch(msg, *, _AI, _ai_anchor, _ai_export_to, _ai_new_sess, _ai_ren_sess,
                            _ai_send, _ai_tabs, _ai_test, _confirm, _dbg_log,
                            _fit_kpis, _gen_daily_report, _grab_to, _inp, _log, _mute,
                            _on_tray_activated, _open_adv_cfg, _open_device_view,
                            _open_docs, _seq_shot, _set_tabs, _set_unread, _testb,
                            _tray_base_icon, _unread, adv_panel, ai_page, all_off,
                            all_on, badge_chg, charger_dev, confirm_exit, g_rec,
                            hero_power, kpi_wrap, mid_w, on_close, on_select,
                            open_countdown, open_proto, pg_lbl, pg_next, pg_prev,
                            poll_rec_stats, poll_sessions, poll_status, port_cards,
                            qa_mute, rail, rc, rec_empty, rec_page, rec_port,
                            rec_range, rec_sort, render_schedules, se, sess_ids,
                            sess_page_rows, sess_tbl, settings, show_session_points,
                            stack, tm, tray, w):
    _dbg_log("dispatch %r" % msg)
    try:
        parts = msg.split()
        if msg == "show":
            w.showNormal(); w.activateWindow()
        elif parts[0] == "matrix":
            # 只读自检：主题矩阵切换四段打点 → JSONL（不改配置）。
            # 用 singleShot 脱离 socket 回调栈，避免 processEvents 重入回收。
            _n = int(parts[1]) if len(parts) > 1 else 6
            _p = parts[2] if len(parts) > 2 else os.path.join(
                os.environ.get('LOCALAPPDATA', '.'), 'CuktechPanel',
                'matrix_probe.jsonl')

            def _run_matrix(_n=_n, _p=_p):
                try:
                    _sum = w._ck_matrix_probe(_n, _p)
                    with open(_p + '.summary.json', 'w',
                              encoding='utf-8') as _f:
                        json.dump(_sum, _f, ensure_ascii=False)
                    _dbg_log('matrix %s' % json.dumps(_sum, ensure_ascii=False))
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:matrix-cmd')
            QTimer.singleShot(0, _run_matrix)
        elif parts[0] == "traytest":
            # R28 自检：托盘/通知能力与前提状态（不含截图）
            _dbg_log('TRAYTEST available=%s supportsMessages=%s visible=%s iconNull=%s '
                     'aumid=%s' % (
                         QSystemTrayIcon.isSystemTrayAvailable(),
                         QSystemTrayIcon.supportsMessages(),
                         tray.isVisible(), _tray_base_icon.isNull(),
                         os.environ.get('CK_AUMID', '')))
        elif parts[0] == "notifytest":
            # R28 自检：一键发系统通知（普通/告警/点击跳转），驱动托盘 showMessage
            _kind = parts[1] if len(parts) > 1 else 'plain'
            _body = parts[2] if len(parts) > 2 else ''
            if _mute.get('on'):
                _n = _ck_mute_count('plain')
                _dbg_log('notifytest muted+counted n=%d' % _n)
                return
            try:
                if _kind == 'jump':
                    _body = _body or '充电功率跃迁 65W→90W（点击定位到概览页）'
                    _toast = (PANEL_TITLE, _body,
                              QSystemTrayIcon.Information, 6000)
                elif _kind == 'warn':
                    _body = _body or '已断开：智能保护'
                    _toast = ('设备状态告警', _body,
                              QSystemTrayIcon.Warning, 6000)
                else:
                    _body = _body or '通知测试：系统通知通路正常'
                    _toast = (PANEL_TITLE, _body,
                              QSystemTrayIcon.Information, 4000)
                _page = 0 if _kind == 'jump' else None
                if _kind == 'shell':
                    # R36 A/B 对照：强制旧通路（Shell_NotifyIcon 瞬态）
                    # C34（缺陷 6）：测试通路也受静音约束，否则「静音后还能弹」是假阳性。
                    if _CK_MUTE.get('on'):
                        _n = _ck_mute_count('shell')
                        _dbg_log('notifytest kind=shell muted+counted n=%d' % _n)
                        return
                    tray.showMessage(PANEL_TITLE, _body or '通知测试：Shell 通路',
                                     QSystemTrayIcon.Information, 4000)
                    _dbg_log('notifytest kind=shell path=tray')
                    return
                _winrt_ok = _ck_toast_send(_toast[0], _toast[1], _kind, _page)
                if not _winrt_ok:
                    tray.showMessage(*_toast)
                _dbg_log('notifytest kind=%s winrt=%s sent avail=%s supports=%s' % (
                    _kind, _winrt_ok, QSystemTrayIcon.isSystemTrayAvailable(),
                    QSystemTrayIcon.supportsMessages()))
            except Exception as _e:
                _dbg_log('notifytest err %r' % (_e,))
        elif parts[0] == "notifylat":
            # R48 / C18：时效探针 notifylat <kind> <tag> <state>
            _kind = parts[1] if len(parts) > 1 else 'plain'
            _tag = parts[2] if len(parts) > 2 else None
            _CK_NOTIFY_CTX['state'] = parts[3] if len(parts) > 3 else 'fg'
            _t_ev = time.perf_counter()
            _ok = _ck_toast_send(PANEL_TITLE,
                                 '时延探针 %s' % (_tag or ''), _kind,
                                 None, _tag, _t_ev)
            if not _ok:
                tray.showMessage(PANEL_TITLE, '时延探针（tray 回退）',
                                 QSystemTrayIcon.Information, 4000)
            _dbg_log('notifylat kind=%s tag=%s state=%s winrt=%s' % (
                _kind, _tag, _CK_NOTIFY_CTX['state'], _ok))
        elif parts[0] == "histdump":
            # R48 / C18：通知中心留痕导出
            try:
                _items = _ck_history_snapshot()
                with open(parts[1], 'w', encoding='utf-8') as _fh:
                    json.dump({'aumid': CK_AUMID, 'count': len(_items),
                               'items': _items}, _fh,
                              ensure_ascii=False, indent=1)
                _dbg_log('histdump n=%d path=%s' % (len(_items), parts[1]))
            except Exception as _e:
                _dbg_log('histdump err %r' % (_e,))
        elif parts[0] == "mute":
            # C34f 自检：与托盘菜单同一路径切换静音（避免坐标点击误伤其他应用）
            _val = (parts[1].strip().lower() in ("on", "1", "true")) if len(parts) > 1 else (not _mute['on'])
            try:
                qa_mute.setChecked(_val)
                _dbg_log('mute cmd=%s on=%s unread=%d'
                         % (_val, _mute['on'], _CK_UNREAD['n']))
            except Exception as _e:
                _dbg_log('mute cmd err %r' % (_e,))
        elif parts[0] == "badge":
            # R28 自检：设置托盘未读角标（0/1/9/99+ 四档截图用）
            try:
                _set_unread(int(parts[1]) if len(parts) > 1 else 0)
                _dbg_log('badge n=%s' % _unread.get('n'))
            except Exception as _e:
                _dbg_log('badge err %r' % (_e,))
        elif parts[0] == "badgeshot" and len(parts) >= 3:
            # R47 / C17 自检：冻结态导出角标位图（32px 原件 + 16px 成品 + 4× 放大）
            try:
                _n, _p = int(parts[1]), parts[2]
                _pm = ck_badge_pixmap(_n, 32, _tray_base_icon.pixmap(QSize(32, 32)))
                _pm.save(_p + '_32.png')
                _s = _pm.scaled(16, 16, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                _s.save(_p + '_16.png')
                _s.scaled(64, 64, Qt.IgnoreAspectRatio, Qt.FastTransformation).save(
                    _p + '_16x4.png')
                print('[badgeshot] n=%d saved=%s_32/16/16x4.png' % (_n, _p), flush=True)
            except Exception as _e:
                print('[badgeshot] err %r' % (_e,), flush=True)
        elif parts[0] == "diag":
            _stack2 = w.findChild(QStackedWidget)
            _dbg_log("DIAG local id=%s count=%s | find id=%s count=%s" %
                     (id(stack), stack.count(),
                      id(_stack2) if _stack2 else None,
                      _stack2.count() if _stack2 else None))
            _dbg_log("DIAG visible=%s geom=%s children=%s" %
                     (w.isVisible(), w.geometry(),
                      len(w.findChildren(QStackedWidget))))
        elif parts[0] == "shot" and len(parts) >= 3:
            idx = int(parts[1]); path = parts[2]
            _dbg_log("shot idx=%s stack=%s valid=%s" %
                     (idx, stack.count(), 0 <= idx < stack.count()))
            if 0 <= idx < stack.count():
                _t_sel = time.perf_counter()
                on_select(idx)
                _dbg_log('select idx=%s ms=%.1f' % (idx, (time.perf_counter() - _t_sel) * 1000))
                QTimer.singleShot(900, lambda p=path, i=idx: _grab_to(p, i))
        elif parts[0] == "peek" and len(parts) >= 2:
            # C33：不切页、不触发 force commit 的纯抓图（曲线节拍取证用）
            QTimer.singleShot(0, lambda p=parts[1]: _grab_to(p))
        elif parts[0] == "cfast" and len(parts) >= 2:
            # C33：同构建 A/B——曲线实时提交开/关
            _CK_CFAST['on'] = parts[1] not in ('0', 'off', 'false')
            _dbg_log('cfast on=%s' % _CK_CFAST['on'])
        elif parts[0] == "seq" and len(parts) >= 3:
            # C33 取证：切页首帧序列（AI 页 0.45s 未绘制根因定位）
            idx = int(parts[1]); _sdir = parts[2]
            try:
                os.makedirs(_sdir, exist_ok=True)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:seqdir')
            _t_sel = time.perf_counter()
            on_select(idx)
            _dbg_log('select idx=%s ms=%.1f' % (idx, (time.perf_counter() - _t_sel) * 1000))
            for _ms in (60, 120, 200, 300, 480, 700, 900):
                QTimer.singleShot(_ms, (lambda m=_ms, i=idx, d=_sdir: _seq_shot(d, i, m)))
            _dbg_log('seq idx=%s dir=%s' % (idx, _sdir))
        elif parts[0] == "devstate" and len(parts) >= 3:
            # R32 自检：设备图/充电图标状态取证（unconn / idle / charging / full）
            # C34（缺陷 2）：四态注入贯穿整页（端口卡/横幅/线稿图标/徽章），
            # 不再只改设备图；`devstate off <png>` 恢复真实轮询。
            _mode, _path = parts[1], parts[2]
            try:
                if _mode in ('off', 'none'):
                    w._ck_devmode = None
                    charger_dev.set_state(False, {})
                elif _mode in ('unconn', 'idle', 'charging', 'full'):
                    w._ck_devmode = _mode
                else:
                    _dbg_log('devstate bad mode=%s' % _mode)
                    _mode = None
                if _mode in ('unconn', 'idle', 'charging', 'full'):
                    _dp = _ck_dev_plan(_mode, {})
                    charger_dev.set_state(_dp['connected'], _dp['raw'],
                                         scene=_dp['scene'])
                _dbg_log('devstate mode=%s dir=%s' % (_mode, charger_dev._img_dir))
            except Exception as _e:
                _dbg_log('devstate err %r' % (_e,))
            try:
                poll_status()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:devstate-poll')
            QTimer.singleShot(700, lambda p=_path: _grab_to(p))
        elif parts[0] == "herotest" and len(parts) >= 2:
            # C29 自检：首屏第一个数字（hero 总功率）空值态 / 有值态取证。
            # 取证口径：裁 hero 数字外扩 16px；同时记录字号与空/值分支命中计数。
            _hd = parts[1]
            try:
                os.makedirs(_hd, exist_ok=True)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:herotest_mkdir')

            def _hero_shot(_tag):
                try:
                    _ck_num_flush(True)
                    _pm = w.grab()
                    # 裁到 hero 左列面板（数字 + W + LIVE + 标题 + 刻度）：
                    # 数字由面板自绘且大字号可能溢出控件矩形，不能只裁 label。
                    # 注意：grab() 是物理像素，mapTo 给的是逻辑像素，必须乘 DPR。
                    _dpr = _pm.devicePixelRatio() or 1.0
                    _pos = mid_w.mapTo(w, QPoint(0, 0))
                    _r = QRectF(_pos.x() - 8, _pos.y() - 8,
                                mid_w.width() + 16, mid_w.height() + 16)
                    _r = _r.intersected(QRectF(0, 0, _pm.width() / _dpr,
                                               _pm.height() / _dpr))
                    _rp = QRect(int(_r.x() * _dpr), int(_r.y() * _dpr),
                                int(_r.width() * _dpr), int(_r.height() * _dpr))
                    _out = os.path.join(_hd, 'hero_%s.png' % _tag)
                    _pm.copy(_rp).save(_out)
                    _dbg_log('herotest tag=%s rect=%s dpr=%.2f fontpx=%s text=%r '
                             'label_w=%d empty_hits=%d value_hits=%d save=%s' % (
                                 _tag, (_rp.x(), _rp.y(), _rp.width(), _rp.height()),
                                 _dpr, hero_power.font().pixelSize(),
                                 hero_power.text(), hero_power.width(),
                                 hero_power._c29_empty_hits,
                                 hero_power._c29_value_hits, _out))
                except Exception as _e:
                    _dbg_log('herotest err %r' % (_e,))

            def _hero_value():
                hero_power.set_target(42.0)
                QTimer.singleShot(800, lambda: _hero_shot('value'))

            def _hero_restore():
                hero_power.set_target(None)
                QTimer.singleShot(800, lambda: _hero_shot('empty2'))

            hero_power.set_target(None)
            # 先切回状态页（首屏），否则抓到的是上次停留页的像素
            on_select(0)
            QTimer.singleShot(1000, lambda: _hero_shot('empty'))
            QTimer.singleShot(1300, _hero_value)
            QTimer.singleShot(2400, lambda: _hero_shot('value'))
            QTimer.singleShot(2700, _hero_restore)
            QTimer.singleShot(3800, lambda: _hero_shot('empty2'))
        elif parts[0] == "c30probe" and len(parts) >= 2:
            # C30 自检：首屏 4 个 KPI 的空值态/有值态分支+实际绘制色取证，
            # 以及时长量纲归一的纯函数向量。冻结 KPI 轮询，避免取证窗口被覆盖。
            _pd = parts[1]
            try:
                os.makedirs(_pd, exist_ok=True)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:c30probe_mkdir')
            _ptags = ('hero', 'kpi_vmax', 'kpi_cur', 'kpi_active', 'kpi_load')

            def _c30_line(_tag):
                try:
                    _lbl = _CK_NUM_PROBE.get(_tag)
                    if _lbl is None:
                        _dbg_log('c30probe label tag=%s MISSING' % _tag)
                        return
                    _pm0 = None
                    _d = (w.devicePixelRatioF()
                          if hasattr(w, 'devicePixelRatioF') else 1.0)
                    _pos = _lbl.mapTo(w, QPoint(0, 0))
                    _c = _lbl._base_txt_color
                    _dbg_log('c30probe label tag=%s text=%r fontpx=%s '
                             'color=#%02x%02x%02x base=#%02x%02x%02x '
                             'empty_hits=%d value_hits=%d rect=(%d,%d,%d,%d) dpr=%.2f'
                             % (_tag, _lbl.text(), _lbl.font().pixelSize(),
                                _c.red(), _c.green(), _c.blue(),
                                _lbl._base_color.red(), _lbl._base_color.green(),
                                _lbl._base_color.blue(),
                                _lbl._c29_empty_hits, _lbl._c29_value_hits,
                                int(_pos.x() * _d), int(_pos.y() * _d),
                                int(_lbl.width() * _d), int(_lbl.height() * _d), _d))
                except Exception as _e:
                    _dbg_log('c30probe label err %r' % (_e,))

            def _c30_shot(_tag):
                try:
                    _ck_num_flush(True)
                    _pm = w.grab()
                    _pm.save(os.path.join(_pd, 'c30_%s.png' % _tag))
                    _d = _pm.devicePixelRatio() or 1.0
                    for _t in _ptags:
                        _c30_line(_t)
                    try:
                        _pos = kpi_wrap.mapTo(w, QPoint(0, 0))
                        _dbg_log('c30probe band kpi rect=(%d,%d,%d,%d) dpr=%.2f'
                                 % (int(_pos.x() * _d), int(_pos.y() * _d),
                                    int(kpi_wrap.width() * _d),
                                    int(kpi_wrap.height() * _d), _d))
                    except Exception as _e2:
                        _dbg_log('c30probe band err %r' % (_e2,))
                    _dbg_log('c30probe shot tag=%s px=%s dpr=%.2f'
                             % (_tag, (_pm.width(), _pm.height()), _d))
                except Exception as _e:
                    _dbg_log('c30probe shot err %r' % (_e,))

            def _c30_values():
                for _t, _v in (('kpi_vmax', 20.1), ('kpi_cur', 3.25),
                               ('kpi_active', 2), ('kpi_load', 56)):
                    _lbl = _CK_NUM_PROBE.get(_t)
                    if _lbl is not None:
                        _lbl.set_target(_v, flash=False)

            def _c30_restore():
                for _t in _ptags:
                    _lbl = _CK_NUM_PROBE.get(_t)
                    if _lbl is not None:
                        _lbl.set_target(None)

            for _v in (0, 59, 61, 3599, 3600, 54029, 86399, 86400, 1083439):
                _dbg_log('c30probe dur in=%d out=%s' % (_v, _ck_fmt_dur(_v)))
            # C32：电量进位向量 + 分支命中计数（bad 只在非法入参自增）
            _dbg_log('c30probe enstat_real %s' % (dict(_CK_EN_STAT),))
            for _v in (0, 0.34, 59.9, 999.4, 999.5, 1319.19, 2542.5,
                       12345.6, None, -1):
                _dbg_log('c30probe en in=%r out=%s' % (_v, _ck_fmt_energy(_v)))
            _dbg_log('c30probe enstat_vec %s' % (dict(_CK_EN_STAT),))
            w._c30_freeze = True
            on_select(0)
            QTimer.singleShot(900, lambda: _c30_shot('empty_in'))
            QTimer.singleShot(1300, _c30_values)
            QTimer.singleShot(2200, lambda: _c30_shot('valued'))
            QTimer.singleShot(2600, _c30_restore)
            QTimer.singleShot(3400, lambda: _c30_shot('empty_out'))
            QTimer.singleShot(3900, lambda: setattr(w, '_c30_freeze', False))
        elif parts[0] == "tab" and len(parts) >= 4:
            # 自检：切到设置页并选既有子标签（0..5，5=进阶配置）
            idx = int(parts[1]); sub = int(parts[2]); path = parts[3]
            _si = stack.indexOf(se)
            if _si >= 0:
                on_select(_si)
                stack.setCurrentIndex(_si)
            try:
                _set_tabs.setCurrentIndex(max(0, min(sub, _set_tabs.count() - 1)))
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3249')
            QTimer.singleShot(900, lambda p=path: _grab_to(p))
        elif parts[0] == "seshot" and len(parts) >= 3:
            # R06 自检：打开第 N 条记录详情并抓图（详情为模态，抓活动模态窗后关闭）
            _ri = int(parts[1])
            _sp = parts[2]

            def _open_detail(_try=0):
                try:
                    if not sess_ids and _try < 5:
                        # 列表刷新存在短空窗（后台拉取期间 sess_ids 被清空），重试
                        QTimer.singleShot(400, lambda: _open_detail(_try + 1))
                        return
                    show_session_points(_ri)
                except Exception:
                    try:
                        import traceback
                        with open(os.path.join(os.environ.get('TEMP', '.'), 'seshot_err.log'),
                                  'w', encoding='utf-8') as _ef:
                            _ef.write(traceback.format_exc())
                    except Exception as _exc:
                        ck_swallow(_exc, 'launcher:3270')

            def _grab_detail():
                try:
                    _d = None
                    for _tw in QApplication.topLevelWidgets():
                        if isinstance(_tw, QDialog) and _tw.isVisible():
                            _d = _tw
                            break
                    if _d is not None:
                        _d.grab().save(_sp)
                        _d.accept()
                    else:
                        with open(os.path.join(os.environ.get('TEMP', '.'), 'seshot_err.log'),
                                  'a', encoding='utf-8') as _ef:
                            _ef.write('NO_MODAL\n')
                except Exception:
                    try:
                        import traceback
                        with open(os.path.join(os.environ.get('TEMP', '.'), 'seshot_err.log'),
                                  'a', encoding='utf-8') as _ef:
                            _ef.write(traceback.format_exc())
                    except Exception as _exc:
                        ck_swallow(_exc, 'launcher:3293')
            QTimer.singleShot(800, _open_detail)
            QTimer.singleShot(4200, _grab_detail)
        elif parts[0] == "devview" and len(parts) >= 3:
            # C28 自检：打开「设备与断开原因」对话框并抓图（模态，抓后关闭）
            # 仅取证插桩，不改任何视觉；BEFORE/AFTER 双跑共用同一命令。
            _vt = int(parts[1]) if parts[1].isdigit() else 0
            _vp = parts[2]

            def _end_devview(_d, _path=_vp, _tab=_vt):
                try:
                    _d.grab().save(_path)
                    _dbg_log('devview saved tab=%s path=%s' % (_tab, _path))
                except Exception as _e:
                    _dbg_log('devview save err %r' % (_e,))
                finally:
                    try:
                        _d.reject()
                    except Exception as _exc:
                        ck_swallow(_exc, 'launcher:c28')

            def _grab_devview(_try=0):
                _d = None
                for _tw in QApplication.topLevelWidgets():
                    if isinstance(_tw, QDialog) and _tw.isVisible():
                        _d = _tw
                        break
                if _d is None:
                    if _try < 10:
                        QTimer.singleShot(350, lambda: _grab_devview(_try + 1))
                    else:
                        _dbg_log('devview NO_MODAL')
                    return
                try:
                    _tb = _d.findChild(QTabWidget)
                    if _tb is not None:
                        _tb.setCurrentIndex(max(0, min(_vt, _tb.count() - 1)))
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:c28')
                QTimer.singleShot(400, lambda dd=_d: _end_devview(dd))

            QTimer.singleShot(600, _grab_devview)
            _open_device_view()
        elif parts[0] == "atab" and len(parts) >= 3:
            # 自检：切到「小源」页并选子页（0=对话 1=Agent 搭建 2=智能分析
            # 3=AI 配置 4=数据与隐私），900 ms 后抓图落盘。
            sub = int(parts[1]); path = parts[2]
            _gi = stack.indexOf(ai_page)
            if _gi >= 0:
                on_select(_gi)
            try:
                _ai_tabs.setCurrentIndex(max(0, min(sub, _ai_tabs.count() - 1)))
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3305')
            QTimer.singleShot(900, lambda p=path: _grab_to(p))
        elif parts[0] == "atool" and len(parts) >= 3:
            # 自检：触发工具卡片并抓图（低/中风险自动执行，高风险停在确认卡）
            _p = parts[1]
            _txt = ' '.join(parts[2:])
            _gi = stack.indexOf(ai_page)
            if _gi >= 0:
                on_select(_gi)
            try:
                _ai_tabs.setCurrentIndex(0)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3317')
            QTimer.singleShot(250, lambda t=_txt: _ai_send(t))
            QTimer.singleShot(2400, lambda p=_p: _grab_to(p))
        elif parts[0] == "acard" and len(parts) >= 3:
            # 自检：按锚点操作工具卡片（tf/tc/tu/tr），-1 表示最后一条
            _h = parts[1]
            _p = parts[2]
            try:
                _ai_anchor(QUrl(_h))
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3327')
            QTimer.singleShot(1400, lambda p=_p: _grab_to(p))
        elif parts[0] == "schedmock" and len(parts) >= 2:
            # R045 / C14 自检：非空定时行布局（仅进程内模拟，不写后端）
            # 上限从 9 提到 2000，用于「大数据滚动」同口径造数。
            try:
                _n = max(0, min(2000, int(parts[1])))
            except Exception:
                _n = 0
            _keys = list(PORT_NAMES.keys())
            _ms = []
            for _i in range(_n):
                _ms.append({'id': 'mock%d' % (_i + 1),
                            'port': _keys[_i % len(_keys)],
                            'time': '%02d:%02d' % (7 + _i, 30 if _i % 2 == 0 else 0),
                            'action': 'on' if _i % 2 == 0 else 'off',
                            'enabled': _i != 1})
            # R045 / C14：置保持标记，防止 poll_automation 覆盖 mock 行；n=0 时恢复真实轮询
            w._schedHold = _n > 0
            render_schedules(_ms)
        elif parts[0] == "schedscroll" and len(parts) >= 2:
            # R045 / C14 自检：定时列表滚到底/指定值（验证隐藏滚动条仍可滚动）
            _sa = w.findChild(QScrollArea, 'schedScroll')
            if _sa is not None:
                _sb = _sa.verticalScrollBar()
                _sb.setValue(_sb.maximum() if parts[1] == 'max' else int(parts[1]))
        elif parts[0] == "win" and len(parts) >= 3:
            # R031 自检：按逻辑尺寸 resize（便于 900×700 / 1000×731 同尺寸对照）
            try:
                w.showNormal()
                w.resize(int(parts[1]), int(parts[2]))
                w.move(20, 20)
                # R031：改尺寸后重算记录页页容量/KPI 字号（矮窗口不压盖）
                QTimer.singleShot(350, lambda: (
                    _fit_kpis(),
                    poll_rec_stats() if stack.currentIndex() == stack.indexOf(rc) else None))
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3336')
        elif parts[0] == "cfgshot" and len(parts) >= 2:
            # R031 自检：设置页 → 进阶配置（默认状态）→ 抓图（复刻基准）
            _p = parts[1]
            _si = stack.indexOf(se)
            if _si >= 0:
                on_select(_si)
            QTimer.singleShot(150, _open_adv_cfg)
            QTimer.singleShot(1700, lambda p=_p: _grab_to(p))
        elif parts[0] == "cfginfo" and len(parts) >= 2:
            # R031 自检：导出进阶配置页几何量化（卡片坐标/尺寸/页高/是否溢出）
            _p = parts[1]
            _si = stack.indexOf(se)
            if _si >= 0:
                on_select(_si)
            QTimer.singleShot(150, _open_adv_cfg)

            def _do_info_ck():
                try:
                    _pg = adv_panel.parentWidget()
                    _cards = []
                    for _h, _b in adv_panel._groups:
                        _c = _h.parentWidget()
                        _pt = _c.mapTo(_pg, _c.rect().topLeft())
                        _cards.append({'name': _h.property('nm'),
                                       'x': _pt.x(), 'y': _pt.y(),
                                       'w': _c.width(), 'h': _c.height(),
                                       'open': _b.isVisible()})
                    _foot = None
                    try:
                        _lay = adv_panel.layout()
                        _items = [_lay.itemAt(_i).widget()
                                  for _i in range(_lay.count())]
                        _items = [_x for _x in _items if _x is not None]
                        if _items:
                            _ys = [_x.mapTo(_pg, _x.rect().topLeft()).y()
                                   for _x in _items]
                            _hs = [_x.height() for _x in _items]
                            _foot = {'bottom': max(_y + _h for _y, _h
                                                   in zip(_ys, _hs))}
                    except Exception as _exc:
                        ck_swallow(_exc, 'launcher:3377')
                    _clip = [c['name'] for c in _cards
                             if c['y'] + c['h'] > _pg.height()]
                    _info = {
                        'window': [w.width(), w.height()],
                        'page': [_pg.width(), _pg.height()],
                        'page_hint_h': _pg.sizeHint().height(),
                        'page_min_h': _pg.minimumSizeHint().height(),
                        'groups': len(_cards),
                        'expanded': [c['name'] for c in _cards if c['open']],
                        'cards': _cards,
                        'footer': _foot,
                        'clipped': _clip,
                        'overflow': bool(
                            _pg.minimumSizeHint().height() > _pg.height()),
                    }
                    with open(_p, 'w', encoding='utf-8') as f:
                        json.dump(_info, f, ensure_ascii=False, indent=1)
                except Exception as e:
                    try:
                        with open(_p, 'w', encoding='utf-8') as f:
                            f.write('ERR %r' % e)
                    except Exception as _exc:
                        ck_swallow(_exc, 'launcher:3400')
            QTimer.singleShot(1300, _do_info_ck)
        elif parts[0] == "acfg" and len(parts) >= 2:
            # 自检：切到配置中心、展开「自动化」组并切换一个开关后抓图
            _p = parts[1]
            _si = stack.indexOf(se)
            if _si >= 0:
                on_select(_si)
            QTimer.singleShot(150, _open_adv_cfg)

            def _do_cfg():
                try:
                    # 1.5.5：「自动化与通知」已迁到自动化页的扁平设置组，
                    # 自检改为直接切换该组里的 notifyEnabled。
                    _c = adv_panel.controls.get('notifyEnabled', (None,))[0]
                    if _c is not None:
                        _c.setChecked(not _c.isChecked())
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:3416')
            QTimer.singleShot(420, _do_cfg)
            QTimer.singleShot(1700, lambda p=_p: _grab_to(p))
        elif parts[0] == "ask" and len(parts) >= 3:
            # 自检：真实提问并等待生成结束再截图（用于本地模型实测）
            _p = parts[1]; _txt = ' '.join(parts[2:])
            _gi = stack.indexOf(ai_page)
            if _gi >= 0:
                on_select(_gi)
            try:
                _ai_tabs.setCurrentIndex(0)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3428')
            _inp.setPlainText(_txt)
            QTimer.singleShot(250, lambda t=_txt: _ai_send(t))
            _st = {'n': 0}

            def _ask_tick(_path=_p):
                _st['n'] += 1
                if (not _AI.get('busy')) or _st['n'] > 240:
                    QTimer.singleShot(500, lambda: _grab_to(_path))
                else:
                    QTimer.singleShot(500, _ask_tick)
            QTimer.singleShot(700, _ask_tick)
        elif parts[0] == "aitest" and len(parts) >= 3:
            # 自检：真实点击「测试连接」，先抓"测试中"帧，结束再抓结果帧
            _mid_p, _fin_p = parts[1], parts[2]
            _gi = stack.indexOf(ai_page)
            if _gi >= 0:
                on_select(_gi)
            try:
                _ai_tabs.setCurrentIndex(3)   # 1.5.5：智能分析插到 2，AI 配置后移
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3449')

            def _aitest_run():
                _ai_test()
                QTimer.singleShot(900, lambda: _grab_to(_mid_p))
                _ts = {'n': 0}

                def _tick():
                    _ts['n'] += 1
                    if _testb.isEnabled() or _ts['n'] > 60:
                        QTimer.singleShot(500, lambda: _grab_to(_fin_p))
                    else:
                        QTimer.singleShot(500, _tick)
                QTimer.singleShot(1500, _tick)
            QTimer.singleShot(250, _aitest_run)
        elif parts[0] == "aiexport" and len(parts) >= 2:
            # 自检：无对话框导出调用记录，可选抓图（隐私/日志页真实导出证据）
            _p = parts[1]
            _gi = stack.indexOf(ai_page)
            if _gi >= 0:
                on_select(_gi)
            try:
                _ai_tabs.setCurrentIndex(4)   # 1.5.6：数据授权+隐私与日志合并为「数据与隐私」
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3473')
            _ai_export_to(_p)
            if len(parts) >= 3:
                QTimer.singleShot(900, lambda p=parts[2]: _grab_to(p))
        elif parts[0] == "docs" and len(parts) >= 2:
            # 自检：打开「说明与协议」轻量页并截图；
            # docs <png> [json] [宽] —— 宽 < 700 走窄屏下拉，json 记每条字数
            _open_docs(0)
            _dlg = getattr(w, '_docs_dlg', None)
            if _dlg is not None:
                if len(parts) >= 4 and parts[3].isdigit():
                    _wd = int(parts[3])
                    _dlg.resize(_wd, 600 if _wd >= 700 else 520)

                def _dump_legal(_d=_dlg, _p=parts[2]):
                    _lv = _d.findChild(ux.LegalView)
                    _out = {'entries': _lv.stats() if _lv is not None else [],
                            'compact': bool(_lv.is_compact()) if _lv else None,
                            'body_chars': (len(_lv.body.toPlainText())
                                           if _lv is not None else 0)}
                    with open(_p, 'w', encoding='utf-8') as _f:
                        json.dump(_out, _f, ensure_ascii=False, indent=1)
                if len(parts) >= 3:
                    QTimer.singleShot(700, _dump_legal)
                QTimer.singleShot(1000, lambda d=_dlg, p=parts[1]:
                                   d.grab().save(p))
        elif parts[0] == "docshot" and len(parts) >= 3:
            # 自检：打开「说明与协议」并选中第 N 条后抓图（逐份正文取证）
            _open_docs(int(parts[1]) if parts[1].isdigit() else 0)
            _dlg = getattr(w, '_docs_dlg', None)
            if _dlg is not None:
                QTimer.singleShot(900, lambda d=_dlg, p=parts[2]:
                                   d.grab().save(p))
        elif parts[0] == "portclick" and len(parts) >= 2:
            # 自检：真实点第一个端口的开关两次（开→关），记状态与抓图。
            # portclick <json> [png前缀]
            _out = parts[1]
            _pre = parts[2] if len(parts) >= 3 else ''
            _rec = {'ok': False}

            def _dump():
                try:
                    with open(_out, 'w', encoding='utf-8') as _f:
                        json.dump(_rec, _f, ensure_ascii=False, indent=1)
                    _dbg_log('portclick %s' % json.dumps(_rec, ensure_ascii=False))
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:portclick-dump')

            def _run_port():
                try:
                    _k = sorted(port_cards.keys())[0]
                    _sw = port_cards[_k][5]
                    _st = port_cards[_k][6]
                    _rec.update({'port': _k, 'before': _sw.isChecked(),
                                 'sw_text_before': _sw.text()})
                    if _pre:
                        w.grab().save(_pre + '_before.png')
                    _t0 = time.time()
                    _sw.click()

                    def _step2():
                        _rec['after_1'] = _sw.isChecked()
                        _rec['row_1'] = _st.text()
                        _rec['ms_1'] = int((time.time() - _t0) * 1000)
                        if _pre:
                            w.grab().save(_pre + '_1.png')
                        _sw.click()

                        def _step3():
                            _rec['after_2'] = _sw.isChecked()
                            _rec['row_2'] = _st.text()
                            _rec['ms_2'] = int((time.time() - _t0) * 1000)
                            _rec['ok'] = True
                            if _pre:
                                w.grab().save(_pre + '_2.png')
                            _dump()
                        QTimer.singleShot(1200, _step3)
                    QTimer.singleShot(1200, _step2)
                except Exception as _e:
                    _rec.update({'ok': False, 'err': repr(_e)})
                    _dump()
            QTimer.singleShot(0, _run_port)
        elif parts[0] == "uiscan" and len(parts) >= 2:
            # R030 自检：全量枚举页面内的 QAbstractScrollArea（含不可见滚动条）
            _out = parts[1]
            _doc = {'pages': [], 'tabs': {}}
            for _i in range(stack.count()):
                _pg = stack.widget(_i)
                _row = {'index': _i, 'name': getattr(_pg, 'objectName', lambda: '')() or type(_pg).__name__,
                        'size': [_pg.width(), _pg.height()], 'areas': []}
                for _sa in _pg.findChildren(QAbstractScrollArea):
                    _d = {'cls': type(_sa).__name__, 'name': _sa.objectName(),
                          'vPolicy': int(_sa.verticalScrollBarPolicy()),
                          'hPolicy': int(_sa.horizontalScrollBarPolicy()),
                          'vVisible': _sa.verticalScrollBar().isVisible(),
                          'hVisible': _sa.horizontalScrollBar().isVisible(),
                          'vMax': _sa.verticalScrollBar().maximum(),
                          'hMax': _sa.horizontalScrollBar().maximum(),
                          'size': [_sa.width(), _sa.height()]}
                    _row['areas'].append(_d)
                _doc['pages'].append(_row)
            try:
                _doc['tabs'] = {'settingsPages': _set_tabs.count(),
                                'aiPages': _ai_tabs.count()}
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:uiscan_tabs')
            with open(_out, 'w', encoding='utf-8') as _f:
                json.dump(_doc, _f, ensure_ascii=False, indent=1)
            print('[uiscan] -> %s pages=%d' % (_out, len(_doc['pages'])), flush=True)
        elif parts[0] == "dumpui" and len(parts) >= 2:
            # R040 / C09 自检：全量导出控件树（类名/对象名/尺寸/位置/父链/滚动区）
            _out = parts[1]
            _rows = []

            def _own_page(_wd):
                _q = _wd.parentWidget()
                while _q is not None:
                    if _q.parentWidget() is stack:
                        return stack.indexOf(_q)
                    _q = _q.parentWidget()
                return -1

            for _wd in QApplication.allWidgets():
                try:
                    _vis = bool(_wd.isVisible())
                    _gp = _wd.mapToGlobal(_wd.rect().topLeft()) if _vis else None
                    _par = _wd.parent() if hasattr(_wd, 'parent') else None
                    _pg = _own_page(_wd)
                    _rows.append({
                        'cls': type(_wd).__name__,
                        'name': _wd.objectName(),
                        'text': (_wd.text()[:40] if hasattr(_wd, 'text') else ''),
                        'vis': _vis,
                        'size': [_wd.width(), _wd.height()],
                        'gp': [_gp.x(), _gp.y()] if _gp else None,
                        'parent': (type(_par).__name__ + '#' + (_par.objectName() or '')) if _par else '',
                        'scroll': isinstance(_wd, QAbstractScrollArea),
                        'page': _pg,
                        # R041 / C10：未导航页不做布局，几何无效；stale=True 不得用于结论
                        'stale': _pg not in (-1, stack.currentIndex()),
                    })
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:dumpui-row')
            _doc = {'ts': ck_time.fmt(), 'count': len(_rows),
                    'cur': stack.currentIndex(),
                    'curName': stack.widget(stack.currentIndex()).objectName(),
                    'stackCount': stack.count(),
                    'pages': [{'i': _i, 'cls': type(stack.widget(_i)).__name__,
                               'name': stack.widget(_i).objectName(),
                               'size': [stack.widget(_i).width(), stack.widget(_i).height()]}
                              for _i in range(stack.count())],
                    'scroll': [r for r in _rows if r['scroll']],
                    'pageCounts': [sum(1 for r in _rows if r['page'] == _i)
                                   for _i in range(stack.count())],
                    'widgets': _rows}
            with open(_out, 'w', encoding='utf-8') as _f:
                json.dump(_doc, _f, ensure_ascii=False, indent=1)
            _dbg_log('dumpui n=%d scroll=%d page=%d -> %s'
                     % (len(_rows), len(_doc['scroll']), stack.currentIndex(), _out))
            print('[dumpui] -> %s n=%d' % (_out, len(_rows)), flush=True)
        elif parts[0] == "pressdump" and len(parts) >= 2:
            # C34（缺陷 5）：按钮按下态取证件。对当前页每个可见 QPushButton
            # 分别以「抬起 / setDown(True)」抓一次并逐像素比对；diff=0 即该按钮
            # 按下无任何视觉反馈（导航/分段/AI 快捷件曾属此类）。
            _dir = parts[1]
            os.makedirs(_dir, exist_ok=True)
            if len(parts) >= 3:
                try:
                    _pi = max(0, min(stack.count() - 1, int(parts[2])))
                    rail.set_selected(_pi)
                    stack.setCurrentIndex(_pi)
                    QApplication.processEvents()
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:pressdump-page')
            _rows = []
            _btns = [b for b in QApplication.allWidgets()
                     if isinstance(b, QPushButton) and b.isVisible()
                     and b.width() > 8 and b.height() > 8]
            for _i, _b in enumerate(_btns):
                try:
                    _b.setDown(False)
                    QApplication.processEvents()
                    _up = _b.grab().toImage()
                    _b.setDown(True)
                    QApplication.processEvents()
                    _dn = _b.grab().toImage()
                    _b.setDown(False)
                    QApplication.processEvents()
                    _diff = 0
                    if _up.size() == _dn.size():
                        for _y in range(0, _up.height(), 2):
                            for _x in range(0, _up.width(), 2):
                                _c0 = _up.pixelColor(_x, _y)
                                _c1 = _dn.pixelColor(_x, _y)
                                if (_c0.red(), _c0.green(), _c0.blue()) != \
                                        (_c1.red(), _c1.green(), _c1.blue()):
                                    _diff += 1
                    _tagb = 'b%02d_%s' % (_i, _b.objectName() or 'btn')
                    _up.save(os.path.join(_dir, _tagb + '_up.png'))
                    _dn.save(os.path.join(_dir, _tagb + '_dn.png'))
                    _rows.append({'i': _i, 'name': _b.objectName(),
                                  'text': (_b.text()[:24] if hasattr(_b, 'text') else ''),
                                  'size': [_b.width(), _b.height()],
                                  'diff': _diff, 'tag': _tagb})
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:pressdump')
            with open(os.path.join(_dir, 'pressdiff.json'), 'w', encoding='utf-8') as _f:
                json.dump({'page': stack.currentIndex(), 'buttons': _rows},
                          _f, ensure_ascii=False, indent=1)
            print('[pressdump] -> %s n=%d zero=%d'
                  % (_dir, len(_rows), sum(1 for r in _rows if r['diff'] == 0)),
                  flush=True)
        elif parts[0] == "uistate" and len(parts) >= 2:
            # C34b：#5 冻结诊断——不开窗口、不抢前台即可读挂起态与控件真值
            _out = parts[1]
            try:
                _d = w._ck_uistate()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:uistate')
                _d = {'error': repr(_exc)}
            with open(_out, 'w', encoding='utf-8') as _f:
                json.dump(_d, _f, ensure_ascii=False, indent=1)
            print('[uistate] -> %s' % _out, flush=True)
        elif parts[0] == "probe" and len(parts) >= 2:
            # C34d：#1 刷新率取证——应用内按固定间隔采样控件真值，避免外部
            # 多次拉起进程带来的抖动。用法：probe <json> [间隔ms] [样本数]
            _out = parts[1]
            _iv = int(parts[2]) if len(parts) >= 3 else 100
            _n = int(parts[3]) if len(parts) >= 4 else 100
            _rows = []
            _t0 = time.monotonic()

            def _probe_one():
                try:
                    _rows.append({
                        't': round((time.monotonic() - _t0) * 1000.0, 1),
                        'ts': round(time.time(), 3),
                        'hero': str(hero_power.text()),
                        'chg': str(badge_chg.text()),
                        'pend': len(_CK_NUMQ['pending']),
                        'armed': bool(_CK_NUMQ['armed']),
                        'beat': bool(UiBeat.instance()._tmr.isActive()),
                        'sus': bool(_CK_SUS_ON['on'])})
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:probe')
                if len(_rows) < _n:
                    QTimer.singleShot(_iv, _probe_one)
                else:
                    with open(_out, 'w', encoding='utf-8') as _f:
                        json.dump(_rows, _f, ensure_ascii=False, indent=1)
                    print('[probe] -> %s n=%d' % (_out, len(_rows)), flush=True)

            QTimer.singleShot(0, _probe_one)
        elif parts[0] == "suspend" and len(parts) >= 2:
            # C34b：等价于「遮挡/最小化 → 挂起」与「恢复可见 → 唤醒」
            _on = str(parts[1]).lower() in ('on', '1', 'true')
            try:
                w._ck_set_sus(_on)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:suspend')
            print('[suspend] %s' % _on, flush=True)
        elif parts[0] == "suspin" and len(parts) >= 2:
            # C34b：钉住/解钉可见性判定（取证用，不改业务逻辑）
            _pin = str(parts[1]).lower() in ('on', '1', 'true')
            _CK_SUS_PIN['on'] = _pin
            if _pin:
                try:
                    w._ck_set_sus(False)
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:suspin')
            print('[suspin] %s' % _pin, flush=True)
        elif parts[0] == "geomrec" and len(parts) >= 2:
            # R031 自检：记录页纵向布局量化（表格/分页条/卡片几何，定位压盖）
            _out = parts[1]

            def _gi(_wd):
                _tl = _wd.mapTo(w, _wd.rect().topLeft())
                return {'cls': type(_wd).__name__,
                        'x': _tl.x(), 'y': _tl.y(),
                        'w': _wd.width(), 'h': _wd.height(),
                        'vis': _wd.isVisible(),
                        'min': [_wd.minimumWidth(), _wd.minimumHeight()],
                        'hint': [_wd.sizeHint().width(), _wd.sizeHint().height()]}

            _d = {'win': [w.width(), w.height()],
                  'card': _gi(g_rec), 'table': _gi(sess_tbl),
                  'prev': _gi(pg_prev), 'lbl': _gi(pg_lbl), 'next': _gi(pg_next),
                  'empty': _gi(rec_empty),
                  'rows': sess_tbl.rowCount(),
                  'viewport': [sess_tbl.viewport().width(), sess_tbl.viewport().height()],
                  'hdr_h': sess_tbl.horizontalHeader().height(),
                  'rowH': sess_tbl.rowHeight(0) if sess_tbl.rowCount() else 0,
                  'stack_geom': _gi(stack.widget(stack.currentIndex())) if stack.count() else None}
            with open(_out, 'w', encoding='utf-8') as _f:
                json.dump(_d, _f, ensure_ascii=False, indent=1)
            print('[geomrec] -> %s' % _out, flush=True)
        elif parts[0] == "recfilter" and len(parts) >= 6:
            # R032 自检：记录页筛选组合（范围/端口/排序）+ 页码，抓图并存几何
            _ri, _pi, _si, _pn = (int(parts[1]), int(parts[2]),
                                  int(parts[3]), int(parts[4]))
            _p = parts[5]
            rec_range.setCurrentIndex(max(0, _ri))
            rec_port.setCurrentIndex(max(0, _pi))
            rec_sort.setCurrentIndex(max(0, _si))
            rec_page['cur'] = max(1, _pn)
            QTimer.singleShot(700, poll_sessions)
            QTimer.singleShot(2300, lambda p=_p: _grab_to(p))
        elif parts[0] == "recpage" and len(parts) >= 3:
            # R032 自检：记录页翻到第 N 页并抓图（验证本地分页可达、无裁切）
            _pn = int(parts[1]); _p = parts[2]
            rec_page['cur'] = max(1, _pn)
            poll_sessions()
            QTimer.singleShot(1600, lambda p=_p: _grab_to(p))
        elif parts[0] == "page":
            # R035 自检：写入当前导航页索引（验证通知点击后的落点）
            _dbg_log('page=%d' % stack.currentIndex())
        elif parts[0] == "nav" and len(parts) >= 2:
            # R035 自检：切到指定导航页（配合通知点击跳转验证）
            _ni = max(0, min(stack.count() - 1, int(parts[1])))
            rail.set_selected(_ni)
            stack.setCurrentIndex(_ni)
            _dbg_log('nav=%d' % _ni)
        elif parts[0] == "quickpanel":
            # R037 自检：走与托盘左键完全相同的 Trigger 分支
            try:
                _on_tray_activated(QSystemTrayIcon.Trigger)
                _dbg_log('quickpanel popup')
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:quickpanel')
        elif parts[0] == "dlgshot" and len(parts) >= 3:
            # R030 自检：打开指定浮层→延时抓图→自动关闭（不动用户设置）
            # C16：可选第 4 参数输出几何 JSON（按钮宽高/外距），供逐浮层审计
            _nm, _p = parts[1], parts[2]
            _jp = parts[3] if len(parts) >= 4 else None

            def _find_and_shoot():
                _d = QApplication.activeModalWidget() or QApplication.activePopupWidget()
                if _d is None:
                    for _tw in QApplication.topLevelWidgets():
                        if _tw.isVisible() and (isinstance(_tw, QDialog) or isinstance(_tw, QMenu)):
                            _d = _tw
                            break
                if _d is None:
                    if _nm == 'tray' and not (os.path.exists(_p)
                                              and os.path.getsize(_p) > 1000):
                        # C16：托盘右键菜单受会话前台焦点限制常被系统立即收回，
                        # 兜底用离屏渲染取证（QSS 同源判定），弹出行为单独记 0。
                        tm.ensurePolished()
                        tm.adjustSize()
                        _ok = tm.grab().save(_p)
                        print('[dlgshot] tray offscreen saved=%s %dx%d'
                              % (_ok, tm.width(), tm.height()), flush=True)
                        if _jp:
                            with open(_jp, 'w', encoding='utf-8') as _f:
                                json.dump({'name': 'tray', 'title': '', 'cls': 'QMenu',
                                           'w': tm.width(), 'h': tm.height(),
                                           'margins': None, 'buttons': [],
                                           'mode': 'offscreen'}, _f, ensure_ascii=False)
                        return
                    print('[dlgshot] %s: no dialog' % _nm, flush=True)
                    return
                _ok = _d.grab().save(_p)
                print('[dlgshot] %s saved=%s %dx%d' % (_nm, _ok, _d.width(), _d.height()), flush=True)
                if _jp:
                    try:
                        _btns = []
                        for _b in _d.findChildren(QPushButton):
                            _tl = _b.mapTo(_d, _b.rect().topLeft())
                            _btns.append({'text': _b.text(), 'name': _b.objectName(),
                                          'x': _tl.x(), 'y': _tl.y(),
                                          'w': _b.width(), 'h': _b.height()})
                        _mg = None
                        try:
                            _m = _d.layout().contentsMargins()
                            _mg = [_m.left(), _m.top(), _m.right(), _m.bottom()]
                        except Exception:
                            pass
                        _jd = {'name': _nm, 'title': _d.windowTitle(),
                               'cls': type(_d).__name__,
                               'w': _d.width(), 'h': _d.height(),
                               'margins': _mg, 'buttons': _btns}
                        with open(_jp, 'w', encoding='utf-8') as _f:
                            json.dump(_jd, _f, ensure_ascii=False, indent=1)
                    except Exception as _exc:
                        ck_swallow(_exc, 'launcher:dlgshot_json')
                try:
                    if isinstance(_d, QDialog):
                        _d.reject()
                    else:
                        _d.close()
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:dlgshot_close')

            def _open_named():
                try:
                    if _nm == 'countdown':
                        open_countdown()
                    elif _nm == 'proto':
                        open_proto()
                    elif _nm == 'device':
                        _open_device_view()
                    elif _nm == 'report':
                        _gen_daily_report()
                    elif _nm == 'sess':
                        if sess_page_rows:
                            show_session_points(0)
                        else:
                            print('[dlgshot] sess: no rows', flush=True)
                    elif _nm == 'raw':
                        _AI['msgs'].append(
                            {'role': 'assistant', 'meta': 'C16 自检',
                             'text': '# 原始响应（自检样例）\n\n```json\n{"ok": true}\n```'})
                        _log.anchorClicked.emit(QUrl('r:%d' % (len(_AI['msgs']) - 1)))
                    elif _nm == 'confirm':
                        _confirm('重启服务', '确认重启充电守护服务？'
                                             '重启期间约 5-15 秒无法读取数据。')
                    elif _nm == 'info':
                        _ck_msg(w, '导出', '已保存：C:\\tmp\\C16_selftest.csv')
                    elif _nm == 'docs':
                        _open_docs(0)
                    elif _nm == 'tray':
                        w.showNormal()
                        w.raise_()
                        w.activateWindow()
                        tm.popup(QPoint(w.x() + 140, w.y() + 140))
                        QTimer.singleShot(300, _find_and_shoot)
                    elif _nm == 'close':
                        _cm = settings.value('closeMode', 'ask')

                        class _DummyClose(object):
                            def ignore(self):
                                pass

                            def accept(self):
                                pass

                        settings.setValue('closeMode', 'ask')
                        try:
                            on_close(_DummyClose())
                        finally:
                            settings.setValue('closeMode', _cm)
                    elif _nm == 'quit':
                        confirm_exit()
                    elif _nm == 'allon':
                        all_on.click()
                    elif _nm == 'alloff':
                        all_off.click()
                    elif _nm == 'newai':
                        _ai_new_sess()
                    elif _nm == 'renai':
                        _ai_ren_sess()
                    else:
                        print('[dlgshot] unknown %s' % _nm, flush=True)
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:dlgshot_open')

            QTimer.singleShot(1200, _find_and_shoot)
            QTimer.singleShot(100, _open_named)
        elif parts[0] == "quit":
            _CK_EXIT_HOW['v'] = 'cmd_quit'
            QApplication.instance().quit()
    except Exception as e:
        _dbg_log("dispatch err %r" % e)


def build_ui(port, api, get_bytes, headless_proc=None, single_server=None):
    _ck_boot_trace('bui_in')
    # R19：合并轮询需要 meta（响应体长度/服务端耗时头）；api_meta 挂在 api 上，
    # 旧调用方（只取 status/data 的）无需改动。
    api_meta = getattr(api, 'api_meta', None) or (
        lambda m, p, payload=None, timeout=8, extra_headers=None:
        (api(m, p, payload, timeout)[0], api(m, p, payload, timeout)[1], {'len': 0, 'srv_ms': None}))
    def time_plot():
        """透明背景、时间轴 PlotWidget；视口禁止白底。C34h：pg 延迟到真正建图时导入。"""
        pg = _ck_pg()

        class TimeAxisItem(pg.AxisItem):
            """epoch 秒 -> HH:MM:SS（窄间隔）/ HH:MM（宽间隔）。"""

            def tickStrings(self, values, scale, spacing):
                out = []
                for v in values:
                    try:
                        lt = time.localtime(float(v))
                        _k = ck_time.HM if spacing >= 60 else ck_time.HMS
                        out.append(ck_time.fmt(_k, lt))
                    except Exception:
                        out.append('')
                return out

        pl = pg.PlotWidget(axisItems={'bottom': TimeAxisItem(orientation='bottom')})
        pl.setBackground(None)
        pl.setAutoFillBackground(False)
        vp = pl.viewport()
        vp.setAutoFillBackground(False)
        vp.setBackgroundRole(vp.palette().ColorRole.NoRole)
        pal = vp.palette()
        pal.setColor(pal.ColorRole.Window, QColor(0, 0, 0, 0))
        pal.setColor(pal.ColorRole.Base, QColor(0, 0, 0, 0))
        vp.setPalette(pal)
        # R01（25 轮）：pyqtgraph 默认视窗 0–1，时间轴会显示 1970 的 08:00:00×N。
        # 起始就落在"近 5 分钟"，后端未就绪/空数据时也不会露出假时间戳。
        pl.setXRange(time.time() - 300, time.time(), padding=0)
        return pl

    def mini_plot(color):
        """迷你功率曲线（1.5.5）：透明、无坐标轴刻度、带淡色网格。

        曲线区由外层常驻（最小高 PORT_MINI_H），这里只负责画布本身：
        有数据画折线，无数据保留空网格 + 图区中央的淡色「无数据」。
        """
        pg = _ck_pg()
        pl = pg.PlotWidget()
        pl.setBackground(None)
        pl.setAutoFillBackground(False)
        pl.setMinimumHeight(44)
        # 宽度不参与 hint，避免有图/无图改变同列卡片宽度。
        pl.setMinimumWidth(0)
        pl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        pl.setMouseEnabled(False, False)
        pl.setMenuEnabled(False)
        pl.hideButtons()
        vp = pl.viewport()
        vp.setAutoFillBackground(False)
        vp.setBackgroundRole(vp.palette().ColorRole.NoRole)
        pal = vp.palette()
        pal.setColor(pal.ColorRole.Window, QColor(0, 0, 0, 0))
        pal.setColor(pal.ColorRole.Base, QColor(0, 0, 0, 0))
        vp.setPalette(pal)
        pl.setXRange(0, 30, padding=0)
        pl.setYRange(0, 20, padding=0)
        pl.disableAutoRange()
        # 空态也保留网格：pyqtgraph 的网格线复用坐标轴笔，轴笔设成 None 时
        # 网格会被一并抹掉（1.5.5 截图实证），因此这里给轴一条很淡的实笔。
        pl.showGrid(x=True, y=True, alpha=0.55)
        for _an in ('bottom', 'left'):
            _ax = pl.getAxis(_an)
            _ax.setStyle(showValues=False, tickLength=0)
            _ax.setPen(pg.mkPen(QColor(148, 163, 184, 58)))
        pl.getAxis('right').setPen(None)
        pl.getAxis('top').setPen(None)
        pl.hideAxis('right')
        pl.hideAxis('top')
        qc = QColor(color)
        curve = pl.plot([], [], pen=pg.mkPen(qc, width=2),
                        fillLevel=0,
                        brush=QBrush(QColor(qc.red(), qc.green(), qc.blue(), 38)))
        return pl, curve

    app = QApplication.instance()
    app.setStyle("Fusion")  # 保证 QHeaderView 等 QSS 正确生效（windows 样式下表头会变白）
    # 强制深色调色板——回退 round_015 亮色渲染，任何未显式着色的部件回落到 9:38 深色
    from PySide6.QtGui import QPalette as _QPal
    _dpal = app.palette()
    for _role, _col in (("Window", "#1c1c1c"), ("WindowText", "#e1e1e1"),
                        ("Base", "#19233a"), ("AlternateBase", "#1a2436"),
                        ("Text", "#e1e1e1"), ("Button", "#19233a"),
                        ("ButtonText", "#e1e1e1"), ("ToolTipBase", "#1a2436"),
                        ("ToolTipText", "#e1e1e1"), ("PlaceholderText", "#8b9db8")):
        _dpal.setColor(getattr(_QPal, _role), QColor(_col))
    _dpal.setColor(_QPal.Highlight, QColor(3, 169, 244))
    _dpal.setColor(_QPal.HighlightedText, QColor("#ffffff"))
    app.setPalette(_dpal)
    # N3：先加载打包的 Noto Sans SC（换机不回退），再设置族名
    load_bundled_font()
    _ck_boot_trace('font_done')
    # R007（D-02）：显式应用字体。实测本机「Microsoft YaHei UI」已被替换为手写体
    # 字形（族名仍在但字形非雅黑），故首选 Noto Sans SC，缺失时回退雅黑/黑体。
    _appfont = QFont()
    _appfont.setFamilies(['Noto Sans SC', 'Microsoft YaHei UI', 'Microsoft YaHei', 'SimHei'])
    _appfont.setPointSize(10)
    app.setFont(_appfont)
    # R007（CK-23）：运行时生成勾选图标，QSS 引用（构建期无 QApplication 不能建 QPixmap）
    _check_png = os.path.join(tempfile.gettempdir(), 'ck_check.png')
    try:
        _pm = QPixmap(16, 16)
        _pm.fill(Qt.transparent)
        _p = QPainter(_pm)
        _p.setRenderHint(QPainter.Antialiasing)
        _pen = QPen(QColor('white'), 2.2)
        _pen.setCapStyle(Qt.RoundCap)
        _pen.setJoinStyle(Qt.RoundJoin)
        _p.setPen(_pen)
        _p.drawPolyline([QPointF(3.5, 8.5), QPointF(6.8, 12.0), QPointF(12.5, 4.5)])
        _p.end()
        _pm.save(_check_png, 'PNG')
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:2975')
    _check_qss = ''
    if os.path.exists(_check_png):
        _check_qss = ("QCheckBox::indicator:checked { background:#38e2ff; border-color:#38e2ff;"
                      " image:url('%s'); }" % _check_png.replace('\\', '/'))
    app.setStyleSheet(LIGHT_QSS + _check_qss)
    w = QMainWindow()
    # R023：520×360 下三列 hero 与设置页会压盖/文字纵切（R022 测试员 P1）；
    # 最小尺寸提升到 WIN_MIN_W×WIN_MIN_H：固定卡两列（748px）与记录页 7 列都放得下。
    w.setMinimumSize(WIN_MIN_W, WIN_MIN_H)
    back = Backdrop()
    w.setCentralWidget(back)
    w.setWindowTitle(PANEL_TITLE)
    # R024-A4（领导）：窗口/任务栏图标=自绘充电主题 ico；R47：路径统一解析
    _ck_win_icon = QIcon(ck_icon_path())
    if not _ck_win_icon.isNull():
        w.setWindowIcon(_ck_win_icon)
    # 窗口尺寸基准见模块顶部 WIN_W/WIN_H（默认不小于最小窗口）
    w.resize(WIN_W, WIN_H)
    # R025：事件过滤器登记表——退出/销毁时统一摘除，避免 Qt 继续向已销毁的 Python
    # 过滤器投递事件（实测崩溃点：_HeroFit.eventFilter 访问越界 0xC0000005）
    w._ef_list = []

    def _inst_ef(obj, flt):
        try:
            obj.installEventFilter(flt)
            w._ef_list.append((obj, flt))
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:3006')

    def _drop_ef(*_a):
        for _o, _f in list(getattr(w, '_ef_list', [])):
            # destroyed 之后对象已在 C++ 侧析构，removeEventFilter 必抛
            # shiboken RuntimeError；这不是异常，验活后跳过即可。
            try:
                from shiboken6 import isValid as _valid
                if not _valid(_o):
                    continue
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:ef-valid')
            try:
                _o.removeEventFilter(_f)
            except RuntimeError as _exc:
                ck_trace('ef_drop_skip', err=type(_exc).__name__)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3013')
        try:
            w._ef_list.clear()
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:3017')
    try:
        QApplication.instance().aboutToQuit.connect(_drop_ef)
        w.destroyed.connect(_drop_ef)
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:3022')
    # 仅恢复窗口位置，不恢复旧尺寸（保持两应用窗口大小一致）
    _geo = QSettings('DoubaoWork', 'CuktechPanel')
    _restored_geo = False
    if _geo.value('geometry'):
        try:
            w.restoreGeometry(_geo.value('geometry'))
            w.resize(WIN_W, WIN_H)
            _avail = w.screen().availableGeometry()
            if w.x() < 0 or w.y() < 0 or w.x() > _avail.width():
                _restored_geo = False
            else:
                _restored_geo = True
        except Exception:
            _restored_geo = False
    w._default_maximized = False
    if not _restored_geo:
        # 无记忆时把基准窗口居中显示
        _screen = app.primaryScreen().availableGeometry()
        w.move(int(_screen.center().x() - WIN_W / 2),
               int(_screen.center().y() - WIN_H / 2))
    outer = QVBoxLayout(back)
    outer.setContentsMargins(12, 12, 12, 12)
    # 品牌已在左侧导航栏，不再重复顶部标题
    toast = ToastManager(w)
    w._toast = toast

    # 主体：左侧导航栏（参考荣耀电脑管家）+ 右侧内容堆栈
    body = QHBoxLayout()
    body.setSpacing(12)
    stack = QStackedWidget()
    # C34h：早亮窗时首屏先给「界面加载中…」占位，避免右侧内容区空白；状态页就绪即移除
    _boot_ph = QLabel('界面加载中…')
    _boot_ph.setAlignment(Qt.AlignCenter)
    _boot_ph.setStyleSheet('color:#8fa6bd; font-size:13px; background:transparent;')
    stack.addWidget(_boot_ph)
    # C34-UI：一级导航收 5 项，第三列显式给页号——AI 入口沉底后「设置」仍是第 6 页
    NAV_ITEMS = [
        ('⌂', '状态', 0),
        ('⏻', '端口控制', 1),
        ('≡', '充电记录', 2),
        ('⟳', '自动化', 3),
        ('⚙', '设置', 5),
    ]
    # R026 的「✦ 小源」从一级栏中段迁到导航底部（辅助型入口不占主导航），
    # 页号保持 4，QStackedWidget 与 Ctrl+1..6 映射不变。
    NAV_BOTTOM = [('✦', '小源', 4)]
    NAV_TIPS = ['', '', '', '', '']
    NAV_BOTTOM_TIPS = ['对话与数据问答']

    _entered = set()
    _task_holders = []

    _inflight = set()  # R15-A01：按 key 去重——轮询重入时丢弃旧任务，不积压线程池
    # R25：live 回包的 UI 线程占用（只统计 done 回调，不含网络 I/O）
    _live_ui = {'n': 0, 's': 0.0, 'max': 0.0}
    w._ck_live_ui = _live_ui

    def qwait(fn, done, key=None):
        """后台线程取数（走共享 daemon 池），信号回主线程执行 done，避免阻塞 UI。"""
        if key is not None:
            if key in _inflight:
                return
            _inflight.add(key)
        holder = _TaskSignal()

        def _deliver(r):
            if key is not None:
                try:
                    _inflight.discard(key)
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:3093')
            _t_done = time.thread_time()
            try:
                done(r)
            except Exception:
                ck_trace('done_err', key=key, tb=traceback.format_exc())
                raise
            finally:
                if key == 'live':
                    _dt_done = time.thread_time() - _t_done
                    _live_ui['s'] += _dt_done
                    _live_ui['n'] += 1
                    if _dt_done > _live_ui['max']:
                        _live_ui['max'] = _dt_done
            QTimer.singleShot(0, lambda: _release_holder(holder))

        holder.result.connect(_deliver, Qt.QueuedConnection)
        _task_holders.append(holder)

        def run():
            try:
                r = fn()
            except Exception as e:
                ck_trace('job_err', key=key, err=str(e), tb=traceback.format_exc())
                r = ('ERR', str(e))
            holder.result.emit(r)
        WORKER_POOL.submit(run)

    def _release_holder(holder):
        try:
            if holder in _task_holders:
                _task_holders.remove(holder)
            holder.deleteLater()
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:3119')

    def enter_page(idx):
        """首次进入错落淡入该页卡片（cardIn），结束后挂回阴影；
        重复进入不再对整页做透明度特效（避免 FBO 切页泄漏）。"""
        if idx in _entered:
            return
        page = stack.widget(idx)
        if page is None:
            return
        _entered.add(idx)
        card_list = sorted(set(c for c in page.findChildren(QGroupBox)
                               if isinstance(c, GlassCard)),
                           key=lambda c: (c.pos().y(), c.pos().x()))
        for i, card in enumerate(card_list):
            fade_in(card, duration=460, delay=70 * i, restore_shadow=True)

    def page_transition(idx):
        """切页时整页快速淡入，避免硬切。"""
        page = stack.widget(idx)
        if page is None:
            return
        old = getattr(page, '_pt_anim', None)
        if old is not None:
            try:
                old.stop()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3146')
        eff = QGraphicsOpacityEffect(page)
        eff.setOpacity(0.0)
        page.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b'opacity', page)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.OutCubic)

        def cleanup():
            if page.graphicsEffect() is eff:
                page.setGraphicsEffect(None)
        anim.finished.connect(cleanup)
        anim.start()
        page._pt_anim = anim

    def on_select(idx):
        stack.setCurrentIndex(idx)
        # 2.1.0：从侧栏离开设备分组页时走同一套收尾，避免二级页残留在栈里
        for _i in range(stack.count() - 1, -1, -1):
            _pw = stack.widget(_i)
            if _pw is not None and _pw.objectName() == 'pageDeviceGroups':
                _sc = getattr(_pw, 'closed', None)
                if _sc is not None:
                    try:
                        _sc.emit()
                    except Exception as _exc:
                        ck_swallow(_exc, 'launcher:c40devleave')
        rail.set_selected(idx)
        enter_page(idx)
        if idx in _entered:
            page_transition(idx)
        # 切页立即刷新一次（定时器按页启停后，隐藏页不再持续轮询）
        _rc = getattr(w, '_refresh_current', None)
        if _rc is not None:
            try:
                _rc()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3175')
        # R028：隐藏页首次渲染时视口高度为 0，进入 AI 页后再滚到底，折叠链接可见
        try:
            _ab = getattr(w, '_ai_to_bottom', None)
            if _ab is not None and idx == stack.indexOf(ai_page) and _ai_tabs.currentIndex() == 0:
                for _d in (30, 160, 400, 800):
                    QTimer.singleShot(_d, _ab)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:3183')

    rail = NavRail(PANEL_TITLE, '星源智控 · 原生客户端', NAV_ITEMS, on_select,
                   tips=NAV_TIPS, bottom_items=NAV_BOTTOM, bottom_tips=NAV_BOTTOM_TIPS)
    # 品牌口徑：自稱統一為「星源智控」
    rail._brand_lbl.setTextFormat(Qt.RichText)
    rail._brand_lbl.setText(
        '<span style="font-size:16px; font-weight:900; color:#ffffff;">星源智控</span>'
        '<span style="font-size:16px; color:#5fd0ff;">&#9889;</span>')
    rail.set_bottom_widget(make_brand_footer(PANEL_TITLE, ux.APP_VERSION))
    body.addWidget(rail)
    body.addWidget(stack, 1)
    outer.addLayout(body, 1)

    # 启动级修复：_dbg_log 原先只在下面自检通道分支内定义，而页面级/托盘槽
    # （_refresh_records、poll_sessions、_ck_overlay_apply 等）在该条件不成立时
    # 仍会引用它 → build_ui 直接抛 NameError，面板起不来。改为无条件绑定模块级
    # 同口径写手 _ck_dbg（同一日志文件、同一格式），条件分支不再重复定义。
    _dbg_log = _ck_dbg

    # 自检命令通道（show/shot/quit），由外部本地 socket 驱动，让应用自己切页截图
    if single_server is not None and getattr(single_server, "cmd_holder", None):
        def _grab_to(path, want=None):
            try:
                pm = w.grab()
                # C33：抓图时记录真实页号——用于识别「抓图落在别页」的竞态（AI 页首帧取证）
                _dbg_log("grab size=%s save=%s path=%s actual=%s want=%s" %
                         (pm.size(), pm.save(path), path,
                          stack.currentIndex(), want))
            except Exception as e:
                _dbg_log("grab err %r" % e)

        def _seq_shot(_dir, idx, ms):
            # C33：切页后首帧序列（60/120/200/300/480/700/900ms）——首帧时间分布取证
            try:
                p = os.path.join(_dir, 'seq_%d_%03d.png' % (idx, ms))
                w.grab().save(p)
                _dbg_log('seqshot idx=%s ms=%s actual=%s path=%s' %
                         (idx, ms, stack.currentIndex(), p))
            except Exception as _e:
                _dbg_log('seqshot err %r' % (_e,))

        def _dispatch(msg):
            return _ck_ui_dispatch(msg,
                                   _AI=_AI,
                                   _ai_anchor=_ai_anchor,
                                   _ai_export_to=_ai_export_to,
                                   _ai_new_sess=_ai_new_sess,
                                   _ai_ren_sess=_ai_ren_sess,
                                   _ai_send=_ai_send,
                                   _ai_tabs=_ai_tabs,
                                   _ai_test=_ai_test,
                                   _confirm=_confirm,
                                   _dbg_log=_dbg_log,
                                   _fit_kpis=_fit_kpis,
                                   _gen_daily_report=_gen_daily_report,
                                   _grab_to=_grab_to,
                                   _inp=_inp,
                                   _log=_log,
                                   _mute=_mute,
                                   _on_tray_activated=_on_tray_activated,
                                   _open_adv_cfg=_open_adv_cfg,
                                   _open_device_view=_open_device_view,
                                   _open_docs=_open_docs,
                                   _seq_shot=_seq_shot,
                                   _set_tabs=_set_tabs,
                                   _set_unread=_set_unread,
                                   _testb=_testb,
                                   _tray_base_icon=_tray_base_icon,
                                   _unread=_unread,
                                   adv_panel=adv_panel,
                                   ai_page=ai_page,
                                   all_off=all_off,
                                   all_on=all_on,
                                   badge_chg=badge_chg,
                                   charger_dev=charger_dev,
                                   confirm_exit=confirm_exit,
                                   g_rec=g_rec,
                                   hero_power=hero_power,
                                   kpi_wrap=kpi_wrap,
                                   mid_w=mid_w,
                                   on_close=on_close,
                                   on_select=on_select,
                                   open_countdown=open_countdown,
                                   open_proto=open_proto,
                                   pg_lbl=pg_lbl,
                                   pg_next=pg_next,
                                   pg_prev=pg_prev,
                                   poll_rec_stats=poll_rec_stats,
                                   poll_sessions=poll_sessions,
                                   poll_status=poll_status,
                                   port_cards=port_cards,
                                   qa_mute=qa_mute,
                                   rail=rail,
                                   rc=rc,
                                   rec_empty=rec_empty,
                                   rec_page=rec_page,
                                   rec_port=rec_port,
                                   rec_range=rec_range,
                                   rec_sort=rec_sort,
                                   render_schedules=render_schedules,
                                   se=se,
                                   sess_ids=sess_ids,
                                   sess_page_rows=sess_page_rows,
                                   sess_tbl=sess_tbl,
                                   settings=settings,
                                   show_session_points=show_session_points,
                                   stack=stack,
                                   tm=tm,
                                   tray=tray,
                                   w=w)
        single_server.cmd_holder["dispatch"] = _dispatch

    # 窄窗口自动折叠左侧导航为图标栏（小窗口适配）
    class _RailAutoCollapse(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Resize:
                rail.set_collapsed(ev.size().width() < 860)  # R15-A04：折叠阈值统一 860
            return False
    _rac = _RailAutoCollapse(w)
    _inst_ef(w, _rac)

    w._rac = _rac


    # ===== Tab 1: 状态（两栏布局，无滚动；功率曲线最大化） =====
    st = StarField()
    st.setObjectName('pageStatus')   # R041 / C10：六页可定位
    sv = QVBoxLayout(st)
    # R030：状态页外边距与其余五页统一（8/12/8/8）
    sv.setContentsMargins(8, 12, 8, 8)
    sv.setSpacing(12)

    # ============ 顶部主卡：设备图 + 大总功率 + 0-120W 刻度 + 4 KPI（横排） ============
    # R009 布局C：连接状态改为主卡底部一条横带（不再与设备挤在一列、消除重叠），
    # 下方整块留给功率曲线。
    g_hero = GlassCard('')
    g_hero.setObjectName('hero')
    g_hero.setStyleSheet(
        'QGroupBox#hero{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,'
        'stop:0 rgba(255,255,255,0.088), stop:0.10 rgba(255,255,255,0.046),'
        'stop:1 rgba(255,255,255,0.014));'
        'border:1px solid rgba(255,255,255,0.13); border-top:1px solid rgba(255,255,255,0.30);'
        'border-radius:16px; margin-top:0; padding:8px 12px; font-weight:600;}')
    hero_outer = QVBoxLayout(g_hero)
    hero_outer.setContentsMargins(12, 8, 12, 8)
    hero_outer.setSpacing(8)
    # 设备图：领导要求移回 Hero 左列（三列布局），完整移植设备状态UI
    charger_dev = ChargerDevice()
    # R026（领导）：设备列按 8pt 栅格收窄并右对齐，消除设备图与数值间的过宽留白
    charger_dev.setFixedWidth(104)
    charger_dev.setMinimumHeight(150)

    # 左：大总功率 + LIVE + 刻度条（弹性：窄窗可压缩到 150）
    # R23：非 legacy 模式把这一列做成不透明承接层，数字+辉光由它自绘，
    # 脏区止于本面板（祖先链 0 陪站），且位图比控件大一圈可完整画出裙边。
    mid_w = QWidget() if _GLOW_LEGACY else _OpaquePanel()
    mid_w.setMinimumWidth(186)
    mid_w.setMaximumWidth(400)
    mid_w.setMinimumHeight(192)
    midv = QVBoxLayout(mid_w)
    midv.setContentsMargins(0, 0, 0, 0)
    midv.setSpacing(8)
    num_row = QHBoxLayout()
    num_row.setSpacing(8)
    _ck_boot_trace('pre_hero')
    hero_power = AnimatedNumberLabel('—')
    hero_power.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
    hero_power.set_formatter(lambda v: '%.1f' % v)
    # R020（领导）：照搬网页 hero——84px 级大数字 + 青→蓝→紫渐变 + 同色辉光
    hero_power.setStyleSheet('font-size:84px; font-weight:800; color:#38e2ff; background:transparent;')
    hero_power.setMinimumHeight(86)
    hero_power.set_gradient('#03a9f4', '#7c4dff')
    # C29（领导：先修首屏前三秒第一眼能看到的东西）：无设备时 '—' 在 84px 下
    # 是一条青→紫粗横杠，第一眼会被读成“进度条/色块”而不是“无数据”。
    # 空值态按 55% 缩字号并转静默灰（只改样式，不动数值语义，也不猜 0）。
    hero_power.set_empty_style(0.55)
    hero_power.set_probe_tag('hero')
    # R22：辉光移到 AnimatedNumberLabel 的不透明子层（_GlowLayer）内，父标签抓底自绘，
    # 数字每帧只重画自身矩形，GlassCard/祖先链 paint 增量归 0。
    if _GLOW_LEGACY:
        _hero_glow = QGraphicsDropShadowEffect(hero_power)
        _hero_glow.setBlurRadius(40)
        _hero_glow.setColor(QColor(3, 169, 244, 150))
        _hero_glow.setOffset(0, 0)
        hero_power.setGraphicsEffect(_hero_glow)
        unit_w = QLabel('W')
        unit_w.setStyleSheet('font-size:26px; font-weight:700; color:#38e2ff; background:transparent;')
        _unit_glow = QGraphicsDropShadowEffect(unit_w)
        _unit_glow.setBlurRadius(22)
        _unit_glow.setColor(QColor(90, 160, 255, 180))
        _unit_glow.setOffset(0, 0)
        unit_w.setGraphicsEffect(_unit_glow)
    else:
        # R23：alpha 必须与 legacy 特效一致（QColor(90,160,255,180)），否则 W 辉光偏强
        unit_w = GlowLabel('W', color=TEAL, glow=(90, 160, 255, 180))
        unit_w.setStyleSheet('font-size:26px; font-weight:700; background:transparent;')
        # R23：两块文字都交给 mid_w 面板自绘；控件只保留布局占位与字号
        hero_power._panel_draw = True
        unit_w._panel_draw = True
        mid_w._paint_extra = _HeroGlowPainter(mid_w, hero_power, unit_w).draw
    unit_w.setAlignment(Qt.AlignBottom)
    # C29：空值态把 W 单位一起转灰。数字缩到 46px 后，26px 的青色 W 反倒成了
    # 首屏最亮的东西，读起来像“有读数”；空值时应整体静默（灰字 + 弱辉光）。
    def _c29_unit_dim(_empty):
        if not hasattr(unit_w, '_color'):
            return   # legacy 分支走图形特效，不参与本项
        unit_w._color = QColor('#7d93ab' if _empty else TEAL)
        unit_w._glow = (QColor(125, 147, 171, 110) if _empty
                        else QColor(90, 160, 255, 180))
        mid_w.update()

    hero_power._empty_cb = _c29_unit_dim
    _c29_unit_dim(hero_power.text() == '—')
    # 回退 R15-C15 嵌套框（QWidget{} 选择器把边框套到了子控件上）
    # 恢复 9:38 简洁胶囊徽章（单 QLabel）；闲置灰改走 live_badge 样式切换
    # R024（领导）：LIVE 徽标重绘——网页 .hero-live-tag 口径（10px/字距2px/胶囊/脉冲圆点）
    live_badge = LiveTag()
    num_row.addWidget(hero_power)
    num_row.addWidget(unit_w, 0, Qt.AlignBottom)
    unit_w.setContentsMargins(0, 0, 0, 12)
    num_row.addStretch(1)
    midv.addLayout(num_row)
    # R025（领导）：LIVE 移到数值正下方（数值 → 状态 → 标题 → 刻度条的纵向层次）
    live_row = QHBoxLayout()
    # R026（领导）：LIVE 与数字底沿再拉开 8px（基线贴底后 2+8=10px 净空）
    live_row.setContentsMargins(4, 4, 0, 0)
    live_row.setSpacing(0)
    live_row.addWidget(live_badge, 0, Qt.AlignLeft | Qt.AlignTop)
    live_row.addStretch(1)
    midv.addLayout(live_row)
    cap_row = QHBoxLayout()
    cap_row.setSpacing(8)
    cap_lbl = QLabel('总功率 (W)')
    cap_lbl.setStyleSheet('color:' + TEXT_SUB + '; font-size:11px;')
    cap_row.addWidget(cap_lbl)
    cap_row.addStretch(1)
    midv.addLayout(cap_row)
    # 刻度条回中列（9:38 基线）
    power_scale = PowerScaleBar(RATED_POWER)
    midv.addWidget(power_scale)
    # R15-C14 ③：刻度条移出中列，作 hero 通栏第二行（对齐网页 hero-grid 底部通栏刻度）
    midv.addStretch(1)

    # R022：900×700 等窄窗口下 84px 数字被 mid_w 裁切（R021 测试员 5/5 复现）。
    # 按 mid_w 可用宽度分档缩放字号与 W/LIVE 底距；宽窗仍保持网页 84px 基准。
    class _HeroFit(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() != QEvent.Resize:
                return False
            _w = ev.size().width()
            # R023：84px 档阈值 300→296（1000×731 实测中栏 299，差 1px 会掉到 70px 档）
            _px = 84 if _w >= 296 else (70 if _w >= 248 else 58)
            if getattr(self, '_px', None) == _px:
                return False
            self._px = _px
            _b = int(round(_px * 0.167))  # 84→14、70→12、58→10，等比缩放底距
            hero_power.setStyleSheet(
                'font-size:%dpx; font-weight:800; color:#38e2ff;'
                ' background:transparent;' % _px)
            # R026：基线贴底后按字号收紧高度（去掉 descent 死区）
            hero_power.setMinimumHeight(int(_px * 0.98) + 4)
            # W 与数字基线对齐（不再靠旧 descent 补偿）
            unit_w.setContentsMargins(0, 0, 0, 0)
            return False

    _hf = _HeroFit(mid_w)
    mid_w.installEventFilter(_hf)
    w._ef_list.append((mid_w, _hf))
    mid_w._hf = _hf

    # 右：4 KPI（横排 1 行，布局C：窄窗每格等比缩小，不再 2×2）
    kpi_labels = {}
    kpi_cells = []
    kpi_specs = (('vmax', '最高电压 (V)', lambda v: '%.1f' % v),
                 ('cur', '实时电流 (A)', lambda v: '%.2f' % v),
                 ('active', '活跃端口 (路)', lambda v: '%d' % int(v)),
                 ('load', '负载率 (%)', lambda v: '%.0f' % v))
    for key, cap, fmt in kpi_specs:
        gk = KpiCell(key)
        # R15-C22：kpicell 顶部 qradialgradient 高光（网页 kpi-card 口径）
        gk.setStyleSheet(
            'QFrame#kpicell{background:qradialgradient(cx:0.5,cy:0.0,radius:1.4,'
            'stop:0 rgba(255,255,255,0.16), stop:0.45 rgba(255,255,255,0.05),'
            'stop:1 rgba(255,255,255,0.02));'
            'border:1px solid rgba(255,255,255,0.12); border-top:1px solid rgba(255,255,255,0.16);'
            'border-radius:12px;}')
        gk.setFixedHeight(78)
        # R023-B1：横向可压缩（Ignored）——仅在窗口被压缩时收缩，1000×731 基准下外观不变
        gk.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        kv = QVBoxLayout(gk)
        # 1.5.5：右上角 14×14 图标为绝对定位（不占布局），内容右留 24 让出图标区，
        # 避免数值控件与图标矩形相交（各窗口宽度下恒成立，不写死坐标）。
        kv.setContentsMargins(12, 8, 24, 8)
        kv.setSpacing(0)
        v = AnimatedNumberLabel('—')
        v.setAlignment(Qt.AlignLeft)
        v.set_formatter(fmt)
        v.setStyleSheet('font-size:26px; font-weight:800; color:#38e2ff;')  # C20-B1：与记录页 KPI 同族
        # C30（领导：首屏前三秒）：4 个 KPI 的 '—' 在 26px 下是四条白色横杠，
        # 且自绘路径读默认白 → 像加载骨架。统一到 C29 的静默灰空值语言（19px 灰）。
        v.set_probe_tag('kpi_%s' % key)
        v.set_empty_style(0.72)
        cap_k = QLabel(cap)
        cap_k.setStyleSheet('color:' + TEXT_SUB + '; font-size:11px; letter-spacing:0.8px;')
        kv.addWidget(v)
        kv.addWidget(cap_k)
        kpi_cells.append(gk)
        kpi_labels[key] = v
    # 主卡：三列（设备图 + 大总功率 + KPI）；R020：行高 150→168，对齐网页 hero 比例
    hero_grid = QWidget()
    hero_grid.setFixedHeight(192)
    hg = QHBoxLayout(hero_grid)
    hg.setContentsMargins(0, 0, 0, 0)
    hg.setSpacing(8)
    hg.addWidget(charger_dev, 0, Qt.AlignVCenter | Qt.AlignRight)
    hg.addWidget(mid_w, 1, Qt.AlignTop)
    kpi_grid = QGridLayout()
    kpi_grid.setSpacing(8)
    kpi_grid.setContentsMargins(0, 0, 0, 0)
    for i, c in enumerate(kpi_cells):
        kpi_grid.addWidget(c, i // 2, i % 2)
        kpi_grid.setRowStretch(i // 2, 0)
    kpi_wrap = QWidget()
    kpi_wrap.setLayout(kpi_grid)
    kpi_wrap.setMinimumWidth(300)
    kpi_wrap.setMaximumWidth(336)
    kpi_wrap.setFixedHeight(168)
    hg.addWidget(kpi_wrap, 1, Qt.AlignVCenter)

    class _HeroNarrow(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() != QEvent.Resize:
                return False
            # R024（综合审查员 P0-1）：900×700 下 100+300+236+24=660 溢出 42px。
            # 改为三档：≥1040 宽档；960–1040 中档；<960 窄档（进一步压缩，禁止溢出）。
            _w = ev.size().width()
            tier = 0 if _w >= 1040 else (1 if _w >= 960 else 2)
            if getattr(self, '_tier', None) == tier:
                return False
            self._tier = tier
            if tier == 2:
                charger_dev.setFixedWidth(88)
                kpi_wrap.setMinimumWidth(196)
                kpi_wrap.setMaximumWidth(204)
                mid_w.setMinimumWidth(180)
                mid_w.setMaximumWidth(320)
            elif tier == 1:
                charger_dev.setFixedWidth(100)
                kpi_wrap.setMinimumWidth(232)
                kpi_wrap.setMaximumWidth(240)
                mid_w.setMinimumWidth(260)
                mid_w.setMaximumWidth(330)
            else:
                charger_dev.setFixedWidth(104)
                kpi_wrap.setMinimumWidth(300)
                kpi_wrap.setMaximumWidth(336)
                mid_w.setMinimumWidth(186)
                mid_w.setMaximumWidth(400)
            return False

    _hn = _HeroNarrow(w)
    _inst_ef(w, _hn)
    w._hn = _hn
    hero_outer.addWidget(hero_grid)
    sv.addWidget(g_hero)

    # ============ 连接状态：主卡底部一条横带（布局C，不与设备挤、不重叠） ============
    conn_sep = QFrame()
    conn_sep.setFrameShape(QFrame.HLine)
    conn_sep.setStyleSheet('background:rgba(255,255,255,0.10); max-height:1px; border:none;')
    hero_outer.addWidget(conn_sep)
    conn_strip = QHBoxLayout()
    conn_strip.setSpacing(8)
    conn_spinner = SpinnerWidget(WARN, 14)
    lbl_conn = QLabel('正在连接…')
    lbl_conn.setStyleSheet('font-size:13px; font-weight:700; color:' + TEXT + ';')
    badge_ble = QLabel('—')
    badge_ble.setStyleSheet('font-size:12px;')
    # C34（缺陷 1）：充电侧独立徽章——「充电器在位 / 充电中 / 未知」与 BLE 连接分开表达。
    badge_chg = QLabel('—')
    badge_chg.setStyleSheet('font-size:12px;')
    # R01（25 轮）：短时轮询失败（1–2 次）只涨计数不提示的旧口径下，丢包场景会继续
    # 展示上一拍旧值且无任何降级标识；补一枚"数据滞后"琥珀提示，恢复即消失。
    lag_lbl = QLabel('')
    lag_lbl.setStyleSheet('font-size:11px; font-weight:700; color:#f59e0b;')
    lag_lbl.hide()
    # R02（25 轮）：滞后提示由「仅状态页」扩为全局同源——端口页在牌桌底部显示同一文案；
    # 列表由 hero 先行建立，端口页构建后回填。
    lag_extra = []
    # 1.5.9：USB-A 小电流并入「设备设置」，开关用同款分段器（关/开）
    trickle_sel = SegmentedSelector([(0, '关'), (1, '开')])
    trickle_sel.setToolTip('USB-A 端口小电流输出，适合蓝牙耳机、手环等小功率设备。')
    # R32：按钮统一为同一组件族 primary / ghost（同高 32、同圆角、同字号）
    conn_quick_b = aligned_button('连接设备', 'primary', min_w=104, h=32)
    restart_b = aligned_button('重启服务', 'ghost', min_w=96, h=32)
    # C34-UI：hero 横带的「小源」已移除——全应用只保留左下角导航底部一个 AI 入口
    conn_strip.addWidget(conn_spinner)
    conn_strip.addWidget(lbl_conn)
    conn_strip.addWidget(badge_ble)
    # 2.1.0：充电器状态不再挂在云连接行；移到「设置 → 连接设置 → 蓝牙状态」设备信息区
    conn_strip.addWidget(lag_lbl)
    conn_strip.addStretch(1)
    conn_strip.addWidget(conn_quick_b)
    conn_strip.addWidget(restart_b)
    hero_outer.addLayout(conn_strip)

    def toggle_trickle(val):
        val = 1 if int(val) else 0
        def _post():
            try:
                api('POST', '/api/set', {'piid': 15, 'value': val}, timeout=CK_T_CMD)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:3758')
        # R007 CK-02：此前误用 _DaemonPool.submit（类调用）→ TypeError 被吞，
        # 小电流从未真正下发；WORKER_POOL 才是本进程实例。
        WORKER_POOL.submit(_post)
    trickle_sel.chosen.connect(toggle_trickle)

    # 曲线卡：时间窗口 + 图例 + 大图（充满）
    g_chart = GlassCard('实时功率曲线')
    chartv = QVBoxLayout(g_chart)
    chartv.setContentsMargins(12, 12, 12, 12)
    chartv.setSpacing(8)
    range_row = QHBoxLayout()
    range_row.setSpacing(8)
    chart_range = QComboBox()
    chart_range.addItems(['近 1 分钟', '近 5 分钟', '近 10 分钟', '近 30 分钟', '近 1 小时'])
    # R023-M1：记住上次曲线时间范围（回填须先于轮询连接，避免误触发）
    _ui_prefs = QSettings('DoubaoWork', 'CuktechPanel')
    # R023：进阶配置中心 / 隐私协议 / AI 通道共用偏好与加密存储
    adv_prefs = ux.Prefs('DoubaoWork', 'CuktechPanel')
    adv_secrets = ux.SecretStore(adv_prefs)
    # 1.5.7：AI 连接配置持久化——%LOCALAPPDATA%\CuktechPanel\ai_config.json
    # 结构：{version, active, profiles{key:{...}}, settings{...}}；Key 用 DPAPI 加密后 base64。
    _AC, _ac_err = ux.ai_config_load()
    _AC_bad = bool(_ac_err)
    if _AC_bad:     # 解析失败/字段坏：不静默沿用旧值，连接字段回落默认并在界面提示
        for _k3 in ('aiProvider', 'aiBaseUrl', 'aiModel', 'aiApiKey'):
            try:
                adv_prefs.set(_k3, '')
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:acbadclr')
    _AC_pf = dict(_AC.get('profiles') or {})
    _AC_set = dict(_AC.get('settings') or {})
    _AC_act = str(_AC.get('active') or '')
    for _k0, _v0 in _AC_set.items():        # 回填：JSON 优先（QSettings 只作兜底）
        try:
            adv_prefs.set(_k0, _v0)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:acfill')
    if _AC_act in _AC_pf:
        _p0 = _AC_pf[_AC_act] or {}
        for _k1, _v1 in (('aiProvider', _p0.get('provider')),
                         ('aiBaseUrl', _p0.get('base_url')),
                         ('aiModel', _p0.get('model'))):
            if _v1:
                adv_prefs.set(_k1, _v1)
    if not _AC_pf:
        _AC_pf = {'local': {'name': '本地端点', 'provider': '本地端点',
                            'base_url': 'http://127.0.0.1:1234/v1',
                            'model': 'qwen2.5-7b-instruct'},
                  'cloud:默认': {'name': '默认', 'provider': 'DeepSeek 云端',
                                'base_url': 'https://api.deepseek.com',
                                'model': 'deepseek-chat'}}
        _AC_act = ('local' if str(_AC_set.get('aiProvider') or '') == '本地端点'
                   else 'cloud:默认')
    try:                       # 1.5.6：首次启动幂等建出 4 个出厂固定组（已有则不动）
        ux.group_seed(adv_prefs)
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:groupseeding')
    try:
        chart_range.setCurrentIndex(max(0, min(4, int(_ui_prefs.value('chartRange', 1)))))
    except Exception:
        chart_range.setCurrentIndex(1)
    chart_range.currentIndexChanged.connect(
        lambda i: _ui_prefs.setValue('chartRange', int(i)))
    chart_range.setFixedHeight(32)   # C20-C1：与全站 32 高按钮族对齐
    range_row.addWidget(chart_range)
    btn_back_live = aligned_button('回到实时', 'ghost', min_w=86, h=32)
    btn_reset_zoom = aligned_button('重置缩放', 'ghost', min_w=86, h=32)
    range_row.addWidget(btn_back_live)
    range_row.addWidget(btn_reset_zoom)
    range_row.addStretch(1)
    chartv.addLayout(range_row)
    # 内联图例：4 端口 + 总功率
    legend_row = QHBoxLayout()
    legend_row.setContentsMargins(4, 0, 4, 0)
    legend_row.setSpacing(16)
    for pk, (pc, pn) in PORT_NAMES.items():
        _dot = QLabel('●')
        _dot.setStyleSheet('color:%s; font-size:11px;' % PORT_COLORS[pk])
        _nm = QLabel(pn)
        _nm.setStyleSheet('color:' + TEXT_SUB + '; font-size:12px;')
        legend_row.addWidget(_dot)
        legend_row.addWidget(_nm)
    _dot_t = QLabel('●')
    _dot_t.setStyleSheet('color:#ffffff; font-size:11px;')
    _nm_t = QLabel('总功率')
    _nm_t.setStyleSheet('color:' + TEXT_SUB + '; font-size:12px;')
    legend_row.addWidget(_dot_t)
    legend_row.addWidget(_nm_t)
    legend_row.addStretch(1)
    chartv.addLayout(legend_row)
    # 功率占比堆叠条（CK-P3-02，对齐网页 powerDist）
    dist_cap = QLabel('功率占比')
    dist_cap.setObjectName('sub')
    dist_cap.setStyleSheet('font-size:11px; padding:2px 0 0;')
    chartv.addWidget(dist_cap)
    power_dist = PowerDistBar()
    chartv.addWidget(power_dist)
    power_dist.setVisible(False)
    dist_cap.setVisible(False)
    _ck_boot_trace('bui_chart_in')
    # C34h：pyqtgraph + 主图/迷你图推迟到首帧之后构建（_build_charts），
    # 首屏先在图表位放骨架占位，保证不空窗。
    plot = None
    power_glow = power_glow2 = power_curve = vline = hline = chart_readout = None
    port_curves = {}
    chart_holder = QWidget()
    _phv = QVBoxLayout(chart_holder)
    _phv.setContentsMargins(0, 0, 0, 0)
    chart_skeleton = QLabel('图表加载中…')
    chart_skeleton.setAlignment(Qt.AlignCenter)
    chart_skeleton.setObjectName('chartSkeleton')
    chart_skeleton.setStyleSheet(
        'background:rgba(3,169,244,0.06); border:1px dashed rgba(159,180,200,90);'
        ' border-radius:10px; color:#8fa6bd; font-size:13px;')
    chart_skeleton.setMinimumHeight(120)
    _phv.addWidget(chart_skeleton)
    chartv.addWidget(chart_holder, 1)
    chart_xs = []
    chart_ys = []
    _ck_boot_trace('bui_chart_done')
    # R015（领导）：Y 量程滞回状态——对齐网页稳定性（量程只升不降，空闲复位）
    chart_y_state = {'upper': RATED_POWER}
    # R018（领导）：网页 liveEngine 完整复刻——1s 实时尾 + /api/chart 基底桶
    live = {'buf': [], 'base_epochs': [], 'baseP': None, 'baseV': None,
            'baseI': None, 'follow': True, 'pend': False}
    chart_state = {'span': 300, 'interval': 5}
    follow_live = {'v': True}

    # ===== C6：空闲曲线文字（无充电时随机展示有趣文字） =====
    IDLE_TEXTS = ['等待充电中…', '⚡ 星源智控 ⚡', '摸鱼中 ゅ', '随时待命',
                  '来充个电吧~', '电量，安排！', '躺平等单', '10 号 GaN 在此']
    idle_overlay = QLabel()
    idle_overlay.setAlignment(Qt.AlignCenter)
    idle_overlay.setStyleSheet(
        'background:transparent; color:#38e2ff; font-size:22px; font-weight:700;')
    idle_overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
    idle_overlay.hide()
    idle_state = {'on': False, 'idx': 0, 'zero_ticks': 0}

    def _center_idle():
        try:
            if plot is None:
                return
            idle_overlay.setFixedWidth(max(80, plot.width() - 40))
            idle_overlay.move(20, plot.height() // 2 - 24)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:3883')

    class _IdleCenter(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Resize:
                _center_idle()
            return False
    _icf = None  # C34h：图在 _build_charts 里创建后再挂 resize 过滤器

    def _rotate_idle():
        if not idle_state['on']:
            return
        import random as _r
        cand = [t for t in IDLE_TEXTS if t != idle_overlay.text()]
        txt = _r.choice(cand) if cand else IDLE_TEXTS[0]
        idle_state['idx'] = IDLE_TEXTS.index(txt)
        idle_overlay.setText(txt)
        _center_idle()

    idle_timer = QTimer()  # C34h：无父对象，图建好后再 start（3.5 s 轮播）
    idle_timer.setInterval(3500)
    idle_timer.timeout.connect(_rotate_idle)

    def _live_tick(ports, paint=True):
        """网页 liveTick：1s 实时点入缓冲（对齐 BLE 帧率）。

        paint=False 用于挂起态：只把点写进缓冲，不碰任何控件，
        恢复可见时曲线窗口内仍有连续数据（此前挂起期间整段丢弃，
        恢复后窗口是空的，表现就是「曲线没了」）。
        """
        now = int(time.time())
        pp = [0.0] * 4
        vv = [0.0] * 4
        cc = [0.0] * 4
        tot = 0.0
        for k in range(4):
            d = ports.get(str(k + 1)) or {}
            pp[k] = float(d.get('power') or 0)
            vv[k] = float(d.get('voltage') or 0)
            cc[k] = float(d.get('current') or 0)
            tot += pp[k]
        live['buf'].append({'t': now, 'p': pp, 'v': vv, 'i': cc, 'tot': tot})
        live['pend'] = True
        chart_state['live_pw'] = {str(k + 1): pp[k] for k in range(4)}
        if paint:
            _paint_mini(chart_state.get('mini_last') or {})
        cutoff = now - chart_state['span'] - 120
        while live['buf'] and live['buf'][0]['t'] < cutoff:
            live['buf'].pop(0)

    def _build_series():
        """网页 buildSeries：基底桶（丢进行中末桶）+ 实时尾拼接，裁剪到窗口。"""
        now = int(time.time())
        span = chart_state['span']
        win_start = now - span
        epochs = []
        P = [[], [], [], [], []]
        V = [[], [], [], []]
        I = [[], [], [], []]
        be = live['base_epochs']
        if be and live['baseP']:
            be = be[:-1]
            nbe = len(be)
            for k in range(5):
                P[k] = list(live['baseP'][k][:nbe])
            for k in range(4):
                bv = live['baseV'][k] if live['baseV'] else []
                bi = live['baseI'][k] if live['baseI'] else []
                V[k] = list(bv[:nbe])
                I[k] = list(bi[:nbe])
            epochs = list(be)
        after = epochs[-1] if epochs else win_start
        for pt in live['buf']:
            if pt['t'] <= after or pt['t'] < win_start:
                continue
            epochs.append(pt['t'])
            for k in range(4):
                P[k].append(pt['p'][k])
                V[k].append(pt['v'][k])
                I[k].append(pt['i'][k])
            P[4].append(pt['tot'])
        cut = 0
        while cut < len(epochs) and epochs[cut] < win_start:
            cut += 1
        if cut:
            epochs = epochs[cut:]
            for k in range(5):
                P[k] = P[k][cut:]
            for k in range(4):
                V[k] = V[k][cut:]
                I[k] = I[k][cut:]
        return epochs, P, V, I

    def _render_live():
        """网页 renderLiveChart：基底+实时尾 → 5 曲线 + Y/X 视窗 + 空闲判定。"""
        if plot is None:   # C34h：图还没建好（首帧前）→ 跳过，下一次提交再画
            return
        epochs, P, V, I = _build_series()
        ck_trace('render_in', epochs=len(epochs), buf=len(live['buf']),
                 base=len(live['base_epochs'] or []))
        chart_xs[:] = epochs
        chart_ys[:] = P[4]
        n = len(epochs)
        if not n:
            ck_trace('render_empty', buf=len(live['buf']))
            nowf = time.time()
            plot.setXRange(nowf - chart_state['span'], nowf + 2, padding=0)
            _paint_mini(chart_state.get('mini_last') or {})
            return
        xs = epochs
        # R023：图表点数上限（降采样）与平滑策略由进阶配置中心控制
        _cap = int(adv_prefs.get('chartPoints') or 120)
        _do_smooth = str(adv_prefs.get('smooth') or '平滑') != '原始'
        if n > _cap > 0:
            _step = max(1, n // _cap)
            xs = epochs[::_step]
            if xs and xs[-1] != epochs[-1]:
                xs = list(xs) + [epochs[-1]]
            # R037：xs 补了末点而降采样后的 P 没补——n>_cap 时 X/Y 必差一位，
            # setData 抛形状异常（chart_diag.log 实证）导致整幅图与迷你图冻结
            P = [(list(arr[::_step])
                  + ([arr[-1]] if arr and (len(arr) - 1) % _step != 0 else []))
                 for arr in P]
        for k in range(4):
            pk = str(k + 1)
            # R037：基底缺序列/短序列（如后端未返回某端口历史）时按尾部对齐，
            # 不再让形状异常炸掉 _render_live（异常会连累迷你图更新段一起跳过）
            _m = min(len(xs), len(P[k]))
            pdata = P[k][-_m:] if _m else []
            xs_k = xs[-_m:] if _m else []
            pkmax = max(pdata) if pdata else 0.0
            if pkmax >= 0.5 and _do_smooth:
                port_curves[pk].setData(*smooth_xy(xs_k, pdata))
            else:
                port_curves[pk].setData(xs_k, pdata)
        _m4 = min(len(xs), len(P[4]))
        tdata = P[4][-_m4:] if _m4 else []
        _sx = xs[-_m4:] if _m4 else []
        tmax = max(tdata) if tdata else 0.0
        if tmax >= 0.5 and _do_smooth:
            _sx, _sy = smooth_xy(_sx, tdata)
        else:
            _sy = tdata
        power_glow.setData(_sx, _sy)
        power_glow2.setData(_sx, _sy)
        power_curve.setData(_sx, _sy)
        # Y 轴：空闲 0-20（网页 suggestedMax 语义）；负载自适应 + 滞回
        p_all = max([tmax] + [max(P[k]) if P[k] else 0.0 for k in range(4)])
        if p_all < 0.1:
            chart_y_state['upper'] = 20
            plot.setYRange(0, 20, padding=0)
        else:
            if tmax >= 1.0:
                _need = min(max(tmax * 1.2, 20.0), RATED_POWER)
                if _need > chart_y_state['upper'] or _need < chart_y_state['upper'] * 0.5:
                    chart_y_state['upper'] = _need
            plot.setYRange(0, chart_y_state['upper'], padding=0)
        if live['follow']:
            nowf = time.time()
            plot.setXRange(nowf - chart_state['span'], nowf + 2, padding=0)
        idle_state['zero_ticks'] = idle_state['zero_ticks'] + 1 if tmax < 0.5 else 0
        set_idle(idle_state['zero_ticks'] >= 2)
        _paint_mini(chart_state.get('mini_last') or {})

    def set_idle(on):
        if plot is None:   # C34h：图未建好
            return
        if on and not idle_state['on']:
            idle_state['on'] = True
            idle_overlay.setText(IDLE_TEXTS[0])
            idle_state['idx'] = 0
            _center_idle()
            idle_overlay.show()
            idle_timer.start()
        elif not on and idle_state['on']:
            idle_state['on'] = False
            idle_timer.stop()
            idle_overlay.hide()

    def on_chart_moved(pos):
        if plot is None or not chart_xs:
            return
        mouse_pt = plot.plotItem.vb.mapSceneToView(pos)
        x = mouse_pt.x()
        # R15-C21：线性 scan → bisect（chart_xs 升序，鼠标悬停路径 O(log n)）
        import bisect as _bisect
        _i = _bisect.bisect_left(chart_xs, x)
        if _i >= len(chart_xs):
            idx = len(chart_xs) - 1
        elif _i > 0 and (x - chart_xs[_i - 1]) < (chart_xs[_i] - x):
            idx = _i - 1
        else:
            idx = _i
        vline.setPos(chart_xs[idx])
        hline.setPos(chart_ys[idx])
        chart_readout.setPos(chart_xs[idx], chart_ys[idx])
        chart_readout.setText('%s  %.1f W' % (
            ck_time.fmt(ck_time.HMS, chart_xs[idx]), chart_ys[idx]))

    # C34h：鼠标联动/手动缩放信号在 _build_charts 建图后挂接
    # R018（网页口径）：仅用户手动拖拽/滚轮才退出跟随（程序性 setXRange 不触发）
    def _on_manual(*a):
        live['follow'] = False

    def back_to_live():
        live['follow'] = True
        poll_status()

    btn_back_live.clicked.connect(back_to_live)
    btn_reset_zoom.clicked.connect(
        lambda: (plot.enableAutoRange() if plot is not None else None))
    sv.addWidget(g_chart, 1)

    # 小窗口紧凑：状态页根据窗口高度压缩曲线、按宽度隐藏连接横带次要元素（无滚动条）
    class _StatusCompact(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Resize:
                h = ev.size().height()
                wd = ev.size().width()
                # R32：125%/150% 下字号放大，固定 192 主卡 + 刻度行已占满，
                # 700 高窗口需把曲线下限从 120 降到 70，否则卡片底部被页面裁掉。
                if plot is not None:
                    plot.setMinimumHeight(70 if h < 780 else 120)
                narrow = wd < 720
                # 窄窗：连接横带只留状态/BLE，隐藏按钮（设置页可访问）
                conn_quick_b.setVisible(not narrow)
                restart_b.setVisible(not narrow)
            return False
    _sc = _StatusCompact(w)
    _inst_ef(w, _sc)
    w._sc = _sc
    # 状态页固定布局，不滚动
    try:
        stack.removeWidget(_boot_ph)
        _boot_ph.deleteLater()
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:boot-ph')
    stack.addWidget(st)

    def ble_connect_toggle(btn):
        """BLE 主动连接/断开（CK-E4），状态页与设置页共用。"""
        want = not btn.text().startswith('断开')
        btn.setEnabled(False)
        btn.setText('连接中…' if want else '断开中…')

        def _job():
            # C7：校验 /api/enable 响应码，未达预期连接态即失败
            try:
                _code, _d = api('POST', '/api/enable', {'enabled': want}, timeout=CK_T_CMD)
            except Exception:
                return ('ERR', None)
            if _code != 200 or (isinstance(_d, dict) and not _d.get('ok', True)):
                return (_code, _d)
            # C34（缺陷 4）：等待粒度 500→250 ms，且先查后睡。
            # 旧实现固定先睡 500 ms 再查，认证若在几十毫秒内完成也要白等半拍；
            # 总超时仍 20×0.5=10 s，语义不变，只去掉无感知空等。
            for _ in range(40):
                try:
                    _c, d = api('GET', '/api/status', timeout=4)
                    if isinstance(d, dict):
                        ok = bool(d.get('connected')) and bool(d.get('authenticated'))
                        if ok == want:
                            return (200, d)
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:4145')
                time.sleep(0.25)
            return ('ERR', 'state')

        def _done(r):
            btn.setEnabled(True)
            _code = r[0] if isinstance(r, tuple) else 'ERR'
            btn.setText('断开连接' if want else '连接设备')
            if _code == 200:
                w._toast.show('已连接设备' if want else '已断开连接', 'ok')
            else:
                w._toast.show('连接操作失败（HTTP %s）' % _code, 'warning')
            poll_status()
        qwait(_job, _done)

    conn_quick_b.clicked.connect(lambda: ble_connect_toggle(conn_quick_b))

    # 连接态防抖：连续 N 次断开才显示“未连接”，避免瞬时抖动误报
    conn_state = {'disconnect_streak': 0, 'ever_ok': False, 'last_connected': None}
    DISCONNECT_THRESHOLD = 3
    settings_cache = {}  # R020-B4：/api/status 的 settings 快照（piid 数字键；倒计时回显用）
    # R020-M2：协议开关勾选缓存独立于 settings_cache——后者被 1s 轮询 clear+update 会抹掉自定义键
    _proto_cache = {}

    def render_conn(connected, ble_ok=False, reason=''):
        # R32：状态行单一真相源——不再出现「● 正在连接…」与「● BLE 未连接」并存；
        # 旋转点由图腾承担，文本不再自带圆点。
        # C31：后端给出 BLE 失败原因时，必须直接显示，替代无限「正在搜索」——禁止静默回落。
        # C34（缺陷 1）：第一行只表达 BLE 链路（「已连接充电器」把链路说成充电器，语义错），
        # 充电器在位/充电中由右侧 _ck_chg_badge 徽章承担，认证态由 _ck_ble_badge 承担。
        if connected:
            conn_state['ever_ok'] = True
            conn_state['disconnect_streak'] = 0
            conn_state['last_connected'] = True
            lbl_conn.setText('蓝牙 已连接')
            lbl_conn.setToolTip('')
            conn_spinner.setVisible(False)
        else:
            conn_state['disconnect_streak'] += 1
            conn_state['last_connected'] = False
            if reason:
                # 状态行只出短状态词，失败原因放悬停提示
                lbl_conn.setText('蓝牙 未连接')
                lbl_conn.setToolTip('原因：%s' % reason)
                conn_spinner.set_color(ERR)
                conn_spinner.setVisible(True)
            # 从未连上过：搜索中（旋转）；曾连上又断开：需连续 3 次才报未连接
            elif not conn_state['ever_ok']:
                lbl_conn.setText('正在搜索充电器')
                lbl_conn.setToolTip('')
                conn_spinner.set_color(WARN)
                conn_spinner.setVisible(True)
            elif conn_state['disconnect_streak'] >= DISCONNECT_THRESHOLD:
                lbl_conn.setText('蓝牙 未连接')
                lbl_conn.setToolTip('')
                conn_spinner.set_color(ERR)
                conn_spinner.setVisible(True)
            else:
                lbl_conn.setText('蓝牙 已连接')
                lbl_conn.setToolTip('')
                conn_spinner.setVisible(False)
        # 蓝牙认证徽章随 1s 状态拍刷新；巴法云徽章由 2s 全局状态拍单写，避免互相覆盖。
        _bt_txt, _bt_col = _ck_ble_badge(bool(connected), bool(ble_ok))
        _setT('badge_ble_set', ble_state_lbl,
              '<span style="color:%s">●</span> %s' % (_bt_col, _bt_txt))

    # R023：后端被杀后 UI 仍显示绿点"已连接"（R022 测试员 P1）——连续 3 次健康检查失败
    # 进入"服务异常"红点态并只提示一次；恢复后自动清除。
    svc_state = {'fail': 0, 'notified': False}

    def _set_lag(text):
        """同一文案在 hero 连接条与端口页同步显示（R02 全局化）。"""
        lag_lbl.setText(text)
        lag_lbl.show()
        for _t in lag_extra:
            _t.setText(text)
            _t.show()

    def _clear_lag():
        if lag_lbl.isVisible():
            lag_lbl.hide()
        for _t in lag_extra:
            if _t.isVisible():
                _t.hide()

    def _mark_service_fail(_code='ERR'):
        svc_state['fail'] += 1
        if svc_state['fail'] >= 3:
            # 2.1.0：状态位只出短状态词，原因进悬停；旋转点由 spinner 承担。
            # 巴法云徽章不在这里写——它由 2s 状态拍单写，避免「首页/设置」两套来源打架。
            lbl_conn.setText('服务异常')
            lbl_conn.setToolTip('后端无响应')
            # R02：连接条已写明"服务异常"，此处只留次数，去掉重复的"数据中断"措辞
            _set_lag('连续 %d 次未更新' % svc_state['fail'])
            conn_spinner.set_color(ERR)
            conn_spinner.setVisible(True)
            _setT('badge_ble_set', ble_state_lbl,
                  '<span style="color:#ef4444">●</span> 蓝牙 状态未知')
            _setT('badge_chg', badge_chg,
                  '<span style="color:#94a3b8">●</span> 充电器 状态未知')
            if not svc_state['notified']:
                svc_state['notified'] = True
                w._toast.show('后端服务无响应，请点击「重启服务」', 'warning')
        else:
            _set_lag('数据滞后 · %d 次未更新' % svc_state['fail'])

    # ===== 合并轮询（/api/live）+ worker 侧结构化 + GUI 差异更新 =====
    # 旧路径：t1 每拍 GET /api/status、t2 每拍 GET /api/chart（两条 TCP、两份 JSON，
    # 且 _chart_done 在 sig 未变时仍要反序列化整段曲线）。
    # 新路径：一次 GET /api/live；服务端按 X-Chart-Tag 判增量（tag 未变则 chart:null），
    # worker 线程完成解析 + 浮点化 + 文案拼装，GUI 只按 key 比对"变了才写"。
    CK_LIVE_STAT = os.environ.get('CK_LIVE_STAT', '')
    _live_stat = {'n': 0, 'fh': None}

    def _live_rec(rec):
        if not CK_LIVE_STAT:
            return
        _live_stat['n'] += 1
        try:
            if _live_stat['fh'] is None:
                _live_stat['fh'] = open(CK_LIVE_STAT, 'a', encoding='utf-8', buffering=1)
            _live_stat['fh'].write(json.dumps(dict(rec, n=_live_stat['n'])) + '\n')
        except Exception:
            _live_stat['fh'] = None

    _ui_prev = {}

    def _setT(key, lbl, val):
        if _ui_prev.get(key) != val:
            _ui_prev[key] = val
            lbl.setText(val)

    def _setV(key, wgt, val):
        if _ui_prev.get(key) != val:
            _ui_prev[key] = val
            wgt.setVisible(bool(val))

    def _setSS(key, wgt, css):
        if _ui_prev.get(key) != css:
            _ui_prev[key] = css
            wgt.setStyleSheet(css)

    def _plan_status(d):
        """worker 线程：把 /api/live 的 status 段整理成"控件该显示什么"。
        只做数值归一 + 文案拼装，不触达任何 Qt 对象。"""
        ports = d.get('ports') or {}
        stg = d.get('settings') if isinstance(d.get('settings'), dict) else {}
        charge = d.get('charge') or {}
        cds = d.get('countdowns') or {}
        pinfo = {}
        total = 0.0
        for k in PORT_NAMES:
            p = ports.get(k) or {}
            try:
                v = float(p.get('voltage') or 0)
                c = float(p.get('current') or 0)
                pw = float(p.get('power') or 0)
            except Exception:
                v = c = pw = 0.0
            total += pw
            ctl = PORT_NAMES[k][0]
            cd = cds.get(ctl) if isinstance(cds.get(ctl), dict) else {}
            rem = int(cd.get('remaining') or 0)
            tmo = None
            if rem > 0:
                tmo = {'text': '倒计时 剩余 ' + _ck_fmt_dur(rem),
                       'color': ERR_SOFT if rem <= 120 else WARN_SOFT if rem <= 600 else '#93a9d1'}
            cs = charge.get(ctl) if isinstance(charge.get(ctl), dict) else {}
            el = cs.get('elapsed')
            if cs.get('active') and isinstance(el, (int, float)) and el >= 0:
                ei = int(el)
                dur = '本次充电：%02d:%02d:%02d' % (ei // 3600, (ei % 3600) // 60, ei % 60)
            else:
                dur = '本次充电：未充电'
            cd2 = None
            rem2 = cs.get('remaining')
            if cs.get('active') and isinstance(rem2, (int, float)) and rem2 is not None:
                ri = int(rem2)
                cd2 = {'text': '剩余 ' + _ck_fmt_dur(ri),
                       'color': ERR_SOFT if ri <= 120 else WARN_SOFT if ri <= 600 else OK}
            pinfo[k] = {'v': round(v, 3), 'c': round(c, 3), 'pw': round(pw, 3),
                        'vt': '电压 %.2f V' % v, 'ct': '电流 %.2f A' % c,
                        'pt': '功率 %.1f W' % pw,
                        'proto': '协议 ' + str(p.get('protocol') or 'idle'),
                        'en': bool(p.get('enabled')), 'active': bool(p.get('active')),
                        'tmo': tmo, 'dur': dur, 'cd': cd2}
        return {'connected': bool(d.get('connected')),
                'ble_ok': bool(d.get('connected')) and bool(d.get('authenticated')),
                # C31：后端最近一次 BLE 失败原因（空串=无）。连不上时必须显示，禁止静默当已连接
                'ble_error': str(d.get('ble_error') or '').strip()[:80],
                'stg': stg,
                'stg15': (int(stg.get('15') or 0) == 1) if ('15' in stg) else None,
                'total': round(total, 2), 'ports': pinfo,
                'raw': {k: {'power': pinfo[k]['pw'], 'voltage': pinfo[k]['v'],
                            'current': pinfo[k]['c'], 'enabled': pinfo[k]['en'],
                            'active': pinfo[k]['active']} for k in pinfo},
                'active_ports': {k for k, p in ports.items()
                                 if isinstance(p, dict) and p.get('enabled') is not False
                                 and float(p.get('power') or 0) > 0},
                'vmax': max([p['v'] for p in pinfo.values()] or [0.0]),
                'cur_sum': round(sum(p['c'] for p in pinfo.values()), 3),
                'n_active': sum(1 for p in pinfo.values() if p['active']),
                'load': round(total / 140.0 * 100.0, 3),
                'scene': (int(stg.get('5') or 1) if isinstance(stg, dict) else None)}

    def _plan_chart(d, interval, now, span, prev_sig):
        """worker 线程：曲线段浮点化/对齐/平滑，GUI 只负责 setData。"""
        datasets = d.get('datasets') or {}
        power_sets = datasets.get('power') or []
        lm = {'C1': '1', 'C2': '2', 'C3': '3', 'A': '4'}
        n = max((len(s.get('data') or []) for s in power_sets), default=0)
        if not n:
            return None
        tot_s = next((s for s in power_sets if s.get('label') == 'Total'), None)
        if tot_s is not None:
            _tail = (tot_s.get('data') or [])[-3:]
        else:
            _tail = []
            for s in power_sets:
                if s.get('label') in lm:
                    _tail = (s.get('data') or [])[-3:] + _tail
        try:
            idle_val = max([float(v or 0) for v in _tail] or [0.0]) <= 0.2
        except Exception:
            idle_val = None
        aligned_start = int((now - span) / interval) * interval
        try:
            sig = (interval, aligned_start, n,
                   tuple(tuple(round(float(v or 0), 2) for v in (st.get('data') or [])[-6:])
                         for st in power_sets))
        except Exception:
            sig = None
        if sig is not None and sig == prev_sig:
            return {'sig': sig, 'same': True, 'idle': idle_val}
        by_label = {s.get('label'): [float(v or 0) for v in (s.get('data') or [])]
                    for s in power_sets}
        if 'Total' in by_label:
            total = by_label['Total']
        else:
            f4 = [by_label.get(lbl, []) for lbl in ('C1', 'C2', 'C3', 'A')]
            n0 = max((len(x) for x in f4), default=0)
            total = [0.0] * n0
            for x in f4:
                for j, v in enumerate(x):
                    if j < n0:
                        total[j] += v
        n = len(total)
        if not n:
            return None
        xs = [aligned_start + j * interval for j in range(n)]
        v_by = {s.get('label'): [float(v or 0) for v in (s.get('data') or [])]
                for s in (datasets.get('voltage') or [])}
        i_by = {s.get('label'): [float(v or 0) for v in (s.get('data') or [])]
                for s in (datasets.get('current') or [])}
        mini = {}
        # R21：port_cards 以端口键 '1'..'4' 取图，这里必须同键（此前键成 C1..A → KeyError 'xs'）
        for lbl, pk in lm.items():
            pdata = by_label.get(lbl, [])
            m = min(len(pdata), 36)
            if not m:
                mini[pk] = {'empty': True}
                continue
            seg = pdata[-m:]
            mxs = list(range(m))
            pkmax = max(seg)
            if pkmax >= 0.5:
                sx, sy = smooth_xy(mxs, seg)
                mini[pk] = {'xs': sx, 'ys': sy, 'max': pkmax, 'm': m}
            else:
                mini[pk] = {'xs': mxs, 'ys': seg, 'max': pkmax, 'm': m}
        return {'sig': sig, 'same': False, 'idle': idle_val,
                'base_epochs': xs,
                'baseP': [by_label.get('C1', [])[:n], by_label.get('C2', [])[:n],
                          by_label.get('C3', [])[:n], by_label.get('A', [])[:n], total[:n]],
                'baseV': [v_by.get('C1', [])[:n], v_by.get('C2', [])[:n],
                          v_by.get('C3', [])[:n], v_by.get('A', [])[:n]],
                'baseI': [i_by.get('C1', [])[:n], i_by.get('C2', [])[:n],
                          i_by.get('C3', [])[:n], i_by.get('A', [])[:n]],
                'mini': mini}

    def _live_job(qs, tag, want_chart, interval):
        _live_job.n = getattr(_live_job, 'n', 0) + 1
        tid = _live_job.n
        t0 = time.perf_counter()
        code, d, meta = api_meta('GET', qs, timeout=8,
                                 extra_headers={'X-Chart-Tag': tag} if want_chart else None)
        t1 = time.perf_counter()
        rec = {'code': code, 'e2e_ms': round((t1 - t0) * 1000, 2),
               'bytes': meta.get('len') or 0,
               'srv_ms': float(meta.get('srv_ms') or -1)}
        if code != 200 or not isinstance(d, dict):
            ck_trace('resp_fail', tid=tid, code=code, rtype=type(d).__name__)
            return {'rec': rec, 'plan': None, 'chart': None, 'tid': tid}
        st = d.get('status') if isinstance(d.get('status'), dict) else d
        plan = _plan_status(st)
        tag2 = d.get('chartTag') or ''
        cplan = None
        ch = d.get('chart')
        if isinstance(ch, dict):
            cplan = _plan_chart(ch, interval, time.time(),
                                chart_state['span'], chart_state.get('sig'))
        rec['tag'] = tag2
        ck_trace('resp', tid=tid, code=code, bytes=rec['bytes'], has_tag=bool(tag2),
                 fields=len(d), has_chart=bool(isinstance(ch, dict)))
        return {'rec': rec, 'plan': plan, 'chart': cplan, 'tid': tid, 'tag': tag2}

    def poll_status():
        _i = chart_range.currentIndex()
        _want = not chart_state.get('idle_stopped') and not _sus['on']
        _qs = '/api/live'
        if _want:
            _qs += '?hours=%s&interval=%s' % (CHART_HOURS[_i], CHART_INTERVAL[_i])
        _tag = chart_state.get('tag') or ''
        qwait(lambda: _live_job(_qs, _tag, _want, int(CHART_INTERVAL[_i])),
              _status_done, key='live')

    def _apply_chart_data(cp, idle_now):
        """R22 · 数据层：每拍吸收新数据到缓冲/基底，但不触达 pyqtgraph。"""
        if not cp:
            return False
        _chart_idle(bool(cp.get('idle')))
        if cp.get('same'):
            return False
        chart_state['sig'] = cp['sig']
        live['base_epochs'] = cp['base_epochs']
        live['baseP'] = cp['baseP']
        live['baseV'] = cp['baseV']
        live['baseI'] = cp['baseI']
        chart_state['pending'] = cp
        return True

    # 1.5.5：端口卡迷你功率曲线绘制层。曲线区常驻，只切换「折线 / 空网格 + 无数据」。
    # 任何一步都先判空（1.5.1 的 AttributeError 就是因为往已置空的槽位 setData）。
    def _paint_mini(mm_map):
        """迷你曲线绘制：只决定「画折线 / 空网格」两种状态，不再隐藏整块。

        1.5.5：曲线区常驻（吃满卡内剩余高度）。断链 / 空载 / 端口关闭时
        清空曲线、回到默认范围，并在图区中央显示淡色「无数据」，
        图区高度与位置保持不变。
        """
        if not port_cards:
            return
        _link_ok = bool(conn_state.get('ever_ok') and conn_state.get('last_connected'))
        mm_map = mm_map or {}
        for pk, tup in port_cards.items():
            box, mc = tup[8], tup[9]
            if box is None:
                continue
            pl = getattr(box, '_plot', None)
            txt = getattr(box, '_txt', None)
            mm = mm_map.get(pk) or {}
            _pw = (chart_state.get('live_pw') or {}).get(str(pk))
            _has = (pl is not None and mc is not None and _link_ok
                    and (_pw is None or float(_pw) >= 0.5)
                    and not mm.get('empty')
                    and 'xs' in mm and float(mm.get('max') or 0) >= 0.5)
            try:
                if _has:
                    mc.setData(mm['xs'], mm['ys'])
                    pl.setXRange(0, max(mm['m'] - 1, 1), padding=0.02)
                    pl.setYRange(0, max(float(mm['max']) * 1.2, 20), padding=0)
                else:
                    # 端口卡重建期间 _plot/_txt 可能已置空：逐项判空后再碰 pyqtgraph
                    if mc is not None:
                        mc.setData([], [])
                    if pl is not None:
                        pl.setXRange(0, 30, padding=0)
                        pl.setYRange(0, 20, padding=0)
                if txt is not None:
                    txt.setVisible(not _has)
                    if not _has:
                        txt.setPos(15, 10)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:mini-paint')

    def _chart_commit(force=False):
        """R22：曲线节拍器（12s）——把最新缓冲一次性提交给 pyqtgraph。

        force=True 用于切页/恢复：立即重绘一帧，不等下一个 12s 窗口。
        """
        if not force and (_CK_DEG['on'] or _CK_NUMQ['defer_all']):
            return False   # R26/R27：可见不聚焦 → 停止提交；空载（defer_all）→ 非 force 不提交，
                           # dirty 留着，回充电/聚焦时 force 补一帧（用户 R27 降级许可）
        if plot is None:   # C34h：图未建好
            return False
        if not force and not chart_state.get('dirty'):
            return False
        cp = chart_state.get('pending')
        chart_state['pending'] = None
        chart_state['dirty'] = False
        _idle_now = bool(chart_state.get('idle_now'))
        if cp:
            chart_state['mini_last'] = cp.get('mini') or {}
        _render_live()
        ck_trace('chart_commit', force=bool(force), had_data=bool(cp),
                 idle=_idle_now, t=round(time.time(), 3))
        return True

    def _chart_commit_fast():
        """C33（缺陷 1）：曲线实时尾随轮询拍提交（默认 2s），只跑 _render_live()。

        12s 节拍器继续负责迷你图（_paint_mini）与范围，pending 仍由它消费；
        这里不碰 pending，也不改 dirty，避免与 t2 抢同一份数据。
        可见不聚焦 / 空载（defer_all）时与 _chart_commit 同门禁，不偷偷耗 CPU。
        CK_CURVE_FAST=0 / `cfast 0` 可关掉（同构建 A/B 对照用，不影响默认行为）。
        """
        if not _CK_CFAST['on']:
            return False
        if plot is None:   # C34h：图未建好
            return False
        if _CK_DEG['on'] or _CK_NUMQ['defer_all']:
            return False
        if not live.get('pend'):
            return False    # 本拍没有新实时点 → 不重复重绘（断连/空载不空转）
        live['pend'] = False
        _render_live()
        if _CK_CFAST_TRACE:
            _dbg_log('curvefast %.3f' % time.time())
        return True

    _poll_pace = {'sig': None, 'step': 0, 'ms': 2000}

    def _ck_pace(total, ports, idle_now):
        """R27：轮询退避——数据未变且处于空载/降级态：2s→5s→10s；一变立刻回 2s。

        聚焦充电态不参与退避（新鲜度 ≤2.5s 不降级）；CK_POLL_MS 固定档关退避（阶梯口径）。
        """
        base = max(1000, int(getattr(w, '_poll_ms', 2000) or 2000))
        # C34（缺陷 3）：曲线点只随轮询拍产生，默认 2s → 曲线天然 0.5Hz（用户感知「半天不动」）。
        # 状态页可见且有负载时把基准压到 1s（对齐网页 liveTick 的 1s 实时点）；其它页仍按
        # 「端口轮询频率」设置，空载退避 2s→5s→10s 逻辑不变（空载曲线是直线，压节奏无收益）。
        try:
            if not idle_now and not _CK_DEG['on'] and stack.currentIndex() == 0:
                base = min(base, 1000)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:pace-curve')
        if _CK_POLL_MS > 0:
            if _poll_pace['ms'] != _CK_POLL_MS:
                _poll_pace['ms'] = _CK_POLL_MS
                ck_trace('pace', ms=_CK_POLL_MS, step=-1, changed=None, fixed=True)
            _fx = _CK_POLL_MS
            t1.setInterval(_fx)
            return _fx
        # 变化判定用"用户可见量"：整数 W + 端口使能/状态。0.05W 级抖动不算变化，
        # 否则空载噪声会让退避永远回落到 2s（实测 mock 空载 total 0→0.1 抖）。
        _sig = (int(round(float(total or 0))),
                tuple(sorted((str(k), int(round(float((p or {}).get('power') or 0))),
                              bool((p or {}).get('enabled', True)),
                              str((p or {}).get('active') or ''))
                             for k, p in (ports or {}).items())))
        _changed = _sig != _poll_pace['sig']
        _poll_pace['sig'] = _sig
        _allow = bool(idle_now or _CK_DEG['on']) and _CK_POLL_BACKOFF
        if _changed or not _allow:
            _poll_pace['step'] = 0
        else:
            _poll_pace['step'] = min(_poll_pace['step'] + 1, 2)
        _ms = (5000, 10000)[_poll_pace['step'] - 1] if _poll_pace['step'] else base
        if _ms != _poll_pace['ms'] or _changed:
            ck_trace('pace', ms=_ms, step=_poll_pace['step'], changed=_changed,
                     idle=bool(idle_now), deg=bool(_CK_DEG['on']),
                     age=round(time.time(), 3))
        _poll_pace['ms'] = _ms
        t1.setInterval(_ms)
        return _ms

    _ls_prev = {'bok': None, 'map': {}}

    def _ls_db_path():
        """历史库绝对路径：优先后端写入的 CUKTECH_HISTORY_DB_PATH，
        否则 app_data_dir()（已含 data 层）+ port_history.db。
        必须延迟到调用时求值——build_ui 可能早于后端设置环境变量。"""
        return os.environ.get('CUKTECH_HISTORY_DB_PATH') or \
            os.path.join(app_data_dir(), 'port_history.db')

    def _last_samples(bok):
        """链正常时不读库；刚断链（或启动即离线）读一次每端口最后采样时间。"""
        if bok:
            _ls_prev['bok'] = True
            return _ls_prev['map']
        if _ls_prev['bok'] is not False:
            _ls_prev['bok'] = False
            _ls_prev['map'] = _db_last_samples(_ls_db_path())
        return _ls_prev['map']

    def _apply_port_state(key, en, active, known, offline=False, last_ts=0.0):
        """缺陷修复：把某端口状态直接落到卡片（徽章/横幅/开关/图标）。

        轮询回包与「动作成功后立即更新」共用同一条路径，避免两套口径漂移。
        两种「不知道」分开写：
          - 从未连上过（DB 无该端口采样）→「● 未知」＋「未连接」置灰；
          - 连过但当前断链 →「● 离线 · 最后更新 HH:MM:SS」，开关只读回显最后已知位，
            明确禁止下发（链断了写不进去，点了就是假成功）。
        """
        tup = port_cards.get(key)
        if not tup:
            return False
        (g, pv, cv, powl, proto, sw_b, st_lbl, ctl, mini, mini_curve,
         cd_lbl, dur_lbl, banner, tmo_lbl, dev_lbl) = tup
        _sc = getattr(w, '_port_style_cache', None)
        if _sc is None:
            _sc = {}
            w._port_style_cache = _sc
        _hms = ck_time.fmt(ck_time.HMS, last_ts) if last_ts else '--:--:--'
        if _sc.get(key) != (en, active, known, offline, int(last_ts)):
            _sc[key] = (en, active, known, offline, int(last_ts))
            if not known and offline:
                col, bg, bd = WARN_SOFT, 'rgba(245,158,11,0.10)', 'rgba(245,158,11,0.30)'
                txt = '● 离线 · 最后更新 ' + _hms
            elif not known:
                # C33（缺陷 3）：未连接/未认证 = 状态未知，显示灰色未知，不冒充「关闭」
                col, bg, bd = TEXT_MUTED, 'rgba(148,163,184,0.10)', 'rgba(148,163,184,0.28)'
                txt = '● 未知'
            elif active:
                col, bg, bd = OK, 'rgba(15,169,104,0.20)', 'rgba(15,169,104,0.55)'
                txt = '● 输出中'
            elif en:
                col, bg, bd = WARN_SOFT, 'rgba(245,158,11,0.16)', 'rgba(245,158,11,0.45)'
                txt = '● 使能'
            else:
                col, bg, bd = TEXT_MUTED, 'rgba(255,255,255,0.06)', 'rgba(255,255,255,0.14)'
                txt = '● 关闭'
            banner.setStyleSheet(
                'QFrame#portbanner { background:%s; border:1px solid %s; '
                'border-radius:8px; }' % (bg, bd))
            st_lbl.setStyleSheet(
                'color:%s; font-size:%dpx; font-weight:700; background:transparent;'
                % (col, 12 if (not known and offline) else 14))
            st_lbl.setText(txt)
            if not known and offline:
                # 只读回显最后已知位：不置灰成「关闭」，也不允许点击下发
                sw_b.setText('关闭输出' if en else '开启输出')
                sw_b.setStyleSheet(
                    'QPushButton{background:rgba(255,255,255,0.04); color:#8fa3b8;'
                    'border:1px dashed rgba(148,163,184,0.45); border-radius:8px;}'
                    'QPushButton:disabled{background:rgba(255,255,255,0.04);'
                    'color:#8fa3b8; border:1px dashed rgba(148,163,184,0.45);}')
                sw_b.setToolTip('离线 · 最后更新 %s，已禁止下发命令' % _hms)
            elif active or en:
                sw_b.setText('关闭输出')
                sw_b.setToolTip('')
                sw_b.setStyleSheet(
                    'QPushButton{background:#0fa968; color:white; border:none;'
                    'border-radius:8px; font-weight:700;}'
                    'QPushButton:hover{background:#12c078;}'
                    'QPushButton:pressed{background:#0a7d4e; padding-top:1px;}'
                    'QPushButton:disabled{background:rgba(15,169,104,0.16); color:#6f8a7f;'
                    'border:1px solid rgba(15,169,104,0.22);}')
            else:
                sw_b.setText('开启输出')
                sw_b.setToolTip('')
                sw_b.setStyleSheet(
                    'QPushButton{background:rgba(255,255,255,0.12); color:#cfe0f0;'
                    'border:1px solid rgba(255,255,255,0.20); border-radius:8px;}'
                    'QPushButton:hover{background:rgba(255,255,255,0.18);}'
                    'QPushButton:pressed{background:rgba(56,226,255,0.30); padding-top:1px;}'
                    'QPushButton:disabled{background:rgba(255,255,255,0.05); color:#5b6b7d;'
                    'border-color:rgba(255,255,255,0.08);}')
            if not known and not offline:
                sw_b.setText('未连接')
                sw_b.setToolTip('从未连接过设备')
            # C19-A1：端口图标随输出态换 on/off（缓存位图，不重缩放）
            _ic = getattr(g, '_port_icon', None)
            if _ic is not None:
                _pm = _port_pm(getattr(g, '_port_ctl', 'c1'), bool(active))
                if not _pm.isNull():
                    _ic.setPixmap(_pm)
        # 单开关按钮回填（blockSignals 避免触发 on_toggle；只在真变了才写）
        if _ui_prev.get(key + '.chk') != en or \
                _ui_prev.get(key + '.en') != known or \
                _ui_prev.get(key + '.off') != offline:
            _ui_prev[key + '.chk'] = en
            _ui_prev[key + '.en'] = known
            _ui_prev[key + '.off'] = offline
            sw_b.blockSignals(True)
            sw_b.setChecked(en)
            # C3：未连接/未认证/离线时开关只读，禁止下发
            sw_b.setEnabled(known)
            sw_b.blockSignals(False)
        return True

    def _status_done(r):
        ck_trace('done', tid=r.get('tid') if isinstance(r, dict) else None,
                 rtype=type(r).__name__,
                 has_plan=bool(isinstance(r, dict) and r.get('plan') is not None),
                 code=(r.get('rec') or {}).get('code') if isinstance(r, dict) else None)
        try:
            if not isinstance(r, dict) or r.get('plan') is None:
                _mark_service_fail((r or {}).get('code', 'ERR')
                                   if isinstance(r, dict) else 'ERR')
                return
            plan = r['plan']
            # C34（缺陷 2）：devstate 四态注入——覆盖可见字段，真实轮询继续跑（清态用 devstate off）
            _dm = getattr(w, '_ck_devmode', None)
            if _dm:
                plan = _ck_dev_plan(_dm, plan)
            _live_rec(r.get('rec') or {})
            svc_state['fail'] = 0
            svc_state['notified'] = False
            _clear_lag()
            if _sus['on']:
                # R22：后台只采集不刷界面；恢复时 _chart_commit(force=True) 一次性补齐
                # C34b：挂起必须"零 UI 写入"——此前连接条/按钮写在早退之前，
                # 造成同一屏里 BLE 徽章已变、充电徽章与 KPI 停在旧值（#5 冻结撕裂）。
                # 2.1.2：挂起期间仍把实时点写进曲线缓冲（不碰控件），
                # 否则恢复后当前窗口内没有任何点，曲线整段为空。
                _live_tick(plan['raw'], paint=False)
                chart_state['dirty'] = True
                ck_trace('suspend_tick', total=plan['total'])
                return
            render_conn(plan['connected'], plan['ble_ok'],
                        plan.get('ble_error') or '')
            _setT('btn', conn_quick_b, '断开连接' if plan['ble_ok'] else '连接设备')
            # USB-A 小电流状态（settings 中 piid 15）
            stg = plan['stg']
            if isinstance(stg, dict):
                settings_cache.clear()
                settings_cache.update(stg)
            if plan['stg15'] is not None:
                _on = bool(plan['stg15'])
                if _ui_prev.get('trickle') != _on:
                    _ui_prev['trickle'] = _on
                    trickle_sel.set_value(1 if _on else 0)
            ports = plan['raw']
            # C51：自动关判定与断开在后端（backend/auto_off.py），状态页是否可见、
            # 窗口是否挂起都不影响；界面不显示无充电计时/倒计时。
            total = plan['total']
            # R018（领导）：网页 liveEngine——1s 实时尾 + 基底拼接渲染
            # R08 性能：空载时 1s 全量重绘 5 条曲线纯属浪费（末段全是 0，视觉不变）。
            # 改为空载每 3 拍渲染一次；一旦有功率立刻恢复逐拍渲染（CK-046）。
            _idle_now = float(total or 0) <= 0.2
            ck_trace('plan', tid=r.get('tid'), total=total, idle_now=_idle_now,
                     chart=bool(r.get('chart')), nports=len(ports))
            # R27：空载/降级态按"数据未变"退避（2s→5s→10s），一变立刻回 2s（见 _ck_pace）
            _ck_pace(total, ports, _idle_now)
            # R27：空载数字上屏合并到节拍（每拍至多一次 setText→update，见 AnimatedNumberLabel._show）
            _defer_prev = _CK_NUMQ['defer_all']
            _CK_NUMQ['defer_all'] = bool(_idle_now)
            if _defer_prev and not _CK_NUMQ['defer_all']:
                _ck_num_flush(force=True)   # 先落旧文本，避免挂起值覆盖本拍新值
                _chart_commit(force=True)   # R27：空载→充电 立刻补一帧（≤1.5s 显示最新值）
            try:
                _chart_idle(_idle_now)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:4520')
            _ic = int(chart_state.get('idle_ticks') or 0)
            _ic = (_ic + 1) if _idle_now else 0
            chart_state['idle_ticks'] = _ic
            _live_tick(ports)
            if r.get('tag'):
                chart_state['tag'] = r['tag']
            # R19：曲线只在"有新数据/有功率/到刷新拍"时渲染——稳态不再 1s 一次 setXRange
            # R22：曲线数据每拍只进缓冲，绘制交给 12s 节拍器（不再逐拍 setData）
            _chart_new = _apply_chart_data(r.get('chart'), _idle_now)
            chart_state['idle_now'] = _idle_now
            chart_state['dirty'] = True
            # C33（缺陷 1）：实时尾每拍跟着走，不再等 12s 才画
            _chart_commit_fast()
            # C34g（#3）：冷启动首帧不等退避/节拍——首包状态到达后强制上屏一次，
            # 保证「启动→首帧数据」在 1 s 内（uistate 直接读标签即可见）。
            if not _CK_NUMQ.get('first_done'):
                _CK_NUMQ['first_done'] = True
                _CK_NUMQ['defer_all'] = False
                _ck_num_flush(force=True)
                _chart_commit(force=True)
                ck_trace('first_frame', tid=r.get('tid'), total=plan['total'],
                         t=round(time.time(), 3))
            ck_trace('render_call', tid=r.get('tid'), total=plan['total'],
                     chart_new=_chart_new, idle=_idle_now)
            # R015（领导）：应用内通知系统真实生效——托盘通知（充电开始/结束）
            # R015-B4：两拍去抖 + per-port 粒度 + 断连清态（防 PD 重协商瞬时 0W 误报）
            try:
                _ncfg = getattr(w, '_notify_cfg', None) or {}
                _npref = getattr(w, '_notify_prefs', None) or {}
                if _npref.get('dnd') or _npref.get('enabled') is False:
                    _ncfg = {}
                if isinstance(_ncfg, dict) and (
                        _ncfg.get('charge_start') or _ncfg.get('charge_end')):
                    _now_act = {k for k, p in ports.items()
                                if p.get('enabled') is not False and float(p.get('power') or 0) > 0}
                    _prev = getattr(w, '_prev_act_ports', None)
                    if not plan['connected']:
                        w._prev_act_ports = set()
                        w._pend_start = set()
                        w._pend_stop = set()
                    elif _prev is not None:
                        _fire_s = getattr(w, '_pend_start', set()) & _now_act
                        _fire_e = getattr(w, '_pend_stop', set()) & (_prev - _now_act)
                        w._pend_start = _now_act - _prev
                        w._pend_stop = _prev - _now_act
                        w._prev_act_ports = _now_act
                        if _fire_s and _ncfg.get('charge_start'):
                            _t1 = ('充电开始',
                                   '、'.join(PORT_NAMES[k][1] for k in sorted(_fire_s))
                                   + ' 开始输出')
                            if not _ck_toast_send(_t1[0], _t1[1], 'plain'):
                                tray.showMessage(_t1[0], _t1[1],
                                                 QSystemTrayIcon.Information, 4000)
                        if _fire_e and _ncfg.get('charge_end'):
                            _t2 = ('充电结束',
                                   '、'.join(PORT_NAMES[k][1] for k in sorted(_fire_e))
                                   + ' 已停止输出')
                            if not _ck_toast_send(_t2[0], _t2[1], 'plain'):
                                tray.showMessage(_t2[0], _t2[1],
                                                 QSystemTrayIcon.Information, 4000)
                    else:
                        w._prev_act_ports = _now_act
                        w._pend_start = set()
                        w._pend_stop = set()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:4570')
            # R15-C07：本地越界告警——后端 automation 阈值，两拍去抖（进入越界才报）、断连清态
            try:
                _thr = getattr(w, '_thresholds', None)
                if isinstance(_thr, dict) and _thr and _ncfg.get('threshold'):
                    _alarm = {}
                    for _pid, _pw in ports.items():
                        if PORT_NAMES.get(_pid) is None:
                            continue
                        _r = _thr.get(_pid)
                        if not isinstance(_r, dict):
                            continue
                        try:
                            _mn = float(_r.get('min_w') or 0)
                            _mx = float(_r.get('max_w') or 0)
                        except Exception:
                            _mn = _mx = 0.0
                        _pwf = float(_pw.get('power') or 0)
                        if _mn > 0 and _pwf < _mn:
                            _alarm[_pid] = ('低于下限 %.0fW' % _mn, _pwf)
                        elif _mx > 0 and _pwf > _mx:
                            _alarm[_pid] = ('超过上限 %.0fW' % _mx, _pwf)
                    _prev_al = getattr(w, '_prev_alarm', {})
                    if not plan['connected']:
                        w._prev_alarm = {}
                    else:
                        w._prev_alarm = _alarm
                        for _pid, (_txt, _pwv) in _alarm.items():
                            if _pid not in _prev_al:
                                _t3 = ('功率告警',
                                       '%s %s（当前 %.1fW）'
                                       % (PORT_NAMES[_pid][1], _txt, _pwv))
                                if not _ck_toast_send(_t3[0], _t3[1], 'warn'):
                                    tray.showMessage(_t3[0], _t3[1],
                                                     QSystemTrayIcon.Warning, 4000)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:4604')
            # R19：以下所有写入都过"变了才写"闸门——轮询回调不再无条件下发 update()
            # C30：断连时第一屏不得出现「0.0 W / 0.0 V / 0.00 A」——那是把“未知”猜成
            # 实测 0，违反「不猜」原则；显示 '—'，只有真连接（含真实空载 0W）才显示数值。
            _na = (lambda _v: _v) if plan['connected'] else (lambda _v: None)
            if _ui_prev.get('hero') != _na(total):
                _old_hero = _ui_prev.get('hero')
                _ui_prev['hero'] = _na(total)
                hero_power.set_target(_na(total), flash=True)
                ck_trace('hero', tid=r.get('tid'), old=_old_hero, new=total,
                         txt=hero_power.text())
            if abs(total - (_ui_prev.get('pwr_scale') or -1.0)) >= 0.1:
                _ui_prev['pwr_scale'] = total
                power_scale.set_value(total)
            # R15-C15：LIVE 徽章空闲置灰（网页 .hero-card.idle 口径）；R024 改自绘脉冲胶囊
            _live_on = total > 0.05
            _red = bool(getattr(charger_dev, 'reduced', False))
            if _ui_prev.get('badge_live') != (_live_on, _red):
                _ui_prev['badge_live'] = (_live_on, _red)
                live_badge.set_on(_live_on, reduced=_red)
            _cstate = (plan['connected'], plan['scene'],
                       tuple((k, plan['ports'][k]['en'], plan['ports'][k]['active'])
                             for k in PORT_NAMES))
            if _ui_prev.get('cdev') != _cstate:
                _ui_prev['cdev'] = _cstate
                charger_dev.set_state(plan['connected'], ports, scene=plan['scene'])
            # C-05 四 KPI（worker 已算好，set_target 内部对同值早退）
            # C30：c30probe 取证窗口内冻结，避免轮询把人工注入的有值态覆盖回 '—'
            if not getattr(w, '_c30_freeze', False):
                kpi_labels['vmax'].set_target(_na(plan['vmax']), flash=True)
                kpi_labels['cur'].set_target(_na(plan['cur_sum']), flash=True)
                kpi_labels['active'].set_target(_na(plan['n_active']), flash=True)
                kpi_labels['load'].set_target(_na(plan['load']), flash=True)
            # 1.5.5：状态行两枚徽章（巴法云 / 蓝牙）统一由 1s 状态拍（render_conn）写入，
            # 12s 数据拍不再重复写，避免两套节奏互相覆盖造成「设置已连接、首页未启用」。
            try:
                # C34d（#11）：每拍喂一次总功率；soc/charge_full 暂无数据源，留协议接口
                _ck_full_track(total, time.monotonic(),
                               soc=plan.get('soc'), full_flag=plan.get('charge_full'))
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:full-track')
            _chg_txt, _chg_col = _ck_chg_badge(plan, getattr(w, '_ck_devmode', None),
                                               _CK_FULL['is_full'])
            _setT('badge_chg', badge_chg,
                  '<span style="color:%s">●</span> %s' % (_chg_col, _chg_txt))
            # R15-C08：批量按钮与倒计时设置随连接态置灰（后端 409 守卫已加，前端同步门控）
            if not getattr(w, '_batch_flight', False):
                if _ui_prev.get('btns_en') != plan['ble_ok']:
                    _ui_prev['btns_en'] = plan['ble_ok']
                    for _b in (cd_b, proto_b, all_on, all_off):
                        _b.setEnabled(plan['ble_ok'])
            # 功率占比堆叠条（CK-P3-02）：无负载时隐藏，空闲不白占空间（CK-V04）
            dist_vals = [(PORT_COLORS[k], plan['ports'][k]['pw']) for k in PORT_NAMES]
            if _ui_prev.get('dist') != dist_vals:
                _ui_prev['dist'] = dist_vals
                power_dist.set_values(dist_vals)
            _has_load = sum(v for _, v in dist_vals) > 0.05
            _setV('dist_vis', power_dist, _has_load)
            _setV('distcap_vis', dist_cap, _has_load)
            _ls = _last_samples(bool(plan['ble_ok']))
            for key in port_cards:
                pi = plan['ports'].get(key)
                if not pi:
                    continue
                (g, pv, cv, powl, proto, sw_b, st_lbl, ctl, mini, mini_curve,
                 cd_lbl, dur_lbl, banner, tmo_lbl, dev_lbl) = port_cards[key]
                _setT(key + '.v', pv, pi['vt'])
                _setT(key + '.c', cv, pi['ct'])
                _setT(key + '.p', powl, pi['pt'])
                _setT(key + '.proto', proto, pi['proto'])
                # R024-A3（领导）：倒计时剩余（d['countdowns'][port]）——与是否充电无关
                if pi['tmo']:
                    _setT(key + '.tmo', tmo_lbl, pi['tmo']['text'])
                    _setSS(key + '.tmo_css', tmo_lbl,
                           'font-size:12px; font-weight:700; color:%s; padding:1px 0;'
                           % pi['tmo']['color'])
                    _setV(key + '.tmo_vis', tmo_lbl, True)
                else:
                    _setV(key + '.tmo_vis', tmo_lbl, False)
                # 本次充电时长（C5）+ 充电倒计时（CK-E1）
                _setT(key + '.dur', dur_lbl, pi['dur'])
                # 1.5.5：本次充电设备名（仅充电中显示；无充电留空，不显示占位编号）
                _dn = (_ck_active_dev_name(_CK_ACTIVE.get(_CK_PORT_PID.get(key, 0)))
                       if pi['active'] else '')
                if _dn:
                    _fm = dur_lbl.fontMetrics()
                    _el = _fm.elidedText(_dn, Qt.ElideRight, 112)
                    _setT(key + '.dev', dev_lbl, _el)
                    dev_lbl.setToolTip(_dn)
                    _setV(key + '.dev_vis', dev_lbl, True)
                else:
                    dev_lbl.setToolTip('')
                    _setV(key + '.dev_vis', dev_lbl, False)
                if pi['cd']:
                    _setSS(key + '.cd_css', cd_lbl,
                           'font-size:12px; font-weight:700; color:%s; padding:1px 0;'
                           % pi['cd']['color'])
                    _setT(key + '.cd', cd_lbl, pi['cd']['text'])
                    _setV(key + '.cd_vis', cd_lbl, True)
                else:
                    _setV(key + '.cd_vis', cd_lbl, False)
                # C33（缺陷 3）：只有 connected+authenticated 时端口状态才是「已知」
                # 徽章/横幅/开关/图标统一走 _apply_port_state（与动作成功后的即时更新同源）
                _apply_port_state(key, bool(pi['en']), bool(pi['active']),
                                  bool(plan['ble_ok']),
                                  (not bool(plan['ble_ok'])) and (key in _ls),
                                  float(_ls.get(key, 0.0)))
        except Exception:
            ck_trace('done_exc', tid=r.get('tid') if isinstance(r, dict) else None,
                     tb=traceback.format_exc())

    def _confirm(title, text):
        """R030：统一二次确认框（无系统问号大图标、中文按钮、按钮宽度受控）。"""
        box = QMessageBox(w)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.NoIcon)
        box.setText(text)
        box.setStandardButtons(QMessageBox.NoButton)
        _y = box.addButton('确定', QMessageBox.AcceptRole)
        _n = box.addButton('取消', QMessageBox.RejectRole)
        box.setDefaultButton(_n)
        box.setStyleSheet('QPushButton{min-width:96px;max-width:96px;}')
        box.exec()
        return box.clickedButton() is _y

    def do_restart():
        if not _confirm('重启服务', '确认重启充电守护服务？重启期间约 5-15 秒无法读取数据。'):
            return
        # R007（D-05）：等待循环移出 UI 线程——此前主线程 sleep 最长 60s 整窗冻结
        restart_b.setEnabled(False)
        restart_b.setText('重启中…')

        def _job():
            # C2：校验 HTTP 码与 ok，非 200 立即失败，不再静默吞错
            try:
                _code, _d = api('POST', '/api/service', {'action': 'restart'}, timeout=CK_T_CMD)
            except Exception:
                return ('ERR', None)
            if _code != 200 or (isinstance(_d, dict) and not _d.get('ok', True)):
                return (_code, _d)
            # 只有端口短暂消失再出现（服务确实重新上线）才算重启成功
            import socket as _s
            for _ in range(120):
                time.sleep(0.5)
                sk = _s.socket()
                sk.settimeout(0.4)
                try:
                    sk.connect((HOST, port))
                    sk.close()
                    return (200, _d)
                except OSError as _exc:
                    ck_swallow(_exc, 'launcher:4757')
            return ('ERR', 'timeout')

        def _done(r):
            restart_b.setEnabled(True)
            restart_b.setText('重启服务')
            code = r[0] if isinstance(r, tuple) else 'ERR'
            if code == 200:
                w._toast.show('服务已重启', 'ok')
                poll_status()
            else:
                w._toast.show('重启失败（HTTP %s）' % code, 'warning')

        qwait(_job, _done)

    restart_b.clicked.connect(do_restart)

    CHART_HOURS = ['0.0167', '0.0833', '0.1667', '0.5', '1']
    chart_minutes = [1, 5, 10, 30, 60]  # R018：liveEngine 显示窗口（分钟）
    CHART_INTERVAL = ['5', '5', '5', '10', '30']  # 后端最小间隔 5s

    def _chart_idle(flag):
        """R19：合并轮询后曲线不再单独起 t2——空载时 /api/live 不带 chart 参数
        （服务端只回 chartTag，不下载整段曲线 JSON）；有功率时立刻补一次带曲线的请求。
        """
        if flag:
            chart_state['idle_stopped'] = True
            return
        if chart_state.get('idle_stopped') is True:
            chart_state['idle_stopped'] = False
            QTimer.singleShot(0, poll_status)

    def poll_chart():
        i = chart_range.currentIndex()
        interval = int(CHART_INTERVAL[i])
        qs = '/api/chart?hours=%s&interval=%s' % (CHART_HOURS[i], CHART_INTERVAL[i])
        qwait(lambda: api('GET', qs), lambda r: _chart_done(r, interval), key='chart')

    def _chart_done(r, interval):
        try:
            if plot is None:   # C34h：图未建好，下一拍再取
                return
            if not isinstance(r, tuple) or r[0] != 200:
                return
            _code, d = r
            if not isinstance(d, dict):
                return
            datasets = d.get('datasets') or {}
            power_sets = datasets.get('power') or []
            label_map = {'C1': '1', 'C2': '2', 'C3': '3', 'A': '4'}
            n = max((len(s.get('data') or []) for s in power_sets), default=0)
            if not n:
                return
            # R18 性能：空载判定与数据签名都只用原始尾部；签名未变时不再把全量序列
            # 转 float（原实现每次成功返回前都先解析完所有点位，空载也在烧 CPU）。
            try:
                _tot = next((s for s in power_sets if s.get('label') == 'Total'), None)
                if _tot is not None:
                    _tail = (_tot.get('data') or [])[-3:]
                else:
                    _tail = []
                    for _s in power_sets:
                        if _s.get('label') in label_map:
                            _tail = (_s.get('data') or [])[-3:] + _tail
                _chart_idle(max([float(v or 0) for v in _tail] or [0.0]) <= 0.2)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:4821')
            # R018（网页 fetchChartData）：alignedStart 对齐桶格，epochs=aligned+k*interval
            now = time.time()
            span = chart_state['span']
            aligned_start = int((now - span) / interval) * interval
            try:
                _sig = (interval, aligned_start, n,
                        tuple(tuple(round(float(v or 0), 2) for v in (st.get('data') or [])[-6:])
                              for st in power_sets))
            except Exception:
                _sig = None
            if _sig is not None and _sig == chart_state.get('sig'):
                return
            chart_state['sig'] = _sig
            by_label = {}
            for s in power_sets:
                by_label[s.get('label')] = [float(v or 0) for v in (s.get('data') or [])]
            # 直接用后端 Total 序列；缺失时兜底累加前 4 端口
            if 'Total' in by_label:
                total = by_label['Total']
            else:
                first4 = [by_label.get(lbl, []) for lbl in ('C1', 'C2', 'C3', 'A')]
                n0 = max((len(x) for x in first4), default=0)
                total = [0.0] * n0
                for x in first4:
                    for j, v in enumerate(x):
                        if j < n0:
                            total[j] += v
            n = len(total)
            if not n:
                return
            xs = [aligned_start + j * interval for j in range(n)]
            v_sets = datasets.get('voltage') or []
            i_sets = datasets.get('current') or []
            v_by = {stm.get('label'): [float(x or 0) for x in (stm.get('data') or [])]
                    for stm in v_sets}
            i_by = {stm.get('label'): [float(x or 0) for x in (stm.get('data') or [])]
                    for stm in i_sets}
            live['base_epochs'] = xs
            live['baseP'] = [by_label.get('C1', [])[:n], by_label.get('C2', [])[:n],
                             by_label.get('C3', [])[:n], by_label.get('A', [])[:n],
                             total[:n]]
            live['baseV'] = [v_by.get('C1', [])[:n], v_by.get('C2', [])[:n],
                             v_by.get('C3', [])[:n], v_by.get('A', [])[:n]]
            live['baseI'] = [i_by.get('C1', [])[:n], i_by.get('C2', [])[:n],
                             i_by.get('C3', [])[:n], i_by.get('A', [])[:n]]
            _render_live()
            # 端口卡迷你曲线由 _paint_mini 统一绘制（mini_last），此处不重复绘制；历史采样照旧。
            # R018：总功率/端口曲线/Y/X/空闲判定已移交 _render_live（liveEngine）
        except Exception as _e:
            try:
                import traceback as _tbmod
                _diag = os.path.join(os.path.expandvars("%LOCALAPPDATA%"),
                                     "CuktechPanel", "chart_diag.log")
                with open(_diag, "a", encoding="utf-8") as _fh:
                    _fh.write("=== %s ===\n" % ck_time.fmt(ck_time.HMS))
                    _fh.write(_tbmod.format_exc())
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:4916')

    # ===== Tab 2: 端口控制 =====
    pt = QWidget()
    pt.setObjectName('pagePorts')   # R041 / C10
    grid = QGridLayout(pt)
    grid.setContentsMargins(6, 6, 6, 6)
    grid.setSpacing(8)
    # C19-A1：端口线稿图标（on/off 两态，44×26）。素材此前 0 引用；
    # 目录复用设备图解析结果（源码/冻结双路已在 ChargerDevice 里做过）。
    _port_pm_cache = {}

    def _port_pm(ctl, on):
        _k = (ctl, bool(on))
        if _k not in _port_pm_cache:
            _pm = QPixmap(os.path.join(getattr(charger_dev, '_img_dir', ''),
                                       'main_card_port_%s_%s.png' % (ctl, 'on' if on else 'off')))
            if not _pm.isNull():
                _pm = _pm.scaled(44, 26, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            _port_pm_cache[_k] = _pm
        return _port_pm_cache[_k]

    port_cards = {}
    _mini_deferred = []
    _ck_boot_trace('bui_mini_in')
    for i, (key, (ctl, name)) in enumerate(PORT_NAMES.items()):
        g = GlassCard(name)
        gv = QVBoxLayout(g)
        # C13：压缩卡片内边距，让批量按钮在 1000×731 首屏可见
        # 1.5.2：纵向从 12/8 收到 8/4、间距 4→2——900×620 卡高 253，含 36px 迷你图
        # 后内容 hint 必须压到 253 以内（原先 280 会被裁 27px）。
        gv.setContentsMargins(12, 8, 12, 4)
        gv.setSpacing(2)
        # 状态横幅：左侧状态文字，右侧单开关按钮（C5：开关集成一个按钮置于右侧）
        banner = QFrame()
        banner.setFixedHeight(40)   # C19-C2：给 32 高开关留 4px 上下内边距
        banner.setObjectName('portbanner')
        bhb = QHBoxLayout(banner)
        bhb.setContentsMargins(10, 4, 8, 4)
        # C19-A1：端口类型图标（C口/USB-A 线稿，随输出态切 on/off）
        p_icon = QLabel()
        p_icon.setFixedSize(44, 26)
        p_icon.setStyleSheet('background:transparent;')
        _pm0 = _port_pm(ctl, False)
        if not _pm0.isNull():
            p_icon.setPixmap(_pm0)
        g._port_icon = p_icon
        g._port_ctl = ctl
        st_lbl = QLabel('● 未知')
        # C33（缺陷 3）：无数据/未连接时不得断言「关闭」——初始为未知灰
        st_lbl.setStyleSheet('font-size:14px; font-weight:700; background:transparent; color:#94a3b8;')
        sw_b = QPushButton('未连接')
        sw_b.setEnabled(False)
        sw_b.setCheckable(True)
        sw_b.setFixedSize(88, 32)   # C19-C2：与全站 32 高按钮同族
        sw_b.setCursor(Qt.PointingHandCursor)
        bhb.addWidget(p_icon, 0)
        bhb.addWidget(st_lbl, 1)
        bhb.addWidget(sw_b, 0)
        # C25：指标行补 14px 线稿（电压/电流/功率/协议），与记录页 KPI 同口径
        pv_box, pv = _metric_cell('电压 —', 'vmax')
        cv_box, cv = _metric_cell('电流 —', 'cur')
        powl_box, powl = _metric_cell('功率 —', 'load')
        proto_box, proto = _metric_cell('协议 —', 'plug')
        # C19-B1：三级字阶——值 15/600、协议 12 次要色
        for _l in (pv, cv, powl):
            _l.setStyleSheet('font-size:15px; font-weight:600; padding:2px 0;')
        proto.setStyleSheet('font-size:12px; color:' + TEXT_SUB + '; padding:2px 0;')
        # 数据 2×2 网格，紧凑且填满中部
        data_grid = QGridLayout()
        data_grid.setContentsMargins(2, 2, 2, 0)
        data_grid.setHorizontalSpacing(12)
        data_grid.setVerticalSpacing(2)
        data_grid.addWidget(pv_box, 0, 0)
        data_grid.addWidget(cv_box, 0, 1)
        data_grid.addWidget(powl_box, 1, 0)
        data_grid.addWidget(proto_box, 1, 1)
        # 本次充电时长（C5）+ 剩余倒计时（CK-E1）
        dur_lbl = QLabel('本次充电 --:--:--')
        dur_lbl.setStyleSheet('font-size:12px; color:#cfe0f0; padding:1px 0;')
        cd_lbl = QLabel('')
        cd_lbl.setStyleSheet('font-size:12px; font-weight:700; padding:1px 0;')
        cd_lbl.hide()
        time_row = QHBoxLayout()
        time_row.setContentsMargins(4, 0, 4, 0)
        time_row.addWidget(dur_lbl)
        time_row.addStretch(1)
        # 1.5.5：本次充电设备名（与记录页同一取名口径；无充电留空，
        # 过长省略 + tooltip，固定 18 高不撑行）
        dev_lbl = QLabel('')
        dev_lbl.setStyleSheet('font-size:12px; color:' + TEXT_SUB + '; padding:1px 0;')
        dev_lbl.setFixedHeight(18)
        dev_lbl.setMaximumWidth(118)
        dev_lbl.hide()
        time_row.addWidget(dev_lbl)
        time_row.addWidget(cd_lbl)
        # R024-A3（领导）：端口倒计时（定时关断）剩余显示——与充电上限剩余（cd_lbl）相互独立
        tmo_lbl = QLabel('')
        tmo_lbl.setStyleSheet('font-size:12px; font-weight:700; color:#93a9d1; padding:1px 0;')
        tmo_lbl.hide()
        time_row.addWidget(tmo_lbl)
        # 1.5.5：迷你功率曲线改为常驻分区（上信息 / 下曲线）。
        # 空载、端口关闭、断链都不再整块隐藏：保留空网格 + 图区中央淡色「无数据」，
        # 高度取卡内剩余空间（最小 PORT_MINI_H），卡与卡之间、充电/空载之间完全一致。
        mini = QWidget()
        mini.setMinimumHeight(PORT_MINI_H)
        mini.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        _mv = QVBoxLayout(mini)
        _mv.setContentsMargins(0, 1, 0, 0)
        _mv.setSpacing(2)
        mini_cap = QLabel('近 3 分钟功率')
        mini_cap.setObjectName('sub')
        mini_cap.setStyleSheet('font-size:11px; padding:0;')
        # 空态文案画在图区中央（mini_plot 内的 TextItem），不再占布局行。
        _mv.addWidget(mini_cap)
        mini_curve = None
        _mini_deferred.append((key, mini))

        def _async_post(payload):
            _pl = dict(payload or {})
            _pl.setdefault('client', '界面按钮')   # 断开归因：入口=界面按钮

            def _job():
                try:
                    return api('POST', '/api/port', _pl, timeout=CK_T_CMD)
                except Exception as e:
                    return ('ERR', str(e))
            return _job

        def on_toggle(checked, c=ctl, k=key):
            # C3：请求飞行中禁用（避免并发连点）；失败立即回滚旧态并 toast
            sw_b.setEnabled(False)
            _prev = not checked
            def _done(r):
                # 回包后按「当前链是否已知」恢复可用性：离线时保持只读，
                # 否则一个断链瞬间的在飞请求会把按钮重新点亮（点了就是假成功）
                sw_b.setEnabled(bool(_ui_prev.get(k + '.en', True)))
                if conn_state.get('ever_ok') and conn_state.get('last_connected'):
                    _code = r[0] if isinstance(r, tuple) else 'ERR'
                    _d = r[1] if isinstance(r, tuple) and len(r) > 1 else None
                    if _code == 200 and (not isinstance(_d, dict) or _d.get('ok', True)):
                        # 缺陷修复：动作成功 → 立即局部更新该行 + 徽章，
                        # 再拉一次真实状态兜底（不依赖切页，也不等下一拍轮询）
                        try:
                            _ck_port_after_action(c, bool(checked))
                        except Exception as _exc:
                            ck_swallow(_exc, 'launcher:port-after')
                        poll_status()
                        return
                sw_b.blockSignals(True)
                sw_b.setChecked(_prev)
                sw_b.blockSignals(False)
                w._toast.show('端口 %s 操作失败' % c, 'warning')
            qwait(_async_post({'port': c, 'action': 'on' if checked else 'off'}), _done)

        sw_b.clicked.connect(on_toggle)
        # 1.5.5：卡内固定分区——信息在上（横幅 / 数据 / 时长行），曲线区吃满剩余高度。
        # 不再插 addStretch，避免曲线隐藏后信息被撑散；曲线区最小高 PORT_MINI_H，
        # 充电与空载同高同位置，四卡由网格等分 → 永远严格等大。
        gv.addWidget(banner)
        gv.addLayout(data_grid)
        gv.addLayout(time_row)
        gv.addWidget(mini, 1)
        # R03（25 轮）：卡顶让出第 0 行给页面级滞后提示条，四卡整体下移一行
        grid.addWidget(g, i // 2 + 1, i % 2)
        port_cards[key] = (g, pv, cv, powl, proto, sw_b, st_lbl, ctl, mini, mini_curve, cd_lbl, dur_lbl, banner, tmo_lbl, dev_lbl)
    # 1.5.5：不再固定 364×322（删除状态行后按旧尺寸会留出底部空白）。
    # 四卡统一 Expanding、同一最小尺寸，由 2×2 网格等分可用区域 → 永远严格等大。
    for _tup in port_cards.values():
        _tup[0].setMinimumSize(260, 200)
        _tup[0].setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    grid.setColumnStretch(0, 1)
    grid.setColumnStretch(1, 1)
    _ck_boot_trace('bui_mini_done')
    # ===== 动作成功后的即时刷新 + 无充电 10 分钟自动关端口 =====
    _ctl2key = {v[0]: k for k, v in PORT_NAMES.items()}

    def _ck_port_after_action(ctl, on):
        """缺陷修复：动作成功即落 UI（换页/轮询都不再是前提），返回耗时 ms。"""
        _t0 = time.perf_counter()
        _k = _ctl2key.get(ctl)
        if _k:
            _apply_port_state(_k, bool(on), False, True)
            _ui_prev[_k + '.chk'] = bool(on)
            _ui_prev[_k + '.en'] = True
        return round((time.perf_counter() - _t0) * 1000.0, 3)

    w._ck_port_after_action = _ck_port_after_action

    def _ck_port_visual(k):
        """自检用只读探针：端口行当前状态文本/按钮/勾选。"""
        _t = port_cards.get(k) or ()
        return {'status': (_t[6].text() if _t else None),
                'btn': (_t[5].text() if _t else None),
                'checked': (_t[5].isChecked() if _t else None)}

    w._ck_port_visual = _ck_port_visual
    # C34h：此时首屏骨架（标题/主按钮/图表占位/端口卡占位）已就位，先亮窗；
    # 余下页面继续构建，可交互时间不再等全量 build_ui。--tray 启动不亮窗，避免托盘模式闪窗。
    try:
        if '--tray' not in sys.argv and not w.isVisible():
            w.show()
            QApplication.processEvents()
            w.repaint()   # 同步首帧：确保占位/骨架真的画出来，而不是只创建了窗口
            _ck_boot_trace('early_show')
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:early-show')
    all_h = QHBoxLayout()
    # C19-C1：并入 aligned_button 组件族（统一 32 高/圆角8/字号13）
    all_on = aligned_button('全部开启', 'primary', min_w=104, h=32)
    all_on.setIcon(QIcon(_glyph_pm('power', 14)))
    all_on.setIconSize(QSize(14, 14))
    all_off = aligned_button('全部关闭', 'danger', min_w=104, h=32)
    all_off.setIcon(QIcon(_glyph_pm('off', 14)))
    all_off.setIconSize(QSize(14, 14))

    def _ports_all(action):
        # C3：未连接门控 + 执行前二次确认 + 失败如实反馈
        if not (conn_state.get('ever_ok') and conn_state.get('last_connected')):
            w._toast.show('未连接充电器，无法批量操作', 'warning')
            return
        if not _confirm('全部%s' % ('开启' if action == 'on' else '关闭'),
                        '确认%s全部端口输出？' % ('开启' if action == 'on' else '关闭')):
            return
        all_on.setEnabled(False)
        all_off.setEnabled(False)
        w._batch_flight = True  # R15-C08：飞行标志，_status_done 置灰同步不干扰
        def _done(r):
            w._batch_flight = False
            all_on.setEnabled(True)
            all_off.setEnabled(True)
            _code = r[0] if isinstance(r, tuple) else 'ERR'
            _d = r[1] if isinstance(r, tuple) and len(r) > 1 else None
            if _code == 200 and (not isinstance(_d, dict) or _d.get('ok', True)):
                # 缺陷修复：批量动作成功同样立即落 UI（四卡同步），再拉真实状态兜底
                try:
                    for _k in PORT_NAMES:
                        _apply_port_state(_k, action == 'on', False, True)
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:ports-all-after')
                poll_status()
                w._toast.show('已%s全部端口' % ('开启' if action == 'on' else '关闭'), 'ok')
            else:
                w._toast.show('%s失败（HTTP %s）' % ('开启' if action == 'on' else '关闭', _code),
                              'warning')
        qwait(lambda: api('POST', '/api/port',
                          {'port': 'all', 'action': action, 'client': '界面按钮'}, timeout=CK_T_CMD),
              _done)

    all_on.clicked.connect(lambda: _ports_all('on'))
    all_off.clicked.connect(lambda: _ports_all('off'))

    # R014（领导）：端口调节（充电倒计时，网页 COUNTDOWN_PIIDS 1-4→piid9-12）
    # 集成到端口控制页：按钮打开二级页设置
    _CD_PIIDS = {'1': 9, '2': 10, '3': 11, '4': 12}

    def open_countdown():
        dlg = QDialog(w)
        # R021-B1：模态一次性弹窗关闭即释放，多次开关不再累积隐藏 QDialog 对象
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        # R022-B1：在飞请求回调存活门——弹窗已关（WA_DeleteOnClose 后对象删除）时丢弃回调
        _alive = {'v': True}
        dlg.finished.connect(lambda _r: _alive.__setitem__('v', False))
        dlg.setWindowTitle('端口倒计时设置')
        dlg.resize(560, 456)
        _dv = QVBoxLayout(dlg)
        _dv.setContentsMargins(16, 12, 16, 12)
        _dv.setSpacing(10)
        _hdr, _badge = dialog_head('端口倒计时设置')
        _dv.addWidget(_hdr)
        dg = QGridLayout()
        dg.setSpacing(10)
        _dv.addLayout(dg, 1)
        for i, (key, (ctl, name)) in enumerate(PORT_NAMES.items()):
            card = QFrame()
            card.setStyleSheet(
                'QFrame{background:%s;'
                'border:1px solid %s; border-radius:12px;}'
                % (ux.TOKENS['surface_soft'], ux.TOKENS['border_soft']))
            cv = QVBoxLayout(card)
            cv.setContentsMargins(12, 12, 12, 12)
            cv.setSpacing(8)
            head = QLabel(name)
            head.setStyleSheet('font-size:%s; font-weight:700; color:%s;'
                               'border:none; background:transparent;'
                               % (ux.TOKENS['font_card'], PORT_COLORS[key]))
            _cur = settings_cache.get(str(_CD_PIIDS[key]))
            try:
                _curv = int(_cur) if _cur not in (None, '') else 0
            except Exception:
                _curv = 0
            cur_lbl = QLabel('当前：%s' % ('未设置' if _curv <= 0 else '%d 分钟' % _curv))
            cur_lbl.setStyleSheet('font-size:11px; color:%s; border:none; background:transparent;'
                                  % ux.TOKENS['text_muted'])
            spin = _CkSpinBox()
            spin.setRange(0, 999)
            spin.setSuffix(' 分钟')
            spin.setSpecialValueText('关闭（0）')
            chips = QHBoxLayout()
            chips.setSpacing(4)
            for _m in (15, 30, 60, 90, 120, 240):
                _b = QPushButton('%d' % _m)
                _b.setFixedWidth(34)
                _b.setStyleSheet('font-size:11px; padding:3px 1px;')
                _b.clicked.connect(lambda _c=False, mm=_m, sp=spin: sp.setValue(mm))
                chips.addWidget(_b)
            setb = QPushButton('设置')
            setb.setMinimumSize(120, 32)

            # R020-CK-F2：clicked 信号会把 checked=False 作为首个位置参数传入，
            # 若首参是 key 默认值会被覆盖成 False → _CD_PIIDS[False] KeyError（"倒计时用不了"根因）。
            # 首参固定接 checked，业务参数全部走默认值。
            def _apply(_c=False, key=key, sp=spin, cur_lbl=cur_lbl, nm=name):
                val = int(sp.value())

                def _job():
                    try:
                        return api('POST', '/api/set',
                                   {'piid': _CD_PIIDS[key], 'value': val}, timeout=CK_T_CMD)
                    except Exception as e:
                        return ('ERR', str(e))

                def _done(r):
                    if not _alive['v']:
                        return
                    _code = r[0] if isinstance(r, tuple) else 'ERR'
                    _d = r[1] if isinstance(r, tuple) and len(r) > 1 else None
                    if _code == 200 and (not isinstance(_d, dict) or _d.get('ok', True)):
                        cur_lbl.setText('当前：%s' % ('关闭' if val <= 0 else '%d 分钟' % val))
                        settings_cache[str(_CD_PIIDS[key])] = val
                        w._toast.show('%s 倒计时已%s'
                                      % (nm, '关闭' if val <= 0 else '设为 %d 分钟' % val), 'ok')
                    else:
                        # R15-C18：后端已加连接态 409 守卫——未连接明确提示，不再报裸 HTTP
                        if _code == 409:
                            w._toast.show('%s 设备未连接，设置未生效' % nm, 'warning')
                        else:
                            w._toast.show('%s 设置失败（HTTP %s）' % (nm, _code), 'warning')
                qwait(_job, _done)
            setb.clicked.connect(_apply)
            cv.addWidget(head)
            cv.addWidget(cur_lbl)
            cv.addWidget(spin)
            cv.addLayout(chips)
            cv.addWidget(setb)
            dg.addWidget(card, i // 2, i % 2)
        dlg.exec()

    # C19-C1：同族 32 高（原裸按钮约 33–35 高，和相邻按钮不齐）
    cd_b = aligned_button('倒计时设置', 'ghost', min_w=104, h=32)
    cd_b.setIcon(QIcon(_glyph_pm('clock', 14)))
    cd_b.setIconSize(QSize(14, 14))
    cd_b.clicked.connect(open_countdown)
    all_h.addWidget(cd_b)

    # R015（领导）：协议开关整合为一个按钮——弹窗勾选各端口要开放的协议，
    # 走 /api/protocol switches 批量写入
    proto_b = aligned_button('协议开关', 'ghost', min_w=96, h=32)
    proto_b.setIcon(QIcon(_glyph_pm('plug', 14)))
    proto_b.setIconSize(QSize(14, 14))

    def open_proto():
        dlg = QDialog(w)
        # R021-B1：同 open_countdown——关闭即释放
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        # R022-B1：在飞保存回调存活门
        _alive = {'v': True}
        dlg.finished.connect(lambda _r: _alive.__setitem__('v', False))
        dlg.setWindowTitle('协议开关设置')
        dlg.resize(560, 400)
        dv = QVBoxLayout(dlg)
        dv.setContentsMargins(16, 12, 16, 12)
        dv.setSpacing(10)
        _hdr, _badge = dialog_head('协议开关设置')
        dv.addWidget(_hdr)
        dgrid = QGridLayout()
        dgrid.setSpacing(10)
        proto_meta = (('c1', 'C口 1', '1', (('pd', 'PD'), ('pps', 'PPS'), ('ufcs', 'UFCS'))),
                      ('c2', 'C口 2', '2', (('pd', 'PD'), ('pps', 'PPS'), ('ufcs', 'UFCS'))),
                      ('c3', 'C口 3', '3', (('scp', 'SCP'), ('ufcs', 'UFCS'))),
                      ('a', 'USB-A', '4', (('scp', 'SCP'), ('ufcs', 'UFCS'))))
        _boxes = {}
        # R020-CK-F1：原代码在弹窗构建期引用 _sw0，而 _sw0 只在 _load_sw 回调内定义——
        # 首次点击"协议开关"即 NameError，弹窗打不开（领导报"协议开关用不了"根因）。
        # 现同步回填：优先用上次会话缓存；无缓存时默认全开（与充电器实际全开态一致，防误全关），
        # 随后 _load_sw 以 /api/status 实况纠正勾选。
        _sw_cur = dict(_proto_cache.get('sw') or {})
        for _i, (pk, nm, ck, metas) in enumerate(proto_meta):
            _card = QFrame()
            _card.setStyleSheet('QFrame{background:%s;'
                                'border:1px solid %s; border-radius:12px;}'
                                % (ux.TOKENS['surface_soft'], ux.TOKENS['border_soft']))
            _cv = QVBoxLayout(_card)
            _cv.setContentsMargins(12, 12, 12, 12)
            _cv.setSpacing(8)
            _head = QLabel(nm)
            _head.setStyleSheet('font-size:%s; font-weight:700; color:%s;'
                                'border:none; background:transparent;'
                                % (ux.TOKENS['font_card'], PORT_COLORS[ck]))
            _cv.addWidget(_head)
            _cur = _sw_cur.get(pk) or {}
            _boxes[pk] = {}
            for _pr, _cap in metas:
                _cb = QCheckBox(_cap)
                _cb.setChecked(bool(_cur.get(_pr, True)))
                _cv.addWidget(_cb)
                _boxes[pk][_pr] = _cb
            dgrid.addWidget(_card, _i // 2, _i % 2)
        dv.addLayout(dgrid)
        _hint = QLabel('关闭 PD 后 PPS 也将关闭；保存后即时写入充电器。')
        _hint.setObjectName('sub')
        _hint.setWordWrap(True)
        dv.addWidget(_hint)
        _saveb = QPushButton('保存协议设置')
        _saveb.setMinimumSize(120, 32)
        _pbrow = QHBoxLayout()
        _pbrow.setSpacing(8)
        _pbrow.addStretch(1)
        _pclose = QPushButton('关闭')
        _pclose.setObjectName('ghost')
        _pclose.setMinimumSize(96, 32)
        _pclose.clicked.connect(dlg.reject)
        _pbrow.addWidget(_pclose)
        _pbrow.addWidget(_saveb)
        dv.addLayout(_pbrow)

        # R019-CK-S1：现状回填改走 qwait 异步（原同步 GET 冻结 UI）；R020-CK-F1 回填结果落缓存；
        # R020-B2：一次性取数不带 key——key 去重会让"快速关-开弹窗"丢纠正请求
        def _load_sw(r):
            try:
                if isinstance(r, tuple) and r[0] == 200 and isinstance(r[1], dict):
                    _sw0 = r[1].get('protocol_switches') or {}
                    _proto_cache['sw'] = dict(_sw0)
                    for pk, m in _boxes.items():
                        cur = _sw0.get(pk) or {}
                        for pr, cb in m.items():
                            cb.setChecked(bool(cur.get(pr)))
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:5212')
        qwait(lambda: api('GET', '/api/status'), _load_sw)

        def _save_proto():
            _sw = {pk: {pr: cb.isChecked() for pr, cb in m.items()}
                   for pk, m in _boxes.items()}

            def _job():
                try:
                    return api('POST', '/api/protocol',
                               {'switches': _sw}, timeout=8)
                except Exception as e:
                    return ('ERR', str(e))

            def _done(r):
                if not _alive['v']:
                    return
                _ok = isinstance(r, tuple) and r[0] == 200 and (
                    not isinstance(r[1], dict) or r[1].get('ok', True))
                if _ok:
                    w._toast.show('协议开关已保存', 'ok')
                    dlg.accept()
                else:
                    w._toast.show('保存失败（HTTP %s）'
                                  % (r[0] if isinstance(r, tuple) else 'ERR'), 'warning')
            qwait(_job, _done)
        _saveb.clicked.connect(_save_proto)
        dlg.exec()

    proto_b.clicked.connect(open_proto)
    all_h.addWidget(proto_b)
    all_h.addWidget(all_on)
    all_h.addWidget(all_off)
    all_h.addStretch(1)
    # R02（25 轮）：端口页同源滞后提示——状态页之外的四个端口卡同样展示旧值。
    # R03（25 轮）：由底部工具栏右端上移至页面第 0 行（卡顶状态条），与连接条同一视线高度，
    # 且不再与「全部开启/关闭」按钮抢一行。
    lag_port = QLabel('')
    lag_port.setStyleSheet('font-size:11px; font-weight:700; color:#f59e0b;'
                           'padding:2px 4px; border:none; background:transparent;')
    lag_port.hide()
    # 1.5.5：删除顶部「● 蓝牙已认证」状态行——端口控制页只保留四张端口卡，
    # 行 0 让给滞后提示条（仍右对齐，不出高度）。
    grid.addWidget(lag_port, 0, 1, 1, 1, Qt.AlignRight | Qt.AlignVCenter)
    lag_extra.append(lag_port)
    grid.addLayout(all_h, 3, 0, 1, 2)
    # 1.5.5：行 0 只放提示条（通常隐藏，高度 0）；富余高度全部给两行卡片，
    # 四卡随窗口等比放大、等分区域，页面底部不再留空。
    grid.setRowStretch(0, 0)
    grid.setRowStretch(1, 1)
    grid.setRowStretch(2, 1)
    grid.setRowStretch(3, 0)
    # R026（领导）：端口控制页去掉外层滚动区——四卡最小高度合计在 900×700 内可容纳（R-011 无滚动条）
    stack.addWidget(pt)
    # 1.5.2 自检：页面未切到前台时 QStackedWidget 不会给隐藏页布局，
    # 直接量到的是 QWidget 默认 480 高（假数据）。把页与栈暴露给 --selftest-port。
    w._ck_port_page = pt
    w._ck_stack = stack
    w._ck_nav_rail = rail  # 只读钩子：截图/自检同步导航高亮
    w._ck_port_eq = _EqualPortWidths(pt, grid, port_cards)   # 四卡严格等宽

    # 端口详情二级页已删除（CK-4）

    # ===== Tab 3: 充电记录 =====
    rc = QWidget()
    rc.setObjectName('pageRecords')   # R041 / C10
    rcv = QVBoxLayout(rc)
    # R043 / C12：150% 页高 602 时纵向预算超页高（分页条压行），边距/间距各收 4 px
    rcv.setContentsMargins(8, 8, 8, 8)
    rcv.setSpacing(4)
    # C-20 范围切换：今日/昨日/本周/本月/全部
    _flt_qss = 'color:' + TEXT_SUB + '; font-size:12px;'

    def _flt_lbl(text):
        _l = QLabel(text)
        _l.setStyleSheet(_flt_qss)
        return _l

    range_h = QHBoxLayout()
    range_h.addWidget(_flt_lbl('范围：'))
    rec_range = QComboBox()
    rec_range.addItems(['近24小时', '今日', '昨日', '本周', '本月', '全部'])
    rec_range.setFixedHeight(32)   # C20-C3：筛选控件并入 32 高族
    rec_period = ['24h', 'today', 'yesterday', 'week', 'month', 'all']
    # R023-M1：记住上次记录范围（回填先于 poll 连接）
    try:
        rec_range.setCurrentIndex(max(0, min(5, int(_ui_prefs.value('recRange', 0)))))
    except Exception:
        rec_range.setCurrentIndex(0)
    rec_range.currentIndexChanged.connect(
        lambda i: _ui_prefs.setValue('recRange', int(i)))
    range_h.addWidget(rec_range)
    # R06：筛选与排序集中在顶部同一行（范围 / 端口 / 排序）
    range_h.addSpacing(16)
    range_h.addWidget(_flt_lbl('端口：'))
    rec_port = QComboBox()
    rec_port.addItems(['全部', 'C1', 'C2', 'C3', 'A'])
    rec_port.setFixedHeight(32)
    range_h.addWidget(rec_port)
    range_h.addSpacing(16)
    range_h.addWidget(_flt_lbl('排序：'))
    rec_sort = QComboBox()
    rec_sort.addItems(['时间倒序', '时间正序', '电量降序', '时长降序'])
    rec_sort.setFixedHeight(32)
    range_h.addWidget(rec_sort)
    range_h.addStretch(1)
    rcv.addLayout(range_h)
    # C-22 统计汇总卡（/api/energy/stats）
    g_stats = QWidget()
    stv3 = QVBoxLayout(g_stats)
    stv3.setContentsMargins(8, 0, 8, 0)
    stv3.setSpacing(2)
    stat_row = QHBoxLayout()
    stat_row.setSpacing(12)
    stat_kpis = {}
    for key, cap in (('wh', '总电量'), ('sess', '会话数'),
                     ('avg', '均功率 W'), ('peak', '峰功率 W'), ('dur', '总时长')):
        box = QVBoxLayout()
        box.setSpacing(0)
        _ic = QLabel()
        _ic.setObjectName('kpiIcon')
        _ic.setFixedSize(16, 16)
        _ic.setPixmap(_glyph_pm(key, 16, '#7e97b3'))   # C20-A2：5 项 KPI 图形标识
        cap_l = QLabel(cap)
        cap_l.setObjectName('kpiCap')
        cap_l.setStyleSheet('color:' + TEXT_SUB + '; font-size:11px; letter-spacing:0.8px;')
        cap_row = QHBoxLayout()
        cap_row.setSpacing(4)
        cap_row.addStretch(1)
        cap_row.addWidget(_ic)
        cap_row.addWidget(cap_l)
        cap_row.addStretch(1)
        val_l = QLabel('—')
        val_l.setAlignment(Qt.AlignCenter)
        val_l.setStyleSheet('font-size:26px; font-weight:800; color:#38e2ff;')
        box.addWidget(val_l)
        box.addLayout(cap_row)
        stat_row.addLayout(box, 1)
        stat_kpis[key] = val_l
    stv3.addLayout(stat_row)
    _kpi_sep = QFrame()   # C20-B2：KPI 行与下部分隔
    _kpi_sep.setFixedHeight(1)
    _kpi_sep.setStyleSheet('background:rgba(255,255,255,0.10); border:none;')
    stv3.addWidget(_kpi_sep)
    rcv.addWidget(g_stats)

    def _fit_kpis():
        """R031：按列宽自动降字号（150% 下「308时46分」不再顶到右边缘）。"""
        try:
            for _l in stat_kpis.values():
                _aw = max(40, _l.width() - 4)
                _px = 26
                while _px > 16:
                    _f = _l.font()
                    _f.setPixelSize(_px)
                    if QFontMetrics(_f).horizontalAdvance(_l.text()) <= _aw:
                        break
                    _px -= 2
                _l.setStyleSheet('font-size:%dpx; font-weight:800; color:#38e2ff;' % _px)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:fit_kpis')

    # ===== R023/R024：智能分析（设备识别 + 断开原因 + 电压体检 + 日报）=====
    # 1.5.5：整块从记录页迁到「小源 → 智能分析」二级页，按整页宽度三栏卡片重排；
    # 记录页只保留明细表，表格随高度填满。统计口径、按钮与 AI 状态文案不变。
    _ins_pg = QWidget()
    _ins_pv = QVBoxLayout(_ins_pg)
    _ins_pv.setContentsMargins(12, 10, 12, 10)
    _ins_pv.setSpacing(8)

    def _ins_card(_title, _glyph):
        _g = GlassCard(_title)
        _g.set_glyph(_glyph)
        _v = QVBoxLayout(_g)
        _v.setContentsMargins(12, 16, 12, 12)
        _v.setSpacing(6)
        _l = QLabel('等待记录数据…')
        _l.setObjectName('sub')
        _l.setWordWrap(True)
        _l.setTextFormat(Qt.RichText)
        _l.setOpenExternalLinks(False)
        _l.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        _v.addWidget(_l, 1)
        return _g, _l

    _ins_cards = QHBoxLayout()
    _ins_cards.setSpacing(10)
    # 1.5.7：设备卡改为「1 行总数 + 固定 5 行设备 + 1 行待确认」的定高结构，
    # 名称超长按行省略并悬浮全名——0 条/满载/超长名都不改卡片几何。
    _ins_dev_card = GlassCard('设备识别')
    _ins_dev_card.set_glyph('scan')
    _devcv = QVBoxLayout(_ins_dev_card)
    _devcv.setContentsMargins(12, 16, 12, 12)
    _devcv.setSpacing(6)
    ins_dev_lbl = QLabel('等待记录数据…')
    ins_dev_lbl.setObjectName('sub')
    ins_dev_lbl.setTextFormat(Qt.RichText)
    ins_dev_lbl.setOpenExternalLinks(False)
    ins_dev_lbl.setAlignment(Qt.AlignLeft | Qt.AlignTop)
    ins_dev_lbl.setFixedHeight(26)
    _devcv.addWidget(ins_dev_lbl)
    # 1.5.9：设备多于一屏时卡内可滚（视口锁定 5 行高，几何不随设备数变化）
    ins_dev_rows = []
    ins_dev_body = QWidget()
    ins_dev_body.setObjectName('devrows')
    _devbox = QVBoxLayout(ins_dev_body)
    _devbox.setContentsMargins(0, 0, 4, 0)
    _devbox.setSpacing(4)
    _devbox.addStretch(1)
    ins_dev_scroll = _CardScroll()
    ins_dev_scroll.setObjectName('devScroll')
    ins_dev_scroll.setWidget(ins_dev_body)
    _row_h = _DevRow(13).height()
    ins_dev_scroll.setFixedHeight(_row_h * 5 + 4 * 4 + 2)
    _devcv.addWidget(ins_dev_scroll)

    def _fill_dev_rows(_items):
        _items = list(_items or [])
        while len(ins_dev_rows) > len(_items):
            _rw = ins_dev_rows.pop()
            _devbox.removeWidget(_rw)
            _rw.setParent(None)
            _rw.deleteLater()
        while len(ins_dev_rows) < len(_items):
            _rw = _DevRow(13)
            _rw.setObjectName('sub')
            # 始终插在末尾 stretch 之前：不依赖 ins_dev_rows 与布局槽位严格同步
            _devbox.insertWidget(max(_devbox.count() - 1, 0), _rw)
            ins_dev_rows.append(_rw)
        for _rw, (_n1, _c1) in zip(ins_dev_rows, _items):
            _rw.set(_n1, _c1)
    ins_dev_link = QLabel('')
    ins_dev_link.setObjectName('sub')
    ins_dev_link.setTextFormat(Qt.RichText)
    ins_dev_link.setOpenExternalLinks(False)
    ins_dev_link.setFixedHeight(20)
    ins_dev_link.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    _devcv.addWidget(ins_dev_link)
    _devcv.addStretch(1)
    _ins_reason_card, ins_reason_lbl = _ins_card('断开原因分布', 'list')
    _ins_volt_card, ins_volt_lbl = _ins_card('电压体检', 'load')
    for _card in (_ins_dev_card, _ins_reason_card, _ins_volt_card):
        _ins_cards.addWidget(_card, 1)
    _ins_pv.addLayout(_ins_cards)
    # 1.5.5：设备级综合分析（按箱：电池/健康 + 充电习惯 + 口径，支持时间范围切换）
    # 1.5.5 追加：多设备改为顶部下拉切换，一次只渲染选中设备，整块不出现纵向滚动条。
    # 1.5.8：这一行重做——左「标题 + 口径?」，右固定宽「设备步进条 + 三段范围」，
    # 与卡片同一栅格（左右边距 0、行高 32、间距 8）。范围不再用 QComboBox：
    # 下拉浮层会压住下方卡片，且展开态与页内控件不是同一套高度/圆角。
    _ins_prof_hdr = QHBoxLayout()
    _ins_prof_hdr.setContentsMargins(0, 0, 0, 0)
    _ins_prof_hdr.setSpacing(8)
    _prof_title = QLabel('设备级综合分析')
    _prof_title.setFixedHeight(32)
    _prof_title.setStyleSheet('font-weight:600;color:#cfe3f5; font-size:13.5px;'
                              ' background:transparent;')
    # 1.5.5：成段口径说明收进「?」，卡内只留两栏正文，保证单设备一屏放完
    _PROF_GLOSS = (
        '口径：满充占比（估算）=「设备充满」或充入量 ≥ 该设备历史最大充入 90% 的会话 ÷ 该箱会话；'
        '充满后由自动断电/拔线结束的会话同样计满充，不以自动断电为唯一判据；循环≈满充次数；'
        '趋势=后一半会话平均峰值相对前一半；时段按会话起始小时；夜间=22:00–06:00；'
        '偏好端口=会话端口占比最高者；惯用方式按偏好端口与平均功率（<15 W 记小电流）判定，'
        '样本 <3 条或偏好占比 <60% 时不给结论；本机会话未记录起始电量与涓流段，'
        '故「低电量才充」「涓流时长」标数据不足。')
    _prof_badge = ux.info_badge('设备级综合分析口径', _PROF_GLOSS)
    # 1.5.7：设备切换改为页内选项条（‹ 名称 n/N › + 设备列表），不用下拉浮层。
    ins_dev_sel = _DevPicker(h=32)
    ins_dev_sel.changed.connect(lambda _k='': _pick_profile())
    ins_dev_sel.setToolTip('选择要查看的设备（固定组置顶，0 条也列出）')
    ins_range = _SegChoice(['近 7 天', '近 30 天', '全部'], seg_w=58, h=32)
    ins_range.setToolTip('统计时间范围（只改数值，不影响固定组是否列出）')
    ins_range.currentIndexChanged.connect(lambda _i: _update_insight())
    _ins_prof_hdr.addWidget(_prof_title)
    _ins_prof_hdr.addWidget(_prof_badge)
    _ins_prof_hdr.addStretch(1)
    _ins_prof_hdr.addWidget(ins_dev_sel)
    _ins_prof_hdr.addWidget(ins_range)
    _ins_pv.addLayout(_ins_prof_hdr)
    # 单设备内容直接铺在页内（不再套 QScrollArea：一屏必须看完，不允许滚动条）
    _ins_host = QWidget()
    _ins_host_v = QVBoxLayout(_ins_host)
    _ins_host_v.setContentsMargins(0, 0, 0, 0)
    _ins_host_v.setSpacing(10)
    _ins_pv.addWidget(_ins_host, 1)
    _prof_state = {'key': ''}
    # 未归箱会话在状态页给出「待确认 N 条 · 手动归箱」入口
    ins_dev_link.linkActivated.connect(lambda _u='': _open_device_groups())

    def _open_cluster():
        """按曲线看分箱形态（只预览；命名后才落库，不改现有归属）。"""
        items, err = load_cluster_items()
        if err or not items:
            w._toast.show('曲线数据不可用：%s' % (err or '暂无可聚类的充电会话'), 'warn')
            return
        BinClusterDialog(w, adv_prefs, items=items,
                         on_applied=_update_insight).exec()

    # 1.5.9：动作区固定一行——4 个按钮（140×34）+ 右侧 2 个问号，右缘对齐；
    # 「充电日报」不再占位（日报入口保留在命令面板）。
    ins_actions = QHBoxLayout()
    ins_actions.setContentsMargins(0, 0, 0, 0)
    ins_actions.setSpacing(8)
    ins_view_b = aligned_button('设备与断开原因', 'ghost', min_w=140)
    ins_diag_b = aligned_button('AI 诊断结论', 'ghost', min_w=140)
    ins_diag_b.clicked.connect(lambda _c=False: DiagnosisDialog(
        w, adv_prefs, adv_secrets, _last_rec.get('rows') or [],
        _last_rec.get('analysis')).exec())
    ins_cluster_b = aligned_button('按曲线看分箱', 'ghost', min_w=140)
    ins_cluster_b.clicked.connect(lambda _c=False: _open_cluster())
    ins_rules_b = aligned_button('断开规则', 'ghost', min_w=140)
    ins_rules_b.clicked.connect(lambda _c=False: EndRulesDialog(
        w, lambda _m, _lv='info': w._toast.show(_m, _lv),
        _update_insight).exec())
    for _ab in (ins_view_b, ins_diag_b, ins_cluster_b, ins_rules_b):
        _ab.setFixedSize(140, 34)
        ins_actions.addWidget(_ab)
    ins_actions.addStretch(1)
    ins_actions.addWidget(ux.help_badge('deviceSensitivity', w, size=34))
    ins_actions.addWidget(ux.help_badge('aiEnabled', w, size=34))
    ins_actions.setAlignment(Qt.AlignVCenter)
    _ins_actions_wrap = QWidget()
    _ins_actions_wrap.setLayout(ins_actions)
    _ins_actions_wrap.setFixedHeight(34)
    _ins_pv.addWidget(_ins_actions_wrap)
    ins_ai_state = QLabel('')
    ins_ai_state.setObjectName('sub')
    ins_ai_state.setWordWrap(True)
    _ins_pv.addWidget(ins_ai_state)

    def _ai_state():
        _cfg = ux.ai_config(adv_prefs, adv_secrets)
        _on = bool(adv_prefs.get('aiEnabled')) and adv_prefs.consent_ok()
        _has = bool(_cfg.get('key')) or str(_cfg.get('provider')) == '本地端点'
        if _on and _has:
            return 'AI 已就绪（%s · %s）：可在左下角「小源」中使用；离线自动降级规则引擎。' % (
                _cfg.get('provider'), _cfg.get('model'))
        if not adv_prefs.consent_ok():
            return 'AI 未启用：尚未同意隐私说明。在左下角「小源 → AI 配置」完成同意与配置。'
        if not _on:
            return 'AI 未启用（左下角「小源 → AI 配置」开启）。本地规则分析不受影响。'
        return 'AI 已开启但未配置 Key：在左下角「小源 → AI 配置」填写；当前使用本地规则引擎。'

    ins_ai_state.setText(_ai_state())
    _last_rec = {'rows': [], 'analysis': None, 'stats': {}}

    def _update_insight():
        rows = _last_rec.get('rows') or []
        # 灵敏度决定归箱阈值；已保存分组时只做就近归箱，箱名保持稳定
        _sens = str(adv_prefs.get('deviceSensitivity') or '均衡')
        _model = ux.device_model_load(adv_prefs)
        # 1.5.7：形态数据一并参与归属，保证与「设备分组管理」条数同口径（未归箱数一致）
        _shp = _last_rec.get('shapes') or {}
        if _last_rec.get('shapes_n') != len(rows):
            _raw = load_shapes([r.get('id') for r in rows]) if rows else {}
            _shp = {ux.session_key(r): _raw[r['id']] for r in rows
                    if r.get('id') in _raw}
            _last_rec['shapes'] = _shp
            _last_rec['shapes_n'] = len(rows)
        an = ux.analyze_sessions(rows, int(adv_prefs.get('mergeGap') or 120),
                                 _sens, _model, _shp)
        _last_rec['analysis'] = an
        if not rows:
            ins_dev_lbl.setText('暂无会话记录')
            _fill_dev_rows([])
            ins_dev_link.setText('<span style="color:#7f93ad">完成一次充电后自动分析</span>')
            ins_reason_lbl.setText('—')
            ins_volt_lbl.setText('—')
            return
        from collections import Counter
        sus = [c for c in an['clusters'] if c.get('suspect')]
        _hint_on = bool(adv_prefs.get('aiDeviceHint', True))
        _un = int(an.get('unassigned') or 0)
        cnt = Counter(r['label'] for r in an['reasons'])
        # 设备卡：总数 + 每个箱一行（箱名统一口径），未归箱入口保持原行为
        # 1.5.7：固定组始终置顶（按用户定义顺序），0 条也列出并标「暂无记录」；
        #        设备总数如实统计固定组在内（自动箱里没有的固定组按 0 条补进来）。
        _names = ux.device_names(_model)
        _boxes = [b for b in ((_model or {}).get('boxes') or []) if isinstance(b, dict)]
        _fixed = [b for b in _boxes if ux.group_is_fixed(b)]
        _fixids = {str(b.get('id') or '') for b in _fixed}
        _by_id = {str(c.get('id') or ''): c for c in an['clusters']}
        _total = len(an['clusters']) + len(_fixids - set(_by_id))
        _dv = ['<span style="color:#38e2ff;font-weight:600">设备总数</span>&nbsp;&nbsp;'
               '<span style="color:#eaf6ff;font-size:18px;font-weight:600">%d</span>'
               % _total
               + (('&nbsp;&nbsp;<span style="color:#7f93ad">疑似同设备 %d</span>'
                   % len(sus)) if _hint_on and sus else '')]
        _ins_dev_card.setToolTip(
            '设备总数 = 自动识别箱 %d + 固定组 %d（固定组 0 条也列出）。'
            % (len(an['clusters']), len(_fixed)))
        _seq = [('fix', b) for b in _fixed]
        _seq += [('auto', c) for c in sorted(
            an['clusters'], key=lambda x: -len(x.get('items') or []))
            if str(c.get('id') or '') not in _fixids]
        _lines = []
        for _kind, _o in _seq:
            if _kind == 'fix':
                _cid = str(_o.get('id') or '')
                _nm = _o.get('name') or _names.get(_cid, '')
                _n0 = len((_by_id.get(_cid) or {}).get('items') or [])
                _cnt = '%d 次' % _n0
            else:
                _cid = str(_o.get('id') or '')
                _nm = _o.get('name') if _o.get('name') not in (None, _cid) \
                    else _names.get(_cid, '')
                _cnt = '%d 次' % len(_o.get('items') or [])
            _lines.append((ux.device_box_name(_cid, _nm), _cnt))
        # 1.5.9：全部设备都列出，超出一屏时在卡内滚动（视口固定 5 行高）
        _fill_dev_rows(_lines)
        if _un:
            ins_dev_link.setText('<a href="devgroups" '
                                 'style="color:#8fd6ff;text-decoration:none">'
                                 '待确认 %d 条 · 手动归箱</a>' % _un)
        else:
            ins_dev_link.setText('<span style="color:#7f93ad">待确认 0 条 · 全部已归箱</span>')
        # 断开原因卡：按次数降序，一行一条；最多 7 行，其余归并为「其他 N 类」
        _cm = cnt.most_common(10)
        if len(_cm) > 7:
            _tail = _cm[7:]
            _cm = _cm[:7] + [('其他 %d 类' % len(_tail),
                              sum(_v for _kk, _v in _tail))]
        _rv = ['<span style="color:#38e2ff;font-weight:600">%s</span>&nbsp;&nbsp;'
               '<span style="color:#cfe3f5">×%d</span>' % (k, v)
               for k, v in _cm]
        # 电压体检（体系化指标上屏）
        _vs = [float(r.get('avg_voltage') or 0) for r in rows if float(r.get('avg_voltage') or 0) > 0]
        if _vs:
            _mean = sum(_vs) / len(_vs)
            _rip = (max(_vs) - min(_vs)) / max(_mean, 1e-6) * 100
            _under = sum(1 for x in _vs if x < 4.5)
            _over = sum(1 for x in _vs if x > 21.0)
            _vv = ['<span style="color:#38e2ff;font-weight:600">均值</span>&nbsp;&nbsp;'
                   '<span style="color:#eaf6ff;font-size:18px;font-weight:600">%.1f V</span>'
                   % _mean,
                   '波动&nbsp;&nbsp;%.1f%%' % _rip,
                   '欠压&nbsp;&nbsp;%d 次' % _under,
                   '过压&nbsp;&nbsp;%d 次' % _over]
        else:
            _vv = ['样本不足（需要含电压的历史会话）']
        # 1.5.5：三块信息分区到三张卡片（原单段富文本拆开），值用次级色
        ins_dev_lbl.setText(_dv[0])
        ins_reason_lbl.setText('<br>'.join(_rv) or '—')
        ins_volt_lbl.setText('<br>'.join(_vv))
        _update_profiles(rows, an)

    def _pick_profile():
        """切换设备：记住选中键并重渲（数据在内存里，切换即刷新）。"""
        _k = ins_dev_sel.current_key()
        if _k:
            _prof_state['key'] = str(_k)
            _update_insight()

    def _update_profiles(rows, an):
        """设备级综合分析：按箱汇总电池/健康与充电习惯；算不出的写「数据不足」。

        1.5.5：设备多于一台时用顶部下拉切换，一次只渲染选中那一台，
        整块在页宽/页高内放完，不出现纵向滚动条。
        """
        while _ins_host_v.count():
            _it = _ins_host_v.takeAt(0)
            _w0 = _it.widget()
            if _w0 is not None:
                # 先脱离父级再排队销毁：deleteLater 只在回到事件循环后才真正删除，
                # 连续刷新时旧卡片会与新卡片叠在同一位置（1.5.5 截图实测重叠）。
                _w0.setParent(None)
                _w0.deleteLater()
        _days = (7, 30, 0)[max(0, ins_range.currentIndex())]
        _cut = (time.time() - _days * 86400) if _days else 0
        _bid = {}
        for _c in (an.get('clusters') or []):
            for _it in (_c.get('items') or []):
                for _sid in (_it.get('_ids') or [_it.get('id')]):
                    try:
                        _bid[int(_sid)] = str(_c.get('id') or '')
                    except (TypeError, ValueError):
                        pass
        _rlab = {}
        for _it, _r0 in zip(an.get('sessions') or [], an.get('reasons') or []):
            for _sid in (_it.get('_ids') or [_it.get('id')]):
                try:
                    _rlab[int(_sid)] = str(_r0.get('label') or '')
                except (TypeError, ValueError):
                    pass
        _cname = {str(c.get('id') or ''): str(c.get('name') or '')
                  for c in (an.get('clusters') or [])}
        _boxes = [b for b in ((ux.device_model_load(adv_prefs) or {}).get('boxes') or [])
                  if isinstance(b, dict)]
        _fixed = [b for b in _boxes if ux.group_is_fixed(b)]
        _all = {str(b.get('id') or ''): str(b.get('name') or '') for b in _boxes}
        _buckets = {}
        for _s in rows:
            if _cut and float(_s.get('start_time') or 0) < _cut:
                continue
            _k = _bid.get(int(_s.get('id') or 0))
            if _k:
                _buckets.setdefault(_k, []).append(_s)
        # 1.5.7：固定组无条件占位（0 条也保留），时间筛选只影响数值、不删条目
        _fixids = [str(b.get('id') or '') for b in _fixed]
        for _fid in _fixids:
            _buckets.setdefault(_fid, [])
        if not _buckets:
            ins_dev_sel.set_items([], '')
            ins_dev_sel.setEnabled(False)
            _prof_state['key'] = ''
            _none = QLabel('暂无可用设备画像：所选时间范围内没有已归箱的会话。')
            _none.setObjectName('sub')
            _none.setWordWrap(True)
            _ins_host_v.addWidget(_none)
            _ins_host_v.addStretch(1)
            return
        # 设备选择器：固定组置顶（按用户定义顺序），其余按会话数降序
        _keys = _fixids + [k for k, _v in sorted(_buckets.items(),
                                                 key=lambda x: -len(x[1]))
                           if k not in _fixids]
        _cur = _prof_state.get('key') or ''
        if _cur not in _keys:
            _cur = _keys[0]
        _prof_state['key'] = _cur
        ins_dev_sel.set_items(
            [(k, ux.device_box_name(k, _cname.get(k) or _all.get(k, '')))
             for k in _keys], _cur)
        ins_dev_sel.setEnabled(True)

        def _avg(vals):
            _v = [float(x) for x in vals if x not in (None, '')]
            return (sum(_v) / len(_v)) if _v else 0.0
        for _k in [_cur]:
            _ss = _buckets[_k]
            _n = len(_ss)
            _hist = [0] * 24
            for _s in _ss:
                try:
                    _hist[time.localtime(float(_s.get('start_time') or 0)).tm_hour] += 1
                except (TypeError, ValueError, OSError):
                    pass
            # 1.5.5：满充按估算口径——「设备充满」或充入量 ≥ 该设备历史最大充入 90%
            # （充满后由自动断电/人工拔线结束的会话也算满充，不再只看断开原因）
            _whs = [float(_s.get('total_wh') or 0) for _s in _ss]
            _whmax = max(_whs) if _whs else 0.0

            def _is_full(_s):
                if _rlab.get(int(_s.get('id') or 0)) == '设备充满':
                    return True
                return _whmax > 0 and float(_s.get('total_wh') or 0) >= _whmax * 0.9

            _full = sum(1 for _s in _ss if _is_full(_s))
            _night = sum(_hist[22:]) + sum(_hist[:6])
            _volts = [float(_s.get('avg_voltage') or 0) for _s in _ss
                      if float(_s.get('avg_voltage') or 0) > 0]
            _ss_sorted = sorted(_ss, key=lambda s: float(s.get('start_time') or 0))
            _peaks = [float(_s.get('peak_power_w') or 0) for _s in _ss_sorted]
            _trend = '数据不足'
            if len(_peaks) >= 4:
                _h = len(_peaks) // 2
                _p1 = _avg(_peaks[:_h])
                _p2 = _avg(_peaks[_h:])
                if _p1 > 0:
                    _trend = '%+.0f%%（前 %d 次→后 %d 次峰值均值）' % (
                        (_p2 - _p1) / _p1 * 100, _h, len(_peaks) - _h)
            _mx = max(_hist) or 1
            _sp = ''.join(('▁▂▃▄▅▆▇█'[min(7, int(_h0 / _mx * 7.99))] if _h0 else '·')
                          for _h0 in _hist)
            _top = sorted(range(24), key=lambda h: -_hist[h])[:3]
            _top_pct = sum(_hist[h] for h in _top) / max(1, _n) * 100
            # 1.5.5：偏好端口 / 惯用充电方式 / 建议充电方式（口径写清，样本不足不编结论）
            _PNAME = {1: 'C1', 2: 'C2', 3: 'C3', 4: 'A'}
            _pc = {}
            for _s in _ss:
                try:
                    _p0 = int(_s.get('port') or 0)
                except (TypeError, ValueError):
                    continue
                _pc[_p0] = _pc.get(_p0, 0) + 1
            if _pc:
                _tp, _tpc = max(_pc.items(), key=lambda kv: kv[1])
                _tpr = round(_tpc / max(1, _n) * 100)
                _pref = '常用 %s 口 %d%%（%d/%d 次）' % (
                    _PNAME.get(_tp, '—'), _tpr, _tpc, _n)
            else:
                _tp, _tpr, _pref = 0, 0, '样本不足（0 条）'
            _avgp = _avg([_s.get('avg_power_w') for _s in _ss])
            _peak_avg = _avg([_s.get('peak_power_w') for _s in _ss])
            if _n < 3:
                _habit = '样本不足（%d 条）' % _n
                _advice = '样本不足（%d 条），暂不给出建议' % _n
                _why = '至少 3 条会话才能推断惯用方式'
            elif _tpr < 60:
                _habit = '无稳定偏好（最高 %s 口 %d%%）' % (_PNAME.get(_tp, '—'), _tpr)
                _advice = '维持自动协商；如需固定可手动指定端口'
                _why = '历史会话端口较分散，未形成 ≥60% 的偏好'
            elif _tp == 4 and _avgp < 15:
                _habit = 'A 口小电流（均 %.1f W，峰值 %.1f W）· 依据 %d 条会话' % (
                    _avgp, _peak_avg, _n)
                _advice = '建议 A 口小电流，峰值约 %.0f W' % _peak_avg
                _why = '该设备 %d 条会话中 %d%% 走 A 口且平均功率 <15 W' % (_n, _tpr)
            else:
                _habit = '%s 口%s（均 %.1f W，峰值 %.1f W）· 依据 %d 条会话' % (
                    _PNAME.get(_tp, '—'), '小电流' if _avgp < 15 else '常规/快充',
                    _avgp, _peak_avg, _n)
                _advice = '建议 %s 口充电，峰值约 %.0f W' % (_PNAME.get(_tp, '—'), _peak_avg)
                _why = '该设备 %d 条会话中 %d%% 走 %s 口' % (
                    _n, _tpr, _PNAME.get(_tp, '—'))
            _evt = [e for e in _CK_RULE_EVENTS
                    if str(e.get('device_key') or '') == _k]
            _evt_txt = ''
            if _evt:
                _e0 = _evt[-1] or {}
                try:
                    _et = time.strftime('%m-%d %H:%M',
                                        time.localtime(float(_e0.get('ts') or 0)))
                except (TypeError, ValueError, OSError):
                    _et = '—'
                if str(_e0.get('event')) == 'auto_hold_end':
                    _evt_txt = '自动切换留痕：%s 结束（%s）' % (
                        _et, _e0.get('end_reason') or '到期')
                else:
                    try:
                        _until = time.strftime('%H:%M', time.localtime(
                            float(_e0.get('until') or 0)))
                    except (TypeError, ValueError, OSError):
                        _until = '—'
                    _evt_txt = '自动切换留痕：%s A 口切小电流（保持至 %s）' % (_et, _until)
            _volt_txt = ('%.1f–%.1f V' % (min(_volts), max(_volts))) if _volts else '数据不足'
            # 1.5.6：补真实可算指标，两栏各 8 项自然等高（不靠拉伸）
            _durs = [float(_s.get('duration_sec') or 0) for _s in _ss]
            _longest = (max(_durs) / 60.0) if _durs else 0.0
            _full_d = [float(_s.get('duration_sec') or 0) / 60.0
                       for _s in _ss if _is_full(_s)]
            _full_avg = (sum(_full_d) / len(_full_d)) if _full_d else None
            _starts = [float(_s.get('start_time') or 0) for _s in _ss
                       if float(_s.get('start_time') or 0) > 0]
            _span_d = (max(_starts) - min(_starts)) / 86400.0 if _starts else 0.0
            _per_day = _n / _span_d if _span_d >= 1.0 else float(_n)
            # 1.5.5：两栏改为真实网格布局（每行一个 QLabel），
            # 富文本表格在窄卡里会把 58% 栏撑出卡片右缘造成截字，这里交给布局换行。
            _rows_l = [('电池与健康', None),
                       ('电压区间', _volt_txt),
                       ('满充占比（估算）', '%d%%（%d/%d）' % (
                           round(_full / max(1, _n) * 100), _full, _n)),
                       ('循环估算', '约 %d 次' % _full),
                       ('峰值趋势', _trend),
                       ('平均充电功率', '%.1f W' % _avgp),
                       ('最长连续充电', '%.0f 分' % _longest),
                       ('满充平均耗时', ('%.0f 分' % _full_avg)
                        if _full_avg is not None else '数据不足'),
                       ('日均充电次数', '%.1f 次/天' % _per_day)]

            _rows_r = [('充电习惯', None),
                       ('常用时段', '%s 点（%d%%）' % (
                           '/'.join(str(h) for h in _top), round(_top_pct))),
                       ('时段分布', '<span style="font-family:Consolas,monospace">%s</span>'
                        % _sp),
                       ('单次平均时长', '%.0f 分' % (
                           _avg([_s.get('duration_sec') for _s in _ss]) / 60.0)),
                       ('平均充入', '%.1f Wh' % _avg([_s.get('total_wh') for _s in _ss])),
                       ('夜间占比', '%d%%（22–6 点）' % round(_night / max(1, _n) * 100)),
                       ('偏好端口', _pref),
                       ('惯用方式', _habit),
                       ('建议方式', _advice)]

            def _fill(lay, rows):
                for _k0, _v0 in rows:
                    if _v0 is None:
                        _t = QLabel('<b>%s</b>' % _k0)
                        _t.setStyleSheet('color:#cfe3f5;')
                    else:
                        _t = QLabel('%s&nbsp;&nbsp;%s' % (
                            ('<b>%s</b>' % _k0) if _k0 == '建议方式' else _k0, _v0))
                    _t.setObjectName('sub')
                    _t.setTextFormat(Qt.RichText)
                    _t.setWordWrap(True)
                    lay.addWidget(_t)

            # 1.5.5：会话数与时间范围移到卡标题，理由收进悬停——正文压到能一屏放完
            _card = GlassCard('%s · %s' % (
                ux.device_box_name(_k, _cname.get(_k) or _all.get(_k, '')),
                '暂无记录' if _n == 0 else ('会话 %d 次' % _n)))
            _card.setToolTip('时间范围：%s<br>建议理由：%s%s'
                             % (ins_range.currentText(), _why,
                                ('<br>' + _evt_txt) if _evt_txt else ''))
            _cv = QVBoxLayout(_card)
            _cv.setContentsMargins(12, 10, 12, 8)
            _cols = QHBoxLayout()
            _cols.setSpacing(18)
            _col_l = QVBoxLayout()
            _col_l.setSpacing(3)
            _col_r = QVBoxLayout()
            _col_r.setSpacing(3)
            _fill(_col_l, _rows_l)
            _fill(_col_r, _rows_r)
            # 不加尾部 stretch：卡片吃满高度后由各行均分剩余空间，左右两栏同高填满
            _cols.addLayout(_col_l, 42)
            _cols.addLayout(_col_r, 58)
            _cv.addLayout(_cols, 1)
            # 卡片吃掉剩余高度：左右两栏同高、正文填满整页，不在底部留空
            _ins_host_v.addWidget(_card, 1)

    def _dev_analysis():
        """与状态页同一口径（灵敏度 + 已保存分组 + 曲线形态）。"""
        return ux.analyze_sessions(
            _last_rec.get('rows') or [], int(adv_prefs.get('mergeGap') or 120),
            str(adv_prefs.get('deviceSensitivity') or '均衡'),
            ux.device_model_load(adv_prefs), _last_rec.get('shapes') or {})

    def _open_device_view():
        an = _last_rec.get('analysis') or _dev_analysis()
        dlg = QDialog(w)
        dlg.setWindowTitle('设备识别与断开原因（本地规则引擎）')
        dlg.resize(820, 560)
        vv = QVBoxLayout(dlg)
        vv.setContentsMargins(16, 16, 16, 16)
        vv.setSpacing(12)
        # R030：两张表改为子标签，避免纵向挤压后被裁切、横向留黑边
        # C34-UI：与设置 / 小源页对齐——同一 objectName、无滚动箭头、右省略、14px 线稿图标
        tabs = QTabWidget()
        tabs.setObjectName('setTabs')
        tabs.setUsesScrollButtons(False)
        tabs.setElideMode(Qt.ElideRight)
        tabs.setDocumentMode(True)
        cl = an.get('clusters') or []
        p1 = QWidget()
        l1 = QVBoxLayout(p1)
        l1.setContentsMargins(12, 12, 12, 12)
        l1.setSpacing(8)
        l1.addWidget(QLabel('设备列表（ID 本地匿名生成，不上传可识别信息）'))
        # C28：先给一行分布摘要，25 个设备不用逐行数就知道有多少待确认/高置信
        _n_sus = sum(1 for c in cl if c.get('suspect'))
        _n_single = sum(1 for c in cl if c.get('single'))
        _n_hi = sum(1 for c in cl if (not c.get('single')) and (not c.get('suspect'))
                    and float(c.get('confidence') or 0) >= 0.82)
        _sum1 = QLabel(
            ('共 %d 个 · 疑似同设备 %d · 高置信归并 %d · 仅 1 次会话 %d'
             % (len(cl), _n_sus, _n_hi, _n_single)) if cl else '暂无设备归并结果。')
        _sum1.setObjectName('sub')
        _sum1.setWordWrap(True)
        l1.addWidget(_sum1)
        t1_ = QTableWidget(0, 5)
        t1_.setHorizontalHeaderLabels(['设备', '会话数', '峰值 W', '置信度', '判定理由'])
        t1_.verticalHeader().setVisible(False)
        t1_.setRowCount(len(cl))
        _hh = t1_.horizontalHeader()
        _hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for _c in range(4):
            _hh.setSectionResizeMode(_c, QHeaderView.ResizeToContents)
        _hh.setSectionResizeMode(4, QHeaderView.Stretch)
        # C28：数值列右对齐（表头同步），让 31/49/0.79 的个位竖向成列
        for _c in (1, 2, 3):
            _it = t1_.horizontalHeaderItem(_c)
            if _it is not None:
                _it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for i, c in enumerate(cl):
            p = max((float(x.get('peak_power_w') or 0) for x in c['items']), default=0)
            t1_.setItem(i, 0, QTableWidgetItem(
                ux.device_box_name(c.get('id'), c.get('name'))))
            t1_.setItem(i, 1, QTableWidgetItem(str(len(c['items']))))
            t1_.setItem(i, 2, QTableWidgetItem('%.0f' % p))
            t1_.setItem(i, 3, QTableWidgetItem('%.2f' % c['confidence']))
            t1_.setItem(i, 4, QTableWidgetItem(
                ('暂无记录（0 条，可在设备分组管理迁入）' if c.get('empty') else
                 ('仅 1 次会话，无法比对' if c.get('single') else
                  ('疑似同一设备，可手动确认/拆分' if c.get('suspect') else
                   ('高置信自动归并' if c['confidence'] >= 0.82 else '不归并（特征差异大）'))))))
            for _c in (1, 2, 3):
                _it = t1_.item(i, _c)
                if _it is not None:
                    _it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        l1.addWidget(t1_, 1)
        tabs.addTab(p1, '设备列表（%d）' % len(cl))
        p2 = QWidget()
        l2 = QVBoxLayout(p2)
        l2.setContentsMargins(12, 12, 12, 12)
        l2.setSpacing(8)
        l2.addWidget(QLabel('断开原因（可解释，依据会话元数据与末端曲线）'))
        t2_ = QTableWidget(0, 3)
        t2_.setHorizontalHeaderLabels(['会话', '断开原因', '证据'])
        t2_.verticalHeader().setVisible(False)
        rs = an.get('reasons') or []
        # C28：245 条平铺时先给分布摘要（前 3 类 + 其他），避免逐条翻找
        _rc = {}
        for _r in rs:
            _rc[_r['label']] = _rc.get(_r['label'], 0) + 1
        _top = sorted(_rc.items(), key=lambda kv: -kv[1])[:3]
        _other = len(rs) - sum(n for _k, n in _top)
        _parts = ['%s %d' % (k, n) for k, n in _top]
        if _other > 0:
            _parts.append('其他 %d' % _other)
        _sum2 = QLabel(('共 %d 条 · ' % len(rs)) + ' · '.join(_parts) if rs
                       else '暂无断开原因记录。')
        _sum2.setObjectName('sub')
        _sum2.setWordWrap(True)
        l2.addWidget(_sum2)
        t2_.setRowCount(len(rs))
        _hh2 = t2_.horizontalHeader()
        for _c in range(2):
            _hh2.setSectionResizeMode(_c, QHeaderView.ResizeToContents)
        _hh2.setSectionResizeMode(2, QHeaderView.Stretch)
        for i, r in enumerate(rs):
            t2_.setItem(i, 0, QTableWidgetItem('#%d' % (i + 1)))
            t2_.setItem(i, 1, QTableWidgetItem(r['label']))
            t2_.setItem(i, 2, QTableWidgetItem(r['evidence']))
        l2.addWidget(t2_, 1)
        tabs.addTab(p2, '断开原因（%d）' % len(rs))
        _tab_boost(tabs, ['server', 'drop'])
        vv.addWidget(tabs, 1)
        _brow = QHBoxLayout()
        _brow.addStretch(1)
        _close_b = QPushButton('关闭')
        _close_b.setObjectName('ghost')
        _close_b.setMinimumWidth(96)
        _close_b.clicked.connect(dlg.reject)
        _brow.addWidget(_close_b)
        vv.addLayout(_brow)
        dlg.exec()

    def _open_device_groups(prev=None):
        """→ 1.5.8：设备识别 →「管理分组…」改为主窗口内的全屏二级页。

        不再开小窗弹层：整页铺满可用区，右上「返回」回到进入前的页；
        关闭后从页面栈摘除并刷新智能分析。形态数据复用主窗口已算好的那份。
        """
        try:
            stk = getattr(w, '_ck_stack', None)
            if stk is None:
                DeviceGroupsDialog(
                    w, adv_prefs, _last_rec.get('rows') or [],
                    str(adv_prefs.get('deviceSensitivity') or '均衡'),
                    on_saved=_update_insight,
                    shapes=_last_rec.get('shapes')).exec()
                _update_insight()
                return
            _cur_pg = prev or stk.currentWidget()
            # 2.1.0：重复点「管理分组」不再叠加二级页，先摘掉同名的旧页
            for _i in range(stk.count() - 1, -1, -1):
                _ow = stk.widget(_i)
                if _ow is not None and _ow.objectName() == 'pageDeviceGroups':
                    stk.removeWidget(_ow)
                    _ow.setParent(None)
                    _ow.deleteLater()
            pg = DeviceGroupsDialog(
                w, adv_prefs, _last_rec.get('rows') or [],
                str(adv_prefs.get('deviceSensitivity') or '均衡'),
                on_saved=_update_insight,
                shapes=_last_rec.get('shapes'), embedded=True)
            pg.setObjectName('pageDeviceGroups')
            pg._prev_page = _cur_pg
            _idx = stk.addWidget(pg)

            def _done():
                try:
                    if stk.currentWidget() is pg and _cur_pg is not None:
                        stk.setCurrentWidget(_cur_pg)
                    pg.setParent(None)
                    pg.deleteLater()
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:c40devclose')
                _update_insight()

            pg.closed.connect(_done)
            stk.setCurrentIndex(_idx)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:c40devopen')

    # 1.5.5：原「进阶配置」独立页拆走三块 + 外观块后，剩余项与迁出项统一用
    # 扁平设置组渲染：标题 + 两列网格，一行一项（名称/控件/?/生效时机），
    # 说明全部在「?」里，不折叠、不滚动。
    _adv_sections = []

    def adv_card(box, title, keys, glyph='sliders', cols=2, labels=None, fixed=None):
        sec = ux.AdvSection(box, adv_prefs, adv_secrets, title, keys, cols=cols,
                            on_dev=_open_device_groups, card_factory=GlassCard,
                            labels=labels, fixed=fixed)
        sec.set_glyph(glyph)
        sec.changed.connect(lambda *_a: _ck_section_apply())
        _adv_sections.append(sec)
        box.addWidget(sec)
        return sec

    def _gen_daily_report():
        an = _last_rec.get('analysis') or _dev_analysis()
        stats = _last_rec.get('stats') or {}
        base = ux.rule_report(stats, an)
        dlg = QDialog(w)
        dlg.setWindowTitle('充电日报（本地规则 + 可选 AI 解读）')
        dlg.resize(780, 560)
        vv = QVBoxLayout(dlg)
        vv.setContentsMargins(16, 12, 16, 12)
        vv.setSpacing(10)
        _hdr, _badge = dialog_head('充电日报', '本地规则')
        vv.addWidget(_hdr)
        tb = QTextBrowser()
        tb.setPlainText(base)
        vv.addWidget(tb, 1)
        st = QLabel('')
        st.setObjectName('sub')
        st.setWordWrap(True)
        vv.addWidget(st)
        row = QHBoxLayout()
        _save = aligned_button('导出 TXT', 'ghost', min_w=120, h=32)
        _close = aligned_button('关闭', 'ghost', min_w=96, h=32)
        row.addStretch(1)
        row.addWidget(_save)
        row.addWidget(_close)
        vv.addLayout(row)

        def _do_save():
            p, _f = QFileDialog.getSaveFileName(dlg, '导出日报', '充电日报.txt', '文本 (*.txt)')
            if p:
                try:
                    with open(p, 'w', encoding='utf-8') as f:
                        f.write(tb.toPlainText())
                    st.setText('已导出：%s' % p)
                except Exception as e:
                    st.setText('导出失败：%s' % e)

        _save.clicked.connect(_do_save)
        _close.clicked.connect(dlg.accept)
        dlg.show()

        # AI 解读：默认关闭；必须已同意隐私说明；字段经 ai_payload 字段定义裁剪。
        if bool(adv_prefs.get('aiEnabled')) and adv_prefs.consent_ok():
            cfg = ux.ai_config(adv_prefs, adv_secrets)
            try:
                if cfg.get('redact'):
                    _pl = ai_payload.build(
                        'report_daily',
                        period=str(stats.get('period', '今日')),
                        total_wh=round(float(stats.get('total_wh') or 0), 2),
                        session_count=int(stats.get('session_count') or 0),
                        peak_power_w=round(float(stats.get('peak_power_w') or 0), 1),
                        total_duration_sec=int(stats.get('total_duration_sec') or 0),
                        device_count=len(an.get('clusters') or []),
                        disconnect_reasons=[r['label'] for r in an.get('reasons') or []])
                else:
                    _pl = ai_payload.build('report_daily',
                                           sessions=_last_rec.get('rows') or [])
            except ai_payload.PayloadError as _pe:
                _code = 'AI-%06d' % (int(time.time() * 1000) % 1000000)
                try:
                    ux.learn_log('AIERR %s daily: %s' % (_code, _pe))
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:aierr2')
                st.setText('数据准备异常，已记录（错误码 %s）；已保留本地规则日报'
                           % _code)
                _badge.setText('本地规则')
                return
            st.setText('正在请求 %s …（失败会自动降级为本地规则）' % cfg.get('provider'))
            _badge.setText('AI 解读中')

            def _done(txt, err):
                if txt:
                    tb.setPlainText(base + '\n\n【AI 解读】\n' + txt)
                    st.setText('AI 解读完成（%s）' % cfg.get('model'))
                    _badge.setText('AI 解读')
                else:
                    st.setText('AI 不可用：%s；已保留本地规则日报' % (err or '未知原因'))
                    _badge.setText('本地规则')
            ux.ai_run(cfg, _pl, _done)
        else:
            st.setText('AI 未开启或未同意隐私说明：当前为本地规则引擎结果（不联网）')

    ins_view_b.clicked.connect(_open_device_view)

    g_rec = GlassCard('充电记录明细 · 双击查看功率曲线')
    recv2 = QVBoxLayout(g_rec)
    recv2.setContentsMargins(12, 16, 12, 12)
    recv2.setSpacing(8)
    sess_tbl = QTableWidget(0, 0)
    sess_tbl.setObjectName('sessTbl')
    sess_tbl.verticalHeader().setVisible(False)
    sess_tbl.verticalHeader().setDefaultSectionSize(28)
    # R031：最小高度按 5 行给；R043 / C12：改按 4 行给（150% 页高 602 时
    # 5 行最小高会超出布局分配，分页条压住末行）
    sess_tbl.setMinimumHeight(28 + 4 * 28)
    sess_tbl.setAutoFillBackground(False)
    sess_tbl.viewport().setAutoFillBackground(False)
    sess_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    sess_tbl.setWordWrap(False)
    sess_tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    sess_tbl.setFrameStyle(QFrame.NoFrame)
    sess_tbl.setShowGrid(False)
    sess_tbl.setAlternatingRowColors(True)
    # R026：记录页不出现滚动条（分页 + 固定页容量；表格自身滚动条隐藏）
    sess_tbl.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sess_tbl.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    # R043 / C12：空态给标题 + 操作提示，不再只有一行灰字
    rec_empty = QLabel('本范围暂无充电记录\n切换范围，或完成一次充电后自动出现')
    rec_empty.setObjectName('sub')
    rec_empty.setAlignment(Qt.AlignCenter)
    rec_empty.setWordWrap(True)
    # C24-V15：空态补一枚 48×48 淡化线稿（与整卡文字同一容器，显隐随原空态走）
    rec_empty_box = QWidget()
    _eb = QVBoxLayout(rec_empty_box)
    _eb.setContentsMargins(0, 0, 0, 0)
    _eb.setSpacing(10)
    _eb.addStretch(1)
    _eb_icon = QLabel()
    _eb_icon.setPixmap(_glyph_pm('sess', 56, '#46647f'))
    _eb_icon.setAlignment(Qt.AlignCenter)
    _eb.addWidget(_eb_icon)
    _eb.addWidget(rec_empty)
    _eb.addStretch(1)
    # R024-A1 领导指出"9 月份就有记录但看不到"：库里有 277 条（6 页），原页面只拉第 1 页且无翻页。
    # 加分页条（上一页/下一页/页码），9 月及更早记录全部可达。
    rec_page = {'cur': 1, 'pages': 1, 'total': 0}
    pg_row = QHBoxLayout()
    pg_row.setSpacing(8)
    pg_prev = aligned_button('上一页', 'ghost', min_w=84, h=32, glyph='arrow_l')   # C20-C2：并回 32 高按钮族
    pg_lbl = QLabel('第 1/1 页 · 共 0 条')
    pg_lbl.setObjectName('sub')
    pg_lbl.setAlignment(Qt.AlignCenter)
    pg_next = aligned_button('下一页', 'ghost', min_w=84, h=32, glyph='arrow_r')
    pg_row.addWidget(pg_prev)
    pg_row.addWidget(pg_lbl, 1)
    pg_row.addWidget(pg_next)
    # 1.5.5：让用户看见「有没有在学」——版本/标注数、上次训练、留出准确率
    # 2.1.2：去掉「按曲线聚类」入口；两行定距排版，只留必要信息
    learn_lb = QLabel('设备学习：未训练')
    learn_lb.setObjectName('sub')
    learn_sub = QLabel('当前按规则判定 · 人工改归属后自动训练')
    learn_sub.setObjectName('sub')
    _learn_col = QVBoxLayout()
    _learn_col.setContentsMargins(0, 2, 0, 2)
    _learn_col.setSpacing(2)
    _learn_col.addWidget(learn_lb)
    _learn_col.addWidget(learn_sub)
    recv2.addLayout(_learn_col)

    def _refresh_learn_lb():
        _m = ux.learn_load() or {}
        if not _m:
            learn_lb.setText('设备学习：未训练')
            learn_sub.setText('当前按规则判定 · 人工改归属后自动训练')
            return
        _h = _m.get('holdout') or {}
        if _h.get('ok'):
            _acc = ('留出准确率 模型 %.0f%% / 规则 %.0f%%（%d 条）'
                    % (float(_h.get('model') or 0) * 100,
                       float(_h.get('rule') or 0) * 100, int(_h.get('n') or 0)))
        else:
            _acc = '留出样本不足'
        learn_lb.setText('设备学习 v%d · 已标注 %d 条'
                         % (int(_m.get('version') or 0),
                            len(_m.get('samples') or [])))
        learn_sub.setText('上次训练 %s · %s'
                          % (_m.get('trained_at') or '—', _acc))

    recv2.addWidget(sess_tbl, 1)
    recv2.addWidget(rec_empty_box, 1)
    recv2.addLayout(pg_row)
    rec_empty_box.hide()
    rcv.addWidget(g_rec, 1)
    # R026：去掉外层滚动包装——记录页固定高度 + 分页，杜绝页面滚动条
    stack.addWidget(rc)

    sess_ids = []
    sess_page_rows = []   # R06：当前页的原始会话字典（详情页直接用，不再按 id 反查）

    # ===== 1.5.5：会话归属人工纠正（双击设备归属 / 右键菜单，多选可批量） =====
    # 人工指定写入 device_model.manual（key=session_key），自动回溯/重算会跳过；
    # 审计落到用户目录 device_assign.log（与 auto_off.log 同一审计惯例）。
    sess_tbl.setContextMenuPolicy(Qt.CustomContextMenu)
    sess_tbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def _box_choices():
        _m = ux.device_model_load(adv_prefs) or {}
        _nm = {}
        for _b in (_m.get('boxes') or []):
            _nm[str(_b.get('id') or '')] = str(_b.get('name') or '')
        for _c in ((_last_rec.get('analysis') or {}).get('clusters') or []):
            _cid = str(_c.get('id') or '')
            if _cid:
                _nm.setdefault(_cid, str(_c.get('name') or ''))
        return [(k, ux.device_box_name(k, v)) for k, v in sorted(_nm.items()) if k]

    def _audit_assign(lines):
        try:
            _d = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
                              'CuktechPanel')
            os.makedirs(_d, exist_ok=True)
            with open(os.path.join(_d, 'device_assign.log'), 'a', encoding='utf-8') as _f:
                for _ln in lines:
                    _f.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), _ln))
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:assign-audit')

    def _assign_rows(bid):
        """bid='' 未归箱；bid=None 恢复自动判定（删除人工项）。多选行批量生效。

        改完立刻进化：箱中心增量更新 → 一次增量重训 → 原子落盘 → version+1 → 审计。
        """
        _rows_sel = sess_tbl.selectionModel()
        _idx = sorted({i.row() for i in _rows_sel.selectedRows()}) if _rows_sel else []
        if not _idx:
            return
        _model = ux.device_model_load(adv_prefs) or {}
        _manual = dict(_model.get('manual') or {})
        _meta = dict(_model.get('manual_meta') or {})
        _names = dict(_box_choices())
        _at = time.strftime('%Y-%m-%d %H:%M')
        _boxes = {str(b.get('id')): b for b in (_model.get('boxes') or [])
                  if isinstance(b, dict)}
        _sel = [sess_page_rows[i] for i in _idx if i < len(sess_page_rows)]
        _shapes = load_shapes([r.get('id') for r in _sel])
        # 1.5.8：选「未归箱」必须写 UNASSIGNED 哨兵，写空串会被自动判定立刻判回箱，
        # 表现为「批量归到未归箱不生效」（批量入口实测复现）。
        _pin = ux.UNASSIGNED if bid == '' else bid
        _lines, _changes = [], []
        for _row in _sel:
            _k = ux.session_key(_row)
            _old = str(_manual.get(_k) or '')
            _sh = _shapes.get(_row.get('id'))
            _cv = ux.cluster_vec(_row, _sh) if _sh else None
            if _old and _old in _boxes and _cv:          # 中心跟着走：旧箱先减
                ux.box_shape_del(_boxes[_old], _cv)
            if bid is None:
                if _k not in _manual:
                    continue
                _manual.pop(_k, None)
                _meta.pop(_k, None)
                _changes.append((_k, None, ux.learn_feats(_row), _cv, 1.0))
                _lines.append('RESTORE key=%s session=%s' % (_k, _row.get('id')))
            else:
                _manual[_k] = _pin
                _meta[_k] = {'at': _at, 'by': '本机用户', 'src': 'manual',
                             'from': _old, 'to': _pin, 'session': _row.get('id')}
                _changes.append((_k, _pin, ux.learn_feats(_row), _cv, 1.0))
                if _cv and _pin in _boxes:
                    ux.box_shape_add(_boxes[_pin], _cv)
                _lines.append('MANUAL key=%s session=%s from=%s to=%s src=manual'
                              % (_k, _row.get('id'), _old or 'auto',
                                 '未归箱' if _pin == ux.UNASSIGNED
                                 else (_names.get(_pin, _pin) or _pin)))
        if not _lines:
            return
        _model['manual'] = _manual
        _model['manual_meta'] = _meta
        _model['boxes'] = list(_boxes.values())
        ux.device_save(adv_prefs, _model)
        _tm = ux.learn_apply(_changes, 'manual')
        _lv = int(_tm.get('version') or 0)
        _audit_assign(_lines + ['MANUAL-BATCH n=%d src=manual v%d dur=%sms%s'
                                % (len(_lines), _lv, _tm.get('dur_ms', '—'),
                                   '' if _tm.get('ok', True) else ' TRAIN_FAIL')])
        w._toast.show('归属已更新（%s %d 条）· 模型 v%d%s'
                      % ('归到未归箱' if _pin == ux.UNASSIGNED else '人工指定',
                         len(_lines), _lv,
                         '' if _tm.get('ok', True) else '（训练失败，沿用上一版）'), 'ok')
        try:
            _refresh_learn_lb()
        except Exception:
            pass
        # 立即按新归属重拉本页（不等下一轮轮询）
        _last_rec['tab_sig'] = None
        try:
            poll_sessions()
        except Exception:
            pass

    def _ask_pick(_title, _label, _items):
        """暗色 + 中文按钮的单选弹窗；_items=[(显示名, 值)]。"""
        _d = QDialog(w)
        _d.setWindowTitle(_title)
        _dv = QVBoxLayout(_d)
        _dv.setContentsMargins(16, 16, 16, 16)
        _dv.setSpacing(12)
        _dv.addWidget(QLabel(_label))
        _cb = QComboBox()
        _cb.setMinimumHeight(32)
        for _nm, _val in _items:
            _cb.addItem(_nm, _val)
        _dv.addWidget(_cb)
        _r = QHBoxLayout()
        _r.addStretch(1)
        _bn = QPushButton('取消')
        _bn.setObjectName('ghost')
        _bn.setMinimumWidth(96)
        _bo = QPushButton('确定')
        _bo.setMinimumWidth(96)
        _bn.clicked.connect(_d.reject)
        _bo.clicked.connect(_d.accept)
        _r.addWidget(_bn)
        _r.addWidget(_bo)
        _dv.addLayout(_r)
        _d.resize(380, _d.sizeHint().height())
        if _d.exec() != QDialog.Accepted:
            return None
        return _cb.currentData()

    def _new_box(name):
        """新建分箱：id 自动取最小未用号，中心取所选会话特征均值（可为空）。"""
        _model = ux.device_model_load(adv_prefs) or {'boxes': []}
        _boxes = list(_model.get('boxes') or [])
        _used = {str(b.get('id')) for b in _boxes}
        _n = 1
        while ('DEV-%04d' % _n) in _used:
            _n += 1
        _bid = 'DEV-%04d' % _n
        _sm = sess_tbl.selectionModel()
        _idx = sorted({i.row() for i in _sm.selectedRows()}) if _sm else []
        _vecs = [ux.learn_feats(sess_page_rows[i]) for i in _idx
                 if i < len(sess_page_rows)]
        _center = {}
        if _vecs:
            _center = {k: round(sum(v[k] for v in _vecs) / len(_vecs), 4)
                       for k in ux.LEARN_DIMS}
        _boxes.append({'id': _bid, 'name': name, 'center': _center, 'n': 0,
                       'review': False, 'move': 0.0, 'prev': ''})
        _model['boxes'] = _boxes
        ux.device_save(adv_prefs, _model)
        _audit_assign(['NEWBOX id=%s name=%s sessions=%d' % (_bid, name, len(_vecs))])
        w._toast.show('已新建分箱「%s」（%s）' % (name, _bid), 'ok')
        if _idx:
            _assign_rows(_bid)
        else:
            try:
                _refresh_learn_lb()
            except Exception:
                pass

    def _add_existing():
        _ch = _box_choices()
        if not _ch:
            w._toast.show('暂无分箱：先「添加新分箱」', 'warn')
            return
        _bid = _ask_pick('添加到已有分箱', '把选中的会话归入：', _ch)
        if _bid:
            _assign_rows(_bid)

    def _add_new():
        _nm, _ok = _ask_text('添加新分箱', '分箱名称（例如：Anker 140W）：')
        _nm = (_nm or '').strip()
        if not _ok or not _nm:
            return
        _new_box(_nm)

    def _open_assign_menu(_gpos):
        _m = QMenu(w)
        _m.addAction('添加到已有分箱…').triggered.connect(lambda _c=False: _add_existing())
        _m.addAction('添加新分箱…').triggered.connect(lambda _c=False: _add_new())
        _m.addSeparator()
        _sub = _m.addMenu('改归属（人工指定）')
        for _bid, _nm in _box_choices():
            _a = _sub.addAction(_nm)
            _a.triggered.connect(lambda _c=False, _b=_bid: _assign_rows(_b))
        _sub.addSeparator()
        _keep = _sub.addAction('未归箱')
        _keep.triggered.connect(lambda _c=False: _assign_rows(''))
        _m.addSeparator()
        _rst = _m.addAction('恢复自动判定')
        _rst.triggered.connect(lambda _c=False: _assign_rows(None))
        _m.exec_(_gpos)

    def _ctx_menu(pos):
        _i = sess_tbl.rowAt(pos.y())
        if _i >= 0:
            _sel = {x.row() for x in sess_tbl.selectionModel().selectedRows()}
            if _i not in _sel:
                sess_tbl.clearSelection()
                sess_tbl.selectRow(_i)
        _open_assign_menu(sess_tbl.viewport().mapToGlobal(pos))

    def _dbl_assign(_r, _c):
        if _c == 2:
            _open_assign_menu(QCursor.pos())

    sess_tbl.customContextMenuRequested.connect(_ctx_menu)
    sess_tbl.cellDoubleClicked.connect(_dbl_assign)
    # 验收钩子：右键菜单/双击菜单里的动作就是调 _assign_rows(bid)，
    # 这里直接暴露同一入口，便于无人值守跑「改归属 / 批量 / 恢复自动」。
    w._ck_assign_rows = _assign_rows
    w._ck_assign_choices = _box_choices
    w._ck_sess_tbl = sess_tbl
    w._ck_prefs = adv_prefs
    w._ck_new_box = _new_box
    w._ck_learn = ux.learn_load

    def show_session_points(row, col=None):
        if col == 2:   # 设备归属列双击留给「改归属」菜单
            return
        try:
            sid = sess_ids[row]
        except Exception:
            return

        dlg = QDialog(w)
        # R022-B2：同族一次性弹窗——关闭即释放；加载回调加存活门（_done 触达 reload_b/kpi2/pl/dlg）
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        _alive = {'v': True}
        dlg.finished.connect(lambda _r: _alive.__setitem__('v', False))
        dlg.setWindowTitle('记录 #%s 明细' % sid)
        dlg.resize(820, 560)
        dv = QVBoxLayout(dlg)
        dv.setContentsMargins(16, 16, 16, 12)
        # R06：详情首屏先给"何时/哪个设备/充了多久/进了多少电"，再给断开原因与判定依据，
        # 最后才是曲线细节；整页固定布局，不出现滚动条。
        row_d = sess_page_rows[row] if row < len(sess_page_rows) else {}

        def _dv_ts(v):
            try:
                return ck_time.fmt(ck_time.MD_HM, v)
            except Exception:
                return '—'

        def _dev_txt(rd):
            try:
                pv = int(rd.get('port'))
            except Exception:
                return '—'
            return ('C%d' % pv) if pv in (1, 2, 3) else 'A'
        _dur = int(float(row_d.get('duration_sec') or 0))
        _pw = float(row_d.get('peak_power_w') or 0)
        # C32：保留原始缺失去向，交给 _ck_fmt_energy 给「—」；不再 `or 0` 猜成 0.00
        _wh_raw = row_d.get('total_wh')
        # R06 追加（不许相对旧版缩减）：旧列表 10 列中的「结束时间 / 协议 / 均功率」
        # 在新列表收进摘要后必须在详情页可达；设备同时给出端口与型号（旧列表只有端口）。
        _end_txt = ('进行中' if row_d.get('is_active')
                    else _dv_ts(row_d.get('end_time')))
        _dev_full = _dev_txt(row_d)
        _mdl = str(row_d.get('device_model') or '').strip()
        if _mdl:
            _dev_full = '%s · %s' % (_dev_full, _mdl)
        _proto = ux.proto_full(row_d.get('protocol'))
        _avg_pw = float(row_d.get('avg_power_w') or 0)
        # 1.5.8：设备判别特征在明细首屏看全（不用再跳页）
        _avg_v = float(row_d.get('avg_voltage') or 0)
        _avg_a = float(row_d.get('avg_current') or 0)
        _ratio = (_pw / _avg_pw) if _avg_pw > 0 else 0.0
        try:
            _hr = datetime.datetime.fromtimestamp(
                float(row_d.get('start_time') or 0)).hour
        except Exception:
            _hr = -1
        # R07：摘要按固定行切分，行高稳定：第 1 行时间/设备/时长，第 2 行电量/功率/协议，
        # 1.5.8 追加第 3 行设备判别特征（均压/均流/峰均比/常用口/时段）。
        _sum = QLabel('开始 %s ｜ 结束 %s ｜ 设备 %s ｜ 时长 %s\n'
                      '充入 %s ｜ 峰功率 %.1f W ｜ 均功率 %.1f W ｜ 协议 %s\n'
                      '均压 %.2f V ｜ 均流 %.2f A ｜ 峰均比 %.2f× ｜ 常用口 %s ｜ 时段 %s'
                      % (_dv_ts(row_d.get('start_time')), _end_txt, _dev_full,
                         _ck_fmt_dur(_dur),
                         _ck_fmt_energy(_wh_raw), _pw, _avg_pw, _proto,
                         _avg_v, _avg_a, _ratio,
                         ux.port_label(row_d.get('port')),
                         ('%d 点' % _hr) if _hr >= 0 else '—'))
        _sum.setObjectName('sub')
        _sum.setWordWrap(True)
        dv.addWidget(_sum)
        try:
            _an1 = ux.analyze_sessions([row_d], int(adv_prefs.get('mergeGap') or 120))
            _rr = (_an1.get('reasons') or [{}])[0]
        except Exception:
            _rr = {}
        _rl = QLabel('断开原因：<b>%s</b>　判定依据：%s'
                     % (_rr.get('label') or '—', _rr.get('evidence') or '信息不足'))
        _rl.setObjectName('sub')
        _rl.setWordWrap(True)
        dv.addWidget(_rl)
        # C-24 降采样下拉：全部/100/200/300/600
        ctrl_row = QHBoxLayout()
        # R07：控件基线对齐——三个控件统一 28 px 行高并垂直居中，消除"降采样："偏上的观感
        _ds_lbl = QLabel('降采样：')
        _ds_lbl.setFixedHeight(28)
        _ds_lbl.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        ctrl_row.addWidget(_ds_lbl)
        ds_combo = QComboBox()
        ds_combo.setFixedHeight(28)
        ds_combo.addItems(['全部', '100', '200', '300', '600'])
        # R023-M1：记住上次明细降采样档位
        try:
            _ds = str(_ui_prefs.value('detailDs', '600'))
            ds_combo.setCurrentText(_ds if _ds in ('全部', '100', '200', '300', '600') else '600')
        except Exception:
            ds_combo.setCurrentText('600')
        ds_combo.currentTextChanged.connect(
            lambda t: _ui_prefs.setValue('detailDs', t))
        ctrl_row.addWidget(ds_combo)
        reload_b = QPushButton('重新加载')
        reload_b.setMinimumSize(96, 32)
        ctrl_row.addWidget(reload_b)
        ctrl_row.addStretch(1)
        dv.addLayout(ctrl_row)
        # C-25 五 KPI：电量/均功率/峰功率/均电压/均电流
        kpi_row2 = QHBoxLayout()
        kpi2 = {}
        for key, cap in (('wh', '电量'), ('avg', '均功率 W'), ('peak', '峰功率 W'),
                         ('volt', '均电压 V'), ('cur', '均电流 A')):
            box = QVBoxLayout()
            cap_l = QLabel(cap)
            cap_l.setObjectName('sub')
            cap_l.setAlignment(Qt.AlignCenter)
            val_l = QLabel('—')
            val_l.setObjectName('big')
            val_l.setAlignment(Qt.AlignCenter)
            box.addWidget(val_l)
            box.addWidget(cap_l)
            kpi_row2.addLayout(box, 1)
            kpi2[key] = val_l
        dv.addLayout(kpi_row2)
        import pyqtgraph as pg
        pl = new_plot(pg)
        pl.addLegend(offset=(8, 8))
        pl.setLabel('bottom', '秒')
        dv.addWidget(pl, 1)

        def load_points():
            ds = ds_combo.currentText()
            qs = '' if ds == '全部' else ('?downsample=%s' % ds)
            # C14：历史点集 get_bytes + 解析改走 qwait 后台线程，主线程显示加载态
            reload_b.setEnabled(False)
            reload_b.setText('加载中…')
            pl.clear()
            pl.setLabel('bottom', '秒')

            def _fetch():
                try:
                    r = get_bytes('/api/sessions/%s/points%s' % (sid, qs), timeout=30)
                    if isinstance(r, tuple) and r[0] == 200:
                        return json.loads(r[1])
                except Exception:
                    return None
                return None

            def _done(d):
                if not _alive['v']:
                    return
                reload_b.setEnabled(True)
                reload_b.setText('重新加载')
                pts = (d or {}).get('points', []) if isinstance(d, dict) else []
                if not pts:
                    _ck_msg(dlg, '明细', '该记录无采样点')
                    return
                # R06：离线模型推理（纯标准库、无网络）；失败或低置信度回落规则判定
                try:
                    import charge_ml
                    _mr = charge_ml.infer(row_d, pts)
                    if _mr and _mr.get('disconnect_reason') \
                            and float(_mr.get('confidence') or 0) >= 0.5 \
                            and _mr.get('disconnect_reason') not in ('未分类', '未知'):
                        _basis = '、'.join('%s %.2f' % (b.get('feature'), b.get('score'))
                                           for b in (_mr.get('basis') or [])[:3])
                        _rl.setText('断开原因：<b>%s</b>　判定依据：%s（离线模型 %s，置信度 %.2f%s）'
                                    % (_mr['disconnect_reason'],
                                       _rr.get('evidence') or '信息不足', charge_ml.MODEL_VERSION,
                                       float(_mr.get('confidence') or 0),
                                       ('；主要特征：' + _basis) if _basis else ''))
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:5703', '模型不可用 → 保持上方规则判定文案')
                # 五 KPI 现算
                def series(k):
                    out = []
                    for p_ in pts:
                        try:
                            _v = p_.get(k)
                            if _v is None:
                                continue
                            out.append(float(_v))
                        except Exception as _exc:
                            ck_swallow(_exc, 'launcher:5713')
                    return out
                powers = series('power')
                volts = series('voltage')
                currents = series('current')
                # 曲线 x 轴：服务端点集的字段名是 timestamp（兼容旧 ts）
                tss = series('ts') or series('timestamp')
                # 电量：优先用会话聚合值（与摘要/列表同源）；聚合缺失时再按采样点梯形积分。
                # 服务端点集的字段名是 timestamp（不是 ts），旧代码读 ts 恒为空 → 现算恒 0.00。
                try:
                    wh = float(row_d.get('total_wh'))
                except (TypeError, ValueError):
                    wh = 0.0
                if wh <= 0:
                    _pairs = []
                    for _p in pts:
                        try:
                            _tt = _p.get('ts')
                            if _tt is None:
                                _tt = _p.get('timestamp')
                            _pairs.append((float(_tt), float(_p.get('power'))))
                        except (TypeError, ValueError):
                            continue
                    for j in range(1, len(_pairs)):
                        dt = _pairs[j][0] - _pairs[j - 1][0]
                        if dt > 0:
                            wh += (_pairs[j][1] + _pairs[j - 1][1]) / 2.0 * dt / 3600.0
                # C32：单位随值（现算值，0 就是 0.00 Wh，与未知的「—」可区分）
                kpi2['wh'].setText(_ck_fmt_energy(wh))
                kpi2['avg'].setText('%.1f' % (sum(powers) / len(powers) if powers else 0))
                kpi2['peak'].setText('%.1f' % (max(powers) if powers else 0))
                kpi2['volt'].setText('%.2f' % (sum(volts) / len(volts) if volts else 0))
                kpi2['cur'].setText('%.2f' % (sum(currents) / len(currents) if currents else 0))
                # 曲线
                cols = {'power': TEAL, 'voltage': REC_BLUE, 'current': WARN_BRIGHT}
                for k, col in cols.items():
                    vals = series(k)
                    if vals:
                        xs = tss if len(tss) == len(vals) else list(range(len(vals)))
                        if xs:
                            xs = [x - xs[0] for x in xs]
                        pl.plot(xs, vals, pen=pg.mkPen(col, width=2), name=k)

            qwait(_fetch, _done)

        reload_b.clicked.connect(load_points)
        ds_combo.currentIndexChanged.connect(lambda _i: load_points())
        _fb = QHBoxLayout()
        _fb.setSpacing(8)
        _fb.addStretch(1)
        _fbc = QPushButton('关闭')
        _fbc.setObjectName('ghost')
        _fbc.setMinimumSize(96, 32)
        _fbc.clicked.connect(dlg.reject)
        _fb.addWidget(_fbc)
        dv.addLayout(_fb)
        dlg.show()
        pop_in(dlg, duration=320)
        load_points()
        dlg.exec()

    sess_tbl.cellDoubleClicked.connect(show_session_points)

    def poll_sessions():
        period = rec_period[rec_range.currentIndex()]
        # R31：后端 limit 钳在 50，limit=500 实际只回 50 行 → 必须按 pages 逐页取全，
        # 否则表格塞 48 行、被卡片底部裁掉且无滚动条（记录页 900×700 可见缺陷）。
        acc = {'rows': [], 'total': 0, 'pages': 1}

        def _part(r, pg):
            try:
                if not isinstance(r, tuple) or r[0] != 200 or not isinstance(r[1], dict):
                    return
                d = r[1]
                acc['rows'].extend(d.get('sessions') or d.get('items') or [])
                acc['total'] = int(d.get('total') or acc['total'] or 0)
                acc['pages'] = max(1, int(d.get('pages') or 1))
                if pg < acc['pages']:
                    _fetch(pg + 1)
                    return
                _sessions_done((200, {'sessions': acc['rows'], 'total': acc['total'],
                                      'pages': 1}))
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:fetch_sessions')

        def _fetch(pg):
            qwait(lambda: api('GET', '/api/sessions?period=%s&limit=50&page=%d'
                              % (period, pg)),
                  lambda r: _part(r, pg), key='sessions_p%d' % pg)

        _fetch(1)

    def _settle_page(depth):
        """R043 / C12：智能分析文字更新会改变表格可用高度；布局稳定后复核页容量。"""
        _all = _last_rec.get('rows_all') or []
        if depth >= 3 or not _all:
            return
        _p = max(3, min(14, sess_tbl.viewport().height() // 28))
        if _p == rec_page.get('psz'):
            return
        _sessions_done((200, {'sessions': _all, 'total': len(_all), 'pages': 1}), depth + 1)

    class _VpWatch(QObject):
        """R043 / C12：表格视口尺寸变化（智能分析文字换行/窗口缩放）时防抖重排。"""

        def __init__(self, obj, cb):
            super().__init__(obj)
            self._cb = cb
            self._t = QTimer(self)
            self._t.setSingleShot(True)
            self._t.setInterval(180)
            self._t.timeout.connect(self._cb)
            obj.installEventFilter(self)

        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Resize:
                self._t.start()
            return False

    _sess_vp_watch = _VpWatch(sess_tbl.viewport(), lambda: _settle_page(0))

    def _sessions_done(r, _depth=0):
        try:
            if not isinstance(r, tuple) or r[0] != 200:
                return
            _code, d = r
            if not isinstance(d, dict):
                return
            rows = d.get('sessions') or d.get('items') or []
            _last_rec['rows_all'] = list(rows)
            # R06：一次取回本范围全部会话（上限 500）→ 本地筛选/排序/分页，
            # 避免"排序只作用于当前页"的假排序；取不全时回落服务端分页。
            _all = len(rows)
            try:
                _total = int(d.get('total') or 0)
            except Exception:
                _total = 0
            if _all >= max(_total, 1):

                def _pnum(v):
                    try:
                        return float(v or 0)
                    except Exception:
                        return 0.0
                _pv = rec_port.currentIndex()
                if _pv:
                    _want = 4 if _pv == 4 else _pv
                    rows = [r_ for r_ in rows
                            if int(_pnum(r_.get('port')) or 0) == _want]
                _si = rec_sort.currentIndex()
                if _si == 0:
                    rows.sort(key=lambda r_: _pnum(r_.get('start_time')), reverse=True)
                elif _si == 1:
                    rows.sort(key=lambda r_: _pnum(r_.get('start_time')))
                elif _si == 2:
                    rows.sort(key=lambda r_: _pnum(r_.get('total_wh')), reverse=True)
                else:
                    rows.sort(key=lambda r_: _pnum(r_.get('duration_sec')), reverse=True)
                # R043 / C12：按表格视口实测行数，100% 不再固定 7 行留白
                _psz = max(3, min(14, sess_tbl.viewport().height() // 28))
                _dbg_log('rec-page psz=%d vp=%d all=%d' % (
                    _psz, sess_tbl.viewport().height(), len(rows)))
                rec_page['psz'] = _psz
                rec_page['total'] = len(rows)
                rec_page['pages'] = max(1, (len(rows) + _psz - 1) // _psz)
                rec_page['cur'] = max(1, min(rec_page['cur'], rec_page['pages']))
                _full_rows = list(rows)
                rows = rows[(rec_page['cur'] - 1) * _psz: rec_page['cur'] * _psz]
            else:
                _full_rows = list(rows)
                try:
                    rec_page['total'] = _total
                    rec_page['pages'] = max(1, int(d.get('pages') or 1))
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:5807')
                rec_page['cur'] = max(1, min(rec_page['cur'], rec_page['pages']))
            pg_lbl.setText('第 %d/%d 页 · 共 %d 条'
                           % (rec_page['cur'], rec_page['pages'], rec_page['total']))
            pg_prev.setEnabled(rec_page['cur'] > 1)
            pg_next.setEnabled(rec_page['cur'] < rec_page['pages'])
            # R023：把真实会话喂给设备识别/断开原因分析
            # R06 性能：记录页每 15 s 重拉一次；数据未变就不重算洞察（500 条全量分析很贵，
            # 实测把切页后 60 s 余波抬到 5.5%）。命中相同签名时直接复用上次结果。
            _ins_sig = (_last_rec.get('stats', {}).get('session_count'),
                        _last_rec.get('stats', {}).get('total_wh'),
                        len(_full_rows),
                        _full_rows[0].get('id') if _full_rows else None,
                        _full_rows[-1].get('id') if _full_rows else None,
                        tuple(sorted((ux.device_model_load(adv_prefs) or {})
                                     .get('manual', {}).items())))
            if _last_rec.get('ins_sig') != _ins_sig:
                try:
                    _last_rec['rows'] = _full_rows
                    _update_insight()
                    _last_rec['ins_sig'] = _ins_sig
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:5827')
            sess_page_rows[:] = [r_ for r_ in rows if isinstance(r_, dict)]
            sess_ids[:] = [int(str(r_.get('id', i + 1))) for i, r_ in enumerate(rows)
                           if isinstance(r_, dict) and 'id' in r_]
            # 箱名来源与「设备分组」页同一份已保存模型；改名后下一轮刷新即生效（不缓存旧名）
            _cur_names = {}
            try:
                for _b in ((ux.device_model_load(adv_prefs) or {}).get('boxes') or []):
                    _cur_names[str(_b.get('id') or '')] = str(_b.get('name') or '')
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:box-names')
            # R06 性能：本页内容签名（页码/筛选/排序/行 id/箱名/视口宽度）未变则跳过表格重建
            # 视口宽度入签名：窗口缩放后按新宽度重算「设备归属」列宽与省略预算
            _tab_sig = (rec_page['cur'], rec_port.currentIndex(),
                        rec_sort.currentIndex(), tuple(sess_ids),
                        tuple(sorted(_cur_names.items())),
                        tuple(sorted((ux.device_model_load(adv_prefs) or {})
                                     .get('manual', {}).items())),
                        sess_tbl.viewport().width())
            if _tab_sig == _last_rec.get('tab_sig'):
                return
            _last_rec['tab_sig'] = _tab_sig
            if not rows:
                # 空态：隐藏表格与分页条，玻璃卡内居中提示
                sess_tbl.hide()
                rec_empty_box.show()
                for _pgw in (pg_prev, pg_lbl, pg_next):
                    _pgw.hide()
            else:
                rec_empty_box.hide()
                sess_tbl.show()
                for _pgw in (pg_prev, pg_lbl, pg_next):
                    _pgw.show()
                # R06：先"何时/哪个口/归属哪个设备箱/为什么断/充了多久/进了多少电"，
                # 再功率；电压电流移入详情页。
                # 1.5.3：归属拆两列——端口归属（C1–C4）与设备归属（设备列表的 DEV 箱）。
                # 2.1.1：末列追加「协议」（短码 + tooltip 全称），占用右侧富余宽度，
                # 不压缩既有七列；最小档仍不触发横向滚动、行高不变。
                cols_def = [('start_time', '时间'), ('port_own', '端口'),
                            ('dev_box', '设备归属'), ('end_reason', '断开原因'),
                            ('duration_sec', '时长'), ('total_wh', '充入'),
                            ('peak_power_w', '峰功率 W'), ('protocol', '协议')]
                hdr = sess_tbl.horizontalHeader()
                hdr.setVisible(True)
                hdr.setHighlightSections(False)
                # R043 / C12：表头默认居中会与左对齐单元格错位（末列拉伸后最明显）
                hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                # R06：两档窗口用同一套列宽，保证对齐/间距一致；总宽 < 700 不触发横向滚动
                hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
                sess_tbl.verticalHeader().setVisible(False)
                sess_tbl.setColumnCount(len(cols_def))
                sess_tbl.setHorizontalHeaderLabels([c[1] for c in cols_def])
                # 1.5.5：数值列按内容实测定宽。Qt 默认样式里 item padding 6px×2 会吃掉
                # 列宽，只按字号宽排版仍会被省略（如 `32.34 Wh` 55px 在 72px 列里被截），
                # 所以统一「实测文本宽 + 18px 内边距/取整」。
                _fm = sess_tbl.fontMetrics()

                def _ccw(_cap, _vals, _floor):
                    _best = _fm.horizontalAdvance(_cap)
                    for _v in _vals:
                        if _v:
                            _best = max(_best, _fm.horizontalAdvance(str(_v)))
                    return max(_floor, _best + 18)

                _col_w = {}
                try:
                    _durs, _whs, _pks = [], [], []
                    for r in rows:
                        try:
                            _durs.append(_ck_fmt_dur(r.get('duration_sec')))
                        except Exception:
                            _durs.append('')
                        try:
                            _whs.append(_ck_fmt_energy(r.get('total_wh')))
                        except Exception:
                            _whs.append('')
                        try:
                            _pks.append('%.1f' % float(r.get('peak_power_w')))
                        except (TypeError, ValueError):
                            _pks.append('')
                    _col_w['start_time'] = _ccw('时间', ['11-15 14:39'], 78)
                    _col_w['port_own'] = _ccw('端口', ['C1', 'A'], 46)
                    _col_w['duration_sec'] = _ccw('时长', _durs, 76)
                    _col_w['total_wh'] = _ccw('充入', _whs, 74)
                    _col_w['peak_power_w'] = _ccw('峰功率 W', _pks, 72)
                except Exception:
                    _col_w = {'start_time': 78, 'port_own': 46, 'duration_sec': 76,
                              'total_wh': 74, 'peak_power_w': 72}
                _fixed_w = sum(_col_w.values())
                _proto_w = 56
                _dev_w, _end_w = 162, 185
                try:
                    _tvw = sess_tbl.viewport().width()
                    if _tvw > 400:
                        # 先给协议列留固定额度，剩余宽度仍按原规则分给归属/原因两列
                        _avail = _tvw - _fixed_w - _proto_w
                        _dev_w = min(280, max(162, _avail - 185))
                        # 精确吃满视口，避免总和溢出触发横向滚动
                        _end_w = max(120, _avail - _dev_w)
                except Exception:
                    pass
                _col_w['dev_box'] = _dev_w
                _col_w['end_reason'] = _end_w
                _col_w['protocol'] = _proto_w
                for _ci, (_k, _cap) in enumerate(cols_def):
                    sess_tbl.setColumnHidden(_ci, False)
                    sess_tbl.setColumnWidth(_ci, _col_w.get(_k, 70))
                hdr.setStretchLastSection(False)
                sess_tbl.setRowCount(len(rows))

                def _fmt_ts(v):
                    try:
                        return ck_time.fmt(ck_time.MD_HM, v)
                    except Exception:
                        return ''

                def _fmt_num(v, nd=2):
                    try:
                        return ('%.' + str(nd) + 'f') % float(v)
                    except Exception:
                        return ''

                def _port_txt(row):
                    try:
                        pv = int(row.get('port'))
                    except Exception:
                        return '—'
                    return ('C%d' % pv) if pv in (1, 2, 3) else 'A'

                # 归因映射：合并会话把同一归因传给其所有原始 id；缺分析结果时回退后端字段
                _reason_by_id = {}
                _an = _last_rec.get('analysis') or {}
                for _it, _rs in zip(_an.get('sessions') or [], _an.get('reasons') or []):
                    for _sid in (_it.get('_ids') or [_it.get('id')]):
                        try:
                            _reason_by_id[int(_sid)] = _rs
                        except (TypeError, ValueError):
                            pass

                # 设备归属：取「智能分析 → 设备列表」的 DEV 分箱（同一数据源，不另立命名）
                _box_by_id, _box_ports, _box_sizes, _box_names = {}, {}, {}, {}
                for _c in (_an.get('clusters') or []):
                    _bid = str(_c.get('id') or '')
                    if not _bid:
                        continue
                    _ports, _n = [], 0
                    for _it in (_c.get('items') or []):
                        _n += 1
                        _pv = _port_txt(_it)
                        if _pv != '—' and _pv not in _ports:
                            _ports.append(_pv)
                        for _sid in (_it.get('_ids') or [_it.get('id')]):
                            try:
                                _box_by_id[int(_sid)] = _bid
                            except (TypeError, ValueError):
                                pass
                    _box_ports[_bid] = _ports
                    _box_sizes[_bid] = _n
                    _box_names[_bid] = str(_c.get('name') or '')

                def _box_title(bid):
                    """统一取名口径（ux.device_box_name）；返回空串表示未命名/占位名。"""
                    _nm = (_cur_names.get(bid) or _box_names.get(bid) or '').strip()
                    _show = ux.device_box_name(bid, _nm)
                    return '' if _show == bid else _show
                _eps = 0.0
                try:
                    _eps = float(_an.get('eps') or 0)
                except (TypeError, ValueError):
                    _eps = 0.0
                # 1.5.5：端口卡「本次充电设备名」复用同一批箱名映射（device_key → 箱名）
                for _r0 in rows:
                    _kk = str(_r0.get('device_key') or '')
                    try:
                        _bb = _box_by_id.get(int(_r0.get('id') or 0)) or ''
                    except (TypeError, ValueError):
                        _bb = ''
                    if _kk and _bb:
                        _CK_BOX_HINT[_kk] = (_bb, _box_title(_bb))
                # 1.5.5：人工指定归属（优先于自动判定；回溯/重算不再覆盖）
                _manual_map, _manual_meta = {}, {}
                try:
                    _mdl = ux.device_model_load(adv_prefs) or {}
                    _manual_map = _mdl.get('manual') or {}
                    _manual_meta = _mdl.get('manual_meta') or {}
                except Exception:
                    pass

                for r_i, row in enumerate(rows):
                    active = bool(row.get('is_active'))
                    for c_i, (k, _cap) in enumerate(cols_def):
                        v = row.get(k)
                        _tip = ''
                        if k in ('start_time', 'end_time'):
                            if v is None:
                                txt = '进行中' if active and k == 'end_time' else '—'
                            else:
                                txt = _fmt_ts(v)
                        elif k == 'port_own':
                            # 端口归属：这条记录落在哪个端口/仓位（C1–C4）
                            _code = _port_txt(row)
                            txt = _code
                            _tip = {'C1': '端口一（C口 1）', 'C2': '端口二（C口 2）',
                                    'C3': '端口三（C口 3）',
                                    'A': 'USB-A 口'}.get(_code, '')
                        elif k == 'dev_box':
                            # 设备归属：设备列表里的 DEV 分箱（不是端口，也不是 MAC）
                            _bid = ''
                            try:
                                _bid = _box_by_id.get(int(row.get('id') or 0)) or ''
                            except (TypeError, ValueError):
                                _bid = ''
                            if _bid:
                                _named = _box_title(_bid)
                                # 只显示箱名/编号；端口已有独立列，不再带「· N口」
                                _show = _named or _bid
                                _mk = ux.session_key(row)
                                _man = _manual_meta.get(_mk) or {}
                                _is_man = _mk in _manual_map
                                _full = _show
                                # 列宽自适应 + 显式右省略：不换行、不撑破表格，全文在 tooltip
                                txt = sess_tbl.fontMetrics().elidedText(
                                    _full, Qt.ElideRight, max(40, _dev_w - 6))
                                if _is_man:
                                    txt = '✎ ' + txt
                                _tip = ('%s\n箱内会话：%d 条'
                                        % (_show, _box_sizes.get(_bid) or 0))
                                if _is_man:
                                    _tip += '\n人工指定 · %s（%s）' % (
                                        _man.get('at') or '时间未记录',
                                        _man.get('by') or '本机用户')
                                # 1.5.5：自动规则留痕（该会话触发过 A 口小电流保持）
                                try:
                                    _sid0 = int(row.get('id') or 0)
                                except (TypeError, ValueError):
                                    _sid0 = 0
                                _rev = [e for e in _CK_RULE_EVENTS
                                        if int(e.get('session_id') or 0) == _sid0]
                                if _rev:
                                    try:
                                        _ut = time.strftime('%H:%M', time.localtime(
                                            float(_rev[-1].get('until') or 0)))
                                    except (TypeError, ValueError, OSError):
                                        _ut = '—'
                                    _tip += '\n自动切换 · A 口小电流（保持至 %s）' % _ut
                            else:
                                txt = '未归箱'
                                _tip = ('未能匹配任何设备箱：指纹与各箱中心相似度均低于'
                                        '阈值 ε=%.2f（分析侧 unassigned）；'
                                        '缺足够相近的历史会话，无法归箱' % _eps)
                        elif k == 'end_reason':
                            _rs = {}
                            try:
                                _rs = _reason_by_id.get(int(row.get('id') or 0)) or {}
                            except (TypeError, ValueError):
                                _rs = {}
                            txt = (_rs.get('label')
                                   or ('进行中' if active else
                                       str(row.get('end_source') or '未识别')))
                            _tip = _rs.get('evidence') or str(row.get('end_evidence') or '')
                        elif k == 'protocol':
                            # 2.1.1：协议短码 + tooltip 全称（仅展示，不进模型）
                            txt = ux.proto_short(v)
                            _tip = ux.proto_full(v)
                        elif k == 'total_wh':
                            # C32：单位随值（表头不再固定 Wh）
                            txt = _ck_fmt_energy(v)
                        elif k == 'peak_power_w':
                            txt = _fmt_num(v, 1)
                            _avgv = row.get('avg_power_w')
                            try:
                                _avgtxt = '%.1f W' % float(_avgv)
                            except (TypeError, ValueError):
                                _avgtxt = '—'
                            _tip = '峰功率 %s W · 均功率 %s' % (txt, _avgtxt)
                        elif k in ('avg_voltage', 'avg_current'):
                            try:
                                fv = float(v)
                                txt = ('%.2f' % fv) if fv > 0 else '—'
                            except Exception:
                                txt = '—'
                        elif k == 'duration_sec':
                            try:
                                txt = _ck_fmt_dur(v)
                            except Exception:
                                txt = ''
                        else:
                            txt = '—' if v is None else str(v)
                        item = QTableWidgetItem(txt)
                        if _tip:
                            item.setToolTip(_tip)
                        if k == 'end_time' and active:
                            item.setForeground(QColor(OK))
                        sess_tbl.setItem(r_i, c_i, item)
            # R043 / C12：首轮分页后等智能分析文字布局稳定，若视口变化则重排
            if _depth < 3:
                QTimer.singleShot(320, lambda: _settle_page(_depth))
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:5919')

    def poll_rec_stats():
        period = rec_period[rec_range.currentIndex()]
        qwait(lambda: api('GET', '/api/energy/stats?period=%s' % period),
              _rec_stats_done, key='rec_stats')

    def _rec_stats_done(r):
        try:
            if not isinstance(r, tuple) or r[0] != 200:
                return
            _code, d = r
            if isinstance(d, dict):
                _last_rec['stats'] = dict(d)
                # C32：电量单位随值进位；缺失给「—」，不再 `or 0` 猜成 0.00
                stat_kpis['wh'].setText(_ck_fmt_energy(d.get('total_wh')))
                stat_kpis['sess'].setText(str(d.get('session_count') or 0))
                stat_kpis['avg'].setText('%.1f' % float(d.get('avg_power_w') or 0))
                stat_kpis['peak'].setText('%.1f' % float(d.get('peak_power_w') or 0))
                dur = int(d.get('total_duration_sec') or 0)
                # C30：统一走量纲归一（改前 308 小时会显示成「308时46分」，小时越界）
                stat_kpis['dur'].setText(_ck_fmt_dur(dur))
                _fit_kpis()
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:5945')

    rec_range.currentIndexChanged.connect(
        lambda _i: (rec_page.__setitem__('cur', 1), poll_sessions(), poll_rec_stats()))
    rec_port.currentIndexChanged.connect(
        lambda _i: (rec_page.__setitem__('cur', 1), poll_sessions()))
    rec_sort.currentIndexChanged.connect(
        lambda _i: (rec_page.__setitem__('cur', 1), poll_sessions()))
    pg_prev.clicked.connect(lambda: (rec_page.__setitem__('cur', max(1, rec_page['cur'] - 1)),
                                     poll_sessions()))
    pg_next.clicked.connect(lambda: (rec_page.__setitem__('cur', min(rec_page['pages'], rec_page['cur'] + 1)),
                                     poll_sessions()))

    # ===== Tab 4: 自动化 =====
    au = QWidget()
    au.setObjectName('pageAutomation')   # R041 / C10
    auv = QVBoxLayout(au)
    # 1.5.5：四卡（通知/阈值/自动规则/定时）在 995×760 一屏放下——
    # 卡片间距 6 px、页边距 6 px，页面级不出现滚动条。
    auv.setContentsMargins(8, 6, 8, 6)
    auv.setSpacing(6)
    # 1.5.5：通知只保留渠道开关——「系统通知 / 免打扰」与设置页「自动化」同键，
    # 主窗口不再重复；开机自启/最小化托盘也已统一到设置，故「自动化与启动」卡整体删除。
    g_notify = GlassCard('通知渠道')
    g_notify.set_glyph('bell')   # C20-A3：卡标题图形标识
    nv = QVBoxLayout(g_notify)
    nv.setContentsMargins(12, 14, 12, 12)
    nv.setSpacing(8)
    # 卡高被挤压时按内容最小高度撑住，不允许控件互相覆盖
    nv.setSizeConstraint(QLayout.SetMinimumSize)
    notif_start = QCheckBox('充电开始通知')
    notif_end = QCheckBox('充电结束通知')
    notif_thr = QCheckBox('功率告警通知')
    for w_ in (notif_start, notif_end, notif_thr):
        w_.setFixedHeight(24)   # C20-C5：行高 19 → 24
    _nrow1 = QHBoxLayout()
    _nrow1.setSpacing(18)
    _nrow1.addWidget(notif_start)
    _nrow1.addWidget(notif_end)
    _nrow1.addWidget(notif_thr)
    _nrow1.addStretch(1)
    notif_save = aligned_button('保存通知设置', 'primary', min_w=160, h=32, glyph='sliders')   # C20-C4
    notif_test = aligned_button('测试通知', 'ghost', min_w=120, h=32, glyph='bell')
    _nrow2 = QHBoxLayout()
    _nrow2.setSpacing(8)
    _nrow2.addWidget(notif_save)
    _nrow2.addWidget(notif_test)
    _nrow2.addWidget(ux.info_badge(
        '通知渠道',
        '总开关与免打扰在「设置 → 自动化」里统一设置；'
        '这里只控制三个渠道：充电开始、充电结束、功率越限（阈值为 0 时不告警）。'),
        0, Qt.AlignVCenter)
    _nrow2.addStretch(1)
    nv.addLayout(_nrow1)
    nv.addLayout(_nrow2)
    auv.addWidget(g_notify)
    _tag_qss = 'color:' + TEXT_SUB + '; font-size:12px;'

    def _tag(text, unit=False):
        _l = QLabel(text)
        _l.setStyleSheet('color:' + TEXT_SUB + '; font-size:11px;' if unit else _tag_qss)
        return _l

    g_auto = GlassCard('定时控制')
    g_auto.set_glyph('clock')
    auto_v = QVBoxLayout(g_auto)
    auto_v.setContentsMargins(12, 10, 12, 8)
    auto_v.setSpacing(8)
    # 新增定时任务行（置顶，便于操作）
    add_row = QHBoxLayout()
    add_row.setSpacing(8)
    sched_port = QComboBox()
    sched_port.setFixedHeight(32)
    for _k, (_c, _n) in PORT_NAMES.items():
        sched_port.addItem(_n, _k)
    # R026：QTimeEdit 在应用级 QSS 下不绘制文本——改为时/分两个数字输入框
    sched_hh = ux.IntField(0, 23, 23)
    sched_hh.setFixedWidth(58)
    sched_mm = ux.IntField(0, 59, 0)
    sched_mm.setFixedWidth(58)
    sched_mm.setText('00')
    sched_action = QComboBox()
    sched_action.setFixedHeight(32)
    sched_action.addItem('关闭', 'off')
    sched_action.addItem('开启', 'on')
    sched_add_b = aligned_button('添加定时', 'primary', min_w=88, h=32, glyph='clock')   # C20-C4
    add_row.addWidget(_tag('端口'))
    add_row.addWidget(sched_port)
    add_row.addWidget(_tag('时间'))
    add_row.addWidget(sched_hh)
    _colon = QLabel(':')
    _colon.setObjectName('sub')
    # R044 / C13：冒号原被布局拉伸到 75–88 px，"23 : 00" 两侧空白过大；定宽 8 px 居中
    _colon.setFixedWidth(8)
    _colon.setAlignment(Qt.AlignCenter)
    add_row.addWidget(_colon)
    add_row.addWidget(sched_mm)
    add_row.addWidget(_tag('动作'))
    add_row.addWidget(sched_action)
    add_row.addWidget(sched_add_b)
    auto_v.addLayout(add_row)
    _sep = QFrame()
    _sep.setFrameShape(QFrame.HLine)
    _sep.setStyleSheet('color: rgba(255,255,255,0.12);')
    auto_v.addWidget(_sep)
    # 定时任务列表容器（动态填充）
    sched_list_host = QWidget()
    sched_list_v = QVBoxLayout(sched_list_host)
    sched_list_v.setContentsMargins(0, 0, 0, 0)
    sched_list_v.setSpacing(8)
    # 1.5.5：空态改为单行（图标+文字横排），避免定时卡底部被裁。
    def _sched_empty_widget():
        box = QWidget()
        _lv = QHBoxLayout(box)
        _lv.setContentsMargins(0, 0, 0, 0)
        _lv.setSpacing(6)
        _ic = QLabel()
        _ic.setPixmap(_glyph_pm('clock', 20, '#3d566e'))
        _ic.setAlignment(Qt.AlignCenter)
        _tx = QLabel('暂无定时任务')
        _tx.setObjectName('sub')
        _lv.addStretch(1)
        _lv.addWidget(_ic)
        _lv.addWidget(_tx)
        _lv.addStretch(1)
        return box
    # R044 / C13：空态在定时卡剩余空间内垂直居中（原顶对齐，100% 卡高 350 时空态贴顶）
    sched_list_v.addStretch(1)
    sched_list_v.addWidget(_sched_empty_widget())
    sched_list_v.addStretch(1)
    # R045 / C14：后端允许 50 条定时任务，卡片高度有限。列表放入隐藏滚动条的滚动区，
    # 行高固定 40+，超出时用滚轮/拖动查看；页面级仍保持无滚动条。
    sched_scroll = QScrollArea()
    sched_scroll.setObjectName('schedScroll')
    sched_scroll.setWidgetResizable(True)
    sched_scroll.setFrameShape(QFrame.NoFrame)
    sched_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sched_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sched_scroll.setStyleSheet('QScrollArea{background:transparent;border:none;}')
    sched_scroll.viewport().setAutoFillBackground(False)
    sched_list_host.setObjectName('schedListHost')
    sched_scroll.setWidget(sched_list_host)
    auto_v.addWidget(sched_scroll, 1)
    auv.addWidget(g_auto, 1)
    # R030：自动化页固定布局，去掉外层 QScrollArea（页面不得出现滚动条）
    stack.addWidget(au)
    # ===== 小源页（R026：导航一级入口；AI 配置为唯一主实现）=====
    import re as _re
    # R027：实测发现严格版提示词会诱发推理模型超长思考（1500–4096 token 全花在思考上）。
    # 改为"通用常识直接答 + 应用内数据缺失要明说 + 限制篇幅"，与直连实测验证过的口径一致。
    AI_SYS = ('你是「小源」，星源智控充电面板内置助手。通用充电知识可直接回答；'
              '涉及本机充电数据时只依据给出的数据，没有相关数据就明确说明，不编造数值。'
              '不输出设备型号、凭据等敏感信息。回答用简体中文、结论先行、150 字以内，'
              '可用 Markdown（标题/列表/表格/代码块）。')

    ai_page = QWidget()
    ai_page.setObjectName('pageAi')   # R041 / C10
    _aiv = QVBoxLayout(ai_page)
    _aiv.setContentsMargins(8, 12, 8, 8)
    _aiv.setSpacing(8)
    _ai_tabs = QTabWidget()
    _ai_tabs.setObjectName('setTabs')
    _ai_tabs.setUsesScrollButtons(False)
    _ai_tabs.setElideMode(Qt.ElideRight)
    _ai_tabs.setDocumentMode(True)
    _aiv.addWidget(_ai_tabs)

    _AI = {'busy': False, 'seq': 0, 'msgs': [], 'calls': [],
           'sessions': {}, 'sid': '默认会话'}

    def _ai_pref(key, dflt):
        try:
            return adv_prefs.get(key, dflt)
        except Exception:
            return dflt

    def _ai_ready():
        return bool(_ai_pref('aiEnabled', False)) and adv_prefs.consent_ok()

    def _ai_cfg():
        return ai_service.make_cfg(adv_prefs, adv_secrets, 'chat', AI_SYS)

    # ---- 多会话：本地保存（QSettings），支持新建/重命名/删除 ----
    _sess_store = QSettings('DoubaoWork', 'CuktechPanel')
    try:
        _raw = _sess_store.value('ai/sessions', '')
        if _raw:
            _AI['sessions'] = json.loads(_raw)
    except Exception:
        _AI['sessions'] = {}
    if not isinstance(_AI['sessions'], dict) or not _AI['sessions']:
        _AI['sessions'] = {'默认会话': []}
    _AI['sid'] = next(iter(_AI['sessions']))
    _AI['msgs'] = _AI['sessions'][_AI['sid']]

    def _ai_persist():
        try:
            _AI['sessions'][_AI['sid']] = _AI['msgs']
            _sess_store.setValue('ai/sessions',
                                 json.dumps(_AI['sessions'], ensure_ascii=False))
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:6117')

    # AI 区页内统一 48 px 条——左可选 26 px 头像 + 18 px tab 名，
    # 右状态徽章复用各页自己的动态标签。外层页头已有「小源」，页内条不重复写助手名。
    def _ai_bar(_name, _badge=None, _avatar=False):
        _bar = QWidget()
        _bar.setObjectName('aiBar')
        _bar.setFixedHeight(48)
        _hb = QHBoxLayout(_bar)
        _hb.setContentsMargins(0, 0, 0, 0)
        _hb.setSpacing(8)
        if _avatar:
            _ic = QLabel()
            _ic.setObjectName('aiAvatar')
            _ic.setFixedSize(26, 26)
            _pm = getattr(charger_dev, '_scene_icons', {}).get('ai')
            if _pm is not None and not _pm.isNull():
                _ic.setPixmap(_pm.scaled(26, 26, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            _ic.setStyleSheet('background:transparent;')
            _hb.addWidget(_ic)   # C20-A4：呼吸点换成 26×26 AI 图形，嵌在 48 px 条内不额外占高
        _t = QLabel(_name)
        _t.setStyleSheet('font-size:18px; font-weight:700; color:' + uikit.TOKENS['text']
                         + '; letter-spacing:1px;')
        _hb.addWidget(_t)
        _hb.addStretch(1)
        if _badge is not None:
            _badge.setObjectName('sub')
            _hb.addWidget(_badge)
        return _bar

    # 1.5.6：Agent 操作日志查看（时间/原话/工具与参数/结果/是否经确认）
    def _show_agent_log():
        _dlg = QDialog(w)
        _dlg.setWindowTitle('Agent 操作日志')
        _dlg.resize(820, 520)
        _vv = QVBoxLayout(_dlg)
        _vv.setContentsMargins(16, 16, 16, 16)
        _vv.setSpacing(10)
        _tt = QLabel('每次执行能力都会留痕；仅保存在本机。')
        _tt.setObjectName('sub')
        _tt.setWordWrap(True)
        _vv.addWidget(_tt)
        _te = QPlainTextEdit()
        _te.setReadOnly(True)
        _rows0 = agent_tools.read_log(300)
        _te.setPlainText('\n'.join(
            '%s  %s  → %s(%s)  %s%s' % (
                r.get('ts'), r.get('text'), r.get('tool'),
                json.dumps(r.get('params') or {}, ensure_ascii=False),
                '可' if r.get('ok') else '失败',
                '（经确认）' if r.get('confirmed') else '')
            for r in _rows0) or '暂无操作记录')
        _vv.addWidget(_te, 1)
        _bc = QHBoxLayout()
        _bc.addStretch(1)
        _cl = aligned_button('关闭', 'ghost', min_w=72, h=32)
        _cl.clicked.connect(_dlg.reject)
        _bc.addWidget(_cl)
        _vv.addLayout(_bc)
        _dlg.exec()

    # ============ Tab 1：对话 ============
    _chat_pg = QWidget()
    _cv = QVBoxLayout(_chat_pg)
    _cv.setContentsMargins(12, 12, 12, 12)
    _cv.setSpacing(8)
    _chan = QLabel('未配置')
    _chan.setMaximumWidth(160)
    _cv.addWidget(_ai_bar('对话', _chan, _avatar=True))

    def _elide_lbl(_lbl, _txt, _w=150):
        # R028：按可用像素省略并保留完整 tooltip，避免卡缘硬切且无省略号
        _txt = str(_txt or '')
        try:
            _lbl.setText(QFontMetrics(_lbl.font()).elidedText(
                _txt, Qt.ElideRight, int(_w)))
        except Exception:
            _lbl.setText(_txt[:18] + '…' if len(_txt) > 18 else _txt)
        _lbl.setToolTip(_txt)

    # ==== R029：小源工具调用可视化（白名单 + 三档风险 + 确认 / 撤销） ====
    _TOOL_META = {
        'query_status': ('读取端口状态', 'low',
                         '只读取本机实时状态，不修改任何设置', False),
        'query_energy': ('读取今日充电统计', 'low',
                         '只读取今日统计，不修改任何设置', False),
        'set_port': ('切换端口供电', 'medium',
                     '改变单个端口通断状态，可在本卡片一键撤销', False),
        'all_off': ('全部端口断电', 'high',
                    '立即关闭全部端口，正在充电的设备会中断；'
                    '本操作不提供撤销，需手动重新开启', True),
    }
    _TOOL_WHITELIST = set(_TOOL_META)

    def _tool_new(_action, _target, _params):
        _meta = _TOOL_META.get(_action)
        _AI['tseq'] = int(_AI.get('tseq', 0)) + 1
        _card = {'role': 'tool', 'tid': _AI['tseq'], 'action': _action,
                 'name': (_meta[0] if _meta else '越权请求'),
                 'target': _target, 'params': _params,
                 'risk': (_meta[1] if _meta else 'high'),
                 'impact': (_meta[2] if _meta else
                            '动作「%s」不在应用内权限白名单（仅限读取与端口开关），'
                            '已拒绝，未产生任何变更。' % _action),
                 'irreversible': (_meta[3] if _meta else True),
                 'status': ('pending' if _meta else 'denied'),
                 'sid': _AI['sid'], 'ts': ck_time.fmt(ck_time.HMS),
                 'expanded': False, 'ms': 0, 'result': '', 'err': '',
                 'prev': {}}
        _AI['msgs'].append(_card)
        return _card

    def _tool_intent(_q):
        _t = re.sub(r'[\s，。！？,!.?]', '', str(_q or ''))
        if re.search(r'(全部关闭|全关|断电|所有端口关闭|4个端口关闭)', _t):
            return ('all_off', '全部端口（C1/C2/C3/USB-A）',
                    {'port': 'all', 'action': 'off'})
        _pm = re.search(r'(关闭|关掉|断开|停用|打开|开启)'
                        r'(?:c口|usb-?a|usb口)?([1-4a])$', _t, re.I)
        if _pm:
            _num = _pm.group(2).lower()
            _key = 'a' if _num == 'a' else _num
            _nm = {'1': 'C 口 1', '2': 'C 口 2', '3': 'C 口 3',
                   'a': 'USB-A'}.get(_key, _key)
            return ('set_port', _nm,
                    {'port': _key,
                     'action': ('on' if _pm.group(1) in ('打开', '开启') else 'off')})
        if re.search(r'(今天|今日).*(充|电量|用电|统计)|充电概况|充了多少', _t):
            return ('query_energy', '今日充电统计', {'period': 'today'})
        if re.search(r'当前功率|现在.*功率|端口状态|读取状态|实时状态', _t):
            return ('query_status', '全部端口', {})
        if re.search(r'(删除|清空).*(记录|数据|文件)|格式化|导出.*(密钥|密码)', _t):
            return ('delete_records', '本机数据', {'scope': 'all'})
        return None

    def _tool_exec(_m):
        if _m.get('status') not in ('pending', 'fail', 'confirm2'):
            return
        if _m.get('status') == 'pending' and _m.get('risk') == 'high':
            _m['status'] = 'confirm2'
            _ai_render()
            _ai_persist()
            return
        _act = _m.get('action')
        if _act not in _TOOL_WHITELIST:
            _m['status'] = 'denied'
            _ai_render()
            _ai_persist()
            return
        _m['status'] = 'running'
        _m['t0'] = time.time()
        _ai_render()

        def _fin(_ok, _res='', _err=''):
            _m['ms'] = int((time.time() - _m.get('t0', time.time())) * 1000)
            _m['status'] = 'done' if _ok else 'fail'
            _m['result'] = _res
            _m['err'] = _err
            _ai_render()
            _ai_persist()

        def _bad(r):
            _fin(False, '', '接口异常：%s' % (r,))

        def _stat_cb(r):
            try:
                _code, _d = r
            except Exception:
                return _bad(r)
            if _code != 200 or not isinstance(_d, dict):
                return _bad(_code)
            _ps = _d.get('ports')
            if isinstance(_ps, dict):
                _it = list(_ps.items())
            elif isinstance(_ps, list):
                _it = [(str(_i + 1), _v) for _i, _v in enumerate(_ps)]
            else:
                _it = []
            _lines = []
            for _k, _v in _it:
                if not isinstance(_v, dict):
                    continue
                _on = _v.get('on', _v.get('enabled', _v.get('open')))
                _pw = _v.get('power', _v.get('w'))
                _lines.append('%s%s%s' % (
                    _k, '开' if _on else '关',
                    (' %.1fW' % _pw) if isinstance(_pw, (int, float)) else ''))
            _fin(True, ' · '.join(_lines[:4]) or '状态已读取，无端口明细')

        def _energy_cb(r):
            try:
                _code, _d = r
            except Exception:
                return _bad(r)
            if _code != 200 or not isinstance(_d, dict):
                return _bad(_code)
            _s = _d.get('stats') if isinstance(_d.get('stats'), dict) else _d
            _wh = _s.get('total_wh', _s.get('wh', _s.get('energy_wh')))
            if isinstance(_wh, (int, float)):
                _fin(True, '今日合计 %s' % _ck_fmt_energy(_wh))
            else:
                _fin(True, '今日统计已读取')

        if _act == 'query_status':
            qwait(lambda: api('GET', '/api/status'), _stat_cb)
            return
        if _act == 'query_energy':
            qwait(lambda: api('GET', '/api/energy/stats?period=today'), _energy_cb)
            return
        if _act == 'set_port':
            _p = _m.get('params') or {}
            _key = _p.get('port')
            _val = _p.get('action')

            def _pre(r):
                try:
                    _code, _d = r
                except Exception:
                    return _bad(r)
                _prev = None
                _ps = _d.get('ports') if isinstance(_d, dict) else None
                if isinstance(_ps, dict) and isinstance(_ps.get(_key), dict):
                    _v = _ps[_key]
                    _prev = _v.get('on', _v.get('enabled', _v.get('open')))
                _m['prev'] = {'on': _prev}

                def _post(r2):
                    try:
                        _c2, _d2 = r2
                    except Exception:
                        return _bad(r2)
                    if _c2 != 200:
                        return _fin(False, '', '端口接口返回 %s' % _c2)
                    _fin(True, '%s 已%s' % (_m.get('target'),
                                            '开启' if _val == 'on' else '关闭'))

                qwait(lambda: api('POST', '/api/port',
                                  {'port': _key, 'action': _val, 'client': '界面按钮'}), _post)

            qwait(lambda: api('GET', '/api/status'), _pre)
            return
        if _act == 'all_off':
            def _ao(r):
                try:
                    _code, _d = r
                except Exception:
                    return _bad(r)
                if _code != 200:
                    return _fin(False, '', '接口返回 %s' % _code)
                _fin(True, '4 个端口已全部断电，需手动重新开启')
            qwait(lambda: api('POST', '/api/port',
                              {'port': 'all', 'action': 'off', 'client': '界面按钮'}), _ao)
            return
        _fin(False, '', '该动作暂无执行器')

    def _tool_undo(_m):
        if _m.get('risk') != 'medium' or _m.get('status') != 'done' \
                or _m.get('undo_of') or _m.get('undone'):
            return
        _p = _m.get('params') or {}
        _prev = (_m.get('prev') or {}).get('on')
        if _prev is None:
            try:
                w._toast.show('未记录到原状态，无法撤销', 'warning')
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:6332')
            return
        _nc = _tool_new('set_port', _m.get('target'),
                        {'port': _p.get('port'),
                         'action': ('on' if _prev else 'off')})
        _nc['name'] = '撤销：%s' % _m.get('name')
        _nc['undo_of'] = _m.get('tid')
        _nc['impact'] = '恢复到执行前状态（%s）' % ('开启' if _prev else '关闭')
        _m['undone'] = True
        _ai_render()
        _ai_persist()
        QTimer.singleShot(0, lambda c=_nc: _tool_exec(c))

    # R028：会话工具独立成第二行，避免 900 宽时被挤压成「搜…」
    _hd2 = QHBoxLayout()
    _hd2.setSpacing(8)
    _sess_cb = QComboBox()
    _sess_cb.setMinimumWidth(120)
    _sess_cb.setMaximumWidth(180)
    for _n in _AI['sessions']:
        _sess_cb.addItem(_n)
    _sess_cb.setCurrentText(_AI['sid'])
    _hd2.addWidget(_sess_cb)

    def _sess_filter(t):
        # R027：会话检索——按下拉实时过滤（保持原顺序，不重建会话数据）
        _t = (t or '').strip().lower()
        _sess_cb.blockSignals(True)
        _cur = _AI['sid']
        _sess_cb.clear()
        for _n in _AI['sessions']:
            if not _t or _t in _n.lower():
                _sess_cb.addItem(_n)
        if _t and _sess_cb.findText(_cur) < 0 and _sess_cb.count():
            _sess_cb.setCurrentIndex(0)
        elif _sess_cb.findText(_cur) >= 0:
            _sess_cb.setCurrentText(_cur)
        _sess_cb.blockSignals(False)

    _sess_srch = QLineEdit()
    _sess_srch.setPlaceholderText('搜索会话')
    _sess_srch.setToolTip('按会话名称过滤')
    _sess_srch.setMaximumWidth(120)
    _sess_srch.setMinimumWidth(96)
    _sess_srch.setMinimumHeight(28)
    _sess_srch.setClearButtonEnabled(True)
    _sess_srch.textChanged.connect(_sess_filter)
    _hd2.addWidget(_sess_srch)
    _newb = aligned_button('新建', 'ghost', min_w=64, h=32, glyph='plus')
    _renb = aligned_button('重命名', 'ghost', min_w=72, h=32, glyph='pen')
    _delb = aligned_button('删除', 'ghost', min_w=64, h=32, glyph='trash')
    _clearb = aligned_button('清空对话', 'ghost', min_w=84, h=32, glyph='broom')
    for _b in (_newb, _renb, _delb, _clearb):
        _hd2.addWidget(_b)
    _hd2.addStretch(1)
    _cv.addLayout(_hd2)

    _log = QTextBrowser()
    _log.setObjectName('aiLog')
    # R041 / C10：日志区必须占位——任何窗口尺寸下不被压成 0 高
    _log.setMinimumHeight(160)
    _log.setOpenLinks(False)
    _log.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    _log.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def _ai_anchor(url):
        # R027：折叠/展开 与 查看原始响应（消息内链接）
        _h = url.toString()
        try:
            _act, _ni = _h.split(':', 1)
            _mi = int(_ni)
            _m = _AI['msgs'][_mi]
        except Exception:
            return
        if _act in ('tf', 'tc', 'tu', 'tr', 'tm'):
            if _act == 'tf':
                _tool_exec(_m)
            elif _act == 'tc':
                if _m.get('status') in ('pending', 'confirm2'):
                    _m['status'] = 'cancel'
                    _m['result'] = '已取消，未执行任何变更'
                    _ai_render()
                    _ai_persist()
            elif _act == 'tu':
                _tool_undo(_m)
            elif _act == 'tr':
                if _m.get('status') == 'fail':
                    _m['status'] = 'pending'
                    _m['err'] = ''
                    _ai_render()
                    _tool_exec(_m)
            elif _act == 'tm':
                _m['expanded'] = not _m.get('expanded')
                _ai_render()
            return
        if _act == 'more':
            _sb = _log.verticalScrollBar()
            _old = _sb.value()
            _oldmax = _sb.maximum()
            _AI['win'] = int(_AI.get('win', 50)) + 50
            _ai_render()
            _sb = _log.verticalScrollBar()
            _sb.setValue(_old + max(40, _sb.maximum() - _oldmax))
            return
        if _act == 'c':
            _m['collapsed'] = True
            _ai_render()
        elif _act == 'x':
            _m['collapsed'] = False
            _ai_render()
        elif _act == 'r':
            _dlg = QDialog(w)
            _dlg.setWindowTitle('原始响应（Markdown）· %s' % _m.get('meta') or '')
            _dlg.resize(780, 560)
            _dv = QVBoxLayout(_dlg)
            _dv.setContentsMargins(16, 12, 16, 12)
            _dv.setSpacing(10)
            _hdr, _badge = dialog_head('原始响应', str(_m.get('meta') or ''))
            _dv.addWidget(_hdr)
            _raw = QPlainTextEdit()
            _raw.setPlainText(_m['text'])
            _raw.setReadOnly(True)
            _raw.setStyleSheet('font-family:Consolas,monospace; font-size:%s;'
                               ' border-radius:8px; background:%s;'
                               % (ux.TOKENS['font_s'], ux.TOKENS['code_bg']))
            _dv.addWidget(_raw)
            _hb = QHBoxLayout()
            _hb.setSpacing(8)
            _hb.addStretch(1)
            _cpb = QPushButton('复制原文')
            _cpb.setObjectName('ghost')
            _cpb.setMinimumSize(120, 32)
            _cpb.clicked.connect(lambda: QApplication.clipboard().setText(_m['text']))
            _clb = QPushButton('关闭')
            _clb.setObjectName('ghost')
            _clb.setMinimumSize(96, 32)
            _clb.clicked.connect(_dlg.accept)
            _hb.addWidget(_cpb)
            _hb.addWidget(_clb)
            _dv.addLayout(_hb)
            _dlg.exec()

    _log.anchorClicked.connect(_ai_anchor)
    _log.setStyleSheet('font-size:13px; border-radius:12px; padding:10px;'
                       ' background:rgba(255,255,255,0.045);'
                       ' border:1px solid rgba(255,255,255,0.10);')
    _cv.addWidget(_log, 1)

    def _ai_md(txt):
        import html as _h
        s = _h.escape(str(txt))
        s = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
        s = _re.sub(r'`([^`]+)`', r'<span style="background:rgba(255,255,255,.10);'
                                  r'border-radius:4px;padding:0 4px;">\1</span>', s)
        out = []
        for ln in s.split('\n'):
            t = ln.rstrip()
            if t.startswith('### '):
                out.append('<div style="font-weight:700;">%s</div>' % t[4:])
            elif t.startswith('## '):
                out.append('<div style="font-weight:700;font-size:14px;">%s</div>' % t[3:])
            elif t.startswith('# '):
                out.append('<div style="font-weight:800;font-size:16px;">%s</div>' % t[2:])
            elif t.startswith('- ') or t.startswith('* '):
                out.append('&nbsp;&nbsp;&bull; ' + t[2:])
            elif t.startswith('> '):
                out.append('<span style="color:' + TEXT_SUB + ';">| %s</span>' % t[2:])
            else:
                out.append(t)
        return '<br>'.join(out)

    _RISK_C = {'low': OK, 'medium': WARN_SOFT, 'high': AI_ERR}
    _RISK_T = {'low': '低风险', 'medium': '中风险', 'high': '高风险'}
    _ST_T = {'pending': '待确认', 'confirm2': '待二次确认', 'running': '执行中',
             'done': '已完成', 'fail': '失败', 'cancel': '已取消', 'denied': '已拒绝'}

    def _tool_html(_m, _mi):
        _risk = _m.get('risk', 'low')
        _c = _RISK_C.get(_risk, TEXT_SUB)
        _st = _m.get('status', 'pending')

        def _disp(_s):
            _s = str(_s or '')
            if _m.get('expanded') or len(_s) <= 120:
                return _ai_md(_s)
            return (_ai_md(_s[:120]) +
                    '… <a href="tm:%d" style="color:#8fd6ff;">展开</a>' % _mi)

        def _btn(_bg, _href, _txt):
            return ('<span style="background-color:%s;">&nbsp;<a href="%s" '
                    'style="color:#0b1220;text-decoration:none;">%s</a>&nbsp;</span>'
                    % (_bg, _href, _txt))

        _acts = []
        if _st == 'pending' and _risk == 'high':
            _acts.append('<span style="color:#ff7a7a;">需确认后执行；取消不产生任何变更。</span>')
            _acts.append(_btn(_c, 'tf:%d' % _mi, '执行'))
            _acts.append(_btn('#3a4658', 'tc:%d' % _mi, '取消'))
        elif _st == 'confirm2':
            _acts.append('<span style="color:#ff7a7a;font-weight:700;">'
                         '二次确认：再次点击立即执行，且不可撤销。</span>')
            _acts.append(_btn(AI_ERR, 'tf:%d' % _mi, '确认执行'))
            _acts.append(_btn('#3a4658', 'tc:%d' % _mi, '取消'))
        elif _st == 'running':
            _acts.append('<span style="color:' + TEXT_SUB + ';">正在执行，请稍候…</span>')
        elif _st == 'done':
            _acts.append('<span style="color:#34d399;font-weight:700;">已执行</span>'
                         '<span style="color:' + TEXT_SUB + ';"> &middot; 耗时 %d ms</span>'
                         % int(_m.get('ms', 0) or 0))
            if _m.get('result'):
                _acts.append('<span style="color:#cfe2f5;">实际变更：%s</span>'
                             % _ai_md(_m['result']))
            if _risk == 'medium' and not _m.get('undo_of'):
                if _m.get('undone'):
                    _acts.append('<span style="color:' + TEXT_SUB + ';">已撤销</span>')
                else:
                    _acts.append('<a href="tu:%d" style="color:#8fd6ff;">撤销</a>' % _mi)
        elif _st == 'fail':
            _acts.append('<span style="color:#ff7a7a;font-weight:700;">执行失败</span>'
                         '<span style="color:#cfe2f5;"> &middot; %s</span>'
                         % _ai_md(_m.get('err') or '未知原因'))
            _acts.append('<a href="tr:%d" style="color:#8fd6ff;">重试</a>' % _mi)
        elif _st == 'cancel':
            _acts.append('<span style="color:' + TEXT_SUB + ';">已取消（未执行任何变更）</span>')
        elif _st == 'denied':
            _acts.append('<span style="color:#ff7a7a;font-weight:700;">已拒绝</span>'
                         '<span style="color:' + TEXT_SUB + ';"> &middot; 白名单外动作，未执行</span>')

        _par = '，'.join('%s=%s' % (_k, _v)
                        for _k, _v in (_m.get('params') or {}).items())
        # R045 / C14：单块 <br> 排版——原嵌套 <div> 在块间距处不上色，形成一条条浅色“白条”缝隙
        _btns = ''.join('<br>%s' % _x for _x in _acts)
        return ((
            '<div style="margin:10px 0 2px 0;">'
            '<span style="color:#8fd6ff;font-weight:700;">工具调用</span>'
            '<span style="color:' + TEXT_SUB + ';"> &middot; 会话「%s」 &middot; %s</span></div>'
            '<div style="border:1px solid rgba(255,255,255,0.18);'
            'border-left:3px solid %s;background-color:rgba(255,255,255,0.055);'
            'padding:7px 10px;line-height:1.55;">'
            '<span style="color:%s;font-weight:700;">%s</span>'
            '<span style="color:' + TEXT_SUB + ';"> &middot; %s &middot; %s</span><br>'
            '<span style="color:#cfe2f5;">目标：%s</span><br>'
            '<span style="color:#cfe2f5;">参数：%s</span><br>'
            '<span style="color:' + TEXT_SUB + ';">预期影响：%s</span>%s</div>'
            ) % (_m.get('sid'), _m.get('ts'), _c, _c,
                 _ai_md(_m.get('name')), _RISK_T.get(_risk, _risk),
                 _ST_T.get(_st, _st), _ai_md(_m.get('target')),
                 _disp(_par or '无（只读）'), _disp(_m.get('impact')), _btns))

    def _ai_render():
        _log.clear()
        if not _AI['msgs']:
            _log.setHtml(
                '<div style="color:' + TEXT_SUB + ';line-height:1.9;">'
                '<div style="font-size:16px;font-weight:700;color:' + TEXT + ';">小源能做什么</div>'
                '&bull; 解读充电日报与异常事件<br>'
                '&bull; 解释掉速 / 过热 / 断开原因<br>'
                '&bull; 回答端口、功率、设备识别相关问题<br>'
                '&bull; 给出自动化建议（只建议，不越权执行）'
                '<br><br>可直接输入问题，或点下面的快捷技能。</div>')
            return
        parts = []
        # R027：消息分页加载——只渲染最近 _AI['win'] 条（默认 50），更早的走"加载更早"锚点
        _msgs = _AI['msgs']
        _win = int(_AI.get('win', 50))
        _skip = max(0, len(_msgs) - _win)
        if _skip > 0:
            parts.append('<div style="text-align:center;margin:2px 0 8px 0;">'
                         '<a href="more:%d" style="color:#8fd6ff;">加载更早消息（还有 %d 条）</a>'
                         '</div>' % (_skip, _skip))
        for _ri, _m in enumerate(_msgs[_skip:]):
            _mi = _skip + _ri
            if _m.get('role') == 'tool':
                parts.append(_tool_html(_m, _mi))
                continue
            if _m['role'] == 'user':
                parts.append(
                    '<div style="margin:8px 0 2px 0;"><span style="color:#8fd6ff;'
                    'font-weight:700;">我</span></div>'
                    '<div style="background:rgba(56,226,255,0.10);border:1px solid '
                    'rgba(56,226,255,0.22);border-radius:8px;padding:6px 10px;">%s</div>'
                    % _ai_md(_m['text']))
            else:
                _meta = ('<span style="color:' + TEXT_SUB + ';"> &middot; %s</span>' % _m.get('meta')
                         if _m.get('meta') else '')
                _txt = _m['text']
                # R027：长回复默认折叠（>500 字），提供 展开/折叠 与 查看原始响应 链接
                _lnk = ''
                if (not _m.get('stream')) and len(_txt) > 500:
                    if _m.get('collapsed'):
                        _body_txt = _txt[:280] + '……'
                        _lnk = '<br><a href="x:%d" style="color:#8fd6ff;">展开全文（%d 字）</a>' % (
                            _mi, len(_txt))
                    else:
                        _body_txt = _txt
                        _lnk = ('<br><a href="c:%d" style="color:#8fd6ff;">折叠</a>'
                                '&nbsp;<a href="r:%d" style="color:#8fd6ff;">查看原始响应</a>'
                                % (_mi, _mi))
                else:
                    _body_txt = _txt
                _body = _ai_md(_body_txt)
                # R027：流式生成时的跟随光标（必须在转义之后再拼 HTML，否则会被当成纯文本）
                if _m.get('stream') and _AI['busy']:
                    _body += ' <span style="color:#8fd6ff;">▌</span>'
                if _lnk:
                    _body += _lnk
                parts.append(
                    '<div style="margin:10px 0 2px 0;"><span style="color:#b7a6ff;'
                    'font-weight:700;">小源</span>%s</div>'
                    '<div style="border-left:2px solid rgba(183,166,255,0.55);'
                    'padding:2px 0 2px 10px;">%s</div>'
                    % (_meta, _body))
        _log.setHtml(''.join(parts))
        # R027：滚到底必须在文档布局完成后执行（setHtml 后高度尚未稳定，直接设值会停在文档中部）
        def _to_bottom():
            try:
                _sb = _log.verticalScrollBar()
                _sb.setValue(_sb.maximum())
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:6639')
        w._ai_to_bottom = _to_bottom
        # R028：富文本布局分多拍完成，分时补滚 4 次，折叠链接不再落到视口外
        for _d in (30, 160, 400, 800):
            QTimer.singleShot(_d, _to_bottom)

    # ---- 快捷技能 ----
    _qrow = QHBoxLayout()
    _qrow.setSpacing(8)
    for _qtxt in ('今天充电概况', '为什么掉速', '设备健康趋势', '本周异常事件'):
        _qb = QPushButton(_qtxt)
        _qb.setObjectName('chip')
        _qb.setMinimumHeight(28)
        _qb.setCursor(Qt.PointingHandCursor)
        _qb.setStyleSheet('QPushButton{font-size:11px;padding:2px 10px;border-radius:8px;'
                          'color:#cfe2f5;border:1px solid rgba(255,255,255,0.16);'
                          'background:rgba(255,255,255,0.05);}'
                          'QPushButton:hover{border:1px solid rgba(56,226,255,0.45);}'
                          'QPushButton:pressed{background:rgba(56,226,255,0.26);'
                          'border:1px solid rgba(56,226,255,0.70);}')
        _qb.clicked.connect(lambda _=False, t=_qtxt: _ai_send(t))
        _qrow.addWidget(_qb)
    _qrow.addStretch(1)
    _cv.addLayout(_qrow)

    # ---- 上下文芯片（勾选=纳入上下文，取消=移除） ----
    _crow = QHBoxLayout()
    _crow.setSpacing(8)
    _chip_style = ('QPushButton{font-size:11px;padding:2px 10px;border-radius:8px;'
                   'color:' + TEXT_SUB + ';border:1px solid rgba(255,255,255,0.14);'
                   'background:transparent;}'
                   'QPushButton:checked{color:' + TEXT + ';'
                   'border:1px solid rgba(56,226,255,0.45);'
                   'background:rgba(56,226,255,0.14);}'
                   'QPushButton:pressed{background:rgba(56,226,255,0.30);'
                   'border:1px solid rgba(56,226,255,0.75);}')
    _chip_session = QPushButton('会话数据')
    _chip_period = QPushButton('时间段 24h')
    _chip_port = QPushButton('当前端口')
    for _c in (_chip_session, _chip_period, _chip_port):
        _c.setCheckable(True)
        _c.setChecked(True)
        _c.setMinimumHeight(28)   # C20-C6：两类 chip 统一 28 高
        _c.setCursor(Qt.PointingHandCursor)
        _c.setStyleSheet(_chip_style)
        _c.setToolTip('取消勾选即从上下文移除该数据')
        _crow.addWidget(_c)
    _crow.addStretch(1)
    _cv.addLayout(_crow)

    class _AiIn(QPlainTextEdit):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setPlaceholderText('向小源提问…（Enter 发送，Shift+Enter 换行）')
            self.setFixedHeight(62)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.setStyleSheet('font-size:13px;border-radius:8px;padding:6px 10px;'
                               'background:rgba(255,255,255,0.06);'
                               'border:1px solid rgba(255,255,255,0.14);color:' + TEXT + ';')

        def keyPressEvent(self, ev):
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and \
                    not (ev.modifiers() & Qt.ShiftModifier):
                if bool(_ai_pref('aiEnterSend', True)):
                    _ai_send()
                    return
            super().keyPressEvent(ev)

    _inp = _AiIn()
    _cv.addWidget(_inp)

    class _ElideLabel(QLabel):
        """R042 / C11：窄窗口不硬裁——按可用宽度降级文案并省略号收尾，完整文案进 tooltip。"""
        _SHORT = 'Enter 发送 · Ctrl+K 聚焦'
        _MINI = 'Enter 发送'

        def __init__(self, text, parent=None):
            super().__init__(text, parent)
            self._full = text
            self.setToolTip(text)
            self.setMinimumWidth(88)
            self.setMaximumWidth(340)

        def sizeHint(self):
            return QSize(340, super().sizeHint().height())

        def resizeEvent(self, ev):
            super().resizeEvent(ev)
            _w = max(0, self.width())
            _t = self._full if _w >= 300 else (self._SHORT if _w >= 140 else self._MINI)
            self.setText(QFontMetrics(self.font()).elidedText(_t, Qt.ElideRight, _w))

    def _ai_grow():
        try:
            _h = int(_inp.document().size().height()) + 18
            _inp.setFixedHeight(max(62, min(96, _h)))
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:6711')
    _inp.textChanged.connect(_ai_grow)

    _brow = QHBoxLayout()
    _brow.setSpacing(8)
    _sendb = aligned_button('发送', 'primary', min_w=84, h=32)
    _stopb = aligned_button('停止', 'ghost', min_w=72, h=32)
    _regenb = aligned_button('重新生成', 'ghost', min_w=96, h=32)
    _copyb = aligned_button('复制最近回复', 'ghost', min_w=122, h=32)
    _hintl = _ElideLabel('Enter 发送 · Shift+Enter 换行 · Ctrl+K 聚焦 · Esc 停止')
    _hintl.setObjectName('sub')
    _brow.addWidget(_sendb)
    _brow.addWidget(_stopb)
    _brow.addWidget(_regenb)
    _brow.addWidget(_copyb)
    _brow.addStretch(1)
    _brow.addWidget(_hintl)
    _cv.addLayout(_brow)
    _ai_tabs.addTab(_chat_pg, '对话')

    # ============ Tab 2：AI 配置 ============
    _cfg_pg = QWidget()
    _cfg_v = QVBoxLayout(_cfg_pg)
    _cfg_v.setContentsMargins(12, 12, 12, 12)
    _cfg_v.setSpacing(8)
    _chan2 = QLabel('未配置')
    _chan2.setMaximumWidth(160)
    _cfg_v.addWidget(_ai_bar('AI 配置', _chan2))
    # 1.5.7：2×2 固定栅格——同排两卡等高（模型通道=能力开关、参数=系统提示词），
    # 两列总高一致、底边对齐；卡内控件按序占位，行距统一 6/8。
    _cfg_grid = QGridLayout()
    _cfg_grid.setSpacing(12)
    _cfg_grid.setColumnStretch(0, 3)
    _cfg_grid.setColumnStretch(1, 2)
    _cfg_grid.setRowStretch(0, 1)
    _cfg_grid.setRowStretch(1, 1)

    def _ai_card(title, row, col, glyph=None, min_h=0):
        _g = GlassCard(title)
        if glyph:
            _g.set_glyph(glyph)
        _g.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        if min_h:
            _g.setMinimumHeight(int(min_h))
        _v = QVBoxLayout(_g)
        _v.setContentsMargins(12, 16, 12, 12)
        _v.setSpacing(6)
        _cfg_grid.addWidget(_g, row, col)
        return _v

    def _fix_labels(_f, _w=84):
        for _i in range(_f.rowCount()):
            _it = _f.itemAt(_i, QFormLayout.LabelRole)
            _lb = _it.widget() if _it is not None else None
            if _lb is not None:
                _lb.setMinimumWidth(int(_w))

    def _form():
        _f = QFormLayout()
        _f.setContentsMargins(0, 0, 0, 0)
        _f.setHorizontalSpacing(12)
        _f.setVerticalSpacing(6)
        _f.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        _f.setRowWrapPolicy(QFormLayout.DontWrapRows)
        _f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return _f

    _v1 = _ai_card('模型通道', 0, 0, 'chat', min_h=216)
    _f1 = _form()
    _v1.addLayout(_f1)
    _prov = QComboBox()
    _prov.setObjectName('aiProvider')
    _prov.addItems(['DeepSeek 云端', '本地端点'])
    _prov.setCurrentText(str(_ai_pref('aiProvider', 'DeepSeek 云端')))
    _base = QLineEdit(str(_ai_pref('aiBaseUrl', 'https://api.deepseek.com')))
    _base.setObjectName('aiBase')
    _base.setPlaceholderText('https://api.deepseek.com 或 http://127.0.0.1:1234/v1')
    _model = QLineEdit(str(_ai_pref('aiModel', 'deepseek-chat')))
    _model.setObjectName('aiModel')
    _key = QLineEdit()
    _key.setObjectName('aiKey')
    _key.setEchoMode(QLineEdit.Password)
    _k_old = ux.dpapi_dec((_AC_pf.get(_AC_act) or {}).get('key_enc')) \
        or (adv_secrets.load_key() or '')
    _key.setPlaceholderText((_mask_secret(_k_old) + ' · 已保存') if _k_old
                            else 'DPAPI 加密保存，仅本机')
    _KOLD = [_k_old]
    _clrkey = aligned_button('清除密钥', 'ghost', min_w=84, h=28)
    _clrkey.setAutoDefault(False)
    _clrkey.setToolTip('删除本套配置里保存的 API Key（其它字段不动）')
    _f1.addRow('通道', _prov)
    _ac_sel = QComboBox()
    _ac_sel.setObjectName('aiProfile')
    _ac_sel.setMinimumHeight(28)
    for _pk0 in _AC_pf:
        _ac_sel.addItem(str((_AC_pf[_pk0] or {}).get('name') or _pk0), _pk0)
    _asi = _ac_sel.findData(_AC_act)
    if _asi >= 0:
        _ac_sel.setCurrentIndex(_asi)
    _ac_newb = aligned_button('新建', 'ghost', min_w=56, h=28)
    _ac_newb.setObjectName('aiNewProfile')
    _ac_newb.setAutoDefault(False)
    _ac_newb.setToolTip('把当前通道另存为一套命名配置（可保存多套云端配置）')
    _ac_row = QHBoxLayout()
    _ac_row.setSpacing(6)
    _ac_row.addWidget(_ac_sel, 1)
    _ac_row.addWidget(_ac_newb)
    _f1.addRow('配置', _ac_row)
    _f1.addRow('Base URL', _base)
    _f1.addRow('模型', _model)
    _keyrow = QHBoxLayout()
    _keyrow.setSpacing(6)
    _keyrow.addWidget(_key, 1)
    _keyrow.addWidget(_clrkey)
    _f1.addRow('API Key', _keyrow)
    _testb = aligned_button('测试连接', 'primary', min_w=104, h=32)
    _testst = QLabel('未测试')
    _testst.setObjectName('sub')
    _trow = QHBoxLayout()
    _trow.setSpacing(8)
    _trow.addWidget(_testb)
    _trow.addWidget(_testst, 1)
    _f1.addRow('状态', _trow)
    _fix_labels(_f1)
    if _AC_bad:     # 1.5.7：解析失败 → 原位可视提示（占已有行，不改变卡片几何）
        _testst.setText('配置需重新填写（读取失败，已回落默认值）')
        _testst.setStyleSheet('color:%s;font-size:12px;' % WARN)
        _testst.setToolTip('本机 ai_config.json 无法解析，已回落默认值；'
                           '请重新填写通道、Base URL 与 API Key 后保存')

    # 能力开关：每项一行「开关 + 说明 + 最近使用次数」，底部月度统计
    _v3 = _ai_card('能力开关', 0, 1, 'gear', min_h=224)
    _c1 = QCheckBox('对话')
    _c1.setChecked(bool(_ai_pref('aiEnabled', False)))
    _c2 = QCheckBox('日报解读')
    _c2.setChecked(bool(_ai_pref('aiDailyReport', False)))
    _c3 = QCheckBox('异常解释')
    _c3.setChecked(bool(_ai_pref('aiExplain', True)))
    _c4 = QCheckBox('设备识别建议')
    _c4.setChecked(bool(_ai_pref('aiDeviceHint', True)))
    _c5 = QCheckBox('分箱复核')
    _c5.setChecked(bool(_ai_pref('aiSceneBoxReview', True)))
    _c6 = QCheckBox('诊断分析')
    _c6.setChecked(bool(_ai_pref('aiSceneDiag', True)))
    _c7 = QCheckBox('自动建议')
    _c7.setChecked(bool(_ai_pref('aiSceneAuto', False)))
    _cap = QGridLayout()
    _cap.setHorizontalSpacing(10)
    _cap.setVerticalSpacing(2)
    _cap.setColumnStretch(1, 1)
    _cap_rows = []
    for _i, (_c, _ds, _sc) in enumerate((
            (_c1, '小源对话问答', 'chat'),
            (_c2, '充电日报的自然语言解读', ''),
            (_c3, '掉速/异常的原因说明', 'diagnosis'),
            (_c4, '分箱与设备归属建议', ''),
            (_c5, '低置信分箱的复核建议', 'box_review'),
            (_c6, '诊断页 AI 结论', 'diagnosis'),
            (_c7, 'Agent 自动执行建议', 'agent'))):
        _c.setMinimumHeight(22)
        _c.setFixedWidth(112)
        _dl = _ElideRow(_ds, 1)
        _dl.setObjectName('sub')
        _dl.setToolTip(_ds)
        _ct = QLabel('—')
        _ct.setObjectName('sub')
        _ct.setFixedWidth(52)
        _ct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        _ct.setToolTip('最近 7 天该能力的调用次数（来自 AI 审计）')
        _cap.addWidget(_c, _i, 0)
        _cap.addWidget(_dl, _i, 1)
        _cap.addWidget(_ct, _i, 2)
        _cap_rows.append((_ct, _sc))
    _v3.addLayout(_cap)
    _usage_lbl = QLabel('')
    _usage_lbl.setObjectName('sub')
    _usage_lbl.setWordWrap(True)
    _usage_lbl.setFixedHeight(32)
    _v3.addWidget(_usage_lbl)
    _v3.addStretch(1)

    _v4 = _ai_card('参数', 1, 0, 'sliders', min_h=224)
    _f4 = _form()
    _v4.addLayout(_f4)
    _temp = QComboBox()
    for _v, _n in (('0.0', '严谨'), ('0.3', '标准（默认）'), ('0.7', '发散')):
        _temp.addItem('%s · %s' % (_v, _n), float(_v))
    _tix = _temp.findData(float(_ai_pref('aiTemperature', 0.3)))
    _temp.setCurrentIndex(_tix if _tix >= 0 else 1)
    _maxt = ux.IntField(100, 4096, int(_ai_pref('aiMaxTokens', 700)))
    _tmo = ux.IntField(5, 180, int(_ai_pref('aiTimeout', 30)))
    _rty = ux.IntField(0, 3, int(_ai_pref('aiRetries', 1)))
    _ctx = QComboBox()
    for _d in (7, 30, 90):
        _ctx.addItem('最近 %d 天' % _d, _d)
    _cix = _ctx.findData(int(_ai_pref('aiCtxDays', 7) or 7))
    _ctx.setCurrentIndex(_cix if _cix >= 0 else 0)
    # R026：900×700 下曾出现行压缩重叠——所有输入控件给最小高度，禁止被挤扁
    for _wgt in (_prov, _base, _model, _key, _temp, _maxt, _tmo, _rty, _ctx):
        _wgt.setMinimumHeight(28)
    _tmo.setToolTip('请求超时 5–180 秒（默认 30）。超时后回落本地简答，不会卡住界面。')
    _rty.setToolTip('失败后重试次数 0–3（默认 1）。')
    _ctx.setToolTip('组装上下文时选取的会话时间范围。')
    _f4.addRow('温度', _temp)
    _f4.addRow('最大长度', _maxt)
    _f4.addRow('请求超时（秒） ?', _tmo)
    _f4.addRow('重试次数 ?', _rty)
    _f4.addRow('上下文范围 ?', _ctx)
    _f4.labelForField(_tmo).setToolTip(
        '范围 5–180 秒，默认 30；超时后回落本地简答，不会卡住界面。')
    _f4.labelForField(_rty).setToolTip('失败后重试次数 0–3，默认 1。')
    _f4.labelForField(_ctx).setToolTip('组装上下文时选取的会话时间范围。')
    _fix_labels(_f4)

    # 1.5.6：系统提示词集中在此编辑，可恢复默认；保存后立即生效。
    # 1.5.7：补真实内容（字数/最后修改/生效范围/预览行数/快捷入口），
    #        并把原 Agent 搭建页的固定边界说明并入本卡下方（只存一份）。
    _v5 = _ai_card('系统提示词', 1, 1, 'doc', min_h=224)
    _prompt_text = [str(_ai_pref('aiSystemPrompt', '') or '')
                    or ai_service.DEFAULT_SYSTEM]
    _prompt_prev = QLabel()
    _prompt_prev.setObjectName('sub')
    _prompt_prev.setWordWrap(True)
    _prompt_prev.setFixedHeight(58)
    _prompt_prev.setStyleSheet('border-radius:8px; padding:6px; background:'
                               'rgba(10,20,40,0.55); color:' + TEXT +
                               '; border:1px solid rgba(120,190,255,0.14);')
    _prompt_prev.setToolTip('结构化提示词：身份职责 / 领域事实 / 数据口径 / '
                            '回答规则 / 行为边界 / 表达要求。')
    _v5.addWidget(_prompt_prev)
    _prompt_meta = QLabel('')
    _prompt_meta.setObjectName('sub')
    _prompt_meta.setFixedHeight(46)
    _v5.addWidget(_prompt_meta)

    def _paint_prompt():
        _t = _prompt_text[0]
        _prompt_prev.setText(_t[:120].replace('\n', ' ') +
                             ('…' if len(_t) > 120 else ''))
        _at = str(adv_prefs.get('aiPromptSavedAt') or '') or '默认（未改）'
        _ln = max(1, min(4, (len(_t) + 39) // 40))
        _prompt_meta.setText(
            '字数 %d · 最后修改 %s\n生效范围：对话 / 复核 / 诊断 共用\n预览摘要 %d 行'
            % (len(_t), _at, _ln))
    _paint_prompt()

    def _edit_prompt():
        _dlg = QDialog(w)
        _dlg.setWindowTitle('编辑系统提示词')
        _dlg.resize(760, 540)
        _dv = QVBoxLayout(_dlg)
        _dv.setContentsMargins(16, 16, 16, 16)
        _dv.setSpacing(10)
        _te = QPlainTextEdit(_prompt_text[0])
        _te.setStyleSheet('border-radius:8px; padding:6px; background:'
                          'rgba(10,20,40,0.55); color:' + TEXT +
                          '; border:1px solid rgba(120,190,255,0.14);')
        _dv.addWidget(_te, 1)
        _db = QHBoxLayout()
        _dr = aligned_button('恢复默认', 'ghost', min_w=88, h=30)
        _dr.clicked.connect(lambda: _te.setPlainText(ai_service.DEFAULT_SYSTEM))
        _dc = aligned_button('取消', 'ghost', min_w=72, h=30)
        _dc.clicked.connect(_dlg.reject)
        _ds = aligned_button('保存', 'primary', min_w=80, h=30)
        _ds.clicked.connect(_dlg.accept)
        _db.addWidget(_dr)
        _db.addStretch(1)
        _db.addWidget(_dc)
        _db.addWidget(_ds)
        _dv.addLayout(_db)
        if _dlg.exec() == QDialog.Accepted:
            _prompt_text[0] = _te.toPlainText().strip() or ai_service.DEFAULT_SYSTEM
            adv_prefs.set('aiSystemPrompt', _prompt_text[0])
            adv_prefs.set('aiPromptSavedAt', ck_time.fmt(ck_time.STAMP))
            _paint_prompt()
            _ac_sync()
            w._toast.show('系统提示词已保存并生效', 'success')

    _preset_b = aligned_button('默认', 'ghost', min_w=60, h=28)
    _preset_b.setToolTip('恢复默认系统提示词')
    _pedit_b = aligned_button('编辑', 'ghost', min_w=60, h=28)
    _pedit_b.setToolTip('编辑系统提示词')
    _imp_b = aligned_button('导入', 'ghost', min_w=60, h=28)
    _imp_b.setToolTip('从 txt 导入提示词（覆盖当前）')
    _exp_b = aligned_button('导出', 'ghost', min_w=60, h=28)
    _exp_b.setToolTip('把当前提示词导出为 txt')
    _prow = QHBoxLayout()
    _prow.setSpacing(6)
    for _b in (_pedit_b, _imp_b, _exp_b, _preset_b):
        _b.setAutoDefault(False)
        _prow.addWidget(_b)
    _prow.addStretch(1)
    _v5.addLayout(_prow)
    _bnd = _ElideRow('固定边界：' + AGENT_BOUNDARY, 2)
    _bnd.setObjectName('sub')
    _bnd.setStyleSheet('background:rgba(255,120,120,0.08);'
                       'border:1px solid rgba(255,120,120,0.28);'
                       'border-radius:8px; padding:4px 8px;')
    _bnd.setToolTip(AGENT_BOUNDARY + '\n\n以上边界为固定项，随每次请求注入，不可关闭。')
    _v5.addWidget(_bnd)
    _v5.addStretch(1)

    def _prompt_export():
        _p, _ = QFileDialog.getSaveFileName(
            w, '导出系统提示词',
            os.path.join(os.path.expanduser('~'), '系统提示词.txt'),
            '文本文件 (*.txt)')
        if not _p:
            return
        try:
            with open(_p, 'w', encoding='utf-8') as _fh:
                _fh.write(_prompt_text[0])
            w._toast.show('已导出系统提示词', 'success')
        except Exception as _e:
            w._toast.show('导出失败：%s' % _e, 'warning')

    def _prompt_import():
        _p, _ = QFileDialog.getOpenFileName(
            w, '导入系统提示词', os.path.expanduser('~'),
            '文本文件 (*.txt);;所有文件 (*.*)')
        if not _p:
            return
        try:
            with open(_p, encoding='utf-8') as _fh:
                _t = _fh.read().strip()
        except Exception as _e:
            w._toast.show('导入失败：%s' % _e, 'warning')
            return
        if not _t:
            w._toast.show('文件是空的，未导入', 'warning')
            return
        _prompt_text[0] = _t
        adv_prefs.set('aiSystemPrompt', _t)
        adv_prefs.set('aiPromptSavedAt', ck_time.fmt(ck_time.STAMP))
        _paint_prompt()
        _ac_sync()
        w._toast.show('提示词已导入并生效', 'success')

    def _prompt_reset():
        _prompt_text[0] = ai_service.DEFAULT_SYSTEM
        adv_prefs.set('aiSystemPrompt', '')
        adv_prefs.set('aiPromptSavedAt', '')
        _paint_prompt()
        _ac_sync()
        w._toast.show('系统提示词已恢复默认', 'success')
    _preset_b.clicked.connect(_prompt_reset)
    _pedit_b.clicked.connect(_edit_prompt)
    _imp_b.clicked.connect(_prompt_import)
    _exp_b.clicked.connect(_prompt_export)
    _cfg_v.addLayout(_cfg_grid, 1)
    _ai_tabs.addTab(_cfg_pg, 'AI 配置')
    if _AC_bad:
        QTimer.singleShot(900, lambda: w._toast.show(
            'AI 配置读取失败，已回落默认值，请重新填写', 'warning'))
    QTimer.singleShot(0, lambda: (_load_profile(_AC_act) if _AC_act in _AC_pf
                                  else _ac_sync()))   # 启动以 JSON 为准回填并落盘
    # ============ Tab：Agent 搭建（C37）============
    # 1.4.0：每条能力先过「数据授权」开关；status 不再是四类之外的特例。
    _ag_auth = {'status': ('aiAuthDevice',),
                'energy': ('aiAuthSession',),
                'report': ('aiAuthSession', 'aiAuthEvent', 'aiAuthDevice'),
                'records': ('aiAuthSession', 'aiAuthEvent', 'aiAuthDevice')}

    def _ag_snapshot(caps):
        """按挂载能力取现有数据源快照；只返回 ai_payload 白名单字段。

        端口状态已改为聚合（端口数 / 开启数 / 总功率），不含逐端口开关与功率。
        """
        _f = {}
        _st = (_last_rec or {}).get('stats') or {}
        _an = (_last_rec or {}).get('analysis') or {}
        _skip = [_c for _c in caps
                 if not all(bool(_ai_pref(_k, True))
                            for _k in _ag_auth.get(_c, ()))]
        _ok = [ _c for _c in caps if _c not in _skip]
        _notes = []
        if 'status' in _ok:
            try:
                _c, _d = api('GET', '/api/status', timeout=CK_T_CMD)
                _ps = (_d.get('ports') if isinstance(_d, dict)
                       and isinstance(_d.get('ports'), dict) else {})
                _ln = [v for v in _ps.values() if isinstance(v, dict)]
                _f['status'] = {
                    'port_count': len(_ps),
                    'on_count': sum(1 for v in _ln
                                    if v.get('on', v.get('active'))),
                    'total_power_w': round(sum(
                        float(v.get('power') or v.get('w') or 0)
                        for v in _ln), 1)}
            except Exception as _e:
                _notes.append('端口状态：读取异常（%s）' % _e)
        if 'energy' in _ok:
            try:
                _c, _d = api('GET', '/api/energy/stats?period=today', timeout=CK_T_CMD)
                _s = (_d.get('stats') if isinstance(_d, dict)
                      and isinstance(_d.get('stats'), dict) else _d) or {}
                _f['energy'] = {'total_wh': _s.get('total_wh', '—'),
                                'session_count': _s.get('session_count', '—')}
            except Exception as _e:
                _notes.append('今日能量：读取异常（%s）' % _e)
        if 'report' in _ok:
            try:
                _f['report'] = str(ux.rule_report(_st, _an))[:800]
            except Exception as _e:
                _notes.append('充电日报：不可用（%s）' % _e)
        if 'records' in _ok:
            _f['records'] = {
                'session_count': _st.get('session_count',
                                         len((_last_rec or {}).get('rows') or [])),
                'reason_count': len(_an.get('reasons') or []),
                'device_count': len(_an.get('clusters') or [])}
        if _skip:
            _f['skipped'] = list(_skip)
        if _notes:
            _f['notes'] = _notes
        return _f

    def _agent_tool_exec(_tool, _params):
        """Agent 工具执行器：复用既有后端接口与分组入口，不绕过安全规则。"""
        return agent_tools.execute(_tool, _params, {
            'api': lambda _m, _p, _b=None, **kw: api(_m, _p, _b, **kw),
            'get_bytes': lambda _p, **kw: get_bytes(_p, **kw),
            'prefs': adv_prefs, 'ux': ux})

    _agent_studio = AgentStudio(
        w, adv_prefs, adv_secrets, _ai_ready, _ai_cfg, _ag_snapshot,
        lambda _m, _lv='info': w._toast.show(_m, _lv),
        tool_fn=_agent_tool_exec)
    # 第 5 个 tab 同样套 48 px 页内条（不允许“四个统一、第五个例外”）。
    _agent_pg = QWidget()
    _agpv = QVBoxLayout(_agent_pg)
    _agpv.setContentsMargins(12, 12, 12, 12)
    _agpv.setSpacing(8)
    _ag_badge = QLabel('本机配置 · 即时生效')
    _agpv.addWidget(_ai_bar('Agent 搭建', _ag_badge))
    _agpv.addWidget(_agent_studio, 1)
    _ai_tabs.insertTab(1, _agent_pg, 'Agent 搭建')
    # 1.5.5：智能分析整块从记录页迁来，紧邻 Agent 搭建独立成页
    _ai_tabs.insertTab(2, _ins_pg, '智能分析')

    # ============ Tab 4：数据与隐私（1.5.6 合并「数据授权」+「隐私与日志」） ============
    _dp_pg = QWidget()
    _dpv = QVBoxLayout(_dp_pg)
    _dpv.setContentsMargins(12, 12, 12, 12)
    _dpv.setSpacing(8)
    _auth_g = GlassCard('数据授权（逐项可关，关闭后不进入上下文）')
    _auth_g.set_glyph('shield')
    _agv = QVBoxLayout(_auth_g)
    _agv.setContentsMargins(12, 16, 12, 12)
    _agv.setSpacing(8)
    _auth_cbs = []
    _auth_spec = (
        ('aiAuthSession', '会话数据（时长/总量/峰值）', True),
        ('aiAuthCurve', '功率曲线（高频采样点）', False),
        ('aiAuthDevice', '设备识别结果（聚类与疑似同设备）', True),
        ('aiAuthEvent', '断开事件与原因分类', True),
        ('aiAuthAuto', '自动化记录（定时与阈值规则）', False),
        ('aiAuthDiag', '诊断日志（仅错误摘要）', False),
    )
    _agrid = QGridLayout()
    _agrid.setHorizontalSpacing(12)
    _agrid.setVerticalSpacing(8)
    for _i, (_k, _n, _dv) in enumerate(_auth_spec):
        _cb = QCheckBox(_n)
        _cb.setChecked(bool(_ai_pref(_k, _dv)))
        _auth_cbs.append((_k, _cb))
        _agrid.addWidget(_cb, _i // 2, _i % 2)
    _agv.addLayout(_agrid)
    _auth_cnt = QLabel('已授权 0 / 6 项')
    _auth_cnt.setObjectName('sub')
    _dpv.addWidget(_auth_g)
    _auth_note = QLabel('未授权的数据不会拼入提问；脱敏开启时只上送聚合特征。'
                       '原始高频曲线与设备标识默认不外发。')
    _auth_note.setObjectName('sub')
    _auth_note.setWordWrap(True)
    _dpv.addWidget(_auth_note)
    _pr_g = GlassCard('发送内容与脱敏')
    _pr_g.set_glyph('eye')
    _prv = QVBoxLayout(_pr_g)
    _prv.setContentsMargins(12, 16, 12, 12)
    _prv.setSpacing(8)
    _prv_lbl = QLabel(
        '发送：所选通道的 base_url、模型名、系统提示词、你勾选授权的数据、你的问题。\n'
        '脱敏开启（默认）：仅聚合特征——时段、总量、会话次数、峰值/平均功率、总时长、'
        '设备组数、断开原因计数。\n'
        '脱敏关闭：会话行字段——id、port、start_time、end_time、total_wh、avg_power_w、'
        'peak_power_w、avg_voltage、avg_current、duration_sec、protocol、total、is_active。\n'
        '任何时候都不发送：原始高频曲线、逐端口开关与功率、设备 MAC/序列号、'
        'API Key 明文、诊断日志原文。')
    _prv_lbl.setObjectName('sub')
    _prv_lbl.setWordWrap(True)
    _prv.addWidget(_prv_lbl)
    _redact = QCheckBox('数据脱敏（只发聚合特征）')
    _redact.setChecked(bool(_ai_pref('aiRedact', True)))
    _prv.addWidget(_redact)
    _dpv.addWidget(_pr_g)
    _lg = GlassCard('调用与操作记录')
    _lg.set_glyph('list')
    _lgv = QHBoxLayout(_lg)
    _lgv.setContentsMargins(12, 12, 12, 12)
    _lgv.setSpacing(8)
    _cnt_lbl = QLabel('本机本次运行 0 次调用')
    _cnt_lbl.setObjectName('sub')
    _expb = aligned_button('导出调用记录', 'ghost', min_w=140, h=32)
    _aglog = aligned_button('查看 Agent 操作日志', 'ghost', min_w=170, h=32)
    _aglog.clicked.connect(lambda _c=False: _show_agent_log())
    _vdoc = aligned_button('查看隐私说明与用户协议', 'ghost', min_w=210, h=32)
    _lgv.addStretch(1)
    _lgv.addWidget(_expb)
    _lgv.addWidget(_aglog)
    _lgv.addWidget(_vdoc)
    _dpv.addWidget(_lg)
    _dpv.addStretch(1)
    _dp_badge = QWidget()
    _dp_bh = QHBoxLayout(_dp_badge)
    _dp_bh.setContentsMargins(0, 0, 0, 0)
    _dp_bh.setSpacing(12)
    _dp_bh.addWidget(_auth_cnt)
    _dp_bh.addWidget(_cnt_lbl)
    _dpv.insertWidget(0, _ai_bar('数据与隐私', _dp_badge))
    _ai_tabs.addTab(_dp_pg, '数据与隐私')
    _tab_boost(_ai_tabs, ['chat', 'pen', 'scan', 'gear', 'shield'])
    w._ck_ai_tabs = _ai_tabs     # 自检用：--selftest-aihead 逐 tab 抓图量条高
    _ai_tabs.currentChanged.connect(
        lambda _i: _agent_studio.refresh_gate())

    # ============ 行为：保存/状态/测试/导出 ============
    def _ai_status():
        try:
            _on = _ai_ready()
            _cf = _ai_cfg()
            _ok = bool(_cf.get('key')) or str(_cf.get('provider')) == '本地端点'
            _st_txt = (('已配置 · ' if _ok else '待填 Key · ') + str(_cf.get('provider'))
                       if _on else '未启用')
            _chan.setText(_st_txt)
            _chan2.setText(_st_txt)
            _auth_cnt.setText('已授权 %d / 6 项' % sum(1 for _k, _b in _auth_cbs
                                                     if _b.isChecked()))
            _cnt_lbl.setText('本机本次运行 %d 次调用' % len(_AI['calls']))
            _u = ai_service.usage()
            _rows_a = ai_service.read_audit(2000)
            _m0 = time.strftime('%Y-%m')
            _mon = [_r for _r in _rows_a if str(_r.get('ts') or '')[:7] == _m0]
            _since = time.strftime('%Y-%m-%d %H:%M:%S',
                                   time.localtime(time.time() - 7 * 86400))
            _wk = [_r for _r in _rows_a if str(_r.get('ts') or '') >= _since]
            for _ct, _sc in _cap_rows:
                _n = sum(1 for _r in _wk if _sc and _r.get('scene') == _sc)
                _ct.setText(('%d 次' % _n) if _sc else '—')
            _usage_lbl.setText(
                '本月调用 %d 次 · 降级 %d 次\n累计 %d 次 · 平均 %d ms'
                % (len(_mon),
                   sum(1 for _r in _mon if _r.get('degraded')),
                   _u['calls'], _u['avg_ms']))
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:6902')

    def _ai_save():
        try:
            adv_prefs.set('aiProvider', _prov.currentText())
            adv_prefs.set('aiBaseUrl', _base.text().strip())
            adv_prefs.set('aiModel', _model.text().strip())
            _kv = _key.text().strip()
            if _kv:
                adv_secrets.save_key(_kv)
                _KOLD[0] = _kv
                _key.clear()
                _key.setPlaceholderText(_mask_secret(_kv) + ' · 已保存')
            elif _KOLD[0]:
                _key.setPlaceholderText(_mask_secret(_KOLD[0]) + ' · 已保存')
            adv_prefs.set('aiEnabled', _c1.isChecked())
            adv_prefs.set('aiDailyReport', _c2.isChecked())
            adv_prefs.set('aiExplain', _c3.isChecked())
            adv_prefs.set('aiDeviceHint', _c4.isChecked())
            adv_prefs.set('aiSceneBoxReview', _c5.isChecked())
            adv_prefs.set('aiSceneDiag', _c6.isChecked())
            adv_prefs.set('aiSceneAuto', _c7.isChecked())
            adv_prefs.set('aiTemperature', float(_temp.currentData() or 0.3))
            adv_prefs.set('aiMaxTokens', _maxt.value())
            adv_prefs.set('aiTimeout', _tmo.value())
            adv_prefs.set('aiRetries', _rty.value())
            adv_prefs.set('aiCtxDays', int(_ctx.currentData() or 7))
            _sys = str(_prompt_text[0]).strip()
            adv_prefs.set('aiSystemPrompt',
                          '' if (not _sys or _sys == ai_service.DEFAULT_SYSTEM)
                          else _sys)
            adv_prefs.set('aiRedact', _redact.isChecked())
            for _k, _cb in _auth_cbs:
                adv_prefs.set(_k, _cb.isChecked())
            _ai_status()
            try:
                _agent_studio.refresh_gate()   # C37：开启「对话」后 Agent 搭建即时解锁
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:c37gate')
            try:
                _refresh_adv_state()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:6929')
        except Exception as e:
            try:
                w._toast.show('保存失败：%s' % e, 'warning')
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:6934')
        _ac_sync()      # 改动即落盘：ai_config.json（切换通道/关窗/退出都以这份为准）

    def _ac_key_now():
        _d = _ac_sel.currentData()
        return str(_d) if _d else (_AC_act or 'cloud:默认')

    def _ac_sync():
        """界面 → ai_config.json（API Key 用 DPAPI 加密后 base64，其它明文字段）。"""
        try:
            _kk = _ac_key_now()
            _pf = dict(_AC_pf.get(_kk) or {})
            _pf.update({
                'name': _ac_sel.currentText() or _pf.get('name') or _kk,
                'provider': _prov.currentText(),
                'base_url': _base.text().strip(),
                'model': _model.text().strip(),
                'temperature': float(_temp.currentData() or 0.3),
                'max_tokens': int(_maxt.value()),
                'timeout': int(_tmo.value()),
                'retries': int(_rty.value()),
                'ctx_days': int(_ctx.currentData() or 7)})
            if _KOLD[0] and not _pf.get('key_enc'):
                _pf['key_enc'] = ux.dpapi_enc(_KOLD[0])
            _AC_pf[_kk] = _pf
            _AC_set.update({
                'aiProvider': _prov.currentText(),
                'aiBaseUrl': _base.text().strip(),
                'aiModel': _model.text().strip(),
                'aiTemperature': float(_temp.currentData() or 0.3),
                'aiMaxTokens': int(_maxt.value()),
                'aiTimeout': int(_tmo.value()),
                'aiRetries': int(_rty.value()),
                'aiCtxDays': int(_ctx.currentData() or 7),
                'aiSystemPrompt': ('' if (not _prompt_text[0].strip()
                                          or _prompt_text[0] == ai_service.DEFAULT_SYSTEM)
                                   else _prompt_text[0]),
                'aiEnabled': _c1.isChecked(),
                'aiDailyReport': _c2.isChecked(),
                'aiExplain': _c3.isChecked(),
                'aiDeviceHint': _c4.isChecked(),
                'aiSceneBoxReview': _c5.isChecked(),
                'aiSceneDiag': _c6.isChecked(),
                'aiSceneAuto': _c7.isChecked(),
                'aiLastProvider': _prov.currentText()})
            ux.ai_config_save({'version': 1, 'active': _kk,
                               'profiles': _AC_pf, 'settings': _AC_set})
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:acsync')

    def _load_profile(_pk):
        _pf = dict(_AC_pf.get(_pk) or {})
        _loc = (_pf.get('provider') == '本地端点') or str(_pk).startswith('local')
        _dfb, _dfm = (('http://127.0.0.1:1234/v1', 'qwen2.5-7b-instruct')
                      if _loc else ('https://api.deepseek.com', 'deepseek-chat'))
        for _wd in (_prov, _base, _model):
            _wd.blockSignals(True)
        _prov.setCurrentText(str(_pf.get('provider') or
                                 ('本地端点' if _loc else 'DeepSeek 云端')))
        _base.setText(str(_pf.get('base_url') or _dfb))     # 字段缺失 → 默认值
        _model.setText(str(_pf.get('model') or _dfm))
        for _wd in (_prov, _base, _model):
            _wd.blockSignals(False)
        _kv = ux.dpapi_dec(_pf.get('key_enc'))
        _KOLD[0] = _kv
        _key.clear()
        _key.setPlaceholderText((_mask_secret(_kv) + ' · 已保存') if _kv
                                else 'DPAPI 加密保存，仅本机')
        for _kn, _wv in (('aiProvider', _prov.currentText()),
                         ('aiBaseUrl', _base.text().strip()),
                         ('aiModel', _model.text().strip())):
            adv_prefs.set(_kn, _wv)
        _ac_sync()

    def _ac_new_profile():
        _nm, _ok = QInputDialog.getText(w, '新建配置',
                                        '配置名称（例如：公司网关）')
        _nm = str(_nm or '').strip()
        if not _ok or not _nm:
            return
        _pl = 'local' if _prov.currentText() == '本地端点' else 'cloud'
        _kk = _pl + ':' + _nm
        _AC_pf[_kk] = {'name': _nm, 'provider': _prov.currentText(),
                       'base_url': _base.text().strip(),
                       'model': _model.text().strip(),
                       'temperature': float(_temp.currentData() or 0.3),
                       'max_tokens': int(_maxt.value()),
                       'timeout': int(_tmo.value()),
                       'retries': int(_rty.value()),
                       'ctx_days': int(_ctx.currentData() or 7),
                       'key_enc': ux.dpapi_enc(_KOLD[0]) if _KOLD[0] else ''}
        _ac_sel.blockSignals(True)
        _ac_sel.addItem(_nm, _kk)
        _ac_sel.setCurrentIndex(_ac_sel.count() - 1)
        _ac_sel.blockSignals(False)
        _ac_sync()
        w._toast.show('已新建配置「%s」' % _nm, 'success')

    def _ac_clear_key():
        _kk = _ac_key_now()
        _pf = dict(_AC_pf.get(_kk) or {})
        _pf['key_enc'] = ''
        _AC_pf[_kk] = _pf
        _KOLD[0] = ''
        _key.clear()
        _key.setPlaceholderText('未保存（已清除）')
        try:
            adv_secrets.save_key('')
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:clrkey')
        _ac_sync()
        w._toast.show('已清除本套配置的 API Key', 'success')

    def _on_provider(_t):
        _i = -1
        if _t == '本地端点':
            _i = _ac_sel.findData('local')
            if _i < 0:
                for _j in range(_ac_sel.count()):
                    if str(_ac_sel.itemData(_j)).startswith('local'):
                        _i = _j
                        break
        else:
            for _j in range(_ac_sel.count()):
                if str(_ac_sel.itemData(_j)).startswith('cloud'):
                    _i = _j
                    break
        if _i >= 0 and _i != _ac_sel.currentIndex():
            _ac_sel.setCurrentIndex(_i)     # 触发 _load_profile → 自动回填
        else:
            _ai_save()

    def _ai_test():
        _ai_save()
        _cf = _ai_cfg()
        _testb.setEnabled(False)
        _testst.setText('测试中…')

        def _d(txt, err):
            _testb.setEnabled(True)
            if txt:
                _elide_lbl(_testst, '连接正常 · %s' % _cf.get('model'),
                           max(120, _testst.width() - 2))
                _elide_lbl(_chan, '已连接 · %s' % _cf.get('provider'), 150)
            else:
                _elide_lbl(_testst, '失败：%s' % (err or '未知'),
                           max(120, _testst.width() - 2))
                _elide_lbl(_chan, '连接失败 · %s' % (err or '未知'), 150)
        ux.ai_run(_cf, ai_payload.build('conn_test'), _d, timeout=15)

    def _ai_export_to(_p):
        # R028：抽出无对话框导出，供管道自检与真实点击共用同一实现
        try:
            with open(_p, 'w', encoding='utf-8') as _f:
                for _c in _AI['calls']:
                    _f.write(json.dumps(_c, ensure_ascii=False) + '\n')
            w._toast.show('已导出 %d 条调用记录' % len(_AI['calls']), 'success')
            return True
        except Exception as e:
            w._toast.show('导出失败：%s' % e, 'warning')
            return False

    def _ai_export():
        _p, _ = QFileDialog.getSaveFileName(
            w, '导出 AI 调用记录',
            os.path.join(os.path.expanduser('~'),
                         'ai_calls_%s.jsonl' % ck_time.fmt(ck_time.STAMP)),
            'JSON Lines (*.jsonl)')
        if not _p:
            return
        _ai_export_to(_p)

    _testb.clicked.connect(_ai_test)
    _expb.clicked.connect(_ai_export)
    _vdoc.clicked.connect(lambda: _open_docs(1))
    for _cb in (_c1, _c2, _c3, _c4, _c5, _c6, _c7, _redact):
        _cb.toggled.connect(lambda _=False: _ai_save())
    for _k, _cb in _auth_cbs:
        _cb.toggled.connect(lambda _=False: _ai_save())
    _prov.currentTextChanged.connect(_on_provider)      # 切通道 → 自动回填该通道配置
    _ac_sel.currentIndexChanged.connect(
        lambda _i: _load_profile(str(_ac_sel.currentData() or '')))
    _ac_newb.clicked.connect(lambda _c=False: _ac_new_profile())
    _clrkey.clicked.connect(lambda _c=False: _ac_clear_key())
    _base.editingFinished.connect(_ai_save)
    _model.editingFinished.connect(_ai_save)
    _key.editingFinished.connect(_ai_save)
    _temp.currentIndexChanged.connect(lambda _i: _ai_save())
    _maxt.textChanged.connect(lambda _t: _ai_save())
    _tmo.textChanged.connect(lambda _t: _ai_save())
    if hasattr(_rty, 'valueChanged'):
        _rty.valueChanged.connect(lambda _v: _ai_save())
    if hasattr(_ctx, 'currentIndexChanged'):
        _ctx.currentIndexChanged.connect(lambda _i: _ai_save())

    # ============ 行为：上下文与对话 ============
    def _ai_ctx_fields():
        """只产出 ai_payload 'chat' 白名单内的字段；授权开关逐项生效。"""
        _f = {}
        try:
            _st = (_last_rec or {}).get('stats') or {}
            _an = (_last_rec or {}).get('analysis') or {}
            if _chip_session.isChecked() and _st and bool(_ai_pref('aiAuthSession', True)):
                # 1.5.6：字段定义派生裁剪——表外字段就地剔除，缺项以人话说明，不拦对话
                _st2, _gap = ai_payload.stats_payload(_st)
                if _st2:
                    _f['session_stats'] = _st2
                if _gap or not _st2:
                    _f['data_gaps'] = ['部分统计项暂缺，本次未纳入']
            if _chip_period.isChecked() and bool(_ai_pref('aiAuthCurve', False)):
                _f['curve_authorized'] = True
            if _chip_port.isChecked() and bool(_ai_pref('aiAuthDevice', True)):
                _n = len(_an.get('clusters') or [])
                if _n:
                    _f['device_groups'] = _n
                    _f['device_suspect'] = any((c or {}).get('suspect')
                                               for c in (_an.get('clusters') or []))
            if bool(_ai_pref('aiExplain', True)) and bool(_ai_pref('aiAuthEvent', True)):
                _rs = _an.get('reasons') or []
                if _rs:
                    from collections import Counter
                    _f['disconnect_reasons'] = dict(
                        Counter(r.get('label') for r in _rs))
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:7016')
        return _f

    def _ai_local_answer(_q):
        """离线/异常兜底：只用库内统计能答什么答什么，标注「本地简答」。"""
        _st = (_last_rec or {}).get('stats') or {}
        _an = (_last_rec or {}).get('analysis') or {}
        _rw = (_last_rec or {}).get('rows') or []
        _q = str(_q or '')
        _ln = []
        _bits = []
        for _k, _tpl in (('total_wh', '充入 %s Wh'),
                         ('session_count', '会话 %s 次'),
                         ('peak_power_w', '峰值 %s W'),
                         ('avg_power_w', '平均 %s W')):
            if _st.get(_k) is not None:
                _bits.append(_tpl % _st.get(_k))
        _du = _st.get('total_duration_sec')
        if _du:
            _bits.append('累计 %d 分钟' % (int(_du) // 60))
        _base = ('本期统计：' + '，'.join(_bits) + '。') if _bits else ''
        _rs = _an.get('reasons') or []
        try:
            from collections import Counter
            _rc = Counter((_r or {}).get('label') or '未分类' for _r in _rs)
        except Exception:
            _rc = {}
        _pk = max(_rw, key=lambda r: float(r.get('peak_power_w') or 0)) if _rw else None
        if any(_w in _q for _w in ('掉速', '变慢', '功率', '慢')):
            if _base:
                _ln.append(_base)
            if _rw:
                _low = [r for r in _rw if float(r.get('avg_power_w') or 0) < 5.0]
                _ln.append('均功率低于 5W 的有 %d/%d 次；峰值最高出现在 %s（%.1fW）。'
                           % (len(_low), len(_rw), time.strftime(
                               '%m-%d %H:%M',
                               time.localtime(float((_pk or {}).get('start_time') or 0)))
                              if _pk else '—',
                              float((_pk or {}).get('peak_power_w') or 0)))
            if _rc:
                _ln.append('断开原因前三位：' + '，'.join(
                    '%s %d 次' % (_k, _v) for _k, _v in _rc.most_common(3)) + '。')
        elif any(_w in _q for _w in ('健康', '电池', '寿命')):
            if _rw:
                _durs = [float(r.get('duration_sec') or 0) for r in _rw]
                _mx = max(float(r.get('total_wh') or 0) for r in _rw)
                _full = [r for r in _rw
                         if float(r.get('total_wh') or 0) >= _mx * 0.9]
                _ln.append('按历史最大充入 %.1fWh 估算，接近满充 %d/%d 次；'
                           '最长一次 %.0f 分钟。' % (
                               _mx, len(_full), len(_rw),
                               (max(_durs) if _durs else 0) / 60.0))
                if _du and _st.get('session_count'):
                    _ln.append('单次平均充入 %.1fWh、平均时长 %.0f 分钟。' % (
                        float(_st.get('total_wh') or 0) / max(1, int(
                            _st.get('session_count') or 1)),
                        float(_du) / 60.0 / max(1, int(_st.get('session_count') or 1))))
            else:
                _ln.append('样本不足（0 条），暂无电池健康结论。')
        elif any(_w in _q for _w in ('异常', '断开', '事件')):
            if _rc:
                _ln.append('断开原因分布：' + '，'.join(
                    '%s %d 次' % (_k, _v) for _k, _v in _rc.most_common(5)) + '。')
            if _pk:
                _ln.append('最高峰值那一次：%s，%.1fW、%.1fWh。' % (
                    time.strftime('%m-%d %H:%M',
                                  time.localtime(float(_pk.get('start_time') or 0))),
                    float(_pk.get('peak_power_w') or 0),
                    float(_pk.get('total_wh') or 0)))
            if not _rc and not _pk:
                _ln.append('样本不足（0 条），暂无异常事件。')
        else:
            if _base:
                _ln.append(_base)
            if _rc:
                _ln.append('断开原因：' + '，'.join(
                    '%s %d 次' % (_k, _v) for _k, _v in _rc.most_common(5)) + '。')
            _cl = _an.get('clusters') or []
            if _cl:
                _ln.append('识别到 %d 组设备形态。' % len(_cl))
        if not _ln:
            _ln.append('当前还没有可用的充电记录，先完成一次充电后即可给出统计。')
        _ln.append('（当前为本地规则回复，未调用在线模型。）')
        return '【本地简答】\n' + '\n'.join(_ln)

    def _ai_send(text=None):
        _q = (text if text is not None else _inp.toPlainText()).strip()
        if not _q:
            try:
                w._toast.show('先输入问题', 'warning')
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:7027')
            return
        if _AI['busy']:
            try:
                w._toast.show('正在生成，可先点「停止」', 'warning')
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:7033')
            return
        if not _ai_pref('aiEnabled', False) or not adv_prefs.consent_ok():
            # 1.5.6：能力未开或未授权在线，也照常回答（纯本地计算，不出站）
            _AI['msgs'].append({'role': 'user', 'text': _q})
            _AI['msgs'].append({'role': 'assistant',
                                'text': _ai_local_answer(_q),
                                'meta': '本地简答'})
            _ai_render()
            _ai_persist()
            return
        # R029：应用内操作走白名单工具卡片；低/中风险即时执行，高风险等待卡片确认
        _ti = _tool_intent(_q)
        if _ti:
            _act, _tgt, _par = _ti
            _AI['msgs'].append({'role': 'user', 'text': _q})
            _card = _tool_new(_act, _tgt, _par)
            try:
                _inp.clear()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:7055')
            _ai_render()
            _ai_persist()
            if _card.get('risk') in ('low', 'medium') and _card.get('status') == 'pending':
                QTimer.singleShot(0, lambda c=_card: _tool_exec(c))
            return
        _AI['seq'] += 1
        _seq = _AI['seq']
        _AI['busy'] = True
        _AI['msgs'].append({'role': 'user', 'text': _q})
        _ai_render()
        _ai_persist()
        _inp.clear()
        _sendb.setEnabled(False)
        _chan.setText('请求中…')
        _cf = _ai_cfg()
        try:
            _pl = ai_service.payload('chat', _q, _ai_ctx_fields())
        except Exception as _pe:
            # 1.5.6：载荷异常也不拦对话——记录后直接用本地规则回答
            try:
                ux.learn_log('AIPAYLOAD %s: %r' % (type(_pe).__name__, _pe))
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:aierr')
            _AI['busy'] = False
            _sendb.setEnabled(True)
            _chan.setText('本地简答')
            _AI['msgs'].append({'role': 'assistant',
                                'text': _ai_local_answer(_q),
                                'meta': '本地简答'})
            _ai_render()
            _ai_persist()
            return
        _t0 = time.time()
        # R027（领导）：流式逐字 + 光标跟随；占位消息先上屏，增量刷新做 120ms 节流
        _AI['msgs'].append({'role': 'assistant', 'text': '',
                            'meta': '%s · 生成中' % _cf.get('model'), 'stream': True})
        _ai_render()
        _acc = {'t': ''}
        _pend = {'on': False}

        def _flush():
            _pend['on'] = False
            if _seq == _AI['seq']:
                _ai_render()

        def _delta(piece):
            if _seq != _AI['seq'] or not piece:
                return
            _acc['t'] += piece
            _AI['msgs'][-1]['text'] = _acc['t']
            if not _pend['on']:
                _pend['on'] = True
                QTimer.singleShot(120, _flush)

        def _done(txt, err):
            if _seq != _AI['seq']:
                return
            _AI['busy'] = False
            _sendb.setEnabled(True)
            _dt = time.time() - _t0
            _ts = ck_time.fmt()
            if txt:
                _AI['msgs'][-1].update({'text': txt, 'stream': False,
                                        'meta': '%s · %.1fs' % (_cf.get('model'), _dt)})
                # R027：长回复默认折叠，避免消息流被单条刷屏
                if len(txt) > 500:
                    _AI['msgs'][-1]['collapsed'] = True
                _AI['calls'].append({'t': _ts, 'model': _cf.get('model'),
                                     'provider': _cf.get('provider'),
                                     'ms': int(_dt * 1000), 'ok': True})
                ai_service.audit('chat', _t0, degraded=False, ok=True)
                _chan.setText('已连接 · %s' % _cf.get('provider'))
            else:
                # 1.5.6：模型不可用/超时/无 key 一律回落本地简答，不显示「生成失败」
                try:
                    ux.learn_log('AISTREAM %s: %s' % (
                        _cf.get('provider'), str(err)[:200]))
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:aistream')
                _AI['msgs'][-1].update({
                    'text': _ai_local_answer(_q),
                    'stream': False, 'meta': '本地简答'})
                ai_service.audit('chat', _t0, degraded=True, ok=True,
                                 note=str(err or '')[:120])
                _AI['calls'].append({'t': _ts, 'model': _cf.get('model'),
                                     'provider': _cf.get('provider'),
                                     'ok': False, 'err': str(err)[:120]})
                _chan.setText('本地简答 · 在线模型暂不可用')
            _ai_render()
            _ai_persist()
            _ai_status()
        ux.ai_stream(_cf, _pl, _delta, _done)

    def _ai_stop():
        if not _AI['busy']:
            return
        _AI['seq'] += 1
        _AI['busy'] = False
        _sendb.setEnabled(True)
        if _AI['msgs'] and _AI['msgs'][-1].get('stream'):
            _AI['msgs'][-1]['stream'] = False
            _AI['msgs'][-1]['meta'] = '已停止'
            if not _AI['msgs'][-1]['text']:
                _AI['msgs'][-1]['text'] = '（已停止生成）'
        else:
            _AI['msgs'].append({'role': 'assistant', 'text': '（已停止生成）'})
        _ai_render()
        _ai_persist()
        _chan.setText('已停止 · 可重新生成')

    def _ai_regen():
        _last_u = None
        for _m in reversed(_AI['msgs']):
            if _m['role'] == 'user':
                _last_u = _m['text']
                break
        if not _last_u:
            return
        while _AI['msgs'] and _AI['msgs'][-1]['role'] == 'assistant':
            _AI['msgs'].pop()
        _ai_render()
        _ai_send(_last_u)

    def _ai_copy():
        _txt = ''
        for _m in reversed(_AI['msgs']):
            if _m['role'] == 'assistant':
                _txt = _m['text']
                break
        if not _txt:
            return
        QApplication.clipboard().setText(_txt)
        try:
            w._toast.show('已复制最近一条回复', 'success')
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:7167')

    def _ai_clear():
        _AI['msgs'].clear()
        _ai_render()
        _ai_persist()

    def _ai_switch(name):
        if name == _AI['sid'] or name not in _AI['sessions']:
            return
        _AI['sessions'][_AI['sid']] = _AI['msgs']
        _AI['sid'] = name
        _AI['msgs'] = _AI['sessions'].setdefault(name, [])
        _AI['win'] = 50  # R027：切会话重置渲染窗口
        _ai_render()

    def _ask_text(title, label, initial=''):
        """R030：替换 QInputDialog（英文 OK/Cancel、系统浅色），统一暗色与中文按钮。"""
        d = QDialog(w)
        d.setWindowTitle(title)
        dv = QVBoxLayout(d)
        dv.setContentsMargins(16, 16, 16, 16)
        dv.setSpacing(12)
        dv.addWidget(QLabel(label))
        le = QLineEdit(initial)
        le.setMinimumHeight(32)
        dv.addWidget(le)
        row = QHBoxLayout()
        row.addStretch(1)
        b_no = QPushButton('取消')
        b_no.setObjectName('ghost')
        b_no.setMinimumWidth(96)
        b_ok = QPushButton('确定')
        b_ok.setMinimumWidth(96)
        b_no.clicked.connect(d.reject)
        b_ok.clicked.connect(d.accept)
        row.addWidget(b_no)
        row.addWidget(b_ok)
        dv.addLayout(row)
        d.resize(380, d.sizeHint().height())
        le.setFocus()
        if d.exec() != QDialog.Accepted:
            return '', False
        return le.text(), True

    def _ai_new_sess():
        _n, _ok = _ask_text('新建会话', '会话名称：')
        _n = (_n or '').strip()
        if not _ok or not _n:
            return
        if _n in _AI['sessions']:
            w._toast.show('会话名已存在', 'warning')
            return
        _AI['sessions'][_AI['sid']] = _AI['msgs']
        _AI['sessions'][_n] = []
        _AI['sid'] = _n
        _AI['msgs'] = _AI['sessions'][_n]
        _sess_cb.addItem(_n)
        _sess_cb.setCurrentText(_n)
        _ai_render()
        _ai_persist()

    def _ai_ren_sess():
        _n, _ok = _ask_text('重命名会话', '新名称：', _AI['sid'])
        _n = (_n or '').strip()
        if not _ok or not _n or _n == _AI['sid']:
            return
        if _n in _AI['sessions']:
            w._toast.show('会话名已存在', 'warning')
            return
        _AI['sessions'][_n] = _AI['sessions'].pop(_AI['sid'])
        _ix = _sess_cb.currentIndex()
        _AI['sid'] = _n
        _sess_cb.setItemText(_ix, _n)
        _ai_persist()
        _ai_status()

    def _ai_del_sess():
        if len(_AI['sessions']) <= 1:
            w._toast.show('至少保留一个会话', 'warning')
            return
        _AI['sessions'].pop(_AI['sid'], None)
        _AI['sid'] = next(iter(_AI['sessions']))
        _AI['msgs'] = _AI['sessions'][_AI['sid']]
        _sess_cb.blockSignals(True)
        _sess_cb.clear()
        for _n in _AI['sessions']:
            _sess_cb.addItem(_n)
        _sess_cb.setCurrentText(_AI['sid'])
        _sess_cb.blockSignals(False)
        _ai_render()
        _ai_persist()

    _sendb.clicked.connect(lambda: _ai_send())
    _stopb.clicked.connect(_ai_stop)
    _regenb.clicked.connect(_ai_regen)
    _copyb.clicked.connect(_ai_copy)
    _newb.clicked.connect(_ai_new_sess)
    _renb.clicked.connect(_ai_ren_sess)
    _delb.clicked.connect(_ai_del_sess)
    _clearb.clicked.connect(_ai_clear)
    _sess_cb.activated.connect(lambda _i: _ai_switch(_sess_cb.currentText()))

    # C34-UI：AI 入口唯一可达路径 = 导航底部「小源」（页内 Ctrl+K 仍为聚焦输入框）
    _sc_k = QShortcut(QKeySequence('Ctrl+K'), ai_page)
    _sc_k.setContext(Qt.WidgetWithChildrenShortcut)
    _sc_k.activated.connect(lambda: (_ai_tabs.setCurrentIndex(0), _inp.setFocus()))
    _sc_e = QShortcut(QKeySequence('Esc'), ai_page)
    _sc_e.setContext(Qt.WidgetWithChildrenShortcut)
    _sc_e.activated.connect(_ai_stop)

    _ai_render()
    _ai_status()
    stack.addWidget(ai_page)

    def save_notify():
        payload = {'charge_start': notif_start.isChecked(),
                   'charge_end': notif_end.isChecked(),
                   'threshold': notif_thr.isChecked()}
        notif_save.setEnabled(False)
        notif_save.setText('保存中…')

        def work():
            try:
                return api('POST', '/api/automation/notify', payload, timeout=10)
            except Exception as e:
                return ('ERR', str(e))

        def done(r):
            notif_save.setEnabled(True)
            if isinstance(r, tuple) and r[0] == 200:
                notif_save.setText('已保存')
                w._notify_cfg = payload
                w._toast.show('通知渠道已保存', 'ok')
            else:
                notif_save.setText('保存通知')
                w._toast.show('保存失败', 'warning')

        qwait(work, done)

    notif_save.clicked.connect(save_notify)

    # R015（领导）：通知系统真实生效——测试通知直达系统托盘
    def _notify_test():
        # C34（缺陷 6）：测试通知此前直接 tray.showMessage，绕过静音开关与 WinRT 通路，
        # 静音后仍会弹（用户实测「静音失效」）。改为与正式通知同一条路径，并显式回报静音。
        if _CK_MUTE.get('on'):
            _n = _ck_mute_count('plain')
            w._toast.show('已静音，未弹窗（未读 %d 条，解除后补发）' % _n, 'info')
            _ck_toast_log('notify_test muted+counted n=%d' % _n)
            return
        _title, _body = PANEL_TITLE, '通知系统工作正常（%s）' % PANEL_TITLE
        if not _ck_toast_send(_title, _body, 'plain'):
            tray.showMessage(_title, _body, QSystemTrayIcon.Information, 4000)
        w._toast.show('已发送测试通知（见系统托盘/右下角）', 'info')
    notif_test.clicked.connect(_notify_test)

    # 1.5.5：阈值规则与 A 口规则的控件已分别迁入「充电限制与保护」「进阶配置」，
    # 其读写逻辑在对应页面构建完成后统一接线（见下方 _set_tabs.addTab(_t5, ...)）。

    # ===== Tab 4b: 端口统计 =====
    stat_t = QWidget()
    stat_v = QVBoxLayout(stat_t)
    stat_v.setContentsMargins(8, 12, 8, 8)
    g_stat = GlassCard('端口统计')
    sv2 = QVBoxLayout(g_stat)
    sv2.setContentsMargins(12, 16, 12, 12)
    sv2.setSpacing(12)
    srow = QHBoxLayout()
    stat_port = QComboBox()
    stat_port.addItems(['C口 1', 'C口 2', 'C口 3', 'USB-A'])
    stat_hours = QComboBox()
    stat_hours.addItems(['近24小时', '近7天', '近30天'])
    stat_b = QPushButton('查询')
    srow.addWidget(stat_port, 1)
    srow.addWidget(stat_hours, 1)
    srow.addWidget(stat_b)
    sv2.addLayout(srow)
    stat_lbl = QLabel('')
    stat_lbl.setObjectName('sub')
    stat_lbl.setWordWrap(True)
    sv2.addWidget(stat_lbl)
    # KPI 网格（结构化呈现，替代原始 JSON 文本）
    kpi_host = QWidget()
    kpi_grid = QGridLayout(kpi_host)
    kpi_grid.setContentsMargins(0, 0, 0, 0)
    kpi_grid.setHorizontalSpacing(12)
    kpi_grid.setVerticalSpacing(12)
    stat_kpi = {}

    def add_kpi(key, cap, col):
        cell = QFrame()
        cell.setStyleSheet('QFrame { background: rgba(255,255,255,0.05);'
                           'border:1px solid rgba(255,255,255,0.10); border-radius:12px; }')
        cell.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        cell.setMinimumHeight(72)
        cv = QVBoxLayout(cell)
        cv.setContentsMargins(12, 12, 12, 12)
        cv.setSpacing(4)
        val = QLabel('—')
        val.setStyleSheet('font-size:18px; font-weight:700; color:#7fd4ff;'
                          'background:transparent; border:none;')
        val.setAlignment(Qt.AlignCenter)
        capl = QLabel(cap)
        capl.setObjectName('sub')
        capl.setStyleSheet('font-size:11px; background:transparent; border:none;')
        capl.setAlignment(Qt.AlignCenter)
        cv.addStretch(1)
        cv.addWidget(val)
        cv.addWidget(capl)
        cv.addStretch(1)
        kpi_grid.addWidget(cell, col // 4, col % 4)
        stat_kpi[key] = val

    kpi_defs = [
        ('samples', '样本数'), ('first_seen', '首见'), ('last_seen', '末见'),
        ('v_avg', '电压均 V'), ('v_min', '电压小 V'), ('v_max', '电压大 V'),
        ('c_avg', '电流均 A'), ('c_max', '电流大 A'),
        ('p_avg', '功率均 W'), ('p_max', '功率大 W'), ('wh', '电量 Wh'),
        ('active', '活跃比'),
    ]
    for i, (k, c) in enumerate(kpi_defs):
        add_kpi(k, c, i)
    for r in range(3):
        kpi_grid.setRowStretch(r, 1)
    sv2.addWidget(kpi_host, 1)
    # 端口统计不再作为独立页，统计卡 g_stat 稍后并入「设置」页（CK-5）

    def set_kpi(key, txt):
        if key not in stat_kpi:
            return
        if txt is None:
            stat_kpi[key].setText('—')
        elif isinstance(txt, float):
            stat_kpi[key].setText('%.2f' % txt)
        else:
            stat_kpi[key].setText(str(txt))

    def render_stats(data):
        def gv(*path, default='—'):
            cur = data
            for p in path:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    return default
            return cur
        set_kpi('samples', gv('samples'))
        set_kpi('first_seen', str(gv('first_seen'))[5:19].replace('T', ' '))
        set_kpi('last_seen', str(gv('last_seen'))[5:19].replace('T', ' '))
        set_kpi('v_avg', gv('voltage', 'avg'))
        set_kpi('v_min', gv('voltage', 'min'))
        set_kpi('v_max', gv('voltage', 'max'))
        set_kpi('c_avg', gv('current', 'avg'))
        set_kpi('c_max', gv('current', 'max'))
        set_kpi('p_avg', gv('power', 'avg'))
        set_kpi('p_max', gv('power', 'max'))
        set_kpi('wh', gv('power', 'total_wh'))
        ar = gv('active_ratio', default=None)
        if ar is not None:
            try:
                set_kpi('active', '%.0f%%' % (float(ar) * 100))
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:7450')

    def do_stats():
        port_i = stat_port.currentIndex() + 1
        hours = ['24', '168', '720'][stat_hours.currentIndex()]
        stat_lbl.setText('查询中…')

        def done(r):
            try:
                if isinstance(r, tuple) and r[0] == 200 and isinstance(r[1], dict):
                    d = r[1].get('data', r[1])
                    render_stats(d if isinstance(d, dict) else {})
                    stat_lbl.setText('')
                else:
                    stat_lbl.setText('查询失败：' + str(r))
            except Exception as e:
                stat_lbl.setText(str(e))

        # R1-02（CK-19）：改走 qwait 信号桥回主线程——原裸 threading.Thread 在
        # 工作线程直接 setText/set_kpi 操作 Qt 控件，违反线程亲和性（崩溃隐患）
        qwait(lambda: api('GET', '/api/statistics/%d?hours=%s' % (port_i, hours)), done)

    stat_b.clicked.connect(do_stats)
    stat_port.currentIndexChanged.connect(lambda *a: do_stats())
    stat_hours.currentIndexChanged.connect(lambda *a: do_stats())
    # 进入页面自动加载默认范围（C口1 / 近24小时）
    QTimer.singleShot(300, do_stats)

    def _clear_layout(lo):
        while lo.count():
            item = lo.takeAt(0)
            w_ = item.widget()
            if w_ is not None:
                w_.deleteLater()

    def render_schedules(scheds):
        # R017-CK03：序列化比对——数据未变化不重建（原每 20s 整表闪烁+重播入场动画）
        try:
            import json as _json
            _sig = _json.dumps(scheds or [], sort_keys=True, ensure_ascii=False,
                               default=str)
            if getattr(render_schedules, '_last_sig', None) == _sig:
                return
            render_schedules._last_sig = _sig
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:7495')
        _clear_layout(sched_list_v)
        scheds = scheds or []
        if not scheds:
            # R044 / C13：与静态空态同口径，上下 stretch 垂直居中
            sched_list_v.addStretch(1)
            sched_list_v.addWidget(_sched_empty_widget())
            sched_list_v.addStretch(1)
            return
        for idx, s in enumerate(scheds):
            row = QFrame()
            # R045 / C14：固定行高，避免多行时被压扁到 26 px 以下（按钮/文字被裁）
            row.setMinimumHeight(40)
            row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            row.setStyleSheet('QFrame { background: rgba(255,255,255,0.06);'
                              'border:1px solid rgba(255,255,255,0.12); border-radius:8px; }')
            if not s.get('enabled', True):
                row.setStyleSheet('QFrame { background: rgba(255,255,255,0.03);'
                                  'border:1px solid rgba(255,255,255,0.08); border-radius:8px; }')
            rh = QHBoxLayout(row)
            rh.setContentsMargins(12, 8, 12, 8)
            rh.setSpacing(12)
            t_lbl = QLabel(str(s.get('time', '')))
            t_lbl.setStyleSheet('font-weight:700; font-size:14px; color:#eaf4ff;')
            pname = dict((k, n) for k, (c, n) in PORT_NAMES.items()).get(
                str(s.get('port')), str(s.get('port')))
            p_lbl = QLabel(pname)
            act = s.get('action')
            act_lbl = QLabel('开启' if act == 'on' else '关闭')
            act_lbl.setStyleSheet('color:%s; font-weight:600;'
                                  % (TEAL if act == 'on' else WARN_BRIGHT))
            pause_b = QPushButton('暂停' if s.get('enabled', True) else '恢复')
            pause_b.setObjectName('ghost')
            del_b = QPushButton('删除')
            del_b.setObjectName('danger')
            sid = s.get('id')
            pause_b.clicked.connect(
                lambda checked, i=sid, en=not s.get('enabled', True):
                toggle_schedule(i, en))
            del_b.clicked.connect(lambda checked, i=sid: delete_schedule(i))
            rh.addWidget(t_lbl)
            rh.addWidget(p_lbl)
            rh.addStretch(1)
            rh.addWidget(act_lbl)
            rh.addWidget(pause_b)
            rh.addWidget(del_b)
            sched_list_v.addWidget(row)
            # 行入场（scheduleRowIn）；大列表（>60 行）不做逐行动画，避免造数卡死
            if len(scheds) <= 60:
                fade_in(row, duration=340, delay=45 * idx)
        sched_list_v.addStretch(1)

    def _ck_scroll_probe(n_sched=2000, n_sess=120, msg_per=20):
        """只读自检：2000 行定时列表 / 120 会话的渲染与滚动耗时（进程内 mock）。

        不写后端、不写 QSettings；结束后恢复原会话与定时空态，仅 CLI 自检调用。
        """
        from PySide6.QtWidgets import QApplication as _QApp
        _rec = {'ok': True}
        # ---- 定时列表：造 n_sched 行 → 渲染 / 首帧 / 滚到底 / 同数据短路 ----
        _keys = list(PORT_NAMES.keys())
        _ms = []
        for _i in range(max(0, int(n_sched))):
            _ms.append({'id': 'perf%d' % (_i + 1),
                        'port': _keys[_i % len(_keys)],
                        'time': '%02d:%02d' % (7 + _i, 30 if _i % 2 == 0 else 0),
                        'action': 'on' if _i % 2 == 0 else 'off',
                        'enabled': _i != 1})
        on_select(stack.indexOf(au))
        _QApp.processEvents()
        w._schedHold = True
        _t0 = time.perf_counter()
        render_schedules(_ms)
        _t1 = time.perf_counter()
        _QApp.processEvents()
        _t2 = time.perf_counter()
        _sb = sched_scroll.verticalScrollBar()
        _sb.setValue(_sb.maximum())
        _QApp.processEvents()
        _t3 = time.perf_counter()
        _sb.setValue(0)
        _QApp.processEvents()
        _t4 = time.perf_counter()
        render_schedules(_ms)   # 同数据二次渲染：走序列化短路，应≈0
        _t5 = time.perf_counter()
        _rec['sched'] = {'n': len(_ms),
                         'render_ms': round((_t1 - _t0) * 1000, 2),
                         'first_frame_ms': round((_t2 - _t1) * 1000, 2),
                         'scroll_max_ms': round((_t3 - _t2) * 1000, 2),
                         'scroll_back_ms': round((_t4 - _t3) * 1000, 2),
                         'dedup_ms': round((_t5 - _t4) * 1000, 2),
                         'sb_max': _sb.maximum(),
                         'host_h': sched_list_host.height(),
                         'viewport_h': sched_scroll.viewport().height()}
        # ---- 120 会话：下拉重建 / 逐条切换（含 _ai_render）/ 检索过滤 ----
        _bak = {'sessions': _AI['sessions'], 'sid': _AI['sid'],
                'msgs': _AI['msgs'], 'win': _AI.get('win', 50)}
        _sess = {}
        for _i in range(max(1, int(n_sess))):
            _acc = []
            for _j in range(max(0, int(msg_per))):
                _acc.append({'role': 'user' if _j % 2 == 0 else 'assistant',
                             'text': '会话 %d 第 %d 条：请解释充电曲线里 65W→45W '
                                     '的掉速原因与影响。' % (_i + 1, _j + 1),
                             'meta': '%s · 0.1s' % ('我' if _j % 2 == 0 else 'perf')})
            _sess['perf会话%03d' % (_i + 1)] = _acc
        _names = list(_sess.keys())
        _AI['sessions'] = _sess
        _AI['sid'], _AI['msgs'] = _names[0], _sess[_names[0]]
        _AI['win'] = 50
        on_select(stack.indexOf(ai_page))
        _ai_tabs.setCurrentIndex(0)
        _QApp.processEvents()
        _t6 = time.perf_counter()
        _sess_cb.blockSignals(True)
        _sess_cb.clear()
        for _n in _sess:
            _sess_cb.addItem(_n)
        _sess_cb.setCurrentText(_AI['sid'])
        _sess_cb.blockSignals(False)
        _QApp.processEvents()
        _t7 = time.perf_counter()
        _sw = []
        for _n in _names:
            _s0 = time.perf_counter()
            _ai_switch(_n)
            _QApp.processEvents()
            _sw.append((time.perf_counter() - _s0) * 1000.0)
        _sw.sort()
        _t8 = time.perf_counter()
        _sess_srch.setText('9')
        _QApp.processEvents()
        _t9 = time.perf_counter()
        _rec['sess'] = {'n': len(_sess), 'msg_per': msg_per,
                        'combo_rebuild_ms': round((_t7 - _t6) * 1000, 2),
                        'switch_p50_ms': round(_sw[int(0.5 * (len(_sw) - 1))], 2),
                        'switch_p99_ms': round(_sw[min(len(_sw) - 1,
                                                       int(0.99 * len(_sw)))], 2),
                        'switch_max_ms': round(_sw[-1], 2),
                        'filter_ms': round((_t9 - _t8) * 1000, 2),
                        'log_h': _log.height(),
                        'log_doc_h': _log.verticalScrollBar().maximum()}
        # ---- 恢复 ----
        _sess_srch.setText('')
        _AI.update(_bak)
        _sess_cb.blockSignals(True)
        _sess_cb.clear()
        for _n in _AI['sessions']:
            _sess_cb.addItem(_n)
        _sess_cb.setCurrentText(_AI['sid'])
        _sess_cb.blockSignals(False)
        _ai_render()
        w._schedHold = False
        render_schedules([])
        _QApp.processEvents()
        _rec['restored'] = True
        return _rec
    w._ck_scroll_probe = _ck_scroll_probe   # 自检用：--selftest-scroll

    def poll_automation():
        qwait(lambda: api('GET', '/api/automation'), _automation_done, key='automation')
        _ck_ar_load()

    def _automation_done(r):
        try:
            if not isinstance(r, tuple) or r[0] != 200:
                return
            _code, d = r
            if isinstance(d, dict):
                data = d.get('data') or d
                scheds = data.get('schedules') or []
                # R045 / C14：schedmock 取证期间保持模拟行，不被真实轮询覆盖
                if not getattr(w, '_schedHold', False):
                    render_schedules(scheds)
                notify = data.get('notify') or {}
                notif_start.setChecked(bool(notify.get('charge_start')))
                notif_end.setChecked(bool(notify.get('charge_end')))
                notif_thr.setChecked(bool(notify.get('threshold')))
                w._notify_cfg = dict(notify) if isinstance(notify, dict) else {}
                # R15-C07：缓存阈值表（automation 接口 thresholds 字段，键为端口号 1-4）
                _thr_raw = data.get('thresholds')
                w._thresholds = _thr_raw if isinstance(_thr_raw, dict) else {}
                # C8e：缓存阈值并回填当前选中端口
                on_thr_port_change._autom = data
                on_thr_port_change(thr_port.currentIndex())
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:7567')

    def add_schedule():
        payload = {'port': sched_port.currentData(),
                   'time': '%02d:%02d' % (sched_hh.value(), sched_mm.value()),
                   'action': sched_action.currentData()}
        sched_add_b.setEnabled(False)

        def work():
            try:
                return api('POST', '/api/automation/schedule', payload, timeout=10)
            except Exception as e:
                return ('ERR', str(e))

        def done(r):
            sched_add_b.setEnabled(True)
            ok = isinstance(r, tuple) and r[0] == 200
            if ok:
                d = r[1] if len(r) > 1 else {}
                data = (d.get('data') if isinstance(d, dict) else None) or d
                render_schedules(data.get('schedules'))
                w._toast.show('定时任务已添加', 'ok')
            else:
                _detail = str(r[1])[:80] if isinstance(r, tuple) and len(r) > 1 else ''
                w._toast.show('添加失败 %s' % _detail, 'warning')

        qwait(work, done)

    def delete_schedule(sid):
        def work():
            try:
                return api('POST', '/api/automation/schedule/delete',
                           {'id': sid}, timeout=10)
            except Exception as e:
                return ('ERR', str(e))

        def done(r):
            if isinstance(r, tuple) and r[0] == 200:
                d = r[1] if len(r) > 1 else {}
                data = (d.get('data') if isinstance(d, dict) else None) or d
                render_schedules(data.get('schedules'))
                w._toast.show('定时任务已删除', 'ok')
            else:
                w._toast.show('删除失败', 'warning')

        qwait(work, done)

    def toggle_schedule(sid, enabled):
        def work():
            try:
                return api('POST', '/api/automation/schedule/toggle',
                           {'id': sid, 'enabled': enabled}, timeout=10)
            except Exception as e:
                return ('ERR', str(e))

        def done(r):
            if isinstance(r, tuple) and r[0] == 200:
                d = r[1] if len(r) > 1 else {}
                data = (d.get('data') if isinstance(d, dict) else None) or d
                render_schedules(data.get('schedules'))
            else:
                w._toast.show('切换失败', 'warning')

        qwait(work, done)

    sched_add_b.clicked.connect(add_schedule)

    # ===== Tab 5: 设置（合理分为 3 个子页面，各自可滚动） =====
    se = QWidget()
    se.setObjectName('pageSettings')   # R041 / C10
    sev = QVBoxLayout(se)
    sev.setContentsMargins(8, 12, 8, 8)
    cfg_inputs = {}   # (section, field) -> widget
    cfg_fields = {}   # section -> list of (field, widget, type)

    # 既有设置页保持原样（领导口径）：原有子标签结构不动，只做最小加法
    _set_tabs = QTabWidget()
    _set_tabs.setObjectName('setTabs')
    # 窄窗口适配：子标签不溢出、不出现滚动箭头；空间不足时文字省略（…）
    _set_tabs.setUsesScrollButtons(False)
    _set_tabs.setElideMode(Qt.ElideRight)
    _set_tabs.setDocumentMode(True)
    sev.addWidget(_set_tabs)

    def new_tab(name):
        """创建一个设置子页面（R030：不再套 QScrollArea，彻底消除页面滚动条）。"""
        page = QWidget()
        v = QVBoxLayout(page)
        # R046 / C15：150%/900×620 下三卡内容被压扁——页边距收 4 px（12/16/12/12 → 12/12/12/8）
        v.setContentsMargins(12, 12, 12, 8)
        v.setSpacing(12)
        _set_tabs.addTab(page, name)
        return page, v

    def card_form(v, title, glyph=None):
        """在子页面布局中加入一张玻璃卡，返回其表单。C24：glyph 为可选标题线稿。"""
        g = GlassCard(title)
        if glyph:
            g.set_glyph(glyph)
        f = QFormLayout(g)
        # R046 / C15：卡片上下内边距各收 4 px，给 150%/900×620 腾出行高
        f.setContentsMargins(12, 12, 12, 8)
        v.addWidget(g)
        return f

    def add_text(form, section, field, label, placeholder=''):
        from PySide6.QtWidgets import QLineEdit
        wgt = QLineEdit()
        if placeholder:
            wgt.setPlaceholderText(placeholder)
        form.addRow(label, wgt)
        cfg_inputs[(section, field)] = wgt
        cfg_fields.setdefault(section, []).append((field, wgt, 'text'))
        return wgt

    def add_check(form, section, field, label, inline=None):
        wgt = QCheckBox()
        if inline is None:
            form.addRow(label, wgt)
        else:
            # 同一行附挂其它控件（如 MQTT 模式）：不新增行高
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(8)
            lay.addWidget(wgt)
            lay.addStretch(1)
            lay.addWidget(inline)
            form.addRow(label, row)
        cfg_inputs[(section, field)] = wgt
        cfg_fields.setdefault(section, []).append((field, wgt, 'check'))
        return wgt

    def add_spin(form, section, field, label, lo, hi, step=1):
        wgt = _CkSpinBox()
        wgt.setRange(lo, hi)
        wgt.setSingleStep(step)
        form.addRow(label, wgt)
        cfg_inputs[(section, field)] = wgt
        cfg_fields.setdefault(section, []).append((field, wgt, 'spin'))
        return wgt

    def add_combo(form, section, field, label, items):
        wgt = QComboBox()
        wgt.addItems(items)
        form.addRow(label, wgt)
        cfg_inputs[(section, field)] = wgt
        cfg_fields.setdefault(section, []).append((field, wgt, 'combo'))
        return wgt

    # ---- 区块 1：连接设置（蓝牙 + MQTT 并排，同一页；不另开 MQTT 子页） ----
    _t1, v_conn = new_tab('连接设置')
    _conn_row = QHBoxLayout()
    _conn_row.setContentsMargins(0, 0, 0, 0)
    _conn_row.setSpacing(12)

    f_ble = card_form(_conn_row, '蓝牙设备', 'ble')
    add_text(f_ble, 'ble', 'mac', 'MAC 地址', '3C:CD:73:..')
    add_text(f_ble, 'ble', 'token', 'Token', '手动填写')
    add_text(f_ble, 'ble', 'ble_key', '蓝牙密钥', '手动填写')
    add_spin(f_ble, 'ble', 'scan_timeout', '扫描超时（秒）', 5, 60)
    # 1.5.5：两段灰色说明收进「?」，页面只留标题 + 控件（原文未删减）。
    ble_help = ux.info_badge(
        '蓝牙凭证与设备信息',
        '凭证已自动回显（掩码显示）；如需更换，清空对应字段后粘贴新值。'
        'MAC / Token / 蓝牙密钥 保存后服务自动重启。\n\n'
        '设备信息连接后自动读取：连接成功后，设备信息（型号/固件/协议版本）由后端自动读取，'
        '端口协议、场景与倒计时等能力随连接自动可用。如需完整信息，请确认充电器上电、'
        '蓝牙已配对认证。')
    ble_conn_b = QPushButton('连接设备')
    ble_conn_b.setMinimumHeight(34)
    # C34（缺陷 1）：设置页同源显示蓝牙认证徽章，避免与状态页两套说法。
    ble_state_lbl = QLabel('')
    ble_state_lbl.setStyleSheet('font-size:12px;')
    # 2.1.0：充电器在位/充电中属于设备信息，与蓝牙认证同排放这里，不再占首页云状态行
    _ble_state_row = QWidget()
    _bsl = QHBoxLayout(_ble_state_row)
    _bsl.setContentsMargins(0, 0, 0, 0)
    _bsl.setSpacing(10)
    _bsl.addWidget(ble_state_lbl)
    _bsl.addWidget(badge_chg)
    _bsl.addStretch(1)
    f_ble.addRow('蓝牙状态', _ble_state_row)
    _bc_row = QWidget()
    _bcl = QHBoxLayout(_bc_row)
    _bcl.setContentsMargins(0, 0, 0, 0)
    _bcl.setSpacing(6)
    _bcl.addWidget(ble_conn_b, 1)
    _bcl.addWidget(ble_help, 0, Qt.AlignVCenter)
    f_ble.addRow('蓝牙连接', _bc_row)
    ble_save = QPushButton('保存蓝牙配置')
    f_ble.addRow(ble_save)
    f_ble.setVerticalSpacing(12)

    # MQTT（家庭自动化 / 巴法云）：与蓝牙同页并排，只有这一处开关。
    # 巴法云的主题是「控制台里已建好的名字」，四路各一格、原样粘贴，不做拼接。
    f_mq = card_form(_conn_row, 'MQTT 集成（家庭自动化）', 'server')
    # 开关与协议模式同一行：不新增行高（900×620 下连接设置页高度吃紧）
    mq_mode = QComboBox()
    mq_mode.addItem('通用', 'generic')
    mq_mode.addItem('巴法云', 'bemfa')
    mq_mode.setToolTip(
        '通用：本机 JSON 主题布局（原有行为，完全不变）。\n'
        '巴法云：官方设备协议，四路各一个主题，纯文本 on/off。')
    _mq_mode_box = QWidget()
    _mq_mode_lay = QHBoxLayout(_mq_mode_box)
    _mq_mode_lay.setContentsMargins(0, 0, 0, 0)
    _mq_mode_lay.setSpacing(6)
    _mq_mode_lbl = QLabel('模式')
    _mq_mode_lbl.setObjectName('sub')
    _mq_mode_lay.addWidget(_mq_mode_lbl)
    _mq_mode_lay.addWidget(mq_mode)
    mq_enabled = add_check(f_mq, 'mqtt', 'enabled', '开启 MQTT', inline=_mq_mode_box)
    cfg_inputs[('mqtt', 'mode')] = mq_mode
    cfg_fields.setdefault('mqtt', []).append(('mode', mq_mode, 'combo'))

    # 服务器与端口同一行：省一行高度给四路主题与状态行
    mq_host = QLineEdit()
    mq_host.setPlaceholderText('localhost')
    mq_port = _CkSpinBox()
    mq_port.setRange(1, 65535)
    mq_port.setValue(1883)
    _srv_row = QWidget()
    _srv_lay = QHBoxLayout(_srv_row)
    _srv_lay.setContentsMargins(0, 0, 0, 0)
    _srv_lay.setSpacing(6)
    _srv_lay.addWidget(mq_host, 1)
    _srv_lay.addWidget(QLabel(':'))
    _srv_lay.addWidget(mq_port)
    f_mq.addRow('服务器', _srv_row)
    cfg_inputs[('mqtt', 'host')] = mq_host
    cfg_fields.setdefault('mqtt', []).append(('host', mq_host, 'text'))
    cfg_inputs[('mqtt', 'port')] = mq_port
    cfg_fields.setdefault('mqtt', []).append(('port', mq_port, 'spin'))

    mq_cid = add_text(f_mq, 'mqtt', 'client_id', '客户端 ID', '可留空')
    mq_topic = add_text(f_mq, 'mqtt', 'topic_prefix', '主题前缀', 'cuktech/charger')
    _prefix_row = f_mq.rowCount() - 1

    # 巴法云四路主题：字段名与控制台设备卡一致，整行宽度便于核对粘贴结果
    mq_topics = {}
    _topic_rows = {}
    for _tk, _tlabel in (('c1', '1号口'), ('c2', '2号口'),
                         ('c3', '3号口'), ('a', 'A口')):
        _ted = add_text(f_mq, 'mqtt', 'topic_' + _tk, _tlabel, '粘贴主题名')
        _ted.setToolTip('%s：把巴法云控制台对应设备卡上的主题名原样粘贴进来' % _tlabel)
        mq_topics[_tk] = _ted
        _topic_rows[_tk] = f_mq.rowCount() - 1
    _topic_row = _topic_rows['c1']

    # 连接状态行 + 测试连接（独立于保存）
    mq_status_lbl = QLabel('未开启')
    mq_status_lbl.setWordWrap(True)
    mq_status_lbl.setStyleSheet('font-size:12px; color:#b8c6d6;')
    mq_test = QPushButton('测试连接')
    mq_test.setToolTip('按当前填写的地址/端口/客户端 ID 试一次握手（不保存、不影响已连接的服务）')
    _status_row = QWidget()
    _status_lay = QHBoxLayout(_status_row)
    _status_lay.setContentsMargins(0, 0, 0, 0)
    _status_lay.setSpacing(6)
    _status_lay.addWidget(mq_status_lbl, 1)
    _status_lay.addWidget(mq_test)
    f_mq.addRow('连接状态', _status_row)

    _help_bemfa = (
        '巴法云：客户端 ID 填「私钥 UID」；服务器 mqtt.bemfa.com:9501（9503=TLS）。'
        '主题到 cloud.bemfa.com/tcp/devicemqtt.html 按 1号口/2号口/3号口/A口 原样复制粘贴；'
        '只能字母数字下划线，须先在控制台建好。QoS 固定 1，QoS2 会被踢下线。')
    _help_generic = (
        '通用模式：明文 TCP（无 TLS）。发布 前缀/status、前缀/port/<c1|c2|c3|a> 等 JSON 主题，'
        '订阅 前缀/set、前缀/port、前缀/ble。')
    # 1.5.5：成段说明不再铺在页面上，收进「模式」行的「?」（原文一字未删）。
    mq_help = ux.info_badge(
        'MQTT 模式说明',
        _help_bemfa + '\n\n' + _help_generic + '\n\n'
        '通用模式（原有布局，行为不变）：\n'
        '· 发布 前缀/status、前缀/port/<c1|c2|c3|a>、前缀/settings、前缀/charge_event（JSON）；\n'
        '· 订阅 前缀/set、前缀/port、前缀/ble（JSON）。\n\n'
        '巴法云模式（官方设备协议）：\n'
        '· 服务器 mqtt.bemfa.com；明文 9501，TLS 9503。客户端 ID 填私钥 UID。\n'
        '· 主题 = 控制台里已建好的完整名字，四路各一个，本机不做任何拼接。\n'
        '· 设备订阅这四个主题；控制方发 <主题>/set（on/off），状态上报 <主题>/up（纯文本）。\n'
        '· 巴法云限制：同一私钥 UID 多客户端会互踢；QoS 只支持 0/1；'
        '主题未在控制台创建会被拒；TLS 只有 9503/9504。')
    _mq_mode_lay.addWidget(mq_help, 0, Qt.AlignVCenter)
    mq_bemfa = QPushButton('填入巴法云预设')
    mq_bemfa.setToolTip('切到巴法云模式，服务器填 mqtt.bemfa.com、端口 9501；'
                        '四个主题仍需从控制台原样粘贴。')
    mq_save = QPushButton('保存 MQTT 配置')
    _mq_btn_row = QWidget()
    _mq_btn_lay = QHBoxLayout(_mq_btn_row)
    _mq_btn_lay.setContentsMargins(0, 0, 0, 0)
    _mq_btn_lay.setSpacing(6)
    _mq_btn_lay.addWidget(mq_bemfa, 1)
    _mq_btn_lay.addWidget(mq_save, 1)
    f_mq.addRow(_mq_btn_row)
    f_mq.setVerticalSpacing(4)

    def _mq_apply_mode_ui():
        """按模式切换：巴法云显示四路主题、隐藏前缀；通用相反。"""
        _is_bemfa = (mq_mode.currentData() == 'bemfa')
        try:
            for _r in _topic_rows.values():
                f_mq.setRowVisible(_r, _is_bemfa)
            f_mq.setRowVisible(_prefix_row, not _is_bemfa)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:mq-mode-ui')
        # 「?」里始终保留两种模式的完整说明（切换模式不删文字）
        if _is_bemfa:
            mq_cid.setPlaceholderText('填你的私钥 UID')
            mq_cid.setToolTip('巴法云：客户端 ID 必须填「私钥 UID」；用户名、密码留空。')
        else:
            mq_cid.setPlaceholderText('可留空')
            mq_cid.setToolTip('')

    mq_mode.currentIndexChanged.connect(lambda *_: _mq_apply_mode_ui())
    _mq_apply_mode_ui()
    w._ck_mq_mode_combo = mq_mode   # 布局自检用：切换模式后重量一次卡高

    # 1.5.9：两张卡按「最高模式」锁高——切模式/改窗口都不再改尺寸
    _conn_cards = []
    for _ci in range(_conn_row.count()):
        _cw = _conn_row.itemAt(_ci).widget()
        if _cw is not None:
            _conn_cards.append(_cw)
    _ix0 = mq_mode.currentIndex()
    _need_h = []
    for _mv in ('bemfa', 'generic'):
        _ix = mq_mode.findData(_mv)
        if _ix >= 0:
            mq_mode.setCurrentIndex(_ix)
            _mq_apply_mode_ui()
            _need_h.append(max([_c.sizeHint().height() for _c in _conn_cards] or [0]))
    mq_mode.setCurrentIndex(_ix0)
    _mq_apply_mode_ui()
    _conn_h = max(_need_h or [0])
    if _conn_h > 0:
        for _c in _conn_cards:
            _c.setFixedHeight(_conn_h)

    def poll_mqtt_status():
        qwait(lambda: api('GET', '/api/mqtt/status', timeout=5),
              _mqtt_status_done, key='mqtt_status')

    def _mqtt_status_done(r):
        try:
            if not isinstance(r, tuple) or r[0] != 200 or not isinstance(r[1], dict):
                # 连续 3 拍取不到状态：不保留旧的「已连接」假象，退回「状态未知」
                _CK_MQTT_FAILS['n'] += 1
                if _CK_MQTT_FAILS['n'] >= 3 and _CK_MQTT.get('state'):
                    _CK_MQTT.update({'state': '', 'host': '', 'port': '', 'reason': ''})
                    _mq_txt, _mq_col = _ck_mqtt_badge()
                    _setT('badge_ble', badge_ble,
                          '<span style="color:%s">●</span> %s' % (_mq_col, _mq_txt))
                    badge_ble.setToolTip(_ck_mqtt_tip())
                return
            _CK_MQTT_FAILS['n'] = 0
            d = r[1]
            # 1.5.5：总览行的巴法云徽章与这里同源（同一拍数据）。
            _CK_MQTT.update({'state': d.get('state') or '', 'host': d.get('host') or '',
                             'port': d.get('port') or '',
                             'reason': d.get('reason') or ''})
            # 2.1.0：首页徽章在状态拍里直接写，跨页不再依赖首页定时器
            _mq_txt, _mq_col = _ck_mqtt_badge()
            _mq_tip = _ck_mqtt_tip()
            badge_ble.setVisible(True)
            _setT('badge_ble', badge_ble,
                  '<span style="color:%s">●</span> %s' % (_mq_col, _mq_txt))
            badge_ble.setToolTip(_mq_tip)
            _lab = _ck_mqtt_state_text()[0] or (d.get('label') or '未知')
            mq_status_lbl.setText(_lab)
            mq_status_lbl.setToolTip(_mq_tip)
            mq_status_lbl.setStyleSheet('font-size:12px; color:%s;' % _mq_col)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:mqtt-status')

    # 1.5.9：下方空白补「快速接入」——蓝牙地址 / 巴法云各 4 步短句，固定高度
    g_conn_tip = GlassCard('快速接入')
    g_conn_tip.setObjectName('connTip')
    _tipv = QHBoxLayout(g_conn_tip)
    _tipv.setContentsMargins(14, 10, 14, 10)
    _tipv.setSpacing(18)
    for _ttl, _steps in (
            ('蓝牙地址', ('1 充电器通电，打开手机蓝牙',
                         '2 安装 nRF Connect 等 BLE 调试 App',
                         '3 扫描「星源智控 / Cuktech」设备',
                         '4 复制 MAC（如 3C:CD:73:1A:2B:3C）填入上方')),
            ('巴法云', ('1 注册 bemfa.com，复制私钥 UID',
                       '2 新建 4 个主题：ck_c1 / ck_c2 / ck_c3 / ck_a',
                       '3 模式选「巴法云」，客户端 ID 填私钥 UID',
                       '4 四路粘贴主题名，测试连接成功后保存'))):
        _col = QVBoxLayout()
        _col.setSpacing(2)
        _th = QLabel(_ttl)
        _th.setStyleSheet('font-size:12px; font-weight:600; color:#8fd6ff;')
        _col.addWidget(_th)
        _tl = QLabel('\n'.join(_steps))
        _tl.setStyleSheet('font-size:12px; color:%s;' % TEXT_SUB)
        _tl.setWordWrap(True)
        _col.addWidget(_tl, 1)
        _tipv.addLayout(_col, 1)
    g_conn_tip.setFixedHeight(172)
    g_conn_tip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    v_conn.addLayout(_conn_row)
    v_conn.addWidget(g_conn_tip)
    v_conn.addStretch(1)
    # 供出包后布局实测：连接设置页可用高度 / 两张卡实际需求高度
    w._ck_conn_page = _t1
    w._ck_conn_row = _conn_row
    w._ck_settings_tabs = _set_tabs

    def _ck_conn_metrics():
        _pg = w._ck_conn_page
        _lay = _pg.layout()
        _mg = _lay.contentsMargins() if _lay is not None else None
        _avail = (_pg.height() - ((_mg.top() + _mg.bottom()) if _mg else 0))
        _b = _conn_row.itemAt(0).widget() if _conn_row.count() > 0 else None
        _m = _conn_row.itemAt(1).widget() if _conn_row.count() > 1 else None
        _bh = _b.sizeHint().height() if _b is not None else -1
        _mh = _m.sizeHint().height() if _m is not None else -1
        _fullw = max(200, _pg.width() - 24)
        _bhf = _b.heightForWidth(_fullw) if _b is not None else -1
        _mhf = _m.heightForWidth(_fullw) if _m is not None else -1
        _tip = w.findChild(QWidget, 'connTip')
        _tip_h = ((_tip.height() + 12) if _tip is not None and _tip.isVisible() else 0)
        _need = max(_bh, _mh) + _tip_h
        return {'page': {'w': _pg.width(), 'h': _pg.height(), 'available_h': _avail},
                'tabs': {'w': (_set_tabs.width() if _set_tabs is not None else -1),
                         'h': (_set_tabs.height() if _set_tabs is not None else -1)},
                'window': {'w': w.width(), 'h': w.height()},
                'ble': {'hint_h': _bh, 'min_h': (_b.minimumSizeHint().height()
                                                 if _b is not None else -1),
                        'fullw_h': _bhf},
                'mqtt': {'hint_h': _mh, 'min_h': (_m.minimumSizeHint().height()
                                                  if _m is not None else -1),
                         'fullw_h': _mhf},
                'tip_h': _tip_h, 'needed_h': _need, 'fits': _need <= _avail}

    w._ck_conn_metrics = _ck_conn_metrics

    def _ck_port_metrics():
        """端口卡几何实测：显示迷你图后，卡片实际高度是否容得下（不裁切）。"""
        _cards = {}
        _fits = True
        for _pk, _tup in port_cards.items():
            _card, _box = _tup[0], _tup[8]
            _hint = _card.sizeHint().height()
            _need = _box.height() if not _box.isHidden() else 0
            _ok = _card.height() >= _hint
            _fits = _fits and _ok
            # 逐项 hint：调纵向预算时直接看是哪一块在吃高度
            _parts = {}
            _ly = _card.layout()
            if _ly is not None:
                for _i in range(_ly.count()):
                    _it = _ly.itemAt(_i)
                    _iw, _il = _it.widget(), _it.layout()
                    if _iw is None and _il is None:
                        continue   # 弹簧/空白项，无尺寸可量
                    _nm = (_iw.objectName() or type(_iw).__name__) if _iw is not None else 'layout'
                    _parts['%d:%s' % (_i, _nm)] = (
                        _iw.sizeHint().height() if _iw is not None else _il.sizeHint().height())
            _cards[_pk] = {'card_w': _card.width(), 'card_h': _card.height(),
                           'card_hint_h': _hint, 'mini_h': _need,
                           'has_plot': getattr(_box, '_plot', None) is not None,
                           'mini_hidden': _box.isHidden(), 'fits': _ok,
                           'parts': _parts}
        _first = next(iter(port_cards.values()), None)
        _ph = (_first[0].parentWidget().height() if _first is not None else -1)
        return {'window': {'w': w.width(), 'h': w.height()},
                'page_h': _ph, 'cards': _cards, 'fits': _fits}

    w._ck_port_metrics = _ck_port_metrics
    w._ck_port_cards = port_cards
    w._ck_chart_commit = _chart_commit
    w._ck_chart_state = chart_state
    w._ck_paint_mini = _paint_mini
    w._ck_conn_state = conn_state   # 自检/截图用：--demo 时可临时置为已连接以强制出曲线

    # ---- 模式与显示（场景 + 息屏，独立子页，无滚动条） ----
    _t2, v_mode = new_tab('模式与显示')

    # 充电模式（场景模式 piid=5，CK-N01）
    g_scene = GlassCard('充电模式（场景模式）')
    g_scene.set_glyph('vmax')
    sf = QVBoxLayout(g_scene)
    sf.setContentsMargins(12, 12, 12, 8)
    sf.setSpacing(8)
    scene_sel = SegmentedSelector([(1, 'AI模式'), (2, '数码生态'),
                                   (3, '单口模式'), (4, '均衡模式')])
    # R046 / C15：900×620 下卡片按 sizeHint 给高，分段器 sizeHint 被 QSS 低估到 18 px，
    # 子按钮（34 px）被父容器裁掉上下沿。固定 34 让卡片 sizeHint 与最小高一致。
    scene_sel.setFixedHeight(34)
    # 1.5.5：灰色说明收进「?」（四种模式的口径全量保留）。
    scene_help = ux.info_badge(
        '充电模式说明',
        'AI模式：自动识别设备智能匹配最优充电功率\n'
        '数码生态：适配苹果/数码生态，多设备协同充电\n'
        '单口模式：单端口满功率输出，专注快充\n'
        '均衡模式：多端口均衡分配功率，稳定充电')
    _sc_row = QWidget()
    _scl = QHBoxLayout(_sc_row)
    _scl.setContentsMargins(0, 0, 0, 0)
    _scl.setSpacing(8)
    _scl.addWidget(scene_sel, 1)
    _scl.addWidget(scene_help, 0, Qt.AlignVCenter)
    sf.addWidget(_sc_row)
    # R046 / C15：900×620（126）与 150%（132）下本卡按 sizeHint 给高、比内容少 8–10 px，
    # 说明文字会压到分段器下沿——把最小高钉到内容实际需要的 142 px。
    g_scene.setMinimumHeight(142)
    v_mode.addWidget(g_scene)

    # 屏幕与显示（息屏 piid=6，CK-P3-01）
    g_screen = GlassCard('屏幕与显示')
    g_screen.set_glyph('screen')
    scrf = QFormLayout(g_screen)
    scrf.setContentsMargins(12, 12, 12, 8)
    screen_sel = SegmentedSelector([(1, '5分钟'), (2, '10分钟'), (3, '30分钟'),
                                    (4, '常亮'), (5, '1分钟')])
    scrf.addRow('息屏时间', screen_sel)
    # 1.5.9：卡片高度锁定，余量均分到卡与卡之间，底部不留整块空白
    v_mode.addStretch(1)
    v_mode.addWidget(g_screen)

    # R014（领导）：网页「设备设置」未移植项并入模式与显示
    # （语言 piid=13 / 空闲息屏 piid=19 / 屏幕方向锁 piid=20，与网页端一致）
    g_dev = GlassCard('设备设置')
    g_dev.set_glyph('gear')
    devf = QVBoxLayout(g_dev)
    devf.setContentsMargins(12, 12, 12, 8)
    devf.setSpacing(8)
    lang_sel = SegmentedSelector([(0, '英文'), (1, '中文')])
    idle_sel = SegmentedSelector([(0, '关'), (1, '开')])
    lock_sel = SegmentedSelector([(0, '关'), (1, '开')])
    # R046 / C15：三行并一行；1.5.9：USB-A 小电流并入本卡，开关与上面三项同款
    _devrow = QHBoxLayout()
    _devrow.setContentsMargins(0, 0, 0, 0)
    _devrow.setSpacing(12)
    for _lb, _sel in (('设备语言', lang_sel), ('空闲息屏', idle_sel),
                      ('屏幕方向锁', lock_sel), ('USB-A 小电流', trickle_sel)):
        _devrow.addWidget(QLabel(_lb))
        _devrow.addWidget(_sel, 1)
    _devrow.addWidget(ux.info_badge(
        'USB-A 小电流',
        '启用后 USB-A 端口以小电流模式输出，适合蓝牙耳机、手环等小功率设备。'),
        0, Qt.AlignVCenter)
    devf.addLayout(_devrow)
    v_mode.addStretch(1)
    v_mode.addWidget(g_dev)

    # 1.5.5：原「进阶配置 → 显示、性能与曲线」里的纯外观项迁入本页
    v_mode.addStretch(1)
    _adv_sec = adv_card(v_mode, '外观', ('theme', 'accent', 'animLevel', 'reduceMotion'),
                        glyph='screen')
    # 1.5.9：整页卡片锁定高度（高度只由结构决定，不随窗口/内容变化）
    for _gc in (g_scene, g_screen, g_dev, _adv_sec):
        _gc.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    v_mode.addStretch(1)

    # ---- 充电限制与保护（独立子页，无滚动条） ----
    _t2b, v_lim = new_tab('充电限制与保护')

    # 1.5.5（定宽网格终版）：2×2 四张等高卡，四卡共用
    # 「标签 / 控件 / 单位 / 状态」四列节奏：列宽固定、左缘对齐；
    # 同行两卡由 QGridLayout 强制等高，卡内余高按行均分，底部不留整块空白。
    _GC, _GU, _GS = 84, 20, 72
    _lim_page = QGridLayout()
    _lim_page.setContentsMargins(0, 0, 0, 0)
    _lim_page.setHorizontalSpacing(10)
    _lim_page.setVerticalSpacing(10)
    _lim_page.setColumnStretch(0, 1)
    _lim_page.setColumnStretch(1, 1)
    _lim_page.setRowStretch(0, 1)
    _lim_page.setRowStretch(1, 1)
    v_lim.addLayout(_lim_page, 1)

    def _lim_card(row, col, title, glyph):
        g = GlassCard(title)
        g.set_glyph(glyph)
        g.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        gr = QGridLayout(g)
        gr.setContentsMargins(12, 12, 12, 10)
        gr.setHorizontalSpacing(8)
        gr.setVerticalSpacing(8)
        _lim_page.addWidget(g, row, col)
        return g, gr

    def _lim_lbl(text, tip=None):
        lb = QLabel(text)
        lb.setObjectName('limName')
        lb.setToolTip(tip or text)
        return lb

    # ---- 左上：充电限制（四行等宽输入框，保存整行）----
    _gA, _grA = _lim_card(0, 0, '充电限制（0 表示不限）', 'battery')
    _gA.setToolTip('单位：分钟；0 表示不限制。C口3 / USB-A 保护未启用，仅记录上限。')
    _laA = [_lim_lbl('C口1 最大时长（分钟）', '0 表示不限制'),
            _lim_lbl('C口2 最大时长（分钟）', '0 表示不限制'),
            _lim_lbl('C口3 最大时长', '保护未启用，上限仅记录'),
            _lim_lbl('USB-A 最大时长', '保护未启用，上限仅记录')]
    _lim_spins = []
    for _fld, _lo, _hi in (('c1', 0, 1440), ('c2', 0, 1440)):
        _sb = _CkSpinBox()
        _sb.setRange(_lo, _hi)
        cfg_inputs[('charge_limit', _fld)] = _sb
        cfg_fields.setdefault('charge_limit', []).append((_fld, _sb, 'spin'))
        _lim_spins.append(_sb)
    # R024-A2 领导裁定：保护套件仅 C口1/C口2——C3/USB-A 上限置灰（保留显示，禁用编辑）
    _lim_note = ux.info_badge(
        '充电保护范围',
        '领导裁定：充电保护（时长上限/低功率保护）仅对 C口1、C口2 生效。')
    _sp3 = _CkSpinBox()
    _sp3.setRange(0, 1440)
    _sp3.setEnabled(False)
    cfg_inputs[('charge_limit', 'c3')] = _sp3
    cfg_fields.setdefault('charge_limit', []).append(('c3', _sp3, 'spin'))
    _spa = _CkSpinBox()
    _spa.setRange(0, 1440)
    _spa.setEnabled(False)
    cfg_inputs[('charge_limit', 'a')] = _spa
    cfg_fields.setdefault('charge_limit', []).append(('a', _spa, 'spin'))
    _lim_spins.extend([_sp3, _spa])
    for _r, (_lb, _sb) in enumerate(zip(_laA, _lim_spins)):
        _sb.setFixedWidth(78)
        _sb.setFixedHeight(34)
        _grA.addWidget(_lb, _r, 0, Qt.AlignLeft | Qt.AlignVCenter)
        _grA.addWidget(_sb, _r, 1, Qt.AlignLeft | Qt.AlignVCenter)
        _grA.setRowStretch(_r, 1)
    # 「?」放在「?」列，紧跟 C口3 控件右侧、卡片内，不会溢出卡边
    _grA.addWidget(_lim_note, 2, 2, Qt.AlignLeft | Qt.AlignVCenter)
    _grA.setColumnMinimumWidth(0, max(lb.sizeHint().width() for lb in _laA))
    _grA.setColumnMinimumWidth(1, 78)
    # 1.5.9：说明点零占地，不再为「?」预留 24 px 空列
    _grA.setColumnMinimumWidth(2, 0)
    _grA.setColumnStretch(3, 1)
    lim_save = QPushButton('保存充电限制')
    lim_save.setMinimumHeight(34)
    _grA.addWidget(lim_save, 4, 0, 1, 4)

    # ---- 右上：智能保护（三行 + 保存）----
    _gB, _grB = _lim_card(0, 1, '智能保护', 'shield')
    _lbB = [_lim_lbl('启用智能充电保护'),
            _lim_lbl('低功率阈值 W'),
            _lim_lbl('持续时长（分钟）')]
    _pr_on = QCheckBox()
    cfg_inputs[('protection', 'enabled')] = _pr_on
    cfg_fields.setdefault('protection', []).append(('enabled', _pr_on, 'check'))
    _pr_w = _CkSpinBox()
    _pr_w.setRange(1, 100)
    cfg_inputs[('protection', 'low_power_w')] = _pr_w
    cfg_fields.setdefault('protection', []).append(('low_power_w', _pr_w, 'spin'))
    _pr_m = _CkSpinBox()
    _pr_m.setRange(1, 120)
    cfg_inputs[('protection', 'low_power_minutes')] = _pr_m
    cfg_fields.setdefault('protection', []).append(('low_power_minutes', _pr_m, 'spin'))
    for _r, (_lb, _ctl) in enumerate(zip(_lbB, (_pr_on, _pr_w, _pr_m))):
        if _ctl is not _pr_on:
            _ctl.setFixedWidth(_GC)
            _ctl.setFixedHeight(34)
        _grB.addWidget(_lb, _r, 0, Qt.AlignLeft | Qt.AlignVCenter)
        _grB.addWidget(_ctl, _r, 1, Qt.AlignLeft | Qt.AlignVCenter)
        _grB.setRowStretch(_r, 1)
    _grB.setColumnMinimumWidth(0, max(lb.sizeHint().width() for lb in _lbB))
    _grB.setColumnMinimumWidth(1, _GC)
    _grB.setColumnStretch(2, 1)
    prot_save = QPushButton('保存保护配置')
    prot_save.setMinimumHeight(34)
    _grB.addWidget(prot_save, 3, 0, 1, 3)

    # ---- 左下：告警阈值规则（标签 40 / 输入 96 / 单位 20 / 「?」26）----
    _gC, _grC = _lim_card(1, 0, '告警阈值规则', 'shield')
    thr_port = QComboBox()
    thr_port.setFixedHeight(32)
    # R024-A2 领导裁定：智能充电保护仅 C口1/C口2——规则端口下拉只列这两口
    for _k in ('1', '2'):
        _c, _n = PORT_NAMES[_k]
        thr_port.addItem(_n, _k)
    thr_min = ux.DoubleField(0.0, 120.0, 0.0)
    thr_max = ux.DoubleField(0.0, 120.0, 0.0)
    _grC.addWidget(_tag('端口'), 0, 0, Qt.AlignLeft | Qt.AlignVCenter)
    _grC.addWidget(thr_port, 0, 1, Qt.AlignLeft | Qt.AlignVCenter)
    _grC.addWidget(_tag('下限'), 1, 0, Qt.AlignLeft | Qt.AlignVCenter)
    _grC.addWidget(thr_min, 1, 1, Qt.AlignLeft | Qt.AlignVCenter)
    _grC.addWidget(_tag('W', True), 1, 2, Qt.AlignLeft | Qt.AlignVCenter)
    _grC.addWidget(_tag('上限'), 2, 0, Qt.AlignLeft | Qt.AlignVCenter)
    _grC.addWidget(thr_max, 2, 1, Qt.AlignLeft | Qt.AlignVCenter)
    _grC.addWidget(_tag('W', True), 2, 2, Qt.AlignLeft | Qt.AlignVCenter)
    # 说明收进「?」，固定最后一列、与端口行同基线，始终在卡片内
    _grC.addWidget(ux.info_badge(
        '告警阈值规则',
        '功率高于上限、或低于下限（且 >0）时触发告警；上下限均为 0 表示清除该端口规则。'),
        0, 3, Qt.AlignLeft | Qt.AlignVCenter)
    for _r in (0, 1, 2):
        _grC.setRowStretch(_r, 1)
    _grC.setColumnMinimumWidth(0, 40)
    _grC.setColumnMinimumWidth(1, _GC)
    _grC.setColumnMinimumWidth(2, _GU)
    _grC.setColumnMinimumWidth(3, 0)
    _grC.setColumnStretch(4, 1)
    for _w in (thr_port, thr_min, thr_max):
        _w.setFixedWidth(_GC)
        _w.setFixedHeight(32)
    thr_save = aligned_button('保存阈值规则', 'primary', min_w=120, h=32, glyph='shield')
    # 保存按钮单独一行，右缘与「?」列右缘对齐，不越出卡片
    _grC.addWidget(thr_save, 3, 0, 1, 4, Qt.AlignRight)

    # ---- 右下：充电与保护（标签 / 控件 / 「?」 / 状态四列定宽）----
    _gD, _grD = _lim_card(1, 1, '充电与保护', 'shield')
    _eff_style = ('color:%s; font-size:11px; border:1px solid %s;'
                  'border-radius:8px; padding:1px 5px; background:transparent;'
                  % (TEXT_SUB, ux.TOKENS['border']))

    def _adv_write(_k, _v):
        adv_prefs.set(_k, _v)
        _ck_section_apply()

    _lbD = [_lim_lbl('过温阈值（℃）'), _lim_lbl('过功率阈值（W）'),
            _lim_lbl('无充电自动关端口'),
            _lim_lbl('无充电关端口延时（分钟）',
                     '连续无输出超过该时长才关端口；范围 1–1440 分钟，默认 10 分钟。'),
            _lim_lbl('抖动合并（秒）')]
    _adv_spec = (('protectTemp', 40, 95, '下次分析'),
                 ('protectPower', 5, 250, '下次分析'),
                 ('autoOffNoCharge', None, None, '即时生效'),
                 ('autoOffDelayMin', 1, 1440, '即时生效'),
                 ('mergeGap', 5, 600, '下次分析'))
    _ao_ref = {}
    for _r, (_lb, (_k, _lo, _hi, _eff)) in enumerate(zip(_lbD, _adv_spec)):
        if _lo is None:
            _ctl = QCheckBox('已开启' if adv_prefs.get(_k) else '已关闭')
            _ctl.setChecked(bool(adv_prefs.get(_k)))
            _ctl.toggled.connect(lambda on, c=_ctl, k=_k: (
                c.setText('已开启' if on else '已关闭'),
                _ao_ref.get('delay') and _ao_ref['delay'].setEnabled(on),
                _adv_write(k, on)))
        else:
            _ctl = ux.IntField(_lo, _hi, int(adv_prefs.get(_k)))
            _ctl.setFixedWidth(_GC)
            _ctl.setFixedHeight(32)
            _ctl.textChanged.connect(
                lambda _t, c=_ctl, k=_k: _adv_write(k, c.value()))
            if _k == 'autoOffDelayMin':
                _ao_ref['delay'] = _ctl
                _ctl.setEnabled(bool(adv_prefs.get('autoOffNoCharge')))
        _st = QLabel(_eff)
        _st.setObjectName('eff')
        _st.setStyleSheet(_eff_style)
        _st.setFixedWidth(_GS)
        _st.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        _grD.addWidget(_lb, _r, 0, Qt.AlignLeft | Qt.AlignVCenter)
        _grD.addWidget(_ctl, _r, 1, Qt.AlignLeft | Qt.AlignVCenter)
        _grD.addWidget(_st, _r, 3, Qt.AlignLeft | Qt.AlignVCenter)
        _grD.setRowStretch(_r, 1)
    _grD.setColumnMinimumWidth(0, max(lb.sizeHint().width() for lb in _lbD))
    _grD.setColumnMinimumWidth(1, _GC)
    _grD.setColumnMinimumWidth(2, 0)
    _grD.setColumnMinimumWidth(3, _GS)
    _grD.setColumnStretch(4, 1)
    # 左右两列等宽：取四卡最小宽度的最大值作为共同列宽（先收窄固定列，避免 995 溢出）
    _cw = max(g.minimumSizeHint().width() for g in (_gA, _gB, _gC, _gD))
    _lim_page.setColumnMinimumWidth(0, _cw)
    _lim_page.setColumnMinimumWidth(1, _cw)

    # ---- 服务与数据（同页下一区块） ----
    _t3, v_sd = new_tab('服务与数据')
    # R015（领导）：无滚动条紧凑版——服务器一行、服务管理一行（重启时间右置）、
    # 文件路径与数据导出合并板块（导出地址 + 充电记录存储 + 导出控件）

    def _reg(section, field, wgt, kind):
        cfg_inputs[(section, field)] = wgt
        cfg_fields.setdefault(section, []).append((field, wgt, kind))

    f_srv = card_form(v_sd, '服务器', 'server')
    _sr = QHBoxLayout()
    _sr.setSpacing(12)
    # R046 / C15：QHBoxLayout 默认 9 px 内边距把每行撑到 53 px；清零后 150%/900×620 才不压扁
    _sr.setContentsMargins(0, 0, 0, 0)
    _sr.addWidget(QLabel('端口'))
    _wp = _CkSpinBox(); _wp.setRange(1, 65535); _wp.setMaximumWidth(110)
    _sr.addWidget(_wp)
    _sr.addWidget(QLabel('日志级别'))
    _wl = QComboBox(); _wl.addItems(['debug', 'info', 'warning', 'error']); _wl.setMaximumWidth(110)
    _sr.addWidget(_wl)
    _sr.addWidget(QLabel('保留天数'))
    _wd = _CkSpinBox(); _wd.setRange(1, 3650); _wd.setMaximumWidth(110)
    _sr.addWidget(_wd)
    # R046 / C15：900×620 下本行被压到按钮仅 91 px，7 字标签两端被裁 → 文案收为 4 字
    srv_save = QPushButton('保存配置'); srv_save.setMaximumWidth(150)
    _sr.addWidget(srv_save)
    _sr.addStretch(1)
    _srw = QWidget(); _srw.setLayout(_sr)
    f_srv.addRow('', _srw)
    _reg('server', 'port', _wp, 'spin')
    _reg('server', 'log_level', _wl, 'combo')
    _reg('server', 'history_retention_days', _wd, 'spin')

    f_svc = card_form(v_sd, '服务管理', 'sliders')
    _vr = QHBoxLayout()
    _vr.setSpacing(12)
    _vr.setContentsMargins(0, 0, 0, 0)
    _va = QCheckBox('开机自启')
    _vb = QCheckBox('每日定时重启')
    _vt_l = QLabel('重启时间')
    _vt = QLineEdit('07:00'); _vt.setMaximumWidth(90)
    svc_save = QPushButton('保存配置'); svc_save.setMaximumWidth(150)
    for _x in (_va, _vb):
        _vr.addWidget(_x)
    _vr.addWidget(_vt_l)
    _vr.addWidget(_vt)
    _vr.addWidget(svc_save)
    _vr.addStretch(1)
    _vrw = QWidget(); _vrw.setLayout(_vr)
    f_svc.addRow('', _vrw)
    # R018（领导）：关闭行为统一到「自动化 → 关闭时最小化到托盘」，本页不再设第二个入口
    _reg('service', 'auto_start', _va, 'check')
    _reg('service', 'daily_restart_enabled', _vb, 'check')
    _reg('service', 'daily_restart_time', _vt, 'text')

    # 文件路径与数据导出（图一移入；含充电记录存储地址/文件）
    f_fp = card_form(v_sd, '文件路径与数据导出', 'folder')
    _prefs = QSettings('DoubaoWork', 'CuktechPanel')
    stor_lbl = QLabel(str(_prefs.value('export_dir', '') or '（未设置，导出时选择）'))
    stor_lbl.setWordWrap(True)
    stor_lbl.setStyleSheet('font-size:11px; color:' + TEXT_SUB + ';')
    stor_b = QPushButton('更改')
    stor_open_b = QPushButton('打开')

    def _pick_export_dir():
        _cur = str(_prefs.value('export_dir', '') or os.path.expanduser('~'))
        d = QFileDialog.getExistingDirectory(w, '选择导出存储路径', _cur)
        if d:
            _prefs.setValue('export_dir', d)
            stor_lbl.setText(d)
            w._toast.show('导出存储路径已更新', 'ok')

    def _open_export_dir():
        _d = str(_prefs.value('export_dir', '') or '')
        if _d and os.path.isdir(_d):
            os.startfile(_d)
        else:
            w._toast.show('尚未设置导出存储路径', 'warning')
    stor_b.clicked.connect(_pick_export_dir)
    stor_open_b.clicked.connect(_open_export_dir)
    _r_e = QHBoxLayout()
    _r_e.setContentsMargins(0, 0, 0, 0)
    _r_e.addWidget(stor_lbl, 1)
    _r_e.addWidget(stor_b)
    _r_e.addWidget(stor_open_b)
    _r_ew = QWidget(); _r_ew.setLayout(_r_e)
    f_fp.addRow('导出数据地址', _r_ew)

    _dbp = os.path.join(app_data_dir(), 'port_history.db')
    db_lbl = QLabel(_dbp)
    db_lbl.setWordWrap(True)
    db_lbl.setStyleSheet('font-size:11px; color:' + TEXT_SUB + ';')
    db_open_b = QPushButton('打开目录')
    db_open_b.clicked.connect(lambda: os.startfile(app_data_dir()))
    _r_d = QHBoxLayout()
    _r_d.setContentsMargins(0, 0, 0, 0)
    _r_d.addWidget(db_lbl, 1)
    _r_d.addWidget(db_open_b)
    _r_dw = QWidget(); _r_dw.setLayout(_r_d)
    f_fp.addRow('充电记录存储', _r_dw)

    exp_port = QComboBox()
    exp_port.addItems(['C口 1', 'C口 2', 'C口 3', 'USB-A'])
    exp_hours = QComboBox()
    exp_hours.addItems(['近24小时', '近7天', '近30天'])
    exp_port_b = QPushButton('导出端口历史')
    exp_sess_b = QPushButton('导出会话')
    exp_energy_b = QPushButton('导出能耗')
    r1 = QHBoxLayout()
    r1.setContentsMargins(0, 0, 0, 0)
    r1.addWidget(exp_port)
    r1.addWidget(exp_hours)
    r1.addWidget(exp_port_b)
    r1.addWidget(exp_sess_b)
    r1.addWidget(exp_energy_b)
    r1.addStretch(1)
    r1w = QWidget(); r1w.setLayout(r1)
    f_fp.addRow('数据导出', r1w)
    # 1.5.5：原「进阶配置 → 数据、日志与诊断」迁入本页；
    # 日志级别 / 保留天数与「服务器」卡同键，已删除副本，只保留服务器卡的真实配置键。
    adv_card(v_sd, '数据与诊断',
             ('autoCleanup', 'sampleInterval', 'debugPanel', 'proxy'), glyph='folder')
    # 1.5.5：四张卡按内容收紧，剩余高度在卡间均分——不在底部留成片空白
    # 2.1.0：条目数不足 8 时（按许可/开关少建卡）按实际条数收口，避免越界插入
    for _pos in range(min(7, v_sd.count()), 0, -2):
        v_sd.insertStretch(_pos, 1)

    # ---- 端口统计（独立子页，原独立导航页已删除） ----
    _t4, v_stat = new_tab('端口统计')
    v_stat.addWidget(g_stat, 1)

    # 设置页：QTabWidget 充满，不套外层滚动（一页无滚动条）
    stack.addWidget(se)

    _cfg_cache = {}
    _mq_ack = {'host': ''}   # 已确认过「明文外发」的 broker 地址（来自 mqtt.remote_acked_host）

    def poll_config():
        qwait(lambda: api('GET', '/api/config'), _config_done, key='config')

    def _config_done(r):
        try:
            if not isinstance(r, tuple) or r[0] != 200:
                return
            _code, d = r
            if not isinstance(d, dict):
                return
            cfg = d.get('config', d)
            # C11：缓存 BLE MAC（连接横带 MAC 末段展示用）
            _ble = cfg.get('ble') or {}
            if isinstance(_ble, dict):
                _cfg_cache['mac'] = str(_ble.get('mac') or '')
            _mq = cfg.get('mqtt') or {}
            if isinstance(_mq, dict):
                _mq_ack['host'] = str(_mq.get('remote_acked_host') or '')
            for section, fields in cfg_fields.items():
                vals = cfg.get(section) or {}
                if not isinstance(vals, dict):
                    continue
                for field, wgt, typ in fields:
                    if field not in vals:
                        continue
                    v = vals[field]
                    if v is None:
                        continue
                    sval = str(v)
                    # 文本字段：显示掩码值（如 0000****0000），让用户知道凭证已配置；
                    # 非文本字段遇到掩码则跳过。后端保存时会自动跳过含 **** 的敏感值。
                    if '****' in sval and typ != 'text':
                        continue
                    try:
                        if typ == 'check':
                            wgt.setChecked(bool(v))
                        elif typ == 'spin':
                            wgt.setValue(int(float(v)))
                        elif typ == 'combo':
                            ix = wgt.findData(sval)
                            if ix < 0:
                                ix = wgt.findText(sval)
                            if ix >= 0:
                                wgt.setCurrentIndex(ix)
                        else:
                            wgt.setText(sval)
                    except Exception as _exc:
                        ck_swallow(_exc, 'launcher:8008')
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:8010')

    # ---- 设备实时状态（场景/息屏/连接），设置页回显（CK-N01/E4/P3-01） ----
    def poll_device_live():
        qwait(lambda: api('GET', '/api/status'), _device_live_done, key='device_live')

    _last_scene = {'v': 1}
    _last_screen = {'v': 1}
    _last_lang = {'v': 1}
    _last_idle = {'v': 0}
    _last_lock = {'v': 0}

    def _device_live_done(r):
        try:
            if not isinstance(r, tuple) or r[0] != 200:
                return
            _code, d = r
            if not isinstance(d, dict):
                return
            stg = d.get('settings') or {}
            if '5' in stg:
                sv = int(stg.get('5') or 1)
                scene_sel.set_value(sv)
                _last_scene['v'] = sv
            if '6' in stg:
                _last_screen['v'] = int(stg.get('6') or 1)
                screen_sel.set_value(int(stg.get('6') or 1))
            if '13' in stg:
                _last_lang['v'] = int(stg.get('13') or 1)
                lang_sel.set_value(_last_lang['v'])
            if '19' in stg:
                _last_idle['v'] = int(stg.get('19') or 0)
                idle_sel.set_value(_last_idle['v'])
            if '20' in stg:
                _last_lock['v'] = int(stg.get('20') or 0)
                lock_sel.set_value(_last_lock['v'])
            connected = bool(d.get('connected')) and bool(d.get('authenticated'))
            ble_conn_b.setText('断开连接' if connected else '连接设备')
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:8057')

    def poll_active_dev():
        """1.5.5：端口卡「本次充电设备名」数据源（5 s 一拍，仅端口页可见时运行）。"""
        def _done(r):
            try:
                if not isinstance(r, tuple) or r[0] != 200:
                    return
                m = {}
                for s in ((r[1] or {}).get('sessions') or []):
                    try:
                        m[int(s.get('port') or 0)] = s
                    except (TypeError, ValueError):
                        continue
                _CK_ACTIVE.clear()
                _CK_ACTIVE.update(m)
                # 映射缺失时补一次记录页拉取（只做一次），保证未命名设备也能显示 DEV 编号
                global _CK_HINT_ASKED
                _miss = any(str(s.get('device_key') or '') and
                            str(s.get('device_key') or '') not in _CK_BOX_HINT
                            for s in m.values())
                if _miss and not _CK_HINT_ASKED:
                    _CK_HINT_ASKED = True
                    try:
                        poll_sessions()
                    except Exception as _exc:
                        ck_swallow(_exc, 'launcher:hint_sessions')
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:active_sessions')
        qwait(lambda: api('GET', '/api/sessions/active'), _done, key='active_sessions')

    def _set_piid(piid, val, sel_wgt=None, prev=None):
        # C7：未连接/未认证时禁用，后端 ok:false 时回滚选择并 toast
        if not (conn_state.get('ever_ok') and conn_state.get('last_connected')):
            w._toast.show('未连接充电器，设置不可用', 'warning')
            if sel_wgt is not None and prev is not None:
                sel_wgt.blockSignals(True)
                sel_wgt.set_value(prev)
                sel_wgt.blockSignals(False)
            return

        def _job():
            try:
                return api('POST', '/api/set', {'piid': piid, 'value': val}, timeout=CK_T_CMD)
            except Exception as e:
                return ('ERR', str(e))

        def _done(r):
            _code = r[0] if isinstance(r, tuple) else 'ERR'
            _d = r[1] if isinstance(r, tuple) and len(r) > 1 else None
            if _code == 200 and (not isinstance(_d, dict) or _d.get('ok', True)):
                poll_device_live()
            else:
                if sel_wgt is not None and prev is not None:
                    sel_wgt.blockSignals(True)
                    sel_wgt.set_value(prev)
                    sel_wgt.blockSignals(False)
                w._toast.show('设置失败（HTTP %s）' % _code, 'warning')
        qwait(_job, _done)

    def _on_scene(v):
        # 1.5.5：模式说明不再占用页面正文，统一在「?」里（四种模式全量列出）。
        _set_piid(5, v, scene_sel, _last_scene['v'])
        _last_scene['v'] = v
    scene_sel.chosen.connect(_on_scene)

    def _on_screen(v):
        _set_piid(6, v, screen_sel, _last_screen['v'])
        _last_screen['v'] = v
    screen_sel.chosen.connect(_on_screen)

    def _on_lang(v):
        _set_piid(13, v, lang_sel, _last_lang['v'])
        _last_lang['v'] = v
    lang_sel.chosen.connect(_on_lang)

    def _on_idle_scr(v):
        _set_piid(19, v, idle_sel, _last_idle['v'])
        _last_idle['v'] = v
    idle_sel.chosen.connect(_on_idle_scr)

    def _on_lock(v):
        _set_piid(20, v, lock_sel, _last_lock['v'])
        _last_lock['v'] = v
    lock_sel.chosen.connect(_on_lock)

    def _on_ble_conn():
        ble_connect_toggle(ble_conn_b)
        QTimer.singleShot(300, poll_device_live)
    ble_conn_b.clicked.connect(_on_ble_conn)

    # ===== 最小加法：进阶配置作为既有设置页的第 6 个子标签（不做分区体系重构）=====
    def _open_docs(start=0):
        """说明与协议：左侧目录 + 右侧一次只显示一份正文（可关闭返回）。"""
        _prev = getattr(w, '_docs_dlg', None)
        if _prev is not None:
            try:
                _prev.close()
                _prev.deleteLater()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:docs-close-prev')
        dlg = QDialog(w)
        dlg.setWindowTitle('说明与协议')
        dlg.resize(860, 600)
        dv = QVBoxLayout(dlg)
        dv.setContentsMargins(16, 12, 16, 12)
        dv.setSpacing(10)
        # 48 px 页内头走 ui_kit.page_head（文档模型＝标题 + 右侧更新日期/版本）
        _hdr, _ = uikit.page_head(
            text='说明与协议',
            meta='最后更新 %s · 版本 v%s' % (ux.DOCS_UPDATED, ux.APP_VERSION))
        dv.addWidget(_hdr)
        # 与首启同意卡、进阶配置「隐私与协议」共用同一视图，不再 tab 叠 tab
        lv = ux.LegalView(dlg)
        lv.select(max(0, min(int(start), len(ux.LEGAL_VIEW_ITEMS) - 1)))
        dv.addWidget(lv, 1)
        _row = QHBoxLayout()
        _row.addStretch(1)
        _cb = QPushButton('关闭')
        _cb.setMinimumSize(96, 32)
        _cb.clicked.connect(dlg.accept)
        _row.addWidget(_cb)
        dv.addLayout(_row)
        # 非阻塞显示（避免嵌套事件循环引发重入崩溃），窗口单例复用
        w._docs_dlg = dlg
        dlg.setModal(True)
        dlg.show()

    # R03（25 轮）：原「查看说明与协议」单独一行，与面板页脚按钮基线差 13 px；
    # 改为创建后交给 AdvPanel 并入同一页脚行（extra_btn）。
    _doc_b = QPushButton('查看说明与协议')
    _doc_b.setToolTip('使用说明、隐私说明与用户协议')
    _doc_b.clicked.connect(lambda: _open_docs(0))

    _t5 = QWidget()
    _t5v = QVBoxLayout(_t5)
    _t5v.setContentsMargins(8, 8, 8, 8)
    # 1.5.5：A 口小电流保持时长从「自动化」迁入进阶配置——同一配置只留一处入口
    g_arule = GlassCard('A 口小电流自动保持')
    g_arule.set_glyph('shield')
    arv = QVBoxLayout(g_arule)
    arv.setContentsMargins(12, 10, 12, 10)
    arv.setSpacing(6)
    arv.setSizeConstraint(QLayout.SetMinimumSize)
    ar_on = QCheckBox('启用规则')
    ar_on.setFixedHeight(24)
    ar_min = ux.IntField(30, 240, 90)
    ar_min.setFixedWidth(72)
    ar_save = aligned_button('保存', 'primary', min_w=72, h=32, glyph='shield')
    ar_row = QHBoxLayout()
    ar_row.setSpacing(8)
    ar_row.addWidget(ar_on)
    ar_row.addSpacing(8)
    ar_row.addWidget(_tag('保持时长'))
    ar_row.addWidget(ar_min)
    ar_row.addWidget(_tag('分钟', True))
    ar_row.addWidget(ux.info_badge(
        'A 口小电流自动保持',
        '触发条件：该设备历史会话 ≥3 条、A 口占比 ≥60%、A 口平均功率 <15 W。'
        '触发后把 USB-A 切到小电流模式并保持设定时长（30–240 分钟，默认 90）。'
        '到期回默认策略（关闭小电流）；保持期内不因短时电流波动反复切换，'
        '设备断开满 2 分钟提前结束。A 口不受「无充电 10 分钟自动断」约束，'
        '两条规则不冲突。每次触发/结束都写入 auto_rules.log 审计。'),
        0, Qt.AlignVCenter)
    ar_row.addWidget(ar_save)
    ar_row.addStretch(1)
    arv.addLayout(ar_row)
    ar_state = QLabel('未启用')
    ar_state.setObjectName('sub')
    ar_state.setWordWrap(True)
    arv.addWidget(ar_state)
    adv_panel = ux.AdvPanel(
        _t5, adv_prefs, adv_secrets,
        on_apply=lambda vals: _apply_adv(vals),
        on_clear=lambda: _clear_data(), on_diag=lambda: _export_diag(),
        on_guide=lambda key: _open_docs(0),
        on_dev=lambda: _open_device_groups(),
        # R026：AI 项只在「小源 → AI 配置」保留一处，进阶配置不再重复
        skip_keys=('aiEnabled', 'aiProvider', 'aiModel', 'aiBaseUrl', 'aiApiKey',
                   'aiRedact', 'aiDailyReport'),
        # 1.5.5：三块内容迁走后本页只剩「设备识别 + 性能与曲线」8 项，
        # 取消手风琴，两组全展开、两列网格，一屏放下（无滚动、无折叠）。
        # 2.1.0：补上「设备分组上限」——可在此直接改（编辑结束即保存生效），
        # 右侧实时显示「已用 N（固定 X · 自建 Y）」，与分组页徽标同一口径。
        groups=(('设备识别', ('deviceSensitivity', 'devShapeThr', 'devCenterMode',
                             'devCandMin', 'devHourGuard', 'devManage',
                             'devMaxBoxes')),
                ('性能与曲线', ('perfMode', 'expFeatures', 'pollInterval',
                               'chartPoints', 'smooth'))),
        flat=True,
        notes={'devMaxBoxes': lambda: '已用 %d（固定 %d · 自建 %d）'
               % (ux.device_cap(adv_prefs)['used'],
                  ux.device_cap(adv_prefs)['fixed'],
                  ux.device_cap(adv_prefs)['custom'])},
        immediate=('devMaxBoxes',),
        extra_btn=_doc_b, card_factory=GlassCard,
        top_cards=(g_arule,))
    # R026：小源 的配置改动需回写到进阶配置状态行（双向同步）
    try:
        adv_panel.changed.connect(lambda *_a: _refresh_adv_state())
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:8188')
    _t5v.addWidget(adv_panel, 1)
    _set_tabs.addTab(_t5, '进阶配置')
    _tab_boost(_set_tabs, ['plug', 'screen', 'battery', 'server', 'avg', 'sliders'])

    # 1.5.5：阈值规则（充电限制与保护页）与 A 口规则（进阶配置页）接线
    def save_threshold_rule():
        pk = str(thr_port.currentData())
        _mn = round(thr_min.value(), 2)
        _mx = round(thr_max.value(), 2)
        # R2-CK24：前端即时校验 min≤max（全零=禁用豁免），防脏阈值触发误报告警
        if (_mn > _mx) and not (_mn == 0.0 and _mx == 0.0):
            w._toast.show('下限不能大于上限（当前 %.0f > %.0f）' % (_mn, _mx), 'warning')
            return
        payload = {'port': pk,
                   'min_w': _mn,
                   'max_w': _mx}
        thr_save.setEnabled(False); thr_save.setText('保存中…')

        def work():
            try:
                return api('POST', '/api/automation/threshold', payload, timeout=10)
            except Exception as e:
                return ('ERR', str(e))

        def done(r):
            thr_save.setEnabled(True); thr_save.setText('保存阈值规则')
            if isinstance(r, tuple) and r[0] == 200:
                w._toast.show('阈值规则已保存', 'ok')
                poll_automation()
            else:
                w._toast.show('保存失败', 'warning')

        qwait(work, done)

    thr_save.clicked.connect(save_threshold_rule)

    # 选择端口时回填该端口已有阈值
    def on_thr_port_change(_idx):
        try:
            _data = getattr(on_thr_port_change, '_autom', {})
            th = (_data.get('thresholds') or {}).get(str(thr_port.currentData()))
            if th:
                thr_min.setValue(float(th.get('min_w') or 0))
                thr_max.setValue(float(th.get('max_w') or 0))
            else:
                thr_min.setValue(0); thr_max.setValue(0)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:7344')
    thr_port.currentIndexChanged.connect(on_thr_port_change)

    # A 口小电流自动保持——读写 /api/auto_rules（热生效，不重启）
    def _ck_ar_render(d):
        d = d or {}
        _on0 = bool(d.get('enabled'))
        ar_on.setChecked(_on0)
        try:
            _hm = int(d.get('hold_minutes') or 90)
        except (TypeError, ValueError):
            _hm = 90
        ar_min.setValue(_hm)
        holds = d.get('holds') or []
        if not _on0:
            _s0 = '未启用：开关关闭时不触发任何自动切换'
        elif holds:
            h = holds[0] or {}
            try:
                _tn = time.strftime('%H:%M', time.localtime(float(h.get('until') or 0)))
            except (TypeError, ValueError, OSError):
                _tn = '—'
            _dev = str(h.get('device_label') or h.get('device_key') or '未知设备')
            _s0 = '保持中：USB-A 小电流 · 至 %s · %s' % (_tn, _dev)
        else:
            _s0 = '已启用：等待「惯用 A 口小电流」设备接入'
        if _on0:
            _s0 += ('<br><span style="color:#7f93ad">生效规则：设备历史 ≥3 条 · '
                    'A 口占比 ≥60%% · 均功率 &lt;15 W → A 口小电流，保持 %d 分钟'
                    '（到期回默认策略）</span>' % _hm)
        ar_state.setText(_s0)

    def _ck_ar_load():
        qwait(lambda: api('GET', '/api/auto_rules'), _ck_ar_load_done, key='auto_rules')
        qwait(lambda: api('GET', '/api/auto_rules/log'), _ck_ar_log_done, key='auto_rule_log')

    def _ck_ar_log_done(r):
        global _CK_RULE_EVENTS
        if isinstance(r, tuple) and r[0] == 200 and isinstance(r[1], dict):
            _CK_RULE_EVENTS = list(r[1].get('events') or [])[-80:]

    def _ck_ar_load_done(r):
        if isinstance(r, tuple) and r[0] == 200 and isinstance(r[1], dict):
            _ck_ar_render(r[1])

    def save_auto_rule():
        payload = {'enabled': ar_on.isChecked(), 'hold_minutes': int(ar_min.value())}
        ar_save.setEnabled(False)

        def work():
            try:
                return api('POST', '/api/auto_rules', payload, timeout=10)
            except Exception as e:
                return ('ERR', str(e))

        def done(r):
            ar_save.setEnabled(True)
            if isinstance(r, tuple) and r[0] == 200 and isinstance(r[1], dict):
                _ck_ar_render(r[1])
                w._toast.show('规则已保存 · 已热生效（无需重启）', 'ok')
            else:
                w._toast.show('保存失败', 'warning')

        qwait(work, done)

    ar_save.clicked.connect(save_auto_rule)
    _ck_ar_load()

    def _refresh_adv_state():
        _ok = adv_prefs.consent_ok()
        _ai = bool(adv_prefs.get('aiEnabled'))
        _ai_lbl = '开' if (_ai and _ok) else ('开·未同意' if _ai else '关')
        # R026：页脚单行只保留两项，其余进 tooltip，避免与按钮重叠
        w._ai_state_text = '隐私：%s ｜ AI：%s' % ('已同意' if _ok else '未同意', _ai_lbl)
        w._ai_state_tip = '隐私：%s ｜ AI：%s ｜ 性能：%s ｜ 动画：%s' % (
            '已同意' if _ok else '未同意', _ai_lbl,
            adv_prefs.get('perfMode'), adv_prefs.get('animLevel'))
        try:
            adv_panel.status.setText(w._ai_state_text)
            adv_panel.status.setToolTip(getattr(w, '_ai_state_tip', w._ai_state_text))
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:8205')

    def _open_adv_cfg():
        """入口：切到既有设置页内的「进阶配置」子标签（不弹独立窗口）。"""
        _si = stack.indexOf(se)
        if _si >= 0:
            stack.setCurrentIndex(_si)
        _set_tabs.setCurrentIndex(_set_tabs.indexOf(_t5))
        _refresh_adv_state()

    _refresh_adv_state()

    def collect_section(section):
        out = {}
        for field, wgt, typ in cfg_fields.get(section, []):
            if typ == 'check':
                out[field] = wgt.isChecked()
            elif typ == 'spin':
                out[field] = wgt.value()
            elif typ == 'combo':
                _dv = wgt.currentData()
                out[field] = _dv if _dv is not None else wgt.currentText()
            else:
                out[field] = wgt.text()
        return out

    def save_section(section, save_b, extra=None):
        # 后端 /api/config 只认 {"config": {section: {...}}}；此前少一层 config，
        # 后端取到空 dict 却仍回 ok（保存空转），已修正。
        data = collect_section(section)
        if extra:
            data.update(extra)
        payload = {'config': {section: data}}
        save_b.setEnabled(False)
        save_b.setText('保存中…')

        def work():
            try:
                code, d = api('POST', '/api/config', payload, timeout=15)
            except Exception:
                return ('ERR', None)
            # C4/C15：非 200 直接失败（保留输入、按钮复位、显示后端 error），
            # 不再静默"已保存"；仅 reconnect_required 才等待服务重启。
            if code != 200:
                return (code, d)
            flags = d if isinstance(d, dict) else {}
            if flags.get('reconnect_required'):
                import socket as _s
                for _ in range(80):
                    time.sleep(0.5)
                    sk = _s.socket()
                    sk.settimeout(0.4)
                    try:
                        sk.connect((HOST, port))
                        sk.close()
                        # 再多等一拍让路由就绪
                        time.sleep(1.0)
                        break
                    except OSError as _exc:
                        ck_swallow(_exc, 'launcher:8258')
            QTimer.singleShot(0, lambda: poll_config())
            return (code, d)

        def done(r):
            save_b.setEnabled(True)
            code = r[0] if isinstance(r, tuple) else 'ERR'
            d = r[1] if isinstance(r, tuple) and len(r) > 1 else None
            if code == 200:
                flags = d if isinstance(d, dict) else {}
                if flags.get('reconnect_required'):
                    save_b.setText('已保存·需重连')
                    w._toast.show('已保存，重连后生效', 'ok')
                elif flags.get('mqtt_reapplied'):
                    # 1.5.5：MQTT 走后端热重连（停旧客户端→按新模式重订阅）
                    save_b.setText('已保存·已重连')
                    w._toast.show('已保存，MQTT 已按新配置重连', 'ok')
                    # 2.1.0：重连后立刻回读，首页/设置徽章同一拍更新
                    QTimer.singleShot(0, poll_mqtt_status)
                elif flags.get('restart_required'):
                    save_b.setText('已保存·需重启')
                    w._toast.show('已保存，重启后生效', 'ok')
                else:
                    save_b.setText('已保存')
                    w._toast.show('已热生效', 'ok')
            else:
                save_b.setText('保存失败')
                err = (d or {}).get('error') if isinstance(d, dict) else None
                w._toast.show('保存失败：%s' % (err or ('HTTP %s' % code)), 'warning')
        qwait(work, done)

    def _is_loopback(host):
        h = (host or '').strip().lower()
        if h in ('localhost', '::1'):
            return True
        try:
            import ipaddress as _ip
            return _ip.ip_address(h).is_loopback
        except ValueError:
            return False

    def save_mqtt():
        """MQTT 保存：巴法云先卡主题名，host 非回环时弹一次数据外发确认。"""
        host = (mq_host.text() or '').strip() or 'localhost'
        mq_host.setText(host)
        _is_bemfa = (mq_mode.currentData() == 'bemfa')
        if _is_bemfa:
            # 主题名必须是控制台里已建好的名字：空/斜杠/非法字符一律当场拦下
            errs = []
            for _k, _lab in (('c1', '1号口'), ('c2', '2号口'),
                             ('c3', '3号口'), ('a', 'A口')):
                _t = (mq_topics[_k].text() or '').strip()
                mq_topics[_k].setText(_t)
                if not _t:
                    errs.append('%s：主题名为空，去巴法云控制台复制主题名粘贴到这里' % _lab)
                elif '/' in _t:
                    errs.append('%s：斜杠不允许，只能字母数字下划线' % _lab)
                elif not re.match(r'^[0-9A-Za-z_]+$', _t):
                    errs.append('%s：只能字母数字下划线（不能有空格、连字符、汉字）' % _lab)
                elif len(_t) > 48:
                    errs.append('%s：主题名过长（%d > 48）' % (_lab, len(_t)))
            if errs:
                # 状态行由 2s 状态拍单写，瞬时结果只走 toast，避免被下一拍覆盖
                w._toast.show('未保存：' + errs[0], 'warning')
                return
        extra = {}
        if mq_enabled.isChecked() and not _is_loopback(host):
            if host != (_mq_ack.get('host') or ''):
                box = QMessageBox(w)
                box.setWindowTitle('确认数据外发')
                box.setIcon(QMessageBox.NoIcon)
                if _is_bemfa:
                    box.setText(
                        '开启后本机会以巴法云设备协议连接 %s:%s，'
                        '把四路开关状态上报到该云端，并接受云端下发的 on/off。\n\n'
                        '数据将离开这台电脑。确认继续？' % (host, mq_port.value()))
                else:
                    box.setText(
                        '开启 MQTT 后，本机会以明文 TCP（无 TLS、无鉴权）连接 %s:%s，'
                        '并把设备状态发送到该地址、接受该地址下发的指令。\n\n'
                        '数据将离开这台电脑。确认继续？' % (host, mq_port.value()))
                box.setStandardButtons(QMessageBox.NoButton)
                _y = box.addButton('确认发送', QMessageBox.AcceptRole)
                _n = box.addButton('取消', QMessageBox.RejectRole)
                box.setDefaultButton(_n)
                box.setStyleSheet('QPushButton{min-width:96px;max-width:96px;}')
                box.exec()
                if box.clickedButton() is not _y:
                    w._toast.show('已取消保存 MQTT 配置', 'warning')
                    return
                _mq_ack['host'] = host
                extra['remote_acked_host'] = host
        save_section('mqtt', mq_save, extra)

    def apply_bemfa_preset():
        """巴法云预设：切模式 + 填官方地址端口；四个主题必须从控制台原样粘贴。"""
        _ix = mq_mode.findData('bemfa')
        if _ix >= 0:
            mq_mode.setCurrentIndex(_ix)
        mq_host.setText('mqtt.bemfa.com')
        mq_port.setValue(9501)
        mq_cid.setPlaceholderText('填你的私钥 UID')
        mq_cid.setToolTip('巴法云：客户端 ID 必须填「私钥 UID」；用户名、密码留空。')
        _mq_apply_mode_ui()
        w._toast.show('已填好 mqtt.bemfa.com:9501；'
                      '四个主题请从控制台原样粘贴', 'ok')
        mq_cid.setFocus()

    def _mq_test_clicked():
        """测试连接：拿当前填写值直接握手一次，不保存、不改现有连接。"""
        mq_test.setEnabled(False)
        mq_test.setText('测试中…')
        payload = {
            'host': (mq_host.text() or '').strip(),
            'port': mq_port.value(),
            'client_id': (mq_cid.text() or '').strip(),
            'mode': mq_mode.currentData() or 'generic',
        }

        def work():
            try:
                return api('POST', '/api/mqtt/test', payload, timeout=15)
            except Exception as _e:
                return ('ERR', {'reason': str(_e)})

        def done(r):
            mq_test.setEnabled(True)
            mq_test.setText('测试连接')
            if not isinstance(r, tuple):
                r = ('ERR', {'reason': '请求失败'})
            _code, d = r
            d = d if isinstance(d, dict) else {}
            if d.get('ok'):
                w._toast.show('测试连接成功（%sms）' % d.get('elapsed_ms', 0), 'ok')
            else:
                reason = d.get('reason') or d.get('error') or ('HTTP %s' % _code)
                w._toast.show('测试失败：%s' % reason, 'warning')

        qwait(work, done)

    mq_bemfa.clicked.connect(apply_bemfa_preset)
    mq_save.clicked.connect(save_mqtt)
    mq_test.clicked.connect(_mq_test_clicked)

    ble_save.clicked.connect(lambda: save_section('ble', ble_save))
    lim_save.clicked.connect(lambda: save_section('charge_limit', lim_save))
    prot_save.clicked.connect(lambda: save_section('protection', prot_save))
    srv_save.clicked.connect(lambda: save_section('server', srv_save))

    # service 区块与 server 分开保存（R008/N5：改后台，原 UI 线程同步 15s）
    def save_service():
        payload = {'config': {'service': collect_section('service')}}
        svc_save.setEnabled(False)
        svc_save.setText('保存中…')

        def work():
            try:
                return api('POST', '/api/config', payload, timeout=15)
            except Exception as e:
                return ('ERR', str(e))

        def done(r):
            svc_save.setEnabled(True)
            code = r[0] if isinstance(r, tuple) else 'ERR'
            svc_save.setText('已保存 HTTP %s' % code)
            if code == 200:
                w._toast.show('服务配置已保存', 'ok')
            else:
                w._toast.show('保存失败', 'warning')
        qwait(work, done)
    svc_save.clicked.connect(save_service)

    def save_csv(name, path, data):
        _edef = str(QSettings('DoubaoWork', 'CuktechPanel').value('export_dir', '') or '')
        _start = os.path.join(_edef, name) if _edef else name
        fn, _ = QFileDialog.getSaveFileName(w, '保存导出文件', _start, 'CSV (*.csv)')
        if not fn:
            return
        with open(fn, 'wb') as f:
            f.write(data)
        _ck_msg(w, '导出', '已保存：%s' % fn)

    def do_exp_port():
        port_i = exp_port.currentIndex() + 1
        hours = ['24', '168', '720'][exp_hours.currentIndex()]
        exp_port_b.setEnabled(False)
        qwait(lambda: get_bytes('/api/export/%d?hours=%s' % (port_i, hours), timeout=30),
              lambda r: _exp_done(r, 'port%d.csv' % port_i, exp_port_b))

    def _build_sessions_csv():
        """1.5.5：会话 CSV 与界面同源同口径（同一 analyze_sessions + device_box_name）。

        列：时间 / 端口 / 设备归属（箱名，未命名才 DEV 编号）/ 断开原因（简化文案）/
        时长 秒 / 充入 Wh / 峰功率 W / 均功率 W。数值不再前端二次换算，
        直接取界面同一行的原始字段，避免与界面数字对不上。
        """
        import csv
        import io
        period = rec_period[rec_range.currentIndex()]
        rows, page, pages = [], 1, 1
        while page <= pages and page <= 20:
            _r = api('GET', '/api/sessions?period=%s&limit=50&page=%d' % (period, page))
            if not isinstance(_r, tuple) or _r[0] != 200 or not isinstance(_r[1], dict):
                return None
            _d = _r[1]
            rows.extend(_d.get('sessions') or _d.get('items') or [])
            pages = max(1, int(_d.get('pages') or 1))
            page += 1
        _pi = rec_port.currentIndex()
        if _pi:
            _want = 4 if _pi == 4 else _pi
            rows = [x for x in rows if int(x.get('port') or 0) == _want]
        _model = ux.device_model_load(adv_prefs)
        an = ux.analyze_sessions(rows, int(adv_prefs.get('mergeGap') or 120),
                                 str(adv_prefs.get('deviceSensitivity') or '均衡'), _model)
        names = ux.device_names(_model)
        reason_by_id, box_by_id = {}, {}
        for _it, _rs in zip(an.get('sessions') or [], an.get('reasons') or []):
            for _sid in (_it.get('_ids') or [_it.get('id')]):
                try:
                    reason_by_id[int(_sid)] = _rs
                except (TypeError, ValueError):
                    pass
        for _c in (an.get('clusters') or []):
            for _it in (_c.get('items') or []):
                for _sid in (_it.get('_ids') or [_it.get('id')]):
                    try:
                        box_by_id[int(_sid)] = str(_c.get('id') or '')
                    except (TypeError, ValueError):
                        pass

        def _num(v, nd=0):
            try:
                return ('%.' + str(nd) + 'f') % float(v)
            except (TypeError, ValueError):
                return ''
        buf = io.StringIO()
        _w = csv.writer(buf, lineterminator='\r\n')
        _w.writerow(['时间', '端口', '设备归属', '断开原因',
                     '时长 秒', '充入 Wh', '峰功率 W', '均功率 W'])
        for row in rows:
            _sid = int(row.get('id') or 0)
            _bid = box_by_id.get(_sid, '')
            _dev = (ux.device_box_name(_bid, names.get(_bid, '')) if _bid else '未归箱')
            _rs = reason_by_id.get(_sid) or {}
            _why = _rs.get('label') or ('进行中' if row.get('is_active') else '未识别')
            try:
                _t = ck_time.fmt(ck_time.MD_HM, row.get('start_time'))
            except Exception:
                _t = ''
            _pv = int(row.get('port') or 0)
            _w.writerow([_t, ('C%d' % _pv) if _pv in (1, 2, 3) else 'A',
                         _dev, _why,
                         _num(row.get('duration_sec'), 0),
                         _num(row.get('total_wh'), 2),
                         _num(row.get('peak_power_w'), 1),
                         _num(row.get('avg_power_w'), 1)])
        return buf.getvalue().encode('utf-8')

    def do_exp_sessions():
        exp_sess_b.setEnabled(False)
        try:
            _data = _build_sessions_csv()
        finally:
            exp_sess_b.setEnabled(True)
        if not _data:
            _ck_msg(w, '导出', '暂无会话记录可导出', warn=True)
            return
        # UTF-8 BOM：Excel 直接双击不乱码
        save_csv('sessions.csv', None, b'\xef\xbb\xbf' + _data)


    def do_exp_energy():
        exp_energy_b.setEnabled(False)
        qwait(lambda: get_bytes('/api/export/energy', timeout=30),
              lambda r: _exp_done(r, 'energy.csv', exp_energy_b))

    def _exp_done(r, name, btn=None):
        if btn is not None:
            btn.setEnabled(True)
        if r[0] == 200:
            save_csv(name, None, r[1])
        else:
            _ck_msg(w, '导出', '失败 HTTP %s' % r[0], warn=True)

    exp_port_b.clicked.connect(do_exp_port)
    exp_sess_b.clicked.connect(do_exp_sessions)
    exp_energy_b.clicked.connect(do_exp_energy)

    # 巴法云/云端已按用户要求整体移除（CK-29），连接卡不再显示云端徽章。

    # ===== timers（按页启停，避免隐藏页空轮询）=====
    t1 = QTimer(w); t1.timeout.connect(poll_status)
    # R22：t2 改为曲线提交节拍器（12s 一帧）；数据由 t1 每拍入缓冲，t2 只负责绘制。
    t2 = QTimer(w); t2.timeout.connect(_chart_commit)
    t3 = QTimer(w); t3.timeout.connect(poll_sessions)
    t4 = QTimer(w); t4.timeout.connect(poll_automation)
    t7 = QTimer(w); t7.timeout.connect(poll_rec_stats)
    t8 = QTimer(w); t8.timeout.connect(poll_device_live)
    # 1.5.5：端口页「本次充电设备名」5 s 一拍
    t_ad = QTimer(w); t_ad.timeout.connect(poll_active_dev)
    # MQTT 连接状态行：设置页可见时 2s 一拍
    t_mq = QTimer(w); t_mq.timeout.connect(poll_mqtt_status)
    # 判定与断开都由后端 backend/auto_off.py 常驻执行，界面不再有判定采样定时器。
    # R015（领导）：刷新率对齐网页端 liveEngine——状态 2s、曲线 1s（R15-C19 修正注释口径）
    for _t, _ms in ((t1, 1000), (t2, 12000), (t3, 15000), (t4, 20000),
                    (t7, 15000), (t8, 5000), (t_mq, 2000), (t_ad, 5000)):
        _t.setInterval(_ms)

    # 页面 -> 该页所需定时器（与 _refresh_current 的 fns 对应）
    # R15-S01：端口页保留 t2（迷你曲线数据源），否则四卡迷你图永无数据
    # 2.1.0：页号对齐导航——小源=4（无轮询）、设置=5；t_mq 升为全局拍，
    #        云端状态不再依赖是否进过设置页；设置页补 t1，蓝牙/充电器徽章与首页同拍。
    PAGE_TIMERS = {0: (t1, t2), 1: (t1, t2, t_ad), 2: (t3, t7),
                   3: (t4,), 4: (), 5: (t1, t8)}
    _always_timers = (t_mq,)
    _all_timers = (t1, t2, t3, t4, t7, t8, t_mq, t_ad)
    w._r19_timers = _all_timers  # R19：进程内探针用它做"停轮询"单变量 A/B（不影响 exe 行为）

    # R18：不可见（最小化/完全遮挡）期间停算，恢复时补一帧
    _sus = {'on': False, 'state': 'unknown'}

    def apply_timers(idx):
        if _sus['on']:
            return
        active = PAGE_TIMERS.get(idx, ()) + _always_timers
        for _t in _all_timers:
            if _t in active:
                if not _t.isActive():
                    _t.start()
            elif _t.isActive():
                _t.stop()
        if t2 in active:
            # R22：切回曲线页立即重绘一帧（不等 12s 窗口）
            QTimer.singleShot(0, lambda: _chart_commit(force=True))

    stack.currentChanged.connect(apply_timers)
    apply_timers(stack.currentIndex())
    # 2.1.0：启动即回读一次云端状态，首页徽章不必等首个 2s 拍
    QTimer.singleShot(0, poll_mqtt_status)

    def _refresh_records(idx):
        # R043 / C12：页容量按表格视口实测；页面隐藏时的几何不可信，
        # 切回记录页后重排一次，避免沿用隐藏态算出的行数（会漏看中间行）。
        if idx == stack.indexOf(rc):
            # 600 ms：等卡片/智能分析文字完成布局后再按视口算行数（80 ms 时几何未稳定）
            _dbg_log('rec-refresh idx=%d' % idx)
            QTimer.singleShot(600, poll_sessions)

    stack.currentChanged.connect(_refresh_records)

    def _win_state():
        try:
            hwnd = int(w.winId())
        except Exception:
            return 'unknown'
        u32 = ctypes.windll.user32
        try:
            if u32.IsIconic(hwnd):
                return 'min'
            if not u32.IsWindowVisible(hwnd):
                return 'hidden'
            rect = wintypes.RECT()
            u32.GetClientRect(hwnd, ctypes.byref(rect))
            cw, ch = rect.right, rect.bottom
            if cw < 100 or ch < 100:
                return 'badgeom'
            pt = wintypes.POINT()
            u32.ClientToScreen(hwnd, ctypes.byref(pt))
            hits = 0
            n = 0
            for fx in (1.0 / 6, 0.5, 5.0 / 6):
                for fy in (1.0 / 6, 0.5, 5.0 / 6):
                    x = pt.x + int(cw * fx)
                    y = pt.y + int(ch * fy)
                    n += 1
                    hit = u32.WindowFromPoint(wintypes.POINT(x, y))
                    root = u32.GetAncestor(hit, 2)  # GA_ROOT
                    if root == hwnd:
                        hits += 1
            return 'front' if hits else 'occluded'
        except Exception:
            return 'unknown'

    w._ck_win_state = _win_state  # R25：让测量 harness 能读到 governor 自己判定的窗口态（不参与逻辑）
    w._ck_sus_on = lambda: bool(_sus['on'])  # R25：同上，读 suspend 开关（不参与逻辑）

    def _pause_anims(pause):
        for obj in w.findChildren(QWidget):
            cls = type(obj).__name__
            try:
                if cls == 'LiveTag':
                    if pause:
                        obj._stop_beat()
                    elif obj._on and not obj._reduced:
                        obj._beat.add(obj._tick)
                        obj._beat_ok = True
                elif cls == 'BreathDot':
                    if pause:
                        obj._stop()
                    elif obj.isVisible():
                        obj._start()
                elif cls == 'FlowProgressBar':
                    # R27：遮挡/最小化 → 流光直接停（不是超长周期）
                    if pause:
                        obj._timer.stop()
                    else:
                        _ck_start(obj._timer, 'flow')
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:8435')
        try:
            _beat = UiBeat.instance()
            if pause:
                _beat._tmr.stop()
            else:
                _beat.wake()
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:sus-beat')

    def _set_suspend(flag):
        if flag == _sus['on']:
            return
        _sus['on'] = flag
        _CK_SUS_ON['on'] = flag
        if flag:
            # R22：遮挡/最小化只停「UI 刷新」，数据采集降频保活（5s 一次），恢复不丢历史点
            # C34b：进挂起前把最后一批延迟数字落屏——否则会出现「徽章已是新值、
            # hero/KPI 还是更旧值」的错位（#5：数字走延迟队列、徽章走直接 setText）。
            try:
                _ck_num_flush(force=True)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:sus-flush')
            for _t in _all_timers:
                if _t is not t1:
                    _t.stop()
            t1.setInterval(5000)
            if not t1.isActive():
                t1.start()
            # 判定与断开在后端运行，与窗口是否挂起无关。
            _pause_anims(True)
        else:
            _pause_anims(False)
            t1.setInterval(max(1000, int(getattr(w, '_poll_ms', 2000) or 2000)))
            _poll_pace['sig'] = None
            _poll_pace['step'] = 0
            w._idle_paced = None
            _CK_NUMQ['defer_all'] = False
            # C34b：挂起期间 _status_done 不再入队，pending 里只剩挂起前的陈旧值，
            # 直接 force 刷会把旧数字又贴一遍；先丢弃，等本拍 poll 回来上真值。
            _CK_NUMQ['pending'].clear()
            _ck_num_flush(force=True)      # R27：恢复立即上屏最新值（≤1.5s 口径）
            apply_timers(stack.currentIndex())
            try:
                poll_status()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:8451')
            try:
                # 2.1.2：恢复可见同时重取曲线基底——基底只在启动/切窗口时取过一次，
                # 挂起超过一个窗口宽度后，旧基底已全部落在窗口外，只补实时点会得到空图。
                poll_chart()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:8451-chart')
            try:
                # 2.1.0：恢复可见立即回读巴法云状态，不等下一个 2s 拍
                poll_mqtt_status()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:8451-mq')
            if stack.currentIndex() in (0, 1):
                try:
                    # R22：恢复可见立即提交一帧（曲线不等 12s）
                    _chart_commit(force=True)
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:8456')
            w.update()

    def _set_degrade(flag):
        """R26：可见但不聚焦——降级不停摆（装饰 2 fps + 曲线暂停提交），恢复聚焦立刻补一帧。"""
        if flag == _CK_DEG['on']:
            return
        _CK_DEG['on'] = flag
        try:
            UiBeat.instance()._tmr.setInterval(_ck_iv(500 if flag else 150))   # R27：降级 2 fps
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:deg-beat')
        if not flag:
            _poll_pace['sig'] = None
            _poll_pace['step'] = 0
            _ck_num_flush(force=True)      # R27：回到聚焦立刻上屏最新值
            try:
                poll_status()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:deg-poll')
            if stack.currentIndex() in (0, 1):
                try:
                    _chart_commit(force=True)   # 恢复聚焦立即补一帧，不等 12s
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:deg-chart')
            w.update()

    w._ck_deg_on = lambda: bool(_CK_DEG['on'])  # R26：只读钩子，供测量门禁
    w._ck_set_deg = _set_degrade               # R26：测试/口径钩子（正常路径仍由 isActiveWindow 触发）
    w._ck_set_sus = _set_suspend               # C34b：等价于遮挡/解锁触发的挂起切换（取证用）
    w._ck_num_stats = _CK_NUMQ                 # R27：数字节流计数（测量只读）
    w._ck_pace_ms = lambda: int(t1.interval())  # R27：当前轮询间隔（退避证据）

    def _ck_uistate():
        """C34b：不依赖前台/截图的界面状态快照（挂起态诊断用）。"""
        def _txt(o):
            try:
                return str(o.text())
            except Exception:
                return ''
        return {
            'win_state': _win_state(),
            'sus': bool(_CK_SUS_ON['on']),
            'pin': bool(_CK_SUS_PIN['on']),
            'sus_flag': bool(_sus['on']),
            'deg': bool(_CK_DEG['on']),
            't1_ms': int(t1.interval()),
            'pending': len(_CK_NUMQ['pending']),
            'armed': bool(_CK_NUMQ['armed']),
            'defer': bool(_CK_NUMQ['defer_all']),
            'muted': bool(_CK_MUTE.get('on')),
            'unread': int(_CK_UNREAD.get('n', 0)),
            # C34g：托盘取证——n 累计激活数；last 最近原因（Context=右键真的打到了我们）；
            # vis/menu 说明图标是否可见、上下文菜单是否仍挂着。
            'tray': {'n': int(_CK_TRAY['n']), 'last': list(_CK_TRAY['last']),
                     'vis': bool(tray.isVisible()),
                     'menu': bool(tray.contextMenu() is not None),
                     'items': len(tm.actions())},
            'beat_on': bool(UiBeat.instance()._tmr.isActive()),
            'applied': int(_CK_NUMQ.get('applied') or 0),
            'prev': {k: v for k, v in _ui_prev.items() if isinstance(v, (int, float, bool, str))},
            'texts': {'hero': _txt(hero_power), 'btn': _txt(conn_quick_b),
                      'conn': _txt(lbl_conn), 'ble': _txt(badge_ble),
                      'chg': _txt(badge_chg)},
        }

    w._ck_uistate = _ck_uistate                # C34b：测试钩子（只读）
    # 2.1.0：连接状态链取证钩子（只读）——首页巴法云徽章 / 设置页状态行 / 蓝牙徽章
    w._ck_mqtt_badge_lbl = badge_ble
    w._ck_mqtt_status_lbl = mq_status_lbl
    w._ck_ble_status_lbl = ble_state_lbl
    w._ck_chg_badge_lbl = badge_chg


    def _vis_tick():
        if _CK_SUS_PIN['on']:       # C34b：取证态钉住，避免 600ms 后又被判 occluded 挂起
            return
        st = _win_state()
        _sus['state'] = st
        _set_suspend(st in ('min', 'hidden', 'occluded'))
        if _CK_FORCE_DEG:            # 测量口径：强制"可见不聚焦"档（不依赖抢前台是否成功）
            _set_degrade(True)
            return
        if st == 'front':
            try:
                _set_degrade(not w.isActiveWindow())
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:vis-active')

    _vis_tmr = QTimer(w)
    _vis_tmr.setInterval(600)   # R26：600ms 一判，恢复聚焦更快补帧（单次 WindowFromPoint 约 µs 级）
    _vis_tmr.timeout.connect(_vis_tick)
    _vis_tmr.start()

    # ===== R024：进阶配置生效逻辑（启动静默回放 + 保存即时应用）=====
    # R25：运行时换主题的最小入口（只做视觉层：QSS 覆盖 + hero 渐变；不动轮询/动画配置）。
    # 主题 QSS 由 ux.THEME_TOKENS 生成（与进页面板同一令牌源），取值不变。
    def _theme_qss(name):
        _t = ux.THEME_TOKENS.get(name)
        if not _t:
            return ''
        return ('QWidget#root{background:%s;} QLabel{color:%s;}'
                'QLabel#sub{color:%s;} QLabel#h1{color:%s;}'
                % (_t['bg'], _t['text'], _t['text_sub'], _t['heading']))

    _THEME_QSS = {_n: _theme_qss(_n) for _n in ('高对比', '暗夜护眼')}
    _ACCENTS = {'青蓝': ('#2fb6ff', '#03a9f4'), '紫青': ('#a78bfa', '#7c4dff')}

    # 824 长帧归因落在 apply_theme。实测结论（_verify\c42_theme_opt）：
    #   · QSS 串不是每次重建——`_THEME_QSS` 启动即预算好；贵的是 setStyleSheet 本身：
    #     整窗挂一次固定 ~80 ms（离屏 1029 个控件），与规则内容无关（同串也 80 ms，
    #     Qt 不做相同串短路）；只挂可见页 21 ms；逐个 QLabel 0.007 ms。
    #   · 窗口级主题 QSS 只有四类规则：QWidget#root 底色（Backdrop 自绘 #1c1c1c，
    #     该规则被 paintEvent 覆盖、实际不生效）、QLabel / QLabel#sub / QLabel#h1 字色。
    # 所以等价改法＝把同一份 QSS 挂到「当前可见容器」而不是整窗：规则文本不变、层叠结果
    # 不变，只是不再为隐藏页付整树 repolish；隐藏页真正上屏时补挂一次（_ck_page_theme）。
    # A/B 用的 CK_THEME_LEGACY 旧路径已删除（对照结论留在 _verify\c42_theme_opt）。
    _ck_tstate = {'theme': None, 'qss': None, 'adv_pending': None}
    _CK_TSEG = {'qss': 0.0, 'adv': 0.0, 'hero': 0.0}

    def _ck_theme_scopes(_nm):
        """把主题 QSS 挂到「当前可见的容器」而不是整窗。

        规则文本与窗口级完全一致，因此控件的层叠结果不变；差异只在作用域：
        隐藏页不再为一次切主题付整树 polish，等它真正上屏时再补挂（_ck_page_theme）。
        """
        _qss = _THEME_QSS.get(_nm, '')
        _n = 0
        for _tgt in (rail, stack.currentWidget()):
            if _tgt is None:
                continue
            _tgt.setStyleSheet(_qss)
            _n += 1
        return _n

    def _ck_page_theme(*_a):
        """切页补挂：新上屏的页按当前主题重挂一次（未切主题时零成本）。"""
        _nm = _ck_tstate['theme']
        if not _nm:
            return
        _pg = stack.currentWidget()
        if _pg is None:
            return
        _pg.setStyleSheet(_THEME_QSS.get(_nm, ''))

    def apply_theme_visual(theme, accent=None):
        _nm = str(theme) or '原版深色'
        _a = time.perf_counter()
        if _nm != _ck_tstate['theme']:
            _ck_theme_scopes(_nm)
        _ck_tstate['theme'] = _nm
        _b = time.perf_counter()
        # 进阶配置面板：令牌立即切（doc_html 等读 TOKENS），面板 QSS 只在面板可见时重挂；
        # 隐藏时标脏，切到该页时补挂（_ck_adv_flush），避免为不可见子树付整树 polish。
        ux.set_theme(_nm)
        if adv_panel.isVisible():
            try:
                # C34-UI（骨架统一 A）：进阶配置面板同步换令牌，本页不再固定深色
                adv_panel.apply_theme(_nm)
                _ck_tstate['adv_pending'] = None
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:theme-adv')
        else:
            _ck_tstate['adv_pending'] = _nm
        # 1.5.5：分散在各二级页的扁平设置组用行内字色，令牌切换后重挂
        for _sec in _adv_sections:
            try:
                _sec.refresh_theme()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:theme-section')
        _c = time.perf_counter()
        try:
            hero_power.set_gradient(*_ACCENTS.get(str(accent), ('#03a9f4', '#7c4dff')))
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:theme')
        _d = time.perf_counter()
        _CK_TSEG['qss'] = (_b - _a) * 1000.0
        _CK_TSEG['adv'] = (_c - _b) * 1000.0
        _CK_TSEG['hero'] = (_d - _c) * 1000.0
    w._ck_apply_theme = apply_theme_visual
    w._ck_theme_state = _ck_tstate          # 探针用：清缓存做同进程 A/B，不改配置
    w._ck_theme_qss = _THEME_QSS            # 探针用：取预算好的主题串

    def _ck_adv_flush(*_a):
        """主题已切、但进阶配置面板当时不可见：等它真正上屏时补挂一次。"""
        _nm = _ck_tstate['adv_pending']
        if not _nm or not adv_panel.isVisible():
            return
        try:
            adv_panel.apply_theme(_nm)
            _ck_tstate['adv_pending'] = None
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:theme-adv-flush')
    try:
        stack.currentChanged.connect(_ck_adv_flush)
        stack.currentChanged.connect(_ck_page_theme)
        _set_tabs.currentChanged.connect(_ck_adv_flush)
    except (RuntimeError, TypeError) as _exc:
        ck_swallow(_exc, 'launcher:theme-adv-hook')

    def _ck_matrix(n=6, out=None):
        """只读自检：主题矩阵切换四段打点（不改配置、不写 prefs）。

        段口径（perf_counter，ms）：
          ①apply_theme  apply_theme_visual 返回（根 QSS 重算）
          ②show/polish  show + ensurePolished + 排空事件（真实 polish）
          ③grab+save    w.grab() + PNG 落盘
          ④first_frame  再排空一次事件循环（首帧布局/绘制对齐）
        """
        from PySide6.QtWidgets import QApplication as _QApp
        _themes = ('原版深色', '高对比', '暗夜护眼')
        _accents = ('青蓝', '紫青')
        _combos = [(t, a) for t in _themes for a in _accents]
        _recs = []
        _was_visible = w.isVisible()
        _dir = os.path.dirname(os.path.abspath(out)) if out else None
        if _dir:
            os.makedirs(_dir, exist_ok=True)
        for _i in range(max(1, int(n))):
            _th, _ac = _combos[_i % len(_combos)]
            _t0 = time.perf_counter()
            apply_theme_visual(_th, _ac)
            _t1 = time.perf_counter()
            if not w.isVisible():
                w.show()
            w.ensurePolished()
            _QApp.processEvents()
            _t2 = time.perf_counter()
            _pm = w.grab()
            _t3 = time.perf_counter()
            if _dir:
                try:
                    _pm.save(os.path.join(_dir, 'matrix_%02d.png' % _i))
                except OSError as _exc:
                    ck_swallow(_exc, 'launcher:matrix-save')
            # 口径修正。原先 frame_ms = t4-t3，把 _pm.save() 的 PNG 编码也算进
            # "首帧重绘"（2.92 Mpx 实测 111.96 ms @dpr2.0）。对照实验（同一代码路径）：
            # 保留 grab+save 39.9 ms / 去 grab 1.2~7.7 ms / 保留 grab 去 save 0.4~7.7 ms，
            # 且 6 轮里 5 轮 paint 事件为 0 —— 该段与绘制无关，是落盘编码。
            _t3b = time.perf_counter()
            _QApp.processEvents()
            _t4 = time.perf_counter()
            _recs.append({'i': _i, 'theme': _th, 'accent': _ac,
                          'apply_ms': round((_t1 - _t0) * 1000, 2),
                          'show_ms': round((_t2 - _t1) * 1000, 2),
                          'grab_ms': round((_t3 - _t2) * 1000, 2),
                          'save_ms': round((_t3b - _t3) * 1000, 2),
                          'frame_ms': round((_t4 - _t3b) * 1000, 2),
                          'total_ms': round((_t4 - _t0) * 1000, 2),
                          # apply 内部三段（qss=整窗 setStyleSheet / adv=进阶配置面板）
                          'qss_ms': round(_CK_TSEG['qss'], 2),
                          'adv_ms': round(_CK_TSEG['adv'], 2),
                          'hero_ms': round(_CK_TSEG['hero'], 2),
                          'w': _pm.width(), 'h': _pm.height()})
        if not _was_visible:
            w.hide()
        if out:
            try:
                with open(out, 'a', encoding='utf-8') as _f:
                    for _r in _recs:
                        _f.write(json.dumps(_r, ensure_ascii=False) + '\n')
            except OSError as _exc:
                ck_swallow(_exc, 'launcher:matrix-out')
        _worst = max(_recs, key=lambda r: r['total_ms']) if _recs else {}
        _tot = sorted(r['total_ms'] for r in _recs) or [0]
        return {'rounds': len(_recs), 'out': out,
                'max_total_ms': _worst.get('total_ms', 0), 'worst': _worst,
                'p50_ms': _tot[int(0.5 * (len(_tot) - 1))],
                'p99_ms': _tot[min(len(_tot) - 1, int(0.99 * len(_tot)))],
                'max_seg': {k: max([r[k] for r in _recs] or [0])
                            for k in ('apply_ms', 'show_ms', 'grab_ms', 'save_ms',
                                      'frame_ms', 'qss_ms', 'adv_ms', 'hero_ms')}}
    w._ck_matrix_probe = _ck_matrix

    _ao_pref_state = {'last': None, 'delay': None}   # 上次已同步给后端的开关/延时，避免每拍重复 POST

    def _apply_adv(vals, silent=False, toast=True):
        poll = max(1, int(vals.get('pollInterval') or 2))
        w._poll_ms = poll * 1000
        w._idle_paced = None
        t1.setInterval(poll * 1000)
        t2.setInterval(12000)   # R22：曲线提交节拍固定 12s（不受轮询间隔影响）
        chart_state['base_ms'] = max(600, poll * 1000)  # R08：空载降频以此为基准
        perf = vals.get('perfMode')
        if perf == '省电':
            t3.setInterval(30000); t7.setInterval(30000)
            t4.setInterval(45000); t8.setInterval(10000)
        elif perf == '高性能':
            t3.setInterval(8000); t7.setInterval(8000)
            t4.setInterval(12000); t8.setInterval(3000)
        else:
            t3.setInterval(15000); t7.setInterval(15000)
            t4.setInterval(20000); t8.setInterval(5000)
        lvl = vals.get('animLevel')
        reduced = bool(vals.get('reduceMotion')) or lvl in ('降级', '关闭') \
            or perf == '省电' or vals.get('theme') == '暗夜护眼'
        try:
            charger_dev.reduced = reduced
            if lvl == '关闭' or perf == '省电':
                charger_dev._anim_timer.stop()
            else:
                charger_dev._anim_timer.setInterval(120 if lvl == '降级' else 50)
                charger_dev._anim_timer.start()
            charger_dev.update()
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:8499')
        # 主题真实生效：以覆盖式 QSS 叠加在应用级样式之上（不破坏既有组件规格）
        apply_theme_visual(vals.get('theme') or '原版深色', vals.get('accent'))
        # 通知/免打扰/开发者调试
        w._notify_prefs = {'enabled': bool(vals.get('notifyEnabled')),
                           'dnd': bool(vals.get('dnd')),
                           'debug': bool(vals.get('debugPanel')),
                           'exp': bool(vals.get('expFeatures'))}
        # 分析与保护的本地阈值（进入分析/断开判定）
        w._adv_thresholds = {'temp': int(vals.get('protectTemp') or 65),
                             'power': int(vals.get('protectPower') or 135),
                             'merge_gap': int(vals.get('mergeGap') or 120),
                             'sensitivity': str(vals.get('deviceSensitivity') or '均衡')}
        # C51：无充电自动关端口——判定与断开都在后端；开关与延时都同步给后端。
        _ao_pref = bool(vals.get('autoOffNoCharge'))
        _ao_delay = max(1, min(1440, int(vals.get('autoOffDelayMin') or ux.AUTO_OFF_MIN)))
        if (_ao_pref != _ao_pref_state['last']
                or _ao_delay != _ao_pref_state['delay']):
            _ao_pref_state['last'] = _ao_pref
            _ao_pref_state['delay'] = _ao_delay
            _dbg_log('C51 auto-off pref=%s delay=%dmin (sync backend)'
                     % (_ao_pref, _ao_delay))
            qwait(lambda: api('POST', '/api/config',
                              {'config': {'auto_off': {'enabled': _ao_pref,
                                                       'window_s': _ao_delay * 60}}},
                              timeout=8),
                  lambda r: None, key='ao_cfg')
        if not silent:
            # 日志级别与保留期写回后端（真实生效）
            _lvl = str(vals.get('logLevel') or 'INFO').lower()
            qwait(lambda: api('POST', '/api/log-level', {'level': _lvl}, timeout=8),
                  lambda r: None, key='loglvl')
            _days = int(vals.get('retentionDays') or 90)
            qwait(lambda: api('POST', '/api/config',
                              {'config': {'server': {'history_retention_days': _days}}},
                              timeout=10), lambda r: None, key='retcfg')
            _sam = int(vals.get('sampleInterval') or 5)
            qwait(lambda: api('POST', '/api/config',
                              {'config': {'server': {'sample_interval': _sam}}}, timeout=10),
                  lambda r: None, key='samcfg')
        # 开机自启统一由「设置 → 服务与数据 → 服务管理」写后端 config.service.auto_start，
        # 由后端任务计划同步；这里不再另写 HKCU\...\Run（同一语义只保留一个入口）。
        _refresh_adv_state()
        if not silent and toast:
            w._toast.show('进阶配置已应用：性能 %s · 动画 %s · 主题 %s'
                          % (perf, lvl, vals.get('theme') or '原版深色'), 'ok')

    def _ck_adv_values():
        """汇总分散在各二级页的扁平设置组（1.5.5 拆分后）→ 一次性应用。

        以本地 Prefs 为底，再叠加各控件当前值：迁出项即时落盘，未保存的
        AdvPanel 编辑也不会被其它页的改动冲掉。
        """
        vals = {k: adv_prefs.get(k) for k in ux.DEFAULTS}
        try:
            vals.update(adv_panel.collect())
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:adv-collect')
        for _sec in _adv_sections:
            try:
                vals.update(_sec.collect())
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:adv-section-collect')
        return vals

    def _ck_section_apply(_key=None, _val=None):
        """扁平设置组改动：AdvSection 已即时写入 Prefs，这里只负责热应用。

        不再弹「已应用」toast（每次改一项都弹会很吵），状态行仍会刷新。
        """
        try:
            _apply_adv(_ck_adv_values(), toast=False)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:adv-section-apply')

    def _export_diag():
        import zipfile
        _dir = os.path.join(os.environ.get('LOCALAPPDATA', '.'), 'CuktechPanel')
        os.makedirs(_dir, exist_ok=True)
        _out = os.path.join(_dir, 'diag_%s.zip' % ck_time.fmt(ck_time.STAMP))
        _stat = api('GET', '/api/status', timeout=10)
        with zipfile.ZipFile(_out, 'w', zipfile.ZIP_DEFLATED) as z:
            for f in os.listdir(_dir):
                if f.endswith('.log'):
                    p = os.path.join(_dir, f)
                    try:
                        z.write(p, f)
                    except Exception as _exc:
                        ck_swallow(_exc, 'launcher:8571')
            z.writestr('status.json', json.dumps(
                _stat[1] if isinstance(_stat, tuple) and len(_stat) > 1 else {}, ensure_ascii=False))
            z.writestr('prefs.json', json.dumps(
                {k: adv_prefs.get(k) for k in ux.DEFAULTS}, ensure_ascii=False))
        return _out

    def _clear_data():
        _dir = os.path.join(os.environ.get('LOCALAPPDATA', '.'), 'CuktechPanel')
        for f in os.listdir(_dir) if os.path.isdir(_dir) else []:
            if f.endswith('.log'):
                try:
                    os.remove(os.path.join(_dir, f))
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:8585')
        qwait(lambda: api('POST', '/api/config',
                          {'config': {'server': {'history_retention_days':
                                                 int(adv_prefs.get('retentionDays') or 90)}}},
                          timeout=10), lambda r: None, key='retcfg2')

    # 启动静默回放（R024：配置不再"只存不用"；不弹 Toast、不重复轰炸后端写接口）
    try:
        _start_vals = {k: adv_prefs.get(k) for k in ux.DEFAULTS}
        _apply_adv(_start_vals, silent=True)
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:8596')
    def _on_chart_range_changed():
        # R018（网页 setTimeRange）：切窗口 → follow + 重取基底
        _i = chart_range.currentIndex()
        chart_state['span'] = chart_minutes[_i] * 60
        chart_state['interval'] = int(CHART_INTERVAL[_i])
        live['follow'] = True
        poll_chart()
    chart_range.currentIndexChanged.connect(_on_chart_range_changed)
    # 首次仍对所有页做一次预热（填充缓存），之后仅当前页持续轮询
    # C34g（#3）：状态页不再等 400 ms/2 s 周期——事件循环一起来就立刻拉一次，
    # 冷启动首帧数据目标 <1 s（uistate 口径，标签直写，不压数字节拍）。
    QTimer.singleShot(0, poll_status)
    for f in (poll_chart, poll_sessions, poll_rec_stats,
              poll_automation, poll_config, poll_device_live):
        QTimer.singleShot(400, f)

    # ===== tray & close =====
    # R024-A4（领导）：托盘图标=自绘充电主题 ico（打包于 _internal/icons，源码态走 assets）
    # R47 / C17：路径统一解析，源码态不再回退通用"显示器"图形
    _ck_icon = QIcon(ck_icon_path())
    tray = QSystemTrayIcon(_ck_icon if not _ck_icon.isNull()
                           else w.style().standardIcon(QStyle.SP_ComputerIcon))
    _tray_base_icon = (_ck_icon if not _ck_icon.isNull()
                       else w.style().standardIcon(QStyle.SP_ComputerIcon))
    # R39 / C08：任务栏 Overlay 角标（ITaskbarList3::SetOverlayIcon）
    class _CK_GUID(ctypes.Structure):
        _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16),
                    ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8)]

    _ov = {'obj': None, 'icons': []}

    def _ck_hicon_from_image(img, size):
        """QImage → 32bpp HICON（CreateDIBSection + CreateIconIndirect，不依赖 PIL）。"""
        _u = ctypes.windll.user32
        _g = ctypes.windll.gdi32
        img = img.convertToFormat(QImage.Format_ARGB32)
        img = img.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        _b = img.constBits()
        buf = bytes(_b) if not hasattr(_b, 'tobytes') else _b.tobytes()

        class _BMIH(ctypes.Structure):
            _fields_ = [('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32),
                        ('biHeight', ctypes.c_int32), ('biPlanes', ctypes.c_uint16),
                        ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                        ('biSizeImage', ctypes.c_uint32), ('biXPelsPerMeter', ctypes.c_int32),
                        ('biYPelsPerMeter', ctypes.c_int32), ('biClrUsed', ctypes.c_uint32),
                        ('biClrImportant', ctypes.c_uint32)]

        bmi = _BMIH()
        bmi.biSize = ctypes.sizeof(_BMIH)
        bmi.biWidth = size
        bmi.biHeight = -size
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        hdc = _u.GetDC(None)
        _ppv = ctypes.c_void_p()
        hbm = _g.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(_ppv), None, 0)
        ctypes.memmove(_ppv, buf, min(len(buf), size * size * 4))
        mask = _g.CreateBitmap(size, size, 1, 1,
                               ctypes.create_string_buffer(size * size // 8 + 8))
        _u.ReleaseDC(None, hdc)

        class _ICONINFO(ctypes.Structure):
            _fields_ = [('fIcon', ctypes.c_int), ('xHotspot', ctypes.c_uint32),
                        ('yHotspot', ctypes.c_uint32), ('hbmMask', ctypes.c_void_p),
                        ('hbmColor', ctypes.c_void_p)]

        ii = _ICONINFO(1, 0, 0, mask, hbm)
        return _u.CreateIconIndirect(ctypes.byref(ii))

    def _ck_overlay_icon(n):
        """n>0 生成红底白字 HICON；n==0 返回 0（表示清除）。"""
        if n <= 0:
            return 0
        sz = 32
        pm = ck_badge_pixmap(n, sz)   # R47：与托盘同源（圆角 8 / 字级 16·12）
        return _ck_hicon_from_image(pm.toImage(), sz)

    def _ck_taskbar():
        if _ov['obj'] is not None:
            return _ov['obj']
        _clsid = _CK_GUID(0x56FDF344, 0xFD6D, 0x11D0,
                          (ctypes.c_ubyte * 8)(0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90))
        _iid = _CK_GUID(0xEA1AFB91, 0x9E28, 0x4B86,
                        (ctypes.c_ubyte * 8)(0x90, 0xE9, 0x9E, 0x9F, 0x8A, 0x5E, 0xEF, 0xAF))
        _p = ctypes.c_void_p()
        _hr = ctypes.windll.ole32.CoCreateInstance(
            ctypes.byref(_clsid), None, 1, ctypes.byref(_iid), ctypes.byref(_p))
        if _hr != 0 or not _p:
            return None
        _vt = ctypes.cast(_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(_vt[3])(_p)   # HrInit
        _ov['obj'] = _p
        return _p

    def _ck_overlay_apply(n):
        """把未读数写到任务栏按钮；失败仅记日志，不影响托盘。"""
        _p = _ck_taskbar()
        if not _p:
            _dbg_log('overlay n=%d ok=False com=none' % n)
            return False
        _hi = _ck_overlay_icon(n)
        _vt = ctypes.cast(_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        _fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_wchar_p)(_vt[18])
        _desc = ('%d 条未读' % n) if n > 0 else ''
        _hr = _fn(_p, ctypes.c_void_p(int(w.winId())), ctypes.c_void_p(_hi), _desc)
        if _hi:
            _ov['icons'].append(_hi)
            while len(_ov['icons']) > 8:
                try:
                    ctypes.windll.user32.DestroyIcon(_ov['icons'].pop(0))
                except Exception:
                    pass
        _dbg_log('overlay n=%d ok=%s hr=%d' % (n, _hr == 0, _hr))
        return _hr == 0

    # R28：未读角标——Qt 无内置 badge，把数字画进 16/32/64px 图标，随未读数重绘
    _unread = {'n': 0}
    _mute = {'on': False}
    _pause = {'on': False}

    def _make_badge_icon(n, size=32):
        # R47 / C17：64px 画布 → 32px（角标占比 30/32），与 Overlay 同源同形态
        base = _tray_base_icon.pixmap(QSize(size, size))
        return QIcon(ck_badge_pixmap(n, size, base))

    def _set_unread(n):
        _unread['n'] = max(0, int(n))
        # C34f：与模块级计数同步，静音收口处（_ck_toast_send）也能刷角标
        _CK_UNREAD['n'] = _unread['n']
        _CK_UNREAD['fn'] = _set_unread
        try:
            tray.setIcon(_make_badge_icon(_unread['n']))
            tray.setToolTip('%s%s' % (PANEL_TITLE,
                                      ('（%d 条未读）' % _unread['n']) if _unread['n'] else ''))
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:traybadge')
        # R39 / C08：任务栏 Overlay 与托盘同源同步
        try:
            _ck_overlay_apply(_unread['n'])
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:overlay')
            _dbg_log('overlay n=%d ok=exc' % _unread['n'])
        w._ck_unread = _unread['n']
    w._ck_unread_set = _set_unread

    _menu_qss = ("QMenu{background:rgba(18,26,38,0.96);"
                 "border:1px solid rgba(120,190,255,0.28);border-radius:12px;padding:8px;}"
                 "QMenu::item{padding:8px 16px;border-radius:8px;color:" + TEXT + ";}"
                 "QMenu::item:selected{background:rgba(90,170,255,0.22);}"
                 "QMenu::separator{height:1px;background:rgba(255,255,255,0.10);"
                 "margin:4px 8px;}")
    tm = QMenu()
    tm.setStyleSheet(_menu_qss)
    # C16：QAction 必须挂到菜单上（缺 parent 时 Python 侧引用释放后动作会被销毁，
    # 托盘右键菜单变空——build32 实测 actions 2→0）。
    show_act = QAction('显示窗口', tm)
    exit_act = QAction('退出（停止服务）', tm)
    # C34（改名）：菜单首项显示应用名，避免「托盘菜单里看不到新名」。
    _name_act = QAction(PANEL_TITLE, tm)
    _name_act.setEnabled(False)
    tm.addAction(_name_act)
    tm.addSeparator()
    tm.addAction(show_act)
    tm.addAction(exit_act)
    tray.setContextMenu(tm)
    tray.setToolTip(PANEL_TITLE)

    def show_window():
        w.showNormal()
        w.raise_()
        w.activateWindow()
        try:
            _hw = int(w.winId())
            _u = ctypes.windll.user32
            _u.ShowWindow(_hw, 9)
            _u.SetForegroundWindow(_hw)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:showwin-foreground')
        _set_unread(0)

    # C33（缺陷 7）：左键 = 打开主界面，右键 = 出菜单。原「左键快捷面板」取消，
    # 其中的「暂停采集 / 静音通知」并入右键菜单——否则静音入口无处可点。
    qa_pause = QAction('暂停采集', tm)
    qa_pause.setCheckable(True)
    qa_mute = QAction('静音通知', tm)
    qa_mute.setCheckable(True)
    tm.insertAction(exit_act, qa_pause)
    tm.insertAction(exit_act, qa_mute)
    tm.insertSeparator(exit_act)

    def _toggle_pause(checked):
        _pause['on'] = bool(checked)
        try:
            if checked:
                for _t in _all_timers:
                    _t.stop()
            else:
                apply_timers(stack.currentIndex())
                poll_mqtt_status()  # 恢复采集立即回读巴法云状态
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:qpause')

    def _toggle_mute(checked):
        _mute['on'] = bool(checked)
        _CK_MUTE['on'] = _mute['on']
        w._ck_muted = _mute['on']
        _ck_toast_log('mute on=%s unread=%d' % (_mute['on'], _CK_UNREAD['n']))
        # C34f：解除静音时把静音期累计的未读合成一条摘要补发（只补一次）
        if not _mute['on'] and _CK_UNREAD['n'] > 0:
            _n_before = _CK_UNREAD['n']
            _p = ux.ck_notify_plan(False, _n_before, 'digest')
            try:
                _set_unread(0)
                _ok = _ck_toast_send(PANEL_TITLE, _p['body'], 'digest')
                if not _ok:
                    tray.showMessage(PANEL_TITLE, _p['body'],
                                     QSystemTrayIcon.Information, 5000)
                _ck_toast_log('mute digest replay n=%d winrt=%s'
                              % (_n_before, _ok))
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:mute-digest')

    qa_pause.toggled.connect(_toggle_pause)
    qa_mute.toggled.connect(_toggle_mute)

    def _sync_menu_checks():
        # C34（缺陷 5）：菜单每次弹出前把勾选态对齐真实状态。
        # C33 的 B4 实操曾把「暂停采集」留在勾选态导致画面静止；这里以代码兜底，
        # 菜单不再可能显示与 _pause/_mute 不一致的勾。
        for _a, _s in ((qa_pause, _pause['on']), (qa_mute, _mute['on'])):
            _a.blockSignals(True)
            _a.setChecked(bool(_s))
            _a.blockSignals(False)

    tm.aboutToShow.connect(_sync_menu_checks)

    def _on_tray_activated(reason):
        # 左键（Trigger）与双击都开主界面；右键菜单由 setContextMenu(tm) 承担。
        # C34g：记录每次激活及原因——用于区分「点击没打到我们的图标（幽灵/重复项）」
        # 与「打到了但 Qt 没弹菜单」这两种完全不同的根因。
        try:
            _CK_TRAY['n'] += 1
            _CK_TRAY['last'] = (_CK_TRAY['last'] + [str(reason)])[-6:]
            _dbg_log('tray activated reason=%s n=%d' % (reason, _CK_TRAY['n']))
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:tray-act-log')
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            show_window()

    tray.activated.connect(_on_tray_activated)

    def _toast_activate_page(_pg):
        try:
            show_window()
            if _pg is not None:
                _idx = max(0, min(stack.count() - 1, int(_pg)))
                rail.set_selected(_idx)
                stack.setCurrentIndex(_idx)
            _ck_toast_log('toast activate_page pg=%s' % _pg)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:toast-act-page')

    _ck_toast_activate['cb'] = _toast_activate_page
    _ck_toast_bridge()  # 主线程预建（保证信号的目标线程是 UI 线程）

    # C34g（缺陷 9）：托盘图标必须显式 NIM_DELETE，否则进程一退就在溢出区留「幽灵图标」。
    # 幽灵占位与新实例同名同图标，右键点过去没有任何进程响应 → 表现就是「右键不出菜单」。
    # Qt 在 QSystemTrayIcon 析构时才发 NIM_DELETE；quit() 走 sys.exit 时析构顺序不保证，
    # 这里在 aboutToQuit 里显式 hide()，把删除动作钉死在退出流程里。
    def _ck_tray_dispose():
        try:
            tray.setContextMenu(None)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:tray-menu-detach')
        try:
            tray.hide()          # → Shell_NotifyIcon(NIM_DELETE)
            _dbg_log('tray NIM_DELETE on quit ok')
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:tray-hide')
    _qapp = QApplication.instance()
    if _qapp is not None:
        _qapp.aboutToQuit.connect(_ck_tray_dispose)

    tray.show()

    # 统一 QSettings 组织名（CUK-R1）：几何与关窗记忆同处 DoubaoWork；迁移旧键
    settings = _geo
    _legacy_settings = QSettings('DreamArchitect', 'CuktechPanel')
    for _k in ('closeMode',):
        if not settings.contains(_k) and _legacy_settings.contains(_k):
            settings.setValue(_k, _legacy_settings.value(_k))

    def confirm_exit():
        """二次确认后级联退出（停托管子进程/服务，再退出）。返回 True 表示已退出。"""
        box = QMessageBox(w)
        box.setWindowTitle('确认退出')
        # R031：去掉大号系统警告图标（与深色弹窗体系不一致），只用文字 + 中文双按钮
        box.setIcon(QMessageBox.NoIcon)
        box.setText('退出将停止后台充电服务，确定要退出吗？')
        # R030：跟随界面语言，替换系统默认的英文 Yes/No
        box.setStandardButtons(QMessageBox.NoButton)
        _btn_yes = box.addButton('退出', QMessageBox.AcceptRole)
        _btn_no = box.addButton('取消', QMessageBox.RejectRole)
        box.setDefaultButton(_btn_no)
        # R031：QSS 的 max-width 对 QMessageBox 按钮不生效，改为固定宽度
        _btn_yes.setFixedWidth(96)
        _btn_no.setFixedWidth(96)
        box.exec()
        if box.clickedButton() is not _btn_yes:
            return False
        # 级联退出：先停托管子进程（同生共死），再停服务
        proc = getattr(w, '_headless_proc', None)
        try:
            if proc is not None:
                proc.kill()
                proc.waitForFinished(3000)
            else:
                api('POST', '/api/service', {'action': 'stop'}, timeout=CK_T_CMD)
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:8658')
        _CK_EXIT_HOW['v'] = 'user_exit'   # R39：菜单/托盘/关闭确认退出，看门狗据此不拉回
        try:
            QApplication.quit()
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:8662')
        return True

    def on_close(event):
        # 保存几何与当前标签（桌面适配）
        _geo.setValue('geometry', w.saveGeometry())
        _geo.setValue('lastTab', stack.currentIndex())
        m = settings.value('closeMode', 'ask')
        if m == 'tray':
            w.hide()
            event.ignore()
            return
        if m == 'exit':
            event.accept() if confirm_exit() else event.ignore()
            return
        dlg = QDialog(w)
        dlg.setWindowTitle('关闭 %s' % PANEL_TITLE)
        dv = QVBoxLayout(dlg)
        dv.setContentsMargins(16, 16, 16, 12)
        dv.setSpacing(8)
        dv.addWidget(QLabel('关闭窗口时希望：'))
        remember = QCheckBox('记住我的选择')
        b_tray = QPushButton('最小化到托盘（服务继续运行）')
        b_exit = QPushButton('退出应用（停止充电服务）')
        b_tray.setMinimumHeight(36)
        b_exit.setMinimumHeight(36)
        b_exit.setObjectName('danger')
        dv.addWidget(b_tray)
        dv.addWidget(b_exit)
        dv.addWidget(remember)

        def pick(val, store):
            if remember.isChecked():
                settings.setValue('closeMode', store)
            dlg.done(val)

        b_tray.clicked.connect(lambda: pick(1, 'tray'))
        b_exit.clicked.connect(lambda: pick(2, 'exit'))
        dlg.exec()
        r = dlg.result()
        if r == 1:
            w.hide()
            event.ignore()
        elif r == 2:
            event.accept() if confirm_exit() else event.ignore()
        else:  # X / ESC：取消，不退出
            event.ignore()

    w.closeEvent = on_close
    show_act.triggered.connect(show_window)
    exit_act.triggered.connect(confirm_exit)
    # 恢复上次标签页
    try:
        lt = int(_geo.value('lastTab', 0))
        if 0 <= lt < stack.count():
            on_select(lt)
    except Exception as _exc:
        ck_swallow(_exc, 'launcher:8716')

    def _refresh_current():
        """R007（D-08）F5：按当前页触发对应轮询。"""
        fns = {0: (poll_status, poll_chart),
               1: (poll_status,),
               2: (poll_sessions, poll_rec_stats),
               3: (poll_automation,),
               4: (poll_config, poll_device_live)}
        for f in fns.get(stack.currentIndex(), ()):
            try:
                f()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:8729')
    w._refresh_current = _refresh_current

    def _ck_build_charts():
        """C34h：首帧之后再付 pyqtgraph 导入 + 主图/迷你图构建，骨架占位原地替换。

        目标：窗口先可见可交互（标题栏/按钮就绪），图表位显示「图表加载中…」骨架，
        不出现空白块；导入与建图（215–450 ms + ~100 ms）挪出可交互关键路径。
        """
        nonlocal plot, power_glow, power_glow2, power_curve, port_curves
        nonlocal vline, hline, chart_readout, _icf
        if plot is not None:
            return
        _t = time.perf_counter()
        try:
            pg = _ck_pg()
            plot = time_plot()
            plot.setMouseEnabled(x=True, y=False)
            plot.setYRange(0, RATED_POWER, padding=0)
            for axname in ('bottom', 'left'):
                _ax = plot.getAxis(axname)
                _ax.setPen(pg.mkPen(QColor(3, 169, 244, 60)))
                _ax.setTextPen(QColor(159, 180, 200))
            plot.showGrid(x=False, y=True, alpha=0.12)
            plot.setDownsampling(auto=True, mode='peak')
            plot.setClipToView(True)
            plot.disableAutoRange()
            try:
                plot.hideButtons()
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:chart-hidebtns')
            power_glow = plot.plot(pen=pg.mkPen(QColor(255, 255, 255, 70), width=8))
            power_glow2 = plot.plot(pen=pg.mkPen(QColor(255, 255, 255, 50), width=4))
            port_curves.clear()
            for pk, (pc, pn) in PORT_NAMES.items():
                port_curves[pk] = plot.plot(
                    pen=pg.mkPen(QColor(PORT_COLORS[pk]), width=2.0), name=pn)
            power_curve = plot.plot(
                pen=pg.mkPen(QColor(255, 255, 255, 225), width=2.2, style=Qt.DashLine),
                name='总功率 W')
            power_curve.setFillLevel(0)
            _fill_grad = QLinearGradient(0, 0, 0, 1)
            _fill_grad.setCoordinateMode(QGradient.ObjectBoundingMode)
            _fill_grad.setColorAt(0, QColor(255, 255, 255, 45))
            _fill_grad.setColorAt(1, QColor(255, 255, 255, 0))
            power_curve.setFillBrush(QBrush(_fill_grad))
            vline = pg.InfiniteLine(angle=90, movable=False,
                                    pen=pg.mkPen(QColor(3, 169, 244, 120), style=Qt.DashLine))
            hline = pg.InfiniteLine(angle=0, movable=False,
                                    pen=pg.mkPen(QColor(3, 169, 244, 120), style=Qt.DashLine))
            plot.addItem(vline, ignoreBounds=True)
            plot.addItem(hline, ignoreBounds=True)
            chart_readout = pg.TextItem(color=INK, fill=QColor(26, 36, 54, 225))
            chart_readout.setZValue(20)
            plot.addItem(chart_readout, ignoreBounds=True)
            idle_overlay.setParent(plot)
            _icf = _IdleCenter(plot)
            plot.viewport().installEventFilter(_icf)
            w._ef_list.append((plot.viewport(), _icf))
            _center_idle()
            plot.scene().sigMouseMoved.connect(on_chart_moved)
            try:
                plot.getViewBox().sigRangeChangedManually.connect(_on_manual)
            except Exception as _exc:
                ck_swallow(_exc, 'launcher:chart-manual')
            plot.setMinimumHeight(70 if w.height() < 780 else 120)
            plot_panel = _OpaquePanel()
            _plv2 = QVBoxLayout(plot_panel)
            _plv2.setContentsMargins(0, 0, 0, 0)
            _plv2.addWidget(plot)
            _idx = _phv.indexOf(chart_skeleton)
            _phv.removeWidget(chart_skeleton)
            chart_skeleton.deleteLater()
            _phv.insertWidget(_idx if _idx >= 0 else 0, plot_panel, 1)
            # 1.5.2：迷你图延迟建图——把骨架 QLabel 原地换成固定高度 PlotWidget，
            # 容器与曲线句柄留在 port_cards[8]/[9]，绘制只由节拍器 + 状态渲染触达。
            for _key, _box in _mini_deferred:
                try:
                    _mi, _mc = mini_plot(PORT_COLORS[_key])
                    _lay = _box.layout()
                    _lay.addWidget(_mi, 1)
                    # 1.5.5：空态文案画在图区正中（不占布局行，不与任何控件相交），
                    # 有数据时隐藏；坐标取空态默认范围 0..30 / 0..20 的中心。
                    _txt = _ck_pg().TextItem('无数据',
                                             color=QColor(148, 163, 184, 140),
                                             anchor=(0.5, 0.5))
                    _txt.setPos(15, 10)
                    _mi.addItem(_txt)
                    _box._plot = _mi
                    _box._txt = _txt
                    _tup = port_cards.get(_key)
                    if _tup:
                        port_cards[_key] = _tup[:9] + (_mc,) + _tup[10:]
                except Exception as _exc:
                    ck_swallow(_exc, 'launcher:mini-deferred')
            if idle_state['on']:
                idle_timer.start()
            _render_live()
            ck_trace('charts_built', ms=round((time.perf_counter() - _t) * 1000.0, 1))
            _ck_boot_trace('charts_built')
        except Exception as _exc:
            ck_swallow(_exc, 'launcher:build-charts')

    def _ck_smoke():
        """构建冒烟：build_ui 可构造 + 逐页/逐 AI tab 切换无异常。

        只读：不写配置、不碰后端。Qt 槽里未捕获的异常不会让进程退出，
        必须经 excepthook 收集，否则门禁会漏掉 _dbg_log 这类 NameError。
        """
        from PySide6.QtCore import QEventLoop as _QEL
        _errs = []
        _old_hook = sys.excepthook

        def _hook(_t, _v, _tb):
            _errs.append('%s: %s' % (getattr(_t, '__name__', _t), _v))

        sys.excepthook = _hook
        try:
            _app = QApplication.instance()
            for _i in range(stack.count()):
                stack.setCurrentIndex(_i)
                _app.processEvents()
            on_select(stack.indexOf(ai_page))
            for _j in range(_ai_tabs.count()):
                _ai_tabs.setCurrentIndex(_j)
                _app.processEvents()
            # 记录页切换后 600 ms 才重排（_refresh_records → poll_sessions），
            # 不跑这段事件循环，门禁等于没盖到出事那条路径。
            _loop = _QEL()
            QTimer.singleShot(900, _loop.quit)
            _loop.exec()
            _app.processEvents()
        except Exception as _exc:
            _errs.append('smoke: %r' % (_exc,))
        finally:
            sys.excepthook = _old_hook
        return {'pages': stack.count(), 'ai_tabs': _ai_tabs.count(),
                'errors': _errs}

    def _ck_show_ai():
        """自检用：把主栈切到「小源」页（AI 区逐 tab 抓图/并排对照的前置）。"""
        on_select(stack.indexOf(ai_page))
        return stack.currentIndex()

    w._ck_show_ai = _ck_show_ai
    w._ck_ai_send = _ai_send
    w._ck_ai_msgs = lambda: _AI['msgs']
    w._ck_ai_busy = lambda: _AI['busy']
    w._ck_smoke = _ck_smoke

    w._ck_build_charts = _ck_build_charts
    _ck_boot_trace('bui_done')
    return w


if __name__ == '__main__':
    main()
