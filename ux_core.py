"""星源智控 UX 内核。

包含：设计令牌、进阶配置中心、问号帮助数据表、隐私说明与用户协议、
使用说明、云端 AI（DeepSeek / 本地 OpenAI 兼容端点）与本地规则引擎降级、
充电会话智能分析（设备指纹 / 断开原因 / 电压体检）。

约束：只用 PySide6 + 标准库；不得在日志/源码中出现用户 API Key 明文。
"""
import base64
import copy
import ctypes
import ctypes.wintypes
import hashlib
import json
import math
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDoubleValidator, QFont, QIntValidator
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPushButton, QScrollArea, QScrollBar,
                               QSizePolicy, QSpinBox, QTabWidget, QTextBrowser,
                               QVBoxLayout, QWidget)

import ai_payload  # noqa: E402  AI 出站载荷唯一构造点（白名单）
from ui_kit import TOKENS, doc_html  # noqa: E402  UI 单源，见 ui_kit.py

# 显式公开面（tools/check_imports.py 对 ui 层要求 __all__）。
# 跨模块可用的名字必须在此列出；新增公开函数请同步补上。
__all__ = [
    'APP_VERSION', 'CONSENT_VERSION', 'DOCS_UPDATED',
    'THEME_TOKENS', 'ACTIVE_THEME', 'set_theme', 'DIALOG_QSS', 'HELP',
    'DEFAULTS', 'SPEC', 'EFFECT', 'Prefs', 'SecretStore',
    'DEV_EPS', 'DEV_SEED', 'DEV_MOVE', 'dev_vec', 'dev_sim', 'session_key',
    'device_model_load', 'device_save', 'device_assign', 'device_build',
    'analyze_sessions', 'voltage_metrics', 'rule_report',
    'ai_config', 'ai_run', 'ai_stream', 'ai_tool',
    'PRIVACY', 'EULA', 'GUIDE', 'LEGAL_VIEW_ITEMS',
    'DOC_BROWSER_QSS', 'LEGAL_NAV_QSS', 'legal_view_text', 'LegalView',
    'proto_names', 'help_badge', 'info_badge', 'labeled_row',
    'IntField', 'DoubleField', 'AdvPanel', 'AdvSection', 'AdvancedConfigDialog',
    'ConsentDialog', 'ensure_consent', 'ck_notify_plan',
]

def _ck_read_version():
    """版本单源：读 VERSION 文件（冻结构建落在 _MEIPASS/_internal，源码直跑读同目录）。

    读不到就返回 0.0.0，让"版本不对"在页脚和构建断言里立刻暴露，而不是回退成旧版本号。
    """
    _base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(_base, 'VERSION'), 'r', encoding='utf-8') as _f:
            return _f.read().strip() or '0.0.0'
    except OSError:
        return '0.0.0'


APP_VERSION = _ck_read_version()
CONSENT_VERSION = 'v2-20261112'
DOCS_UPDATED = '2026-11-16'   # 使用说明/功能说明/隐私说明/用户协议的最后校对日期


def _auto_off_minutes():
    """无充电自动关端口窗口（分钟）：唯一来源 backend/auto_off.DEFAULT_WINDOW_S。

    不写死数字：按源码路径动态加载常量（打包后可能在 _internal/ 下），
    所有候选路径都读不到时才回落到与常量相同的 10。
    """
    import importlib.util
    _here = os.path.dirname(os.path.abspath(__file__))
    _base = getattr(sys, '_MEIPASS', _here)
    _cands = []
    for _p in (os.path.join(_base, '_internal', 'backend', 'auto_off.py'),
               os.path.join(_base, 'backend', 'auto_off.py'),
               os.path.join(_here, 'backend', 'auto_off.py')):
        if _p not in _cands:
            _cands.append(_p)
    for _p in _cands:
        if not os.path.isfile(_p):
            continue
        try:
            _spec = importlib.util.spec_from_file_location('_ux_auto_off', _p)
            if _spec is None or _spec.loader is None:
                continue
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            return max(1, int(round(float(_mod.DEFAULT_WINDOW_S) / 60.0)))
        except Exception:
            continue
    return 10


AUTO_OFF_MIN = _auto_off_minutes()

# ---------------------------------------------------------------- 设计令牌
# TOKENS / doc_html 已收敛到 ui_kit.py（本产品唯一一份 UI 常量），
# 此处只做引用；set_theme() 更新的是同一个 dict 对象，引用方随之生效。
# 零差令牌（surface_soft / border_soft / text_muted / code_bg / font_card /
# font_h）与四处二级页现有字面量逐字一致，替换后像素零变化。

# 主题令牌：三套主题只覆盖下列键，其余令牌（字号/间距/圆角）不随主题变化。
# 这是 ux_core 组件的唯一配色来源：主窗口的窗口级主题 QSS 也由本表生成，
# 因此「切主题后本页仍固定深色」不会再出现。默认值与原版深色（LIGHT_QSS 的
# INK/SUB/root 底色）逐值一致。
THEME_TOKENS = {
    '原版深色': {
        'bg': '#1c1c1c',
        'surface': 'rgba(255,255,255,0.045)',
        'surface_hi': 'rgba(255,255,255,0.085)',
        'border': 'rgba(255,255,255,0.13)',
        'text': '#e8f4ff', 'text_sub': '#9fb4d0', 'heading': '#f3f7fc',
        'accent': '#03a9f4', 'accent3': '#2fb6ff',
    },
    '暗夜护眼': {
        'bg': '#141414',
        'surface': 'rgba(223,231,216,0.05)',
        'surface_hi': 'rgba(223,231,216,0.10)',
        'border': 'rgba(223,231,216,0.18)',
        'text': '#dfe7d8', 'text_sub': '#9fb09a', 'heading': '#eef4e8',
        'accent': '#03a9f4', 'accent3': '#2fb6ff',
    },
    '高对比': {
        'bg': '#0c0c0c',
        'surface': 'rgba(255,255,255,0.06)',
        'surface_hi': 'rgba(255,255,255,0.13)',
        'border': 'rgba(255,255,255,0.34)',
        'text': '#ffffff', 'text_sub': '#d5e2f0', 'heading': '#ffffff',
        'accent': '#38e2ff', 'accent3': '#2fb6ff',
    },
}
ACTIVE_THEME = '原版深色'


def set_theme(name):
    """切换设计令牌；未知主题回落原版深色，返回实际生效的主题名。"""
    global ACTIVE_THEME
    ACTIVE_THEME = name if name in THEME_TOKENS else '原版深色'
    TOKENS.update(THEME_TOKENS[ACTIVE_THEME])
    return ACTIVE_THEME


DIALOG_QSS = """
QDialog { background: %(bg)s; color: %(text)s; }
QLabel { color: %(text)s; font-size: %(font_body)s; }
QLabel#hint { color: %(text_sub)s; }
QTabWidget::pane { border: 1px solid %(border)s; border-radius: 12px; }
QTabBar::tab { background: rgba(255,255,255,0.05); color: %(text_sub)s;
  padding: 6px 12px; border-radius: 8px; margin-right: 4px; font-size: 12px; }
QTabBar::tab:selected { background: rgba(3,169,244,0.22); color: #fff; }
QPushButton { background: rgba(255,255,255,0.06); border: 1px solid %(border)s;
  border-radius: 8px; padding: 6px 14px; min-height: 28px; color: %(text)s; }
QPushButton:hover { background: rgba(3,169,244,0.18); }
QPushButton:disabled { color: #64748b; }
QComboBox, QLineEdit, QSpinBox { background: rgba(255,255,255,0.07);
  border: 1px solid %(border)s; border-radius: 8px; padding: 5px 8px;
  min-height: 26px; color: %(text)s; }
QComboBox:focus, QLineEdit:focus, QSpinBox:focus { border-color: %(accent)s; }
QCheckBox { spacing: 8px; color: %(text)s; }
QScrollArea { background: transparent; border: none; }
QTextBrowser { background: rgba(255,255,255,0.04); border: 1px solid %(border)s;
  border-radius: 10px; padding: 10px; color: %(text)s; }
"""


def _qss():
    return DIALOG_QSS % TOKENS


# ---------------------------------------------------------------- 帮助数据表
HELP = {
    # 通用
    'theme': ('主题配色', '切换应用整体配色。原版深色对齐网页端；暗夜护眼降低亮度；高对比提升文字与背景对比度（WCAG AA）。',
              '长时间夜间使用建议暗夜护眼；视力敏感或强环境光建议高对比。'),
    'animLevel': ('动画与特效强度', '标准=完整动效；降级=去掉辉光呼吸与渐变流光；关闭=只保留必要过渡。',
                  '低配机器或晕动敏感用户建议降级/关闭；关闭后曲线与数据刷新不受影响。'),
    'reduceMotion': ('减少动效', '开启后页面切换与卡片入场缩短到 120ms，并关闭循环动画。',
                     '符合系统"减少动态效果"偏好；不影响数据实时性。'),
    'perfMode': ('性能模式', '省电=轮询/动画降频，适合长期挂机；均衡=默认；高性能=更快刷新与更顺动画。',
                 '省电模式会牺牲曲线细腻度换取 CPU/内存占用下降。'),
    'accent': ('强调色', '品牌默认使用青→蓝→紫；青蓝更冷静；紫青更活泼。仅影响强调元素，不影响告警色。',
               '自定义配色会与图表端口色共用同一套令牌。'),
    # 数据
    'sampleInterval': ('采样间隔（秒）', '后端写入历史库的采样周期。越小越细，占用越大。',
                       '默认 5s；省电可 10s；最小 1s 会增加 SQLite 写入与内存。'),
    'chartPoints': ('图表点数', '单条曲线最多绘制的点数，超出自动降采样（LTTB）。',
                    '点数越高越细腻但重绘越慢；60–120 为推荐区间。'),
    'smooth': ('曲线平滑', '平滑=贝塞尔插值让曲线顺滑；原始=直接连线，保留尖峰。',
               '排查异常波动时用原始；日常观感用平滑。'),
    'pollInterval': ('端口轮询频率（秒）', 'UI 向本地服务拉取状态/曲线的间隔。',
                     '1s 最灵敏但费 CPU；2s 均衡；5s 省电。断网时不会加速重试。'),
    'autoOffNoCharge': ('无充电 %d 分钟自动关闭端口' % AUTO_OFF_MIN,
                        '某端口处于开启状态、连续 %d 分钟输出电流低于 0.05 A 时自动关闭该端口，'
                        '并弹提示、写日志。刚开启的前 60 秒不计时；中途检测到充电立即重新计时。'
                        '仅 C1/C2/C3 参与计时断电；A 口不受自动断电约束（豁免名单默认 ["a"]，'
                        '可在配置 auto_off.exempt 中调整，改动无需重启）。'
                        % AUTO_OFF_MIN,
                        '适合「插着不拔、充满不断电」的场景省电与护电池；'
                        '小电流涓流设备（耳机/手环）建议关闭，避免被误判为空载。'),
    'retentionDays': ('数据保留期限（天）', '超过期限的充电记录与曲线点会被清理；0 表示永久保留。',
                      '清理不可恢复，请先导出。'),
    'autoCleanup': ('自动清理', '每次启动时按保留期限清理历史库。',
                    '关闭后仅手动清理；建议开启并配合导出。'),
    # 通知/日志
    'notifyEnabled': ('系统通知', '充电开始/结束、功率越界时弹托盘通知。',
                      '免打扰开启时通知静默记录，不弹窗。'),
    'dnd': ('免打扰', '屏蔽所有弹窗与声音，仅写入事件日志。',
            '会议/夜间建议开启；不影响安全保护动作。'),
    'logLevel': ('日志级别', 'INFO=常规；DEBUG=含通讯细节（体积增长快）；WARNING=只记异常。',
                 '排查问题后请调回 INFO，避免日志膨胀。'),
    'autoStart': ('开机自启', '登录 Windows 后自动启动本应用（写入当前用户启动项）。',
                  '服务器长期运行建议开启；笔记本建议关闭以省电。'),
    'minimizeTray': ('关闭时最小化到托盘', '关闭窗口时隐藏到托盘，后台服务继续运行。',
                     '选择退出应用会停止本地服务。'),
    'proxy': ('网络代理', '仅用于 AI 请求，格式 http://127.0.0.1:7890；留空直连。',
              '代理不可达时 AI 自动降级为本地规则引擎，不影响主功能。'),
    'expFeatures': ('实验性功能', '开启尚未稳定的新能力（可能变化或回退）。',
                    '遇到异常请关闭并反馈；不影响既有数据。'),
    'debugPanel': ('开发者调试入口', '显示请求耗时、缓存命中、线程与定时器统计。',
                   '仅排障使用，会增加少量日志与 CPU。'),
    # AI
    'aiEnabled': ('启用 AI 分析', '允许把聚合后的充电统计发给所选 AI 服务商生成解读。',
                  '默认关闭；开启前需阅读隐私说明并同意。原始曲线默认不上传。'),
    'aiProvider': ('AI 通道', '云端=DeepSeek（OpenAI 兼容）；本地=LM Studio/Ollama 等本地端点。',
                   '本地通道默认关闭且不内置模型；不可达时自动回退规则引擎。'),
    'aiModel': ('模型名', 'DeepSeek 默认 deepseek-chat；本地端点填其模型标识。',
                '型号与计费以服务商官方文档为准，本应用不写死价格。'),
    'aiBaseUrl': ('Base URL', 'OpenAI 兼容根地址，例如 https://api.deepseek.com 或 http://127.0.0.1:1234/v1。',
                  '自定义服务商只需兼容 /chat/completions。'),
    'aiApiKey': ('API Key', '仅保存在本机受保护存储（Windows DPAPI 加密），不写日志、不进源码。',
                 '泄露后请立即到服务商控制台吊销并重新生成。'),
    'aiRedact': ('数据脱敏', '只发送聚合特征（时长/总量/峰值/异常计数），不发送原始高频曲线与设备标识。',
                 '关闭会显著增加上传数据量与隐私风险，不建议。'),
    'aiAutoManage': ('AI 自动充电管理', '按策略自动调整充电上限/提醒；每次动作需确认，可回滚并留操作日志。',
                     '默认关闭；保护类动作（过温/过压）永远由本地规则优先。'),
    'aiDailyReport': ('每日自动日报', '每天固定时间生成一次日报（最多一天一次，失败不重试轰炸）。',
                      '未配置 Key 或不可达时不发起请求。'),
    # 识别/分析
    'deviceSensitivity': ('设备识别灵敏度', '高=更容易把相似会话归为同一设备；低=更保守。',
                          '误合并多时调低；漏合并多时调高。'),
    'devBoxes': ('设备分组箱数', '已取消固定箱数：分箱按实际存在数量动态增长，新建即多一个。',
                 '本键已从配置项移除，仅历史配置文件可能残留，读取时忽略。'),
    'devManage': ('管理分组', '给每箱命名、重新分组并预览归属；名字在重分组时按最近中心迁移。',
                  '重新分组后会标出「待确认」的箱，避免名字静默搬错。'),
    'devShapeThr': ('形态匹配阈值', '未归箱会话到箱中心的距离超过「箱内距离×该系数」即不硬归。',
                    '单位 0.1，默认 16 即 1.6；调大=更容易归入（可能误并）。'),
    'devCenterMode': ('中心更新策略', '增量=只按变化的会话更新中心；全量=每次用全部会话重算。',
                      '两种结果一致，全量更慢但便于核对。'),
    'devCandMin': ('候选新设备最小条数', '未归箱会话聚出的候选簇少于该条数并进「未分类」。',
                   '默认 8；样本少时调低会更容易冒出候选。'),
    'devMaxBoxes': ('设备分组上限', '固定组 + 自建组的总数上限；到达上限后不再新建分组，'
                                  '重建分组也不会超额。',
                    '范围 3–30，默认 12；改完立即保存生效，下一次「重新分组」按新值执行。'),
    'devHourGuard': ('人工作业区间约束', '启用后，「管理分组」里填的维度区间作为硬约束优先于自动中心。',
                     '关闭时全部回退自动中心判定。'),
    'devFullTrainN': ('全量重训阈值（条）', '单次迁移/移出影响的样本数超过该值走全量重训，否则走增量。',
                      '建组/合并/删组属于结构变更，始终全量；默认 30。'),
    'mergeGap': ('抖动合并阈值（秒）', '两个会话间隔小于该值视为同一次充电（PD 重协商抖动）。',
                 '默认 120s；频繁握手可调大。'),
    'protectTemp': ('过温保护阈值（°C）', '超过该温度判定为智能保护断开并提示。',
                    '阈值过低会误报，过高有安全风险；默认 65°C。'),
    'protectPower': ('过功率保护阈值（W）', '超过该功率判定为智能保护断开。',
                     '需与充电器额定功率匹配（默认 135W）。'),
}

DEFAULTS = {
    'theme': '原版深色', 'animLevel': '标准', 'reduceMotion': False,
    'perfMode': '均衡', 'accent': '品牌默认',
    'sampleInterval': 5, 'chartPoints': 120, 'smooth': '平滑', 'pollInterval': 2,
    'retentionDays': 90, 'autoCleanup': True,
    'notifyEnabled': True, 'dnd': False, 'logLevel': 'INFO',
    'autoStart': False, 'minimizeTray': True, 'proxy': '', 'expFeatures': False,
    'debugPanel': False,
    'aiEnabled': False, 'aiProvider': 'DeepSeek 云端', 'aiModel': 'deepseek-chat',
    'aiBaseUrl': 'https://api.deepseek.com', 'aiApiKey': '', 'aiRedact': True,
    'aiAutoManage': False, 'aiDailyReport': False,
    'deviceSensitivity': '均衡', 'devModel': '',
    'devShapeThr': 16, 'devCenterMode': '增量', 'devCandMin': 8, 'devHourGuard': True,
    'devFullTrainN': 30, 'devMaxBoxes': 12,
    'mergeGap': 120, 'protectTemp': 65, 'protectPower': 135,
    # 1.5.5：无充电自动关闭端口新装默认开启（与 backend/config.yaml、config.py 缺省一致）；
    # 文案分钟数取自 backend/auto_off.py 的窗口常量
    'autoOffNoCharge': True,
    # 1.5.8：无充电多久后关端口可调（分钟）；默认沿用后端窗口常量（10 分钟）
    'autoOffDelayMin': AUTO_OFF_MIN,
}

# 分组总数上限的可调范围（2.1.0）：设置项、输入校验、分组页徽标共用同一份
DEV_CAP_MIN = 3
DEV_CAP_MAX = 30

SPEC = [
    ('充电与保护', [
        ('protectTemp', '过温保护阈值（°C）', 'int', (40, 95)),
        ('protectPower', '过功率保护阈值（W）', 'int', (5, 250)),
        ('autoOffNoCharge', '无充电自动关闭端口', 'bool', None),
        ('autoOffDelayMin', '无充电关端口延时（分钟，1–1440，默认 %d）' % AUTO_OFF_MIN,
         'int', (1, 1440)),
    ]),
    ('设备识别', [
        ('deviceSensitivity', '设备识别灵敏度', 'choice', ['保守', '均衡', '激进']),
        ('mergeGap', '抖动合并阈值（秒）', 'int', (5, 600)),
        ('devShapeThr', '形态匹配阈值', 'int', (5, 30)),
        ('devCenterMode', '中心更新策略', 'choice', ['增量', '全量']),
        ('devCandMin', '候选新设备最小条数', 'int', (3, 50)),
        ('devHourGuard', '人工作业区间约束', 'bool', None),
        ('devFullTrainN', '全量重训阈值（条）', 'int', (5, 200)),
        ('devMaxBoxes', '设备分组上限', 'int', (DEV_CAP_MIN, DEV_CAP_MAX)),
        ('devManage', '管理分组', 'action', '管理分组'),
    ]),
    ('自动化', [
        ('aiAutoManage', 'AI 自动充电管理', 'bool', None),
        ('notifyEnabled', '系统通知', 'bool', None),
        ('dnd', '免打扰', 'bool', None),
        ('minimizeTray', '关闭时最小化到托盘', 'bool', None),
    ]),
    ('AI 与数据', [
        ('aiEnabled', '启用 AI 分析', 'bool', None),
        ('aiProvider', 'AI 通道', 'choice', ['DeepSeek 云端', '本地端点']),
        ('aiModel', '模型名', 'text', None),
        ('aiBaseUrl', 'Base URL', 'text', None),
        ('aiApiKey', 'API Key', 'secret', None),
        ('aiRedact', '数据脱敏', 'bool', None),
        ('aiDailyReport', '每日自动日报', 'bool', None),
        ('retentionDays', '数据保留期限（天）', 'int', (0, 3650)),
        ('autoCleanup', '自动清理', 'bool', None),
        ('sampleInterval', '采样间隔（秒）', 'int', (1, 60)),
    ]),
    ('显示与性能', [
        ('theme', '主题配色', 'choice', ['原版深色', '暗夜护眼', '高对比']),
        ('accent', '强调色', 'choice', ['品牌默认', '青蓝', '紫青']),
        ('animLevel', '动画与特效强度', 'choice', ['标准', '降级', '关闭']),
        ('reduceMotion', '减少动效', 'bool', None),
    ]),
    # R026：9 项单组在 900×700 展开会溢出，拆成「外观」「运行」两组（仍无滚动条）
    ('运行与曲线', [
        ('perfMode', '性能模式', 'choice', ['省电', '均衡', '高性能']),
        ('expFeatures', '实验性功能', 'bool', None),
        ('pollInterval', '端口轮询频率（秒）', 'int', (1, 10)),
        ('chartPoints', '图表点数', 'int', (30, 600)),
        ('smooth', '曲线平滑', 'choice', ['平滑', '原始']),
    ]),
    ('诊断与日志', [
        ('logLevel', '日志级别', 'choice', ['INFO', 'DEBUG', 'WARNING', 'ERROR']),
        ('debugPanel', '开发者调试入口', 'bool', None),
        ('proxy', '网络代理', 'text', None),
    ]),
]

# 每项的效果时机（改完立刻给用户明确反馈；不允许"存了不用"）
# C34-UI（B′，2026-11-11）：24 项按语义合并为 4 组。
# 卡头 32 px 下只有 6 项组（数据、日志与诊断）与 9 项组（显示、性能与曲线）超限需要滚动。
_BY_KEY = {_it[0]: _it for _name, _items in SPEC for _it in _items}
SPEC = [
    ('充电与保护', [_BY_KEY[k] for k in ('protectTemp', 'protectPower',
                                  'autoOffNoCharge', 'mergeGap')]),
    ('自动化与通知', [_BY_KEY[k] for k in ('aiAutoManage', 'notifyEnabled',
                                  'dnd', 'minimizeTray')]),
    ('数据、日志与诊断', [_BY_KEY[k] for k in ('retentionDays', 'autoCleanup',
                                  'sampleInterval', 'logLevel',
                                  'debugPanel', 'proxy')]),
    ('显示、性能与曲线', [_BY_KEY[k] for k in ('theme', 'accent', 'animLevel',
                                  'reduceMotion', 'perfMode',
                                  'expFeatures', 'pollInterval',
                                  'chartPoints', 'smooth')]),
    # 设备识别独立成组（灵敏度 / 管理入口；箱数已改为按实际分箱动态增长）
    ('设备识别', [_BY_KEY[k] for k in ('deviceSensitivity', 'devShapeThr',
                                      'devCenterMode', 'devCandMin',
                                      'devHourGuard', 'devMaxBoxes',
                                      'devManage')]),
]

EFFECT = {

    'protectTemp': '下次分析生效', 'protectPower': '下次分析生效',
    'autoOffNoCharge': '即时（下一采样拍生效）',
    'deviceSensitivity': '下次分析生效',
    'devManage': '即时', 'mergeGap': '下次分析生效',
    'aiAutoManage': '下次告警生效', 'notifyEnabled': '即时', 'dnd': '即时',
    'minimizeTray': '下次关窗生效',
    'aiEnabled': '即时', 'aiProvider': '即时', 'aiModel': '即时',
    'aiBaseUrl': '下次请求', 'aiApiKey': '立即加密保存', 'aiRedact': '下次请求',
    'aiDailyReport': '下次启动生效', 'retentionDays': '即时（后端）',
    'autoCleanup': '下次启动', 'sampleInterval': '需重启服务',
    'theme': '即时', 'accent': '即时', 'animLevel': '即时', 'reduceMotion': '即时',
    'perfMode': '即时', 'expFeatures': '即时', 'pollInterval': '即时',
    'chartPoints': '即时', 'smooth': '即时',
    'logLevel': '即时', 'debugPanel': '即时', 'proxy': '下次请求',
}


# ---------------------------------------------------------------- 偏好读写
class Prefs:
    """统一 QSettings 读写（组 adv/ 与 legal/）。"""

    def __init__(self, org, app):
        self.s = QSettings(org, app)

    def get(self, key, default=None):
        d = DEFAULTS.get(key, default)
        try:
            v = self.s.value('adv/' + key, d)
        except Exception:
            return d
        if isinstance(d, bool):
            return bool(v) if not isinstance(v, str) else v.lower() in ('1', 'true', 'yes')
        if isinstance(d, int):
            try:
                return int(v)
            except Exception:
                return d
        return v

    def set(self, key, value):
        self.s.setValue('adv/' + key, value)

    def sync(self):
        """立即落盘（即时生效项用，避免退出前断电/强杀丢最后改动）。"""
        try:
            self.s.sync()
        except Exception:
            pass

    def reset(self):
        for k in list(DEFAULTS):
            try:
                self.s.remove('adv/' + k)
            except Exception:
                pass

    def consent_ok(self):
        return str(self.s.value('legal/accepted', '')) == CONSENT_VERSION

    def consent_decided(self):
        """本版本是否已经问过（同意或拒绝都算）。只有版本号变化才需要再问一次。"""
        v = str(self.s.value('legal/accepted', '') or '')
        return v == CONSENT_VERSION or v == CONSENT_VERSION + '#no'

    def set_consent(self, ok):
        # 同意与拒绝都写同一个键：同意=vX；拒绝=vX#no。
        # 这样「清掉 legal/accepted」就等价于回到首启状态（两种决定一起清）。
        self.s.setValue('legal/accepted',
                        CONSENT_VERSION if ok else CONSENT_VERSION + '#no')
        self.s.setValue('legal/accepted_at',
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        # 必须立即落盘：托盘快速退出/被外部结束进程时，QSettings 的延迟写会丢，
        # 丢掉就会变成「已经同意过但每次启动还弹」。
        self.s.sync()

    def consent_at(self):
        return str(self.s.value('legal/accepted_at', '') or '')


# ---------------------------------------------------------------- 加密存储
class SecretStore:
    """Windows DPAPI（当前用户）加密；不可用时退化为加盐混淆（并标记）。"""

    _ENTROPY = b'XingHong-Cuktech-UX-v1'

    @staticmethod
    def _dpapi(data, protect=True):
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [('cbData', ctypes.wintypes.DWORD),
                        ('pbData', ctypes.POINTER(ctypes.c_char))]

        def _blob(b):
            buf = ctypes.create_string_buffer(b, len(b))
            return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        bin_blob, keep = _blob(data)
        ent, keep2 = _blob(SecretStore._ENTROPY)
        out = DATA_BLOB()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = fn(ctypes.byref(bin_blob), None, ctypes.byref(ent), None, None,
                0x01, ctypes.byref(out))
        if not ok:
            raise OSError('DPAPI failed')
        try:
            raw = ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)
        return raw

    def __init__(self, prefs):
        self.p = prefs

    def save_key(self, key):
        key = (key or '').strip()
        if not key:
            self.p.s.remove('adv/aiApiKeyEnc')
            return
        try:
            enc = self._dpapi(key.encode('utf-8'), True)
            self.p.s.setValue('adv/aiApiKeyEnc', 'dpapi:' + base64.b64encode(enc).decode())
        except Exception:
            salt = hashlib.sha256(self._ENTROPY).digest()
            raw = bytes(b ^ salt[i % len(salt)] for i, b in enumerate(key.encode('utf-8')))
            self.p.s.setValue('adv/aiApiKeyEnc', 'obf:' + base64.b64encode(raw).decode())

    def load_key(self):
        v = str(self.p.s.value('adv/aiApiKeyEnc', '') or '')
        if not v:
            return ''
        try:
            kind, _, payload = v.partition(':')
            raw = base64.b64decode(payload)
            if kind == 'dpapi':
                return self._dpapi(raw, False).decode('utf-8')
            salt = hashlib.sha256(self._ENTROPY).digest()
            return bytes(b ^ salt[i % len(salt)] for i, b in enumerate(raw)).decode('utf-8')
        except Exception:
            return ''


# ---------------------------------------------------------------- AI 配置持久化
AI_CONFIG_DEFAULT = {'version': 1, 'active': 'local', 'profiles': {},
                     'settings': {}}


def ai_config_dir():
    _b = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(_b, 'CuktechPanel')


def ai_config_path():
    return os.path.join(ai_config_dir(), 'ai_config.json')


def ai_config_load():
    """读 ai_config.json：缺失返回空配置；解析失败返回 (空配置, 错误说明)。"""
    _p = ai_config_path()
    if not os.path.exists(_p):
        return dict(AI_CONFIG_DEFAULT), ''
    try:
        with open(_p, 'r', encoding='utf-8') as fh:
            _d = json.load(fh)
        if not isinstance(_d, dict):
            raise ValueError('根节点不是对象')
        _d.setdefault('version', 1)
        _d.setdefault('profiles', {})
        _d.setdefault('settings', {})
        _d.setdefault('active', 'local')
        if not isinstance(_d['profiles'], dict) or not isinstance(
                _d['settings'], dict):
            raise ValueError('profiles / settings 类型错误')
        return _d, ''
    except Exception as e:
        return dict(AI_CONFIG_DEFAULT), str(e)


def _restrict(path):
    """文件权限收到当前用户（Windows ACL），失败静默。"""
    try:
        _u = os.environ.get('USERNAME') or ''
        if not _u:
            return
        import subprocess
        subprocess.run(['icacls', path, '/inheritance:r',
                        '/grant:r', '%s:F' % _u],
                       capture_output=True, timeout=8,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception:
        pass


def ai_config_save(data):
    """原子写 ai_config.json，并把权限收到当前用户。"""
    os.makedirs(ai_config_dir(), exist_ok=True)
    _p = ai_config_path()
    _tmp = _p + '.tmp'
    with open(_tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(_tmp, _p)
    _restrict(_p)
    return _p


def dpapi_enc(text):
    _raw = SecretStore._dpapi(str(text or '').encode('utf-8'), True)
    return 'dpapi:' + base64.b64encode(_raw).decode()


def dpapi_dec(tok):
    _k, _, _b = str(tok or '').partition(':')
    if _k != 'dpapi' or not _b:
        return ''
    try:
        return SecretStore._dpapi(base64.b64decode(_b), False).decode('utf-8')
    except Exception:
        return ''


# ---------------------------------------------------------------- 规则引擎
def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


# 设备识别——灵敏度决定「归箱」阈值，三档必须能在归属结果上看出差别。
DEV_EPS = {'保守': 0.72, '均衡': 0.62, '激进': 0.52}
DEV_MAX_BOXES = 12            # 分组总数上限缺省值（固定组+非固定组合计，可在设置页调整）
DEV_SEED = 20261111          # 重分组固定随机种子：同一批数据每次结果一致
DEV_MOVE = 0.25              # 名字迁移时中心位移超过该值 → 标「待确认」
_DEV_DIM = ('peak', 'dur', 'volt', 'port', 'hour')
_DEV_W = {'peak': 0.34, 'dur': 0.22, 'volt': 0.18, 'port': 0.14, 'hour': 0.12}


def dev_vec(s):
    """会话 → 5 维归一化特征（全部在本机算，不上传）。"""
    try:
        hour = datetime.fromtimestamp(_f(s.get('start_time'))).hour
    except Exception:
        hour = 0
    port = str(s.get('port') or '')
    return {
        'peak': min(1.0, max(0.0, _f(s.get('peak_power_w')) / 140.0)),
        'dur': min(1.0, max(0.0, _f(s.get('duration_sec')) / 14400.0)),
        'volt': min(1.0, max(0.0, _f(s.get('avg_voltage')) / 24.0)),
        'port': 1.0 if port.upper().endswith('2') else 0.0,
        'hour': min(1.0, max(0.0, hour / 24.0)),
    }


def dev_center(vecs):
    """一组 5 维特征的平均中心（时刻按圆周平均，端口取多数）——用于合并后中心重算。"""
    vs = [v for v in (vecs or []) if isinstance(v, dict)]
    if not vs:
        return {}
    n = float(len(vs))
    out = {}
    for k in _DEV_DIM:
        if k == 'hour':
            hx = sum(math.cos(2 * math.pi * _f(v.get(k))) for v in vs)
            hy = sum(math.sin(2 * math.pi * _f(v.get(k))) for v in vs)
            out[k] = round((math.atan2(hy, hx) / (2 * math.pi)) % 1.0, 4)
        elif k == 'port':
            out[k] = 1.0 if sum(_f(v.get(k)) for v in vs) > n / 2.0 else 0.0
        else:
            out[k] = round(sum(_f(v.get(k)) for v in vs) / n, 4)
    return out


def _dev_dist(a, b):
    """加权欧氏距离（0..1）；时刻按圆周处理，避免 23 点与 0 点被拉远。

    port 是二值分类（C1/C2），必须按「同/不同」算，不能按圆周算——
    否则 0 与 1 会被折成同一个点，两个端口被误判为同一设备。
    """
    d2 = 0.0
    for k in _DEV_DIM:
        d = abs(_f(a.get(k)) - _f(b.get(k)))
        if k == 'hour':
            d = min(d, 1.0 - d)
        d2 += _DEV_W[k] * d * d
    return min(1.0, math.sqrt(max(0.0, d2)))


def dev_sim(a, b):
    return max(0.0, 1.0 - _dev_dist(a, b))


def dev_dist(a, b):
    """公开的加权距离（供共用 AI 服务层使用，避免跨模块访问私有符号）。"""
    return _dev_dist(a, b)


def session_key(s):
    """会话稳定键：优先数据库 id，其次 开始时间|端口|结束时间|电量。

    用于手动归箱覆盖（不参与聚类）；同一条记录反复出现时键保持一致。
    """
    if s.get('id') not in (None, ''):
        return 'id:%s' % s.get('id')
    return '%d|%s|%d|%.3f' % (
        int(_f(s.get('start_time'))), str(s.get('port') or ''),
        int(_f(s.get('end_time'))), _f(s.get('total_wh')))


def device_model_load(prefs):
    """读取已保存的分组模型（centroid + 名字）；损坏/缺失返回 None。"""
    try:
        raw = prefs.get('devModel', '')
        if isinstance(raw, str):
            raw = json.loads(raw) if raw.strip() else None
        return raw if isinstance(raw, dict) and raw.get('boxes') else None
    except Exception:
        return None


def device_save(prefs, model):
    try:
        prefs.set('devModel', json.dumps(model, ensure_ascii=False))
    except Exception:
        pass


def device_cap(prefs):
    """分组总数上限与占用（唯一口径）：上限 / 已用 / 固定组 / 自建组。

    上限来自设置项 devMaxBoxes（可在「进阶配置 → 设备识别」直接改，即时保存），
    重建分组、新建分组都按这个值限制；模型缺失时占用为 0。
    """
    try:
        cap = int(prefs.get('devMaxBoxes') or DEV_MAX_BOXES)
    except (TypeError, ValueError):
        cap = DEV_MAX_BOXES
    cap = max(DEV_CAP_MIN, min(DEV_CAP_MAX, cap))
    boxes = [b for b in ((device_model_load(prefs) or {}).get('boxes') or [])
             if isinstance(b, dict)]
    fixed = sum(1 for b in boxes if group_is_fixed(b))
    return {'cap': cap, 'used': len(boxes), 'fixed': fixed,
            'custom': len(boxes) - fixed}


# ------------------------------------------------------------ 归属增量学习（kNN）
# 1.5.5：人工改归属/新建分箱 → 抽取特征 → 增量训练 → 落盘 → 下次预测优先用模型。
# 约束：纯 Python（无 numpy/sklearn）、无 GPU、模型可删（删了自动退回规则链路）。
LEARN_MIN_PER_CLASS = 3      # 同一分箱至少 N 条标注才参与训练（防过拟合）
LEARN_MIN_CONF = 0.62        # 模型置信度低于该值 → 退回现有规则判定
LEARN_K = 5                  # kNN 邻居数
LEARN_DIMS = ('peak', 'avgp', 'wh', 'dur', 'volt', 'amp',
              'hour_s', 'hour_c', 'port', 'form')
# 1.5.8：版本号口径 = 累计训练次数每满 1000 次进一版（不再一次训练 +1）。
# 累计训练次数 n_train 只落盘/进审计，界面只显示版本号。
LEARN_TRAIN_PER_VER = 1000
_LEARN_CACHE = {'sig': None, 'data': None}


def learn_version(n_train):
    """累计训练次数 → 版本号（每满 1000 次 +1；不足 1000 记为 v0）。"""
    return max(0, int(n_train or 0)) // LEARN_TRAIN_PER_VER


def _clamp01(x):
    try:
        return min(1.0, max(0.0, float(x)))
    except Exception:
        return 0.0


def learn_dir():
    """模型与训练日志目录：%LOCALAPPDATA%\\CuktechPanel（可备份、可删除）。"""
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'CuktechPanel')


def learn_path():
    return os.path.join(learn_dir(), 'device_learn.json')


def learn_log_path():
    return os.path.join(learn_dir(), 'device_learn.log')


def learn_feats(s):
    """会话 → 10 维归一化特征（全部用库中已有字段，本机计算，不上传）。"""
    peak = _f(s.get('peak_power_w'))
    avgp = _f(s.get('avg_power_w'))
    wh = _f(s.get('total_wh'))
    dur = _f(s.get('duration_sec'))
    volt = _f(s.get('avg_voltage'))
    amp = _f(s.get('avg_current'))
    if avgp <= 0 and wh > 0 and dur > 0:      # 旧会话缺 avg_power_w 时用 Wh/时长补
        avgp = wh * 3600.0 / dur
    if amp <= 0 and volt > 0 and avgp > 0:    # 缺 avg_current 时按 P/U 反推
        amp = avgp / volt
    try:
        _t = datetime.fromtimestamp(_f(s.get('start_time')))
        frac = (_t.hour + _t.minute / 60.0) / 24.0
    except Exception:
        frac = 0.0
    port = str(s.get('port') or '').upper()
    return {'peak': _clamp01(peak / 140.0), 'avgp': _clamp01(avgp / 140.0),
            'wh': _clamp01(wh / 100.0), 'dur': _clamp01(dur / 14400.0),
            'volt': _clamp01(volt / 24.0), 'amp': _clamp01(amp / 6.0),
            # 时刻按圆周编码，避免 23 点与 0 点被拉远
            'hour_s': round(math.sin(2 * math.pi * frac) * 0.5 + 0.5, 4),
            'hour_c': round(math.cos(2 * math.pi * frac) * 0.5 + 0.5, 4),
            'port': 1.0 if port.endswith('2') else 0.0,
            # 峰均比：恒功率段占比，充到末端的会话该值明显下降
            'form': _clamp01(avgp / peak if peak > 0 else 0.0)}


def learn_load():
    """读模型（带 mtime 缓存）；文件缺失/损坏 → None（调用方退回规则）。"""
    p = learn_path()
    try:
        sig = (os.path.getmtime(p), os.path.getsize(p))
    except Exception:
        _LEARN_CACHE['sig'] = None
        _LEARN_CACHE['data'] = None
        return None
    if _LEARN_CACHE['sig'] == sig and _LEARN_CACHE['data'] is not None:
        return _LEARN_CACHE['data']
    try:
        with open(p, 'r', encoding='utf-8') as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            d = None
    except Exception:
        d = None
    if isinstance(d, dict) and d.get('n_train') is None:
        # 1.5.8 平滑接上旧数据：旧口径 version 就是「每训练一次 +1」的累计次数，
        # 因此 n_train = 旧 version，再按每 1000 次回算版本号，不从 0 重来。
        _v_old = int(d.get('version') or 0)
        d['n_train'] = _v_old
        d['version'] = learn_version(_v_old)
        d['ver_legacy'] = _v_old
    _LEARN_CACHE['sig'] = sig
    _LEARN_CACHE['data'] = d
    return d


def learn_save(m):
    """原子写模型文件（先写 .tmp 再替换），并让缓存失效。"""
    p = learn_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(m, fh, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        return False
    _LEARN_CACHE['sig'] = None
    _LEARN_CACHE['data'] = None
    return True


def learn_log(line):
    try:
        os.makedirs(learn_dir(), exist_ok=True)
        with open(learn_log_path(), 'a', encoding='utf-8') as fh:
            fh.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), line))
    except Exception:
        pass


def _l2(a, b):
    d = 0.0
    for k in LEARN_DIMS:
        dd = _f(a.get(k)) - _f(b.get(k))
        d += dd * dd
    return math.sqrt(max(0.0, d))


def _knn_vote(feat, samples, k=None, skip_key=None):
    """kNN 投票 → (箱 id, 置信度, 到同箱邻居的平均距离)。

    置信度 = 票占比 × (0.5 + 0.5×距离余量)：票全给同一箱且最近异箱明显更远时
    接近 1；票分裂或异箱与同箱一样近时落到 0.5 以下。2.1.2 之前用
    `exp(-2.5×距离)`，多分类下普遍压到 0.62 阈值以下，模型实际从未生效。
    """
    pts = [p for p in (samples or [])
           if isinstance(p, dict) and isinstance(p.get('vec'), dict)
           and (skip_key is None or str(p.get('key')) != str(skip_key))]
    if not pts:
        return None, 0.0, 1.0
    pairs = sorted(((_l2(feat, p['vec']), p) for p in pts), key=lambda x: x[0])
    kk = max(1, min(int(k or LEARN_K), len(pairs)))
    votes, dists = {}, {}
    for d, p in pairs[:kk]:
        bid = str(p.get('bid') or '')
        if not bid:
            continue
        votes[bid] = votes.get(bid, 0.0) + (float(p.get('w') or 1.0)
                                            / (0.05 + d))
        dists.setdefault(bid, []).append(d)
    if not votes:
        return None, 0.0, 1.0
    tot = sum(votes.values()) or 1.0
    bid = max(votes, key=lambda x: votes[x])
    md = sum(dists[bid]) / max(1, len(dists[bid]))
    d_other = min([d for d, p in pairs[:max(kk * 4, kk)]
                   if str(p.get('bid') or '') != bid] or [1.0])
    margin = (d_other - md) / max(d_other + md, 1e-6)
    conf = (votes[bid] / tot) * (0.5 + 0.5 * margin)
    return bid, round(max(0.0, min(1.0, conf)), 3), md


def learn_predict(feat, model=None, skip_key=None):
    """返回 (箱 id, 置信度)；无可用模型时返回 (None, 0.0)。

    skip_key：留一评估时跳过同键样本，避免「自己投自己」把置信度抬到 1。
    """
    m = model if isinstance(model, dict) else learn_load()
    if not m:
        return None, 0.0
    pts = [p for p in (m.get('samples') or []) if isinstance(p.get('vec'), dict)]
    _cnts = m.get('counts')
    if isinstance(_cnts, dict) and _cnts:      # 同箱标注不足的类别不参与预测
        pts = [p for p in pts
               if int(_cnts.get(str(p.get('bid'))) or 0) >= LEARN_MIN_PER_CLASS]
    bid, conf, _md = _knn_vote(feat, pts, skip_key=skip_key)
    return bid, conf


def learn_train(samples, eps=DEV_EPS['均衡'], holdout=True):
    """按当前标注重算质心 + 做一次留出评估（4:1 切分，样本不足如实标注）。

    注意：全部标注都落盘（samples），只有「同箱 ≥N 条」的样本参与质心与评估，
    这样先标注后补足的过程不会丢数据，也不会因为 2 条样本就过拟合。
    holdout=False：增量路径——只更新样本与质心，不做留出评估（更快，日志标明）。
    """
    all_pts = [p for p in (samples or [])
               if isinstance(p, dict) and p.get('bid')
               and _vec_ok(p.get('vec'))]
    cnt = {}
    for p in all_pts:
        cnt[str(p['bid'])] = cnt.get(str(p['bid']), 0) + 1
    keep = {b for b, n in cnt.items() if n >= LEARN_MIN_PER_CLASS}
    pts = [p for p in all_pts if str(p['bid']) in keep]
    cents, cnum = {}, {}
    for p in pts:
        b = str(p['bid'])
        cnum[b] = cnum.get(b, 0) + 1
        c = cents.setdefault(b, {k: 0.0 for k in LEARN_DIMS})
        for k in LEARN_DIMS:
            c[k] += _f(p['vec'].get(k))
    for b, c in cents.items():
        for k in LEARN_DIMS:
            c[k] = round(c[k] / max(1, cnum[b]), 4)
    # 留出集：每类每第 4 条抽 1 条做验证（该类 ≥4 条才有留出）
    train, val = [], []
    seen = {}
    for p in sorted(pts, key=lambda x: str(x.get('key') or '')):
        b = str(p['bid'])
        seen[b] = seen.get(b, 0) + 1
        (val if seen[b] % 4 == 0 and cnt_ok(cnt, b) else train).append(p)
    metrics = {'ok': False, 'n': len(all_pts), 'note': '样本不足'}
    if not holdout:
        metrics = {'ok': False, 'n': len(all_pts), 'note': '增量更新（未做留出评估）'}
        train = val = []
    elif train and val:
        ok_m = ok_r = 0
        _m = {'samples': train, 'counts': cnum}
        for p in val:
            lb, lc = learn_predict(p['vec'], _m, skip_key=p.get('key'))
            if lb and lb == str(p['bid']) and lc >= LEARN_MIN_CONF:
                ok_m += 1
            best, bsc = None, 0.0
            for b, c in cents.items():
                sc = dev_sim(p['vec'], c)
                if sc > bsc:
                    best, bsc = b, sc
            if best and bsc >= eps and best == str(p['bid']):
                ok_r += 1
        n = len(val)
        metrics = {'ok': True, 'n': n, 'model': round(ok_m / n, 3),
                   'rule': round(ok_r / n, 3),
                   'note': '留出 %d 条（4:1）' % n}
    else:
        metrics['note'] = '样本不足（可用标注 %d 条，需每箱 ≥%d 条且有留出）' % (
            len(all_pts), LEARN_MIN_PER_CLASS)
    return {'ok': True, 'trained_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'eps': eps, 'k': LEARN_K, 'min_per_class': LEARN_MIN_PER_CLASS,
            'samples': [{k: p.get(k) for k in
                         ('key', 'bid', 'vec', 'cvec', 'at', 'w', 'src')}
                        for p in all_pts],
            'trainable': len(pts),
            'centroids': cents, 'counts': cnum, 'holdout': metrics}


def cnt_ok(cnt, b):
    return int(cnt.get(b) or 0) >= 4


def learn_apply(changes, source='manual', drop_bid=None, drop_bids=None,
                full=None, n_full=30):
    """批量改归属 → 一次重训 → 原子落盘 → version+1 → 日志（不动全库历史判定）。

    changes = [(key, bid, feats, cvec, weight)]；bid 为空表示撤销该条标注。
    drop_bid / drop_bids：删箱/合并时把该箱旧样本一并剔出训练集。
    full：True=全量（重算质心 + 留出评估），False=增量（只更新样本/质心），
          None=按规则——结构变更或影响条数 > n_full 走全量。
    失败留痕并保留上一版模型可用（返回旧 version + ok=False，不写坏文件）。
    """
    t0 = time.time()
    old = learn_load() or {'version': 0, 'n_train': 0, 'samples': []}
    _n0 = int(old.get('n_train') if old.get('n_train') is not None
              else (old.get('version') or 0))
    pts = [p for p in (old.get('samples') or [])
           if isinstance(p, dict) and _vec_ok(p.get('vec'))]
    _drop = {str(x) for x in ([drop_bid] if drop_bid else []) + list(drop_bids or [])}
    if _drop:
        pts = [p for p in pts if str(p.get('bid')) not in _drop]
    _keys = {str(c[0]) for c in changes or []}
    pts = [p for p in pts if str(p.get('key')) not in _keys]
    at = time.strftime('%Y-%m-%d %H:%M')
    for _c in changes or []:
        _k, _b = str(_c[0]), (_c[1] if len(_c) > 1 else None)
        _f = _c[2] if len(_c) > 2 else {}
        _cv = _c[3] if len(_c) > 3 else None
        _w = _c[4] if len(_c) > 4 else 1.0
        if _b and _vec_ok(_f):
            rec = {'key': _k, 'bid': str(_b), 'vec': dict(_f or {}),
                   'at': at, 'w': round(float(_w or 1.0), 3), 'src': str(source or '')}
            if isinstance(_cv, list) and len(_cv) >= CLUSTER_N:
                rec['cvec'] = [round(float(x), 4) for x in _cv]
            pts.append(rec)
    _structural = str(source or '') in ('create', 'delete', 'merge', 'merge_undo',
                                        'seed', 'seed-adopt')
    _full = bool(full) if full is not None else (_structural or len(_keys) > int(n_full or 30))
    try:
        trained = learn_train(pts, holdout=_full)
    except Exception as exc:                       # 训练异常 → 旧模型继续可用
        learn_log('TRAIN_FAIL src=%s n=%d err=%r keep_n_train=%d keep_v%d'
                  % (source, len(_keys), exc, _n0, learn_version(_n0)))
        out = dict(old)
        out.update({'ok': False, 'err': repr(exc), 'mode': 'full' if _full else 'incremental',
                    'n_train': _n0, 'version': learn_version(_n0)})
        return out
    # 1.5.8：版本号 = 累计训练次数每满 1000 次 +1；n_train 每次训练都 +1（只落盘/审计）
    _n1 = _n0 + 1
    _v0 = learn_version(_n0)
    _v1 = learn_version(_n1)
    trained['n_train'] = _n1
    trained['version'] = _v1
    trained['mode'] = 'full' if _full else 'incremental'
    trained['last'] = {'src': str(source or ''), 'n': len(_keys), 'at': at,
                       'ver': _v1, 'n_train': _n1, 'mode': trained['mode']}
    if not learn_save(trained):                    # 落盘失败 → 旧文件仍完整，版本不前进
        learn_log('TRAIN_SAVE_FAIL src=%s n=%d keep_n_train=%d keep_v%d'
                  % (source, len(_keys), _n0, _v0))
        out = dict(old)
        out.update({'ok': False, 'err': 'save_failed',
                    'n_train': _n0, 'version': _v0})
        return out
    _dur = int((time.time() - t0) * 1000)
    _h = trained.get('holdout') or {}
    learn_log('TRAIN n_train=%d→%d v%d→v%d src=%s mode=%s n=%d boxes=%d dur=%dms holdout=%s'
              % (_n0, _n1, _v0, _v1, source, trained['mode'], len(_keys),
                 len(trained['counts']), _dur,
                 ('%s/%s' % (_h.get('model'), _h.get('rule')))
                 if _h.get('ok') else _h.get('note')))
    trained['dur_ms'] = _dur
    trained['v0'] = _v0
    return trained


def learn_upsert(key, bid, feats, at=None, weight=1.0, cvec=None, source='manual'):
    """单条人工修正写回模型（learn_apply 的单条包装，语义保持不变）。

    weight：标签来源权重——manual=1.0，cluster=0.6；rule 不进训练（调用方不传）。
    """
    return learn_apply([(key, bid, feats, cvec, weight)], source)


# ------------------------------------------------- 曲线形态聚类（簇级命名，无监督）
CLUSTER_N = 32          # 形态重采样点数（0–100% 进度）
CLUSTER_MIN_SIZE = 5    # 簇内少于该条数 → 并入「未分类」


def curve_shape(points, n=CLUSTER_N, min_pts=8):
    """[(ts, power)] 时间归一化重采样到 n 点，再除以峰值（幅值归一化）。"""
    pts = [(float(t), float(p or 0)) for t, p in (points or [])]
    if len(pts) < min_pts:
        return None
    pts.sort(key=lambda x: x[0])
    t0, t1 = pts[0][0], pts[-1][0]
    if t1 - t0 <= 0:
        return None
    mx = max(p for _, p in pts)
    if mx <= 0.5:
        return None
    out, lo = [], 0
    for i in range(n):
        want = t0 + (t1 - t0) * i / (n - 1.0)
        while lo + 1 < len(pts) and pts[lo + 1][0] < want:
            lo += 1
        t_a, p_a = pts[lo]
        t_b, p_b = pts[min(lo + 1, len(pts) - 1)]
        p = p_a + (p_b - p_a) * (want - t_a) / (t_b - t_a) if t_b > t_a else p_a
        out.append(round(max(0.0, p) / mx, 4))
    return out


def cluster_vec(s, sh):
    """形态 32 维 + 电压平台(×1.5 权重) / Wh / 时长 / 峰值 / 端口 five aux。

    电压平台加权、端口单独成维：交叉验证里同一设备更依赖"接在哪个口 + 电压平台"，
    形态只负责区分充电策略（恒流长充 / 大电流快充 / 碎片补电）。
    """
    port = str(s.get('port') or '').upper()
    return list(sh) + [min(1.0, _f(s.get('avg_voltage')) / 24.0) * 1.5,
                       min(1.0, _f(s.get('total_wh')) / 100.0),
                       min(1.0, _f(s.get('duration_sec')) / 14400.0),
                       min(1.0, _f(s.get('peak_power_w')) / 140.0),
                       1.0 if port.endswith('2') else 0.0]


def _cdist(a, b):
    return math.sqrt(sum((x - y) * (x - y) for x, y in zip(a, b)))


def shape_dist(a, b):
    """形态特征欧氏距离（公开别名；界面层不得直接引用 `_cdist`）。"""
    return _cdist(a, b)


def cluster_km(X, k, seed=7, iters=40):
    """纯 Python k-means（k-means++ 初始化，固定种子可复现）。"""
    n = len(X)
    k = max(1, min(int(k), n))
    rnd = random.Random(seed)
    cents = [X[rnd.randrange(n)]]
    while len(cents) < k:
        d2 = [min(_cdist(x, c) ** 2 for c in cents) for x in X]
        tot = sum(d2) or 1.0
        r, acc = rnd.random() * tot, 0.0
        for i, d in enumerate(d2):
            acc += d
            if acc >= r:
                cents.append(X[i])
                break
        else:
            cents.append(X[-1])
    lab = [0] * n
    for _ in range(iters):
        new = [min(range(k), key=lambda c: _cdist(x, cents[c])) for x in X]
        if new == lab:
            break
        lab = new
        for c in range(k):
            mem = [X[i] for i in range(n) if lab[i] == c]
            if mem:
                cents[c] = [sum(col) / len(mem) for col in zip(*mem)]
    return lab


def cluster_silhouette(X, lab, cap=320):
    """平均轮廓系数（>cap 时等距抽样，控制 O(n²) 成本）。"""
    idx = list(range(len(X)))
    if len(idx) > cap:
        idx = idx[::max(1, len(idx) // cap)]
    Xs = [X[i] for i in idx]
    ls = [lab[i] for i in idx]
    n = len(Xs)
    if n < 3:
        return 0.0
    D = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            D[i][j] = D[j][i] = _cdist(Xs[i], Xs[j])
    out = []
    for i in range(n):
        same = [D[i][j] for j in range(n) if j != i and ls[j] == ls[i]]
        if not same:
            out.append(0.0)
            continue
        a = sum(same) / len(same)
        b = float('inf')
        for c in set(ls):
            if c == ls[i]:
                continue
            other = [D[i][j] for j in range(n) if ls[j] == c]
            if other:
                b = min(b, sum(other) / len(other))
        out.append(0.0 if b == float('inf') or max(a, b) <= 0
                   else (b - a) / max(a, b))
    return sum(out) / len(out)


def cluster_sessions(items, k=None, min_size=None):
    """items=[{'key','session','shape'}] → 聚类结果（含每簇平均曲线与规模区间）。

    k=None 时在 2–6 中按轮廓系数择优；少于 min_size（默认 CLUSTER_MIN_SIZE）的簇
    并入 cid=-1。这里只用于「未归箱会话里发现候选新设备」，不再给全库分簇。
    """
    _ms = max(2, int(min_size or CLUSTER_MIN_SIZE))
    rows = [it for it in items or [] if it.get('shape')]
    if len(rows) < max(8, _ms):
        return {'ok': False, 'note': '样本不足（%d 条有曲线）' % len(rows)}
    X = [cluster_vec(it['session'], it['shape']) for it in rows]
    best = None
    curve = []
    for _k in range(2, 7):
        lab = cluster_km(X, _k)
        sil = cluster_silhouette(X, lab)
        curve.append({'k': _k, 'sil': round(sil, 4)})
        if best is None or sil > best[1]:
            best = (_k, sil, lab)
    if k:
        kk = max(2, min(6, int(k)))
        lab = cluster_km(X, kk)
        sil = cluster_silhouette(X, lab)
    else:
        kk, sil, lab = best
    out = []
    for c in sorted(set(lab)):
        mem = [rows[i] for i in range(len(rows)) if lab[i] == c]
        if len(mem) < _ms:
            for m in mem:
                out.append({'cid': -1, 'manual': m})
            continue
        out.append({
            'cid': c, 'n': len(mem),
            'keys': [m['key'] for m in mem],
            'dur': [min(_f(m['session'].get('duration_sec')) for m in mem),
                    max(_f(m['session'].get('duration_sec')) for m in mem)],
            'wh': [round(min(_f(m['session'].get('total_wh')) for m in mem), 1),
                   round(max(_f(m['session'].get('total_wh')) for m in mem), 1)],
            'peak': round(sum(_f(m['session'].get('peak_power_w'))
                              for m in mem) / len(mem), 1),
            'shape': [round(sum(m['shape'][i] for m in mem) / len(mem), 3)
                      for i in range(CLUSTER_N)],
        })
    small = [c for c in out if c.get('cid') == -1]
    clusters = [c for c in out if c.get('cid') != -1]
    return {'ok': True, 'k': kk, 'sil': round(sil, 4), 'curve': curve,
            'covered': len(rows) - len(small), 'small': len(small),
            'clusters': clusters}


# --------------------------------------- 分箱即形态簇（箱中心 / 阈值判定 / 增量更新）
# 1.5.5 语义修正：分箱本身就是簇，不做"先 k-means 分全库再命名"。
# 箱中心 = 箱内全部会话的 37 维形态特征均值（32 点归一化曲线 + 电压/Wh/时长/峰值/端口）。
# 用 Welford 增量式维护 n / mean / m2：增删一条会话 O(37)，不需要全量重算。
BOX_MATCH = 2.2         # 形态距离绝对上限（超过 → 不硬归，进未归箱）
BOX_MATCH_K = 1.6       # 或 箱内平均距离 × K（箱越松阈值越宽），两者取小
BOX_MATCH_FLOOR = 0.25  # 单条会话的箱（spread≈0）也要留最小容差


def box_shape_add(box, cvec):
    """把一条会话的形态向量并入箱中心（增量均值 + 方差）。box 原地修改。"""
    if not isinstance(box, dict) or not cvec:
        return box
    x = [float(v) for v in cvec]
    sc = box.get('sc') if isinstance(box.get('sc'), dict) else {}
    n = int(sc.get('n') or 0)
    mean = [float(v) for v in (sc.get('mean') or [])]
    m2 = float(sc.get('m2') or 0.0)
    if n <= 0 or len(mean) != len(x):
        n, mean, m2 = 0, [0.0] * len(x), 0.0
    n += 1
    d1 = [xi - mi for xi, mi in zip(x, mean)]
    mean = [mi + di / n for mi, di in zip(mean, d1)]
    d2 = [xi - mi for xi, mi in zip(x, mean)]
    m2 += sum(a * b for a, b in zip(d1, d2))
    box['sc'] = {'n': n, 'mean': [round(v, 4) for v in mean],
                 'm2': round(max(0.0, m2), 4)}
    return box


def box_shape_del(box, cvec):
    """把一条会话从箱中心里减去（Welford 反向），删完为空则移除该中心。"""
    if not isinstance(box, dict) or not cvec:
        return box
    sc = box.get('sc') if isinstance(box.get('sc'), dict) else {}
    n = int(sc.get('n') or 0)
    mean = [float(v) for v in (sc.get('mean') or [])]
    if n <= 0 or len(mean) != len(cvec):
        return box
    x = [float(v) for v in cvec]
    if n == 1:
        box.pop('sc', None)
        return box
    n2 = n - 1
    mean2 = [(n * mi - xi) / n2 for mi, xi in zip(mean, x)]
    m2 = max(0.0, float(sc.get('m2') or 0.0)
             - sum((xi - mi) * (xi - mi2) for xi, mi, mi2 in zip(x, mean, mean2)))
    box['sc'] = {'n': n2, 'mean': [round(v, 4) for v in mean2],
                 'm2': round(m2, 4)}
    return box


def box_shape_spread(box):
    """箱内均方根距离 sqrt(m2/n)——阈值用（增量可维护）；界面展示的平均距离另行算。"""
    sc = box.get('sc') if isinstance(box, dict) else None
    if not isinstance(sc, dict) or int(sc.get('n') or 0) <= 0:
        return 0.0
    return math.sqrt(max(0.0, float(sc.get('m2') or 0.0)) / int(sc['n']))


BOX_CONS_DIMS = [
    ('peak_power_w', '峰值功率', 'num', 'W'),
    ('avg_power_w', '平均功率', 'num', 'W'),
    ('total_wh', '充入电量', 'num', 'Wh'),
    ('duration_min', '时长', 'num', '分钟'),
    ('avg_voltage', '平均电压', 'num', 'V'),
    ('avg_current', '平均电流', 'num', 'A'),
    ('ratio', '峰均比', 'num', '×'),
    ('port', '常用口', 'cat', ''),
    ('hour', '时段', 'hour', ''),
    # 2.1.1：协议只做可选约束维度——勾选才按它过滤，不预置、默认不生效；
    # 协议不进特征向量，不参与模型训练。
    ('protocol', '协议', 'cat', ''),
]


def port_label(port):
    """端口归一：后端存 1/2/3/a，约束与界面写 C1/C2/C3/A，统一成后者。"""
    _p = str(port or '').strip().lower()
    return {'1': 'C1', '2': 'C2', '3': 'C3', '4': 'A'}.get(_p, _p.upper())


# 2.1.1：协议展示口径（短码用于列表/会话行，全称用于 tooltip/明细）。
# 未握手、空闲或空值统一记「未知协议」，不做任何猜测。
PROTO_FULL = {
    'PD': 'PD 3.0',
    'PPS': 'PD PPS',
    'QC': 'QC 3.0+',
    'UFCS': 'UFCS 融合快充',
    '5V': '5V 标准（未握手）',
}


def proto_short(v):
    """协议短码（≤5 字符）；空值/idle 显示「—」，未知值原样大写。"""
    _p = str(v or '').strip().upper()
    if not _p or _p == 'IDLE':
        return '—'
    return _p[:5]


def proto_full(v):
    """协议全称；空值/idle 记为「未知协议」。"""
    _p = str(v or '').strip().upper()
    if not _p or _p == 'IDLE':
        return '未知协议'
    return PROTO_FULL.get(_p, _p)


def session_dim(s, key):
    """会话的单个维度实测值（时长按分钟、峰均比现算、时段取开始小时）。"""
    if key == 'duration_min':
        return float(s.get('duration_sec') or 0) / 60.0
    if key == 'ratio':
        _dur = max(1.0, float(s.get('duration_sec') or 0))
        _avg = float(s.get('avg_power_w') or 0) or (float(s.get('total_wh') or 0)
                                                    * 3600.0 / _dur)
        return float(s.get('peak_power_w') or 0) / max(1e-6, _avg)
    if key == 'hour':
        try:
            return datetime.fromtimestamp(float(s.get('start_time') or 0)).hour
        except Exception:
            return None
    if key == 'port':
        return port_label(s.get('port'))
    if key == 'protocol':
        return proto_short(s.get('protocol'))
    return float(s.get(key) or 0)


def box_cons_ok(s, box, enabled=True):
    """人工区间硬约束：启用的维度全部满足才可归该箱；没填的维度不管。"""
    if not enabled:
        return True
    cons = (box or {}).get('cons') or {}
    for key, _lb, kind, _u in BOX_CONS_DIMS:
        c = cons.get(key)
        if not isinstance(c, dict) or not c.get('on'):
            continue
        v = session_dim(s, key)
        if v is None:
            continue
        if kind == 'num':
            _lo, _hi = c.get('min'), c.get('max')
            if _lo is not None and v < float(_lo) - 1e-9:
                return False
            if _hi is not None and v > float(_hi) + 1e-9:
                return False
        elif kind == 'cat':
            vals = [str(x).strip() for x in (c.get('vals') or []) if str(x).strip()]
            if vals and v not in vals:
                return False
        else:
            _f, _t = c.get('from'), c.get('to')
            if _f is not None and _t is not None:
                f, t = int(_f), int(_t)
                if f <= t:
                    if not (f <= v <= t):
                        return False
                elif not (v >= f or v <= t):
                    return False
    return True


def box_dim_dist(rows, key):
    """某维度在给定会话里的实测分布：最小 / 中位 / 最大（界面用来判断区间窄不窄）。"""
    vals = [session_dim(s, key) for s in rows or []]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    vals.sort()
    return (vals[0], vals[len(vals) // 2], vals[-1])


def box_nearest(cvec, model, session=None, k=None):
    """未归箱会话 → (箱 id, 到中心距离, 该箱阈值)；带 session 时先过人工区间硬约束。"""
    _kk = float(k or BOX_MATCH_K)
    best = (None, float('inf'), 0.0)
    for b in ((model or {}).get('boxes') or []):
        if not isinstance(b, dict):
            continue
        if session is not None and not box_cons_ok(session, b):
            continue
        sc = b.get('sc') if isinstance(b.get('sc'), dict) else {}
        c = sc.get('mean') or []
        if len(c) != len(cvec):
            continue
        d = _cdist(c, cvec)
        thr = max(BOX_MATCH_FLOOR, min(BOX_MATCH, _kk * box_shape_spread(b)))
        if d < best[1]:
            best = (str(b.get('id') or ''), d, thr)
    return best


def box_shape_rebuild(model, items, manual=None):
    """箱新建/改名/删除/合并后重算全部箱中心（有曲线的会话才计入）。"""
    boxes = [b for b in ((model or {}).get('boxes') or []) if isinstance(b, dict)]
    manual = manual if isinstance(manual, dict) else ((model or {}).get('manual') or {})
    for b in boxes:
        b.pop('sc', None)
    by_id = {str(b.get('id')): b for b in boxes}
    for it in items or []:
        bid = str(manual.get(it.get('key')) or '')
        if bid in by_id and it.get('shape'):
            box_shape_add(by_id[bid], cluster_vec(it['session'], it['shape']))
    return model



def box_shape_merge(box_a, box_b):
    """把箱 b 的中心合并进箱 a（Welford 两组合并），b 的约束一并清空。"""
    sa = (box_a or {}).get('sc') if isinstance((box_a or {}).get('sc'), dict) else {}
    sb = (box_b or {}).get('sc') if isinstance((box_b or {}).get('sc'), dict) else {}
    na, nb = int(sa.get('n') or 0), int(sb.get('n') or 0)
    ma = [float(v) for v in (sa.get('mean') or [])]
    mb = [float(v) for v in (sb.get('mean') or [])]
    if nb and ma and mb and len(ma) == len(mb):
        n = na + nb
        mc = [(na * x + nb * y) / n for x, y in zip(ma, mb)]
        delta = [x - y for x, y in zip(ma, mb)]
        m2 = (float(sa.get('m2') or 0.0) + float(sb.get('m2') or 0.0)
              + sum(d * d for d in delta) * na * nb / n)
        box_a['sc'] = {'n': n, 'mean': [round(v, 4) for v in mc],
                       'm2': round(max(0.0, m2), 4)}
    elif nb and not ma:
        box_a['sc'] = {'n': nb, 'mean': [round(v, 4) for v in mb],
                       'm2': round(float(sb.get('m2') or 0.0), 4)}
    return box_a


def auto_bin_name(mem):
    """按功率/时长/电量的中位数给描述性建议名（不出现 DEV 编号）。"""
    _f = lambda s, k: float(s.get(k) or 0)
    if not mem:
        return '未分类'
    _peak = sorted(_f(s, 'peak_power_w') for s in mem)[len(mem) // 2]
    _dur = sorted(_f(s, 'duration_sec') for s in mem)[len(mem) // 2] / 60.0
    _wh = sorted(_f(s, 'total_wh') for s in mem)[len(mem) // 2]
    if _peak >= 45:
        p = '快充·高功率'
    elif _peak >= 15:
        p = '常规·中功率'
    else:
        p = '小电流·低功率'
    d = '长时' if _dur >= 120 else ('中时' if _dur >= 30 else '短时')
    w = '大电量' if _wh >= 40 else ('中电量' if _wh >= 8 else '小电量')
    return '%s·%s·%s' % (p, d, w)


def auto_bins(items, kmax=5, min_size=None):
    """真实会话 → 初始分箱（宁粗不碎：先取最优轮廓，再向下折到 ≥0.9 最优的最小 k）。

    返回 {'ok','k','sil','bins':[{name,n,shape,spread,dims:{dim:[min,med,max]},
    center(10 维规则中心), members:[会话]}]}
    """
    rows = [it for it in items or [] if it.get('shape')]
    if len(rows) < 12:
        return {'ok': False, 'msg': '样本不足（%d 条有曲线）' % len(rows)}
    _ms = max(3, int(min_size or 5))
    X = [cluster_vec(it['session'], it['shape']) for it in rows]
    kmax = max(2, min(5, int(kmax or 5)))
    scored = []
    for k in range(2, kmax + 1):
        lab = cluster_km(X, k)
        scored.append((k, cluster_silhouette(X, lab), lab))
    best = max(scored, key=lambda x: x[1])
    pick = best
    for cand in scored:                       # 能少就少：同等质量取更小的 k
        if cand[1] >= best[1] * 0.9:
            pick = cand
            break
    k, sil, lab = pick
    used = sorted({c for c in lab if lab.count(c) >= _ms})
    bins = []
    for c in used:
        idx = [i for i in range(len(rows)) if lab[i] == c]
        mem = [rows[i] for i in idx]
        ss = [rows[i]['session'] for i in idx]
        _tmp = {}
        for i in idx:
            box_shape_add(_tmp, X[i])
        _sc = _tmp.get('sc') or {}
        _dim = {}
        for _k, _lb, _kind, _u in BOX_CONS_DIMS:
            if _kind == 'cat':
                _dim[_k] = '、'.join(sorted({str(s.get('port') or '') for s in ss} - {''}))
                continue
            _d = box_dim_dist(ss, _k)
            if _d is not None:
                _dim[_k] = [round(_d[0], 2), round(_d[1], 2), round(_d[2], 2)]
        feats = [learn_feats(s) for s in ss]
        bins.append({
            'name': auto_bin_name(ss), 'n': len(mem), 'cid': c,
            'shape': [round(sum(m['shape'][i] for m in mem) / len(mem), 3)
                      for i in range(CLUSTER_N)],
            'spread': round(math.sqrt(max(0.0, float(_sc.get('m2') or 0))
                                      / max(1, int(_sc.get('n') or 1))), 4),
            'dims': _dim,
            'center': {dk: round(sum(f[dk] for f in feats) / len(feats), 4)
                       for dk in LEARN_DIMS},
            'members': mem, 'shape_vec': X[idx[0]] if idx else []})
    return {'ok': True, 'k': k, 'sil': round(sil, 4), 'bins': bins,
            'curve': [{'k': a, 'sil': round(b, 4)} for a, b, _ in scored]}


def auto_bins_apply(model, items, kmax=5):
    """把自动分箱写成正式分箱（来源 auto）；固定组的名称/成员/结构整体保留。

    只有「未归入固定组」的会话参与聚类；固定组的人工归属原样保留，不被重写。
    """
    m = model if isinstance(model, dict) else {}
    old_boxes = [b for b in (m.get('boxes') or []) if isinstance(b, dict)]
    fixed = [b for b in old_boxes if group_is_fixed(b)]
    fid = {str(b.get('id') or '') for b in fixed}
    manual = dict(m.get('manual') or {})
    meta = dict(m.get('manual_meta') or {})
    prot = {k for k, v in manual.items()
            if str(v) in fid or str(v) == UNASSIGNED}
    for it in items or []:                       # 固定组形态命中的会话也保护起来
        if it.get('key') in prot or not it.get('shape') or not fixed:
            continue
        _nb, _nd, _nthr = box_nearest(cluster_vec(it['session'], it['shape']),
                                      {'boxes': fixed}, it['session'])
        if _nb and _nd <= _nthr:
            prot.add(it['key'])
    rest = [it for it in items or [] if it.get('key') not in prot]
    res = auto_bins(rest, kmax)
    if not res.get('ok'):
        if not fixed:
            return res
        m['boxes'] = fixed
        m['manual'] = {k: v for k, v in manual.items() if str(v) in fid}
        m['manual_meta'] = meta
        return {'ok': True, 'k': 0, 'sil': 0, 'boxes': [], 'model': m,
                'curve': [], 'kept': len(fixed),
                'msg': '其余会话不足（%d 条），仅保留固定组' % len(rest)}
    at = datetime.now().strftime('%Y-%m-%d %H:%M')
    used = set(fid)
    made = []
    boxes = list(fixed)
    for bn in res['bins']:
        bid = _new_group_id([{'id': x} for x in used], 'DEV')
        used.add(bid)
        box = {'id': bid, 'name': bn['name'], 'parent': '', 'center': bn['center'],
               'n': bn['n'], 'review': False, 'move': 0.0, 'prev': '', 'at': at,
               'src': 'auto', 'fixed': False}
        _tmp = {}
        for it in bn['members']:
            box_shape_add(_tmp, cluster_vec(it['session'], it['shape']))
        box['sc'] = _tmp.get('sc')
        boxes.append(box)
        made.append((bid, bn['name'], bn['n']))
        for it in bn['members']:
            manual[it['key']] = bid
            meta[it['key']] = {'at': at, 'by': 'auto', 'src': 'auto',
                               'to': bid, 'session': it['session'].get('id')}
    m['boxes'] = boxes
    m['manual'] = {k: v for k, v in manual.items()
                   if str(v) in fid or str(v) in {b['id'] for b in boxes}
                   or str(v) == UNASSIGNED}
    m['manual_meta'] = meta
    return {'ok': True, 'k': res['k'], 'sil': res['sil'], 'boxes': made,
            'model': m, 'curve': res['curve'], 'kept': len(fixed)}


def device_box_name(bid, name=''):
    """设备箱唯一取名口径（界面所有「设备归属」文案都走这里）。

    用户起过名 → 显示名字；空名或「设备 N / 设备箱 N」自动占位名 → 显示 DEV 编号。
    调试日志可继续直接用 id，不必经过本函数。
    """
    import re as _re
    _nm = str(name or '').strip()
    if _nm and not _re.fullmatch(r'设备\s*箱?\s*\d+', _nm):
        return _nm
    return str(bid or '')


def device_names(model):
    """{箱 id: 名称}，名称可能为空/占位；显示时再经 device_box_name 归一。"""
    return {str(b.get('id') or ''): str(b.get('name') or '')
            for b in ((model or {}).get('boxes') or []) if isinstance(b, dict)}


# ------------------------------------------------ 分组层级（一级/二级）+ 固定组
# 1.5.6：固定组 = 任何用户命名/人工持有的组（预置 4 个只是出厂种子）。
# 判据落在 box['fixed']；重分组 / 自动初始分箱 / 候选聚类都不得改写固定组。
def _gcons_num(lo, hi, src, note=''):
    return {'on': True, 'min': lo, 'max': hi, 'src': src, 'note': note}


def _gcons_cat(vals, src, note=''):
    return {'on': True, 'vals': list(vals), 'src': src, 'note': note}


def _gcons_hour(lo, hi, src, note=''):
    return {'on': True, 'from': lo, 'to': hi, 'src': src, 'note': note}


_HOUR_NOTE = '时段由使用习惯决定，预置不作限制（推定）'
GROUP_PRESETS = (
    {'key': 'pad6s', 'id': 'GRP-PAD6S', 'name': '小米 Pad 6S Pro',
     'alias': ('小米pad6spro', 'xiaomipad6spro', 'pad6spro', '小米平板6spro'),
     'cons': {
         'peak_power_w': _gcons_num(20, 125, 'spec', '官方 120 W 有线快充；下限按快充启动功率留裕量'),
         'avg_power_w': _gcons_num(12, 100, 'est', '按恒流段与末段降功率估算'),
         'total_wh': _gcons_num(8, 40, 'spec', '电池 10000 mAh ≈ 38.7 Wh（官方）'),
         'duration_min': _gcons_num(25, 180, 'est', '按 10000 mAh / 120 W 与末段涓流估算'),
         'avg_voltage': _gcons_num(5, 21, 'spec', '120 W 档为 20 V 平台（官方）'),
         'avg_current': _gcons_num(0.2, 6.5, 'spec', '120 W ≈ 20 V × 6 A（官方）'),
         'ratio': _gcons_num(1.1, 3.5, 'est', '按峰值/平均功率经验估算'),
         'port': _gcons_cat(('C1', 'C2'), 'est', '常用 C 口，A 口不参与快充'),
         'hour': _gcons_hour(0, 23, 'est', _HOUR_NOTE)}},
    {'key': 'vivo', 'id': 'GRP-VIVO', 'name': 'vivo X200 Pro Mini',
     'alias': ('vivox200promini', 'vivox200promini手机', 'x200promini'),
     'cons': {
         'peak_power_w': _gcons_num(15, 100, 'spec', '官方 90 W 有线快充；上限留裕量'),
         'avg_power_w': _gcons_num(8, 80, 'est', '按恒流段与末段降功率估算'),
         'total_wh': _gcons_num(5, 23, 'spec', '电池 5700 mAh ≈ 21.78 Wh（官方）'),
         'duration_min': _gcons_num(15, 120, 'est', '按 5700 mAh / 90 W 估算'),
         'avg_voltage': _gcons_num(4.5, 12, 'spec', '90 W 档为 11 V 平台（官方）'),
         'avg_current': _gcons_num(0.2, 8.5, 'spec', '11 V × 8.2 A ≈ 90 W（官方）'),
         'ratio': _gcons_num(1.1, 3.2, 'est', '按峰值/平均功率经验估算'),
         'port': _gcons_cat(('C1', 'C2'), 'est', '常用 C 口'),
         'hour': _gcons_hour(0, 23, 'est', _HOUR_NOTE)}},
    {'key': 'watch', 'id': 'GRP-WATCH', 'name': '华为 Watch GT5',
     'alias': ('华为watchgt5', 'huaweiwatchgt5', 'watchgt5', '华为手表gt5'),
     'cons': {
         'peak_power_w': _gcons_num(0.5, 7, 'spec', '官方无线充电 5 W；上限留裕量'),
         'avg_power_w': _gcons_num(0.3, 5, 'est', '按小电流恒流段估算'),
         'total_wh': _gcons_num(0.2, 2.5, 'spec', '电池 524 mAh ≈ 2.0 Wh（官方）'),
         'duration_min': _gcons_num(40, 160, 'est', '官方约 100 分钟充满；区间留裕量'),
         'avg_voltage': _gcons_num(4.5, 6.5, 'est', '手表电池为单节平台，按 5 V 输入估算'),
         'avg_current': _gcons_num(0.05, 1.5, 'est', '按 5 W / 5 V 估算'),
         'ratio': _gcons_num(1.0, 4.0, 'est', '小电流长时充电，末段占比高'),
         'port': _gcons_cat(('C1', 'A'), 'est', '磁吸线可接 C 口或 A 口'),
         'hour': _gcons_hour(0, 23, 'est', _HOUR_NOTE)}},
    {'key': 'powerbank', 'id': 'GRP-PB', 'name': '充电宝（星源智控、小米）',
     'alias': ('充电宝星源智控小米', '充电宝', 'powerbank'),
     'cons': {
         'peak_power_w': _gcons_num(8, 105, 'est', '两款充电宝输入规格不同，取并集推定'),
         'avg_power_w': _gcons_num(4, 65, 'est', '按输入恒流段估算'),
         'total_wh': _gcons_num(8, 80, 'est', '按 10000–20000 mAh 容量段推定'),
         'duration_min': _gcons_num(30, 260, 'est', '按容量/输入功率估算'),
         'avg_voltage': _gcons_num(5, 21, 'est', '按 PD 5–20 V 输入推定'),
         'avg_current': _gcons_num(0.2, 5.5, 'est', '按 100 W / 20 V 上限估算'),
         'ratio': _gcons_num(1.2, 4.0, 'est', '按峰值/平均功率经验估算'),
         'port': _gcons_cat(('C1', 'C2'), 'est', '常用 C 口输入'),
         'hour': _gcons_hour(0, 23, 'est', _HOUR_NOTE)}},
)
UNASSIGNED = '__none__'      # manual 哨兵：人工移出分组，回到未归箱且不被自动判回
# 2.1.2：重新分组产出的两个「干净落点」——偏离项（明显偏离固定组特征）与
# 无法判定（模型/约束都给不出高置信归属）。它们都是普通组（非固定），下次
# 重新分组会连同未归箱一起重新判定；组成员靠 manual 钉住，保证条数可复核。
SPECIAL_ROLES = {'outlier': '偏离项', 'undecided': '无法判定'}
SPECIAL_IDS = {'outlier': 'GRP-OUTLIER', 'undecided': 'GRP-UNDECIDED'}


def group_special(box):
    """特殊落点角色：outlier / undecided / ''（普通组或固定组）。"""
    return str((box or {}).get('special') or '')


def _vec_ok(v):
    """特征向量是否可用：空字典/全零向量不能进训练集（会把 kNN 拉成噪声）。"""
    if not isinstance(v, dict) or not v:
        return False
    return any(abs(_f(x)) > 1e-9 for x in v.values())


def _gnorm(s):
    """名称归一：忽略空白/标点/大小写，用于重名与别名匹配。"""
    return re.sub(r'[\s_·・.,，。()（）\-—]', '', str(s or '')).lower()


def group_is_fixed(box):
    """固定组判据：用户命名/人工持有（fixed 标记或出厂种子）。"""
    return bool(isinstance(box, dict) and (box.get('fixed') is True or box.get('preset')))


def group_children(model, bid):
    return [b for b in ((model or {}).get('boxes') or [])
            if isinstance(b, dict) and str(b.get('parent') or '') == str(bid or '')]


def group_tree(model):
    """按一级 → 二级展开 [{box, depth, children}]，孤儿（父不存在）按一级处理。"""
    boxes = [b for b in ((model or {}).get('boxes') or []) if isinstance(b, dict)]
    ids = {str(b.get('id')) for b in boxes}
    tops = [b for b in boxes if str(b.get('parent') or '') not in ids]
    out = []
    for t in tops:
        out.append((t, 0))
        for c in group_children(model, t.get('id')):
            out.append((c, 1))
    return out


def group_cons_ready(box):
    """固定组是否可作「约束兜底」接收判定：固定、无形态中心、至少一项启用约束。

    出厂种子组（Pad / vivo / Watch / 充电宝）只有维度约束、没有曲线中心，旧逻辑里
    任何一条路径都不会命中它们，于是长期 0 条——这里给出明确的兜底条件。
    """
    if not group_is_fixed(box):
        return False
    sc = box.get('sc') if isinstance(box.get('sc'), dict) else {}
    if sc.get('mean'):
        return False
    cons = box.get('cons') or {}
    return any(isinstance(c, dict) and c.get('on') for c in cons.values())


def group_cons_score(s, box, enabled=True):
    """约束兜底的贴合度：约束全满足才返回 [0,1]（越小越贴合），否则 None。"""
    if not group_cons_ready(box) or not box_cons_ok(s, box, enabled):
        return None
    cons = box.get('cons') or {}
    acc, n = 0.0, 0
    for key, _lb, kind, _u in BOX_CONS_DIMS:
        c = cons.get(key)
        if not isinstance(c, dict) or not c.get('on') or kind != 'num':
            continue
        lo, hi, v = c.get('min'), c.get('max'), session_dim(s, key)
        if lo is None or hi is None or v is None:
            continue
        mid = (float(lo) + float(hi)) / 2.0
        half = max(1e-6, (float(hi) - float(lo)) / 2.0)
        acc += abs(float(v) - mid) / half
        n += 1
    return (acc / n) if n else 1.0


def group_cons_pick(s, boxes, enabled=True):
    """多个固定组都能接时取贴合度最高者；同分按分组顺序（确定性，避免随机归属）。"""
    best = ('', 0.0)
    for b in boxes or []:
        if not isinstance(b, dict):
            continue
        sc = group_cons_score(s, b, enabled)
        if sc is None:
            continue
        if not best[0] or sc < best[1]:
            best = (str(b.get('id') or ''), sc)
    return best


def group_resolve(s, model, sens='均衡', manual=None, shapes=None, thr=None,
                  cons=True, learn=None, detail=False):
    """会话归属的唯一口径（卡片计数 / 组内会话 / 未归箱 / 分析页全部走这里）。

    顺序：人工 > 形态就近 > 学习模型 > 规则相似度 > 固定组约束兜底。
    返回箱 id（未归箱为 ''）；detail=True 时返回 (id, 来源, 分值)。
    """
    _none = ('', '', 0.0)
    boxes = [b for b in ((model or {}).get('boxes') or []) if isinstance(b, dict)]
    if not boxes:
        return _none if detail else ''
    ids = {str(b.get('id')) for b in boxes}
    _bmap = {str(b.get('id')): b for b in boxes}
    _manual = manual if isinstance(manual, dict) else ((model or {}).get('manual') or {})
    k = session_key(s)
    mk = str(_manual.get(k) or '')
    if mk == UNASSIGNED:
        return _none if detail else ''
    if mk in ids:
        return (mk, 'manual', 1.0) if detail else mk
    _sh = (shapes or {}).get(k)
    if _sh:
        nb, nd, nthr = box_nearest(cluster_vec(s, _sh), model, s, thr)
        if nb in ids and nd <= nthr:
            # 置信度统一为「越大越好」，供面板的置信度区间筛选使用
            _cf = max(0.0, 1.0 - nd / max(nthr, 1e-9))
            return (nb, 'shape', _cf) if detail else nb
    _learn = learn if learn is not None else learn_load()
    if _learn:
        lb, lc = learn_predict(learn_feats(s), _learn)
        if lb in ids and lc >= LEARN_MIN_CONF and box_cons_ok(s, _bmap.get(lb), cons):
            return (lb, 'learn', lc) if detail else lb
    v = dev_vec(s)
    eps = DEV_EPS.get(sens, DEV_EPS['均衡'])
    best, best_sc = '', 0.0
    for b in boxes:
        c = b.get('center')
        if not isinstance(c, dict) or not c:
            continue
        if not box_cons_ok(s, b, cons):
            continue
        sc = dev_sim(v, c)
        if sc > best_sc:
            best, best_sc = str(b.get('id')), sc
    if best and best_sc >= eps:
        return (best, 'rule', best_sc) if detail else best
    cb, cs = group_cons_pick(s, boxes, cons)
    if cb:
        return (cb, 'cons', 1.0 - cs) if detail else cb
    return _none if detail else ''


def group_members(model, bid, rows, shapes=None, thr=None, cons=True,
                  sens='均衡'):
    """组内会话：与 device_assign 同一口径（人工 > 形态 > 学习 > 规则 > 约束兜底）。"""
    _boxes = [b for b in ((model or {}).get('boxes') or []) if isinstance(b, dict)]
    if not any(str(b.get('id')) == str(bid) for b in _boxes):
        return []
    _learn = learn_load()
    out = []
    for s in rows or []:
        if group_resolve(s, model, sens, shapes=shapes, thr=thr, cons=cons,
                         learn=_learn) == str(bid):
            out.append(s)
    return out


def _prune_preset_cons(box, preset):
    """删掉「系统预置且用户没动过」的维度约束；有删除返回 True。

    判定：该维与出厂预置值完全相等，且 src 不是 user。值不同或 src=user
    一律视为用户设过，保留（分不清按用户设过处理）。
    """
    _cons = box.get('cons')
    if not isinstance(_cons, dict) or not _cons:
        return False
    _pre = preset.get('cons') or {}
    _drop = [k for k, v in _cons.items()
             if isinstance(v, dict) and k in _pre and v == _pre[k]
             and str(v.get('src') or '') != 'user']
    if not _drop:
        return False
    for k in _drop:
        _cons.pop(k, None)
    if _cons:
        box['cons'] = _cons
    else:
        box.pop('cons', None)
    return True


def group_seed(prefs):
    """幂等建出 4 个出厂固定组；已有同名/同 preset 组则收养。

    约束一律不默认：建组不写 cons，收养也不补齐缺失维度；只维护 preset/fixed
    标记，并清理历史版本写回的系统预置约束（用户动过的不清）。
    """
    m = device_model_load(prefs) or {}
    boxes = [b for b in (m.get('boxes') or []) if isinstance(b, dict)]
    _changed = False
    for b in boxes:                       # 仅旧模型补 fixed 标记（缺 fixed 字段的用户组）
        if 'fixed' not in b and str(b.get('src') or '') in ('manual', 'user', 'preset'):
            b['fixed'] = True
            _changed = True
    created, adopted = [], []
    for p in GROUP_PRESETS:
        _hit = next((b for b in boxes if str(b.get('preset') or '') == p['key']), None)
        if _hit is None:
            _hit = next((b for b in boxes
                          if _gnorm(b.get('name')) in p['alias']
                          or _gnorm(device_box_name(b.get('id'), b.get('name'))) in p['alias']),
                         None)
        if _hit is not None:
            if not _hit.get('preset') or not _hit.get('fixed'):
                _hit['preset'] = p['key']
                _hit['fixed'] = True
                _hit.setdefault('src', 'preset')
                _changed = True
                adopted.append((_hit.get('id'), p['name']))
            if _prune_preset_cons(_hit, p):
                _changed = True
            continue
        box = {'id': p['id'], 'name': p['name'], 'parent': '', 'fixed': True,
               'preset': p['key'], 'src': 'preset', 'n': 0, 'review': False,
               'move': 0.0, 'prev': '',
               'at': time.strftime('%Y-%m-%d %H:%M')}
        boxes.append(box)
        created.append((p['id'], p['name']))
        _changed = True
    if created or adopted or _changed:
        m['boxes'] = boxes
        m.setdefault('manual', {})
        m.setdefault('manual_meta', {})
        device_save(prefs, m)
    if created or adopted:
        tm = learn_apply([], 'seed')
        learn_log('GROUP-SEED created=%s adopted=%s v%s' % (
            created, adopted, tm.get('version')))
    return {'ok': True, 'created': created, 'adopted': adopted,
            'boxes': len(boxes)}


def _new_group_id(boxes, prefix='G'):
    _used = {str(b.get('id') or '') for b in boxes}
    i = 1
    while ('%s-%04d' % (prefix, i)) in _used:
        i += 1
    return '%s-%04d' % (prefix, i)


def group_mutate(prefs, op, bid=None, name=None, parent='', keys=None,
                 target=None, cons=None, confirm=False, cvecs=None, feats=None,
                 source='user', n_full=30, bids=None, n_total=None, rows=None):
    """分组结构/成员变更的唯一入口（建/改名/删/迁移/移出/锁定/约束/合并）。

    每次操作都触发一次模型更新：结构变更（建组/合并/删组）或影响条数 > n_full
    走全量重训，其余走增量；审计写 op、影响条数、模式、version 前后与耗时。
    cvecs/feats：{会话键: 37 维形态 / 10 维特征}，由界面层从曲线与会话算好传入。
    返回 {'ok','msg','affected','mode','v0','version','dur_ms'}。
    """
    m = device_model_load(prefs) or {}
    boxes = [b for b in (m.get('boxes') or []) if isinstance(b, dict)]
    manual = dict(m.get('manual') or {})
    meta = dict(m.get('manual_meta') or {})
    by_id = {str(b.get('id') or ''): b for b in boxes}
    at = time.strftime('%Y-%m-%d %H:%M')
    keys = [str(k) for k in (keys or [])]
    cvecs = cvecs or {}
    feats = feats or {}
    _tm = None
    affected = 0
    _drop = []
    _nm_tag = ''

    def _fail(msg):
        return {'ok': False, 'msg': msg, 'affected': 0, 'mode': '',
                'v0': 0, 'version': 0, 'dur_ms': 0}

    if op == 'seed':
        return group_seed(prefs)
    if op == 'create':
        _nm = str(name or '').strip()
        if not _nm:
            return _fail('分组名不能为空')
        if any(_gnorm(b.get('name')) == _gnorm(_nm) for b in boxes):
            return _fail('分组名「%s」已存在' % _nm)
        _cap = device_cap(prefs)['cap']
        if len(boxes) + 1 > _cap:
            return _fail('分组总数已达上限（当前 %d 个 / 上限 %d 个）：'
                         '请先删除或合并分组，或在「进阶配置 → 设备识别 → 设备分组上限」调高上限'
                         % (len(boxes), _cap))
        _p = str(parent or '')
        if _p:
            if _p not in by_id:
                return _fail('上级分组不存在')
            if str(by_id[_p].get('parent') or ''):
                return _fail('二级分组下不能再建子组（最多两级）')
        box = {'id': _new_group_id(boxes, 'G'), 'name': _nm, 'parent': _p,
               'fixed': False, 'src': 'user', 'n': 0, 'review': False,
               'move': 0.0, 'prev': '', 'at': at}
        boxes.append(box)
        affected = 1
    elif op == 'rename':
        b = by_id.get(str(bid or ''))
        if b is None:
            return _fail('分组不存在')
        _nm = str(name or '').strip()
        if not _nm:
            return _fail('分组名不能为空')
        if any(x is not b and _gnorm(x.get('name')) == _gnorm(_nm) for x in boxes):
            return _fail('分组名「%s」已存在' % _nm)
        b['name'] = _nm
        # 1.5.8：改名不再隐式升级为固定组（非固定组改名后仍可被重新分组改写）
        b['src'] = 'user'
        affected = 1
    elif op == 'delete':
        b = by_id.get(str(bid or ''))
        if b is None:
            return _fail('分组不存在')
        if group_is_fixed(b) and not confirm:
            return _fail('「%s」是固定组，删除前请确认（不会被重新分组改写）' % b.get('name'))
        _del = [b] + [c for c in group_children(m, b.get('id'))]
        _ids = {str(x.get('id')) for x in _del}
        for k, v in list(manual.items()):
            if str(v) in _ids:
                manual.pop(k, None)
                meta.pop(k, None)
                affected += 1
        boxes = [x for x in boxes if str(x.get('id')) not in _ids]
        _drop = list(_ids)
    elif op == 'move':
        _t = by_id.get(str(target or ''))
        if _t is None:
            return _fail('目标分组不存在')
        for k in keys:
            _old = str(manual.get(k) or '')
            if _old == str(target):
                continue
            manual[k] = str(target)
            meta[k] = {'at': at, 'by': 'user', 'src': str(source or 'grpmove'),
                       'to': str(target)}
            _cv = cvecs.get(k)
            if _cv and _old in by_id:
                box_shape_del(by_id[_old], _cv)
            if _cv:
                box_shape_add(_t, _cv)
            affected += 1
        if affected == 0:
            return _fail('所选会话已在该分组内')
    elif op == 'remove':
        for k in keys:
            _old = str(manual.get(k) or '')
            if _old in by_id and cvecs.get(k):
                box_shape_del(by_id[_old], cvecs[k])
            manual[k] = UNASSIGNED           # 哨兵：回到未归箱且不被自动判回
            meta[k] = {'at': at, 'by': 'user', 'src': 'remove', 'to': ''}
            affected += 1
        if affected == 0:
            return _fail('未选择会话')
    elif op in ('lock', 'unlock'):
        b = by_id.get(str(bid or ''))
        if b is None:
            return _fail('分组不存在')
        if op == 'unlock' and b.get('preset') and not confirm:
            return _fail('出厂固定组解锁需确认')
        b['fixed'] = op == 'lock'
        affected = 1
    elif op == 'cons':
        b = by_id.get(str(bid or ''))
        if b is None:
            return _fail('分组不存在')
        if cons:
            b['cons'] = cons
        else:
            b.pop('cons', None)      # 清空即清空：任何组都不回填预置区间
        affected = 1
    elif op == 'merge':
        _ids = [str(x) for x in (bids or ([bid, target] if target else [bid]))
                if str(x)]
        _sel = []
        for _i in _ids:
            _bx = by_id.get(_i)
            if _bx is None:
                return _fail('分组不存在：%s' % _i)
            if _bx not in _sel:
                _sel.append(_bx)
        if len(_sel) < 2:
            return _fail('请选择至少两个分组再合并')
        _cids = [str(x.get('id')) for x in _sel]
        _keep = _sel[0]
        _kid = str(_keep.get('id'))
        # 结构合并：界面层把参与合并各箱的成员（统一口径解析结果）全部传进来，
        # 逐条钉到保留组。这样合并是「并集」，不会因中心变化把既有归属改判走。
        _pins = []
        for k in keys:
            if k and k not in _pins:
                _pins.append(k)
        _total = sum(int(x.get('n') or 0) for x in _sel)
        if _pins:
            _total = max(_total, len(_pins))
        if n_total:                 # 界面层给的是实时成员数，优先用于「共 N 条」
            _total = max(_total, int(n_total))
        # 一次性撤销快照（只保留最近一次合并）：合并前的箱定义 + 成员人工归属
        m['merge_undo'] = {
            'at': at, 'keep': _kid,
            'boxes': [copy.deepcopy(x) for x in _sel],
            'manual': {k: str(v) for k, v in manual.items() if str(v) in _cids},
            'meta': {k: copy.deepcopy(meta[k]) for k in manual
                     if str(manual.get(k)) in _cids and k in meta},
            # 本次新钉入的键（原先没有人工归属）：撤销时全部摘掉，恢复自动判定
            'pinned': [k for k in _pins if k not in manual],
            'ids': _cids}
        for k in _pins:
            _old = str(manual.get(k) or '')
            manual[k] = _kid
            if _old in _cids:
                meta[k] = dict(meta.get(k) or {}, to=_kid)
            else:
                meta[k] = {'at': at, 'by': 'merge', 'src': 'merge', 'to': _kid}
            affected += 1
        for _bx in _sel[1:]:
            _oid = str(_bx.get('id'))
            if not _pins:           # 未传成员（旧调用方）：退回只搬人工归属
                for k, v in list(manual.items()):
                    if str(v) == _oid:
                        manual[k] = _kid
                        meta[k] = dict(meta.get(k) or {}, src='merge', at=at, to=_kid)
                        affected += 1
            for _ch in group_children(m, _oid):     # 子组挂到保留组下
                _ch['parent'] = _kid
            box_shape_merge(_keep, _bx)
            _keep['fixed'] = bool(group_is_fixed(_keep) or group_is_fixed(_bx))
            boxes = [x for x in boxes if str(x.get('id')) != _oid]
            _drop.append(_oid)
        _keep['src'] = 'manual'
        if str(name or '').strip():
            _keep['name'] = str(name).strip()
            _keep['fixed'] = True
        affected = max(affected, _total, len(_sel))
        _nm_tag = ' names=' + '、'.join(str(x.get('name') or x.get('id'))
                                       for x in _sel)
        if _pins:
            _nm_tag += ' pins=%d' % len(_pins)
    elif op == 'merge_undo':
        _u = m.get('merge_undo')
        if not isinstance(_u, dict) or not _u.get('boxes'):
            return _fail('没有可撤销的合并')
        _kid = str(_u.get('keep') or '')
        boxes = [x for x in boxes if str(x.get('id')) != _kid]
        boxes.extend(copy.deepcopy(x) for x in _u['boxes'])
        for k, v in (_u.get('manual') or {}).items():
            manual[k] = str(v)
            affected += 1
        for k in (_u.get('pinned') or []):      # 合并时新钉入的键：撤销即摘掉，恢复自动
            manual.pop(k, None)
            meta.pop(k, None)
            affected += 1
        for k, v in (_u.get('meta') or {}).items():
            meta[k] = copy.deepcopy(v)
        _drop = [_kid]
        m.pop('merge_undo', None)
    else:
        return _fail('未知操作：%s' % op)

    m['boxes'] = boxes
    m['manual'] = manual
    m['manual_meta'] = meta
    device_save(prefs, m)
    _ch = []
    _vec = {}
    for _sm in (learn_load() or {}).get('samples') or []:
        if isinstance(_sm, dict):
            _vec[str(_sm.get('key'))] = _sm.get('vec') or {}
    _rowmap = {}
    for _s in rows or []:
        if isinstance(_s, dict):
            _rowmap[session_key(_s)] = _s

    def _feat_of(k):
        _f0 = feats.get(k)
        if not _vec_ok(_f0):
            _s0 = _rowmap.get(k)
            if _s0 is not None:
                _f0 = learn_feats(_s0)
        if not _vec_ok(_f0):
            _f0 = _vec.get(k) or {}
        return _f0

    if op in ('move', 'remove'):
        for k in keys:
            _b = str(manual.get(k) or '')
            if op == 'remove':
                _ch.append((k, None, _feat_of(k), cvecs.get(k), 1.0))
            elif _b:
                _ch.append((k, _b, _feat_of(k), cvecs.get(k), 1.0))
    try:
        _tm = learn_apply(_ch, op, drop_bids=_drop or None, n_full=n_full)
    except Exception as exc:                      # 训练失败不阻断结构变更（已落盘）
        learn_log('GROUP-TRAIN-FAIL op=%s n=%d err=%r' % (op, affected, exc))
        _tm = {'ok': False,
               'n_train': int((learn_load() or {}).get('n_train') or 0),
               'version': int((learn_load() or {}).get('version') or 0),
               'mode': '', 'dur_ms': 0, 'v0': 0}
    learn_log('GROUP op=%s bid=%s target=%s bids=%s affected=%d mode=%s'
              ' n_train=%s v%s→%s dur=%dms%s'
              % (op, bid or '-', target or '-', ','.join(
                  str(x) for x in (bids or [])) or '-', affected,
                 _tm.get('mode') or '-', _tm.get('n_train', '-'),
                 _tm.get('v0'), _tm.get('version'),
                 int(_tm.get('dur_ms') or 0), _nm_tag))
    return {'ok': True, 'msg': '', 'affected': affected,
            'mode': _tm.get('mode') or '', 'v0': int(_tm.get('v0') or 0),
            'version': int(_tm.get('version') or 0),
            'n_train': int(_tm.get('n_train') or 0),
            'dur_ms': int(_tm.get('dur_ms') or 0),
            'boxes': len(boxes)}


def device_assign(sessions, model, sens='均衡', manual=None, shapes=None,
                  thr=None, cons=True):
    """常态路径：只做就近归箱，不重算中心；认不出的进「未归箱」，不硬塞。

    归属判定统一走 group_resolve（人工 > 形态 > 学习 > 规则 > 固定组约束兜底），
    与卡片计数、组内会话同源，不会出现「卡片 0 条 / 组内 300 条」。
    """
    boxes = [b for b in ((model or {}).get('boxes') or []) if isinstance(b, dict)]
    eps = DEV_EPS.get(sens, DEV_EPS['均衡'])
    manual = manual if isinstance(manual, dict) else ((model or {}).get('manual') or {})
    groups = [{'id': b.get('id'), 'name': b.get('name') or b.get('id'),
               'center': b.get('center') or {}, 'n': 0, 'manual': 0,
               'parent': str(b.get('parent') or ''), 'fixed': group_is_fixed(b)}
              for b in boxes]
    by_id = {str(g['id']): g for g in groups}
    unassigned = 0
    hit = [0.0] * len(groups)
    _learn = learn_load()
    for s in sessions or []:
        if not isinstance(s, dict):
            continue
        _bid, _src, _sc = group_resolve(s, model, sens, manual=manual,
                                        shapes=shapes, thr=thr, cons=cons,
                                        learn=_learn, detail=True)
        if _bid not in by_id:
            unassigned += 1
            continue
        g = by_id[_bid]
        g['n'] += 1
        if _src == 'manual':
            g['manual'] += 1
            _w = 1.0
        elif _src == 'shape':
            g['cluster'] = int(g.get('cluster') or 0) + 1
            _w = max(0.0, 1.0 - float(_sc))
        elif _src == 'learn':
            g['learn'] = int(g.get('learn') or 0) + 1
            _w = float(_sc)
        else:
            g['rule'] = int(g.get('rule') or 0) + 1
            _w = float(_sc)
        hit[groups.index(g)] += _w
    for i, g in enumerate(groups):
        g['confidence'] = (round(hit[i] / g['n'], 2) if g['n'] else 0.0)
    return {'ok': True, 'eps': eps, 'sens': sens, 'groups': groups,
            'unassigned': unassigned, 'manual_total': len(manual)}


def _kmeans(vecs, k, iters=40):
    """确定性 k-means（k-means++ 播种，固定 DEV_SEED，纯 Python）。"""
    import random
    rnd = random.Random(DEV_SEED)
    centers = [dict(vecs[rnd.randrange(len(vecs))])]
    while len(centers) < k:
        d2 = [min(_dev_dist(v, c) for c in centers) ** 2 for v in vecs]
        tot = sum(d2)
        if tot <= 1e-12:
            return centers
        r = rnd.random() * tot
        acc, pick = 0.0, len(vecs) - 1
        for i, x in enumerate(d2):
            acc += x
            if acc >= r:
                pick = i
                break
        centers.append(dict(vecs[pick]))
    for _ in range(iters):
        buckets = [[] for _ in centers]
        for v in vecs:
            j = min(range(len(centers)), key=lambda t: _dev_dist(v, centers[t]))
            buckets[j].append(v)
        new = []
        for j, b in enumerate(buckets):
            if b:
                new.append({d: sum(x[d] for x in b) / len(b) for d in _DEV_DIM})
            else:
                far = max(vecs, key=lambda v: min(_dev_dist(v, c) for c in centers))
                new.append(dict(far))
        if all(_dev_dist(a, b) < 1e-9 for a, b in zip(centers, new)):
            centers = new
            break
        centers = new
    return centers


def _fresh_dev_name(used_names):
    """生成不与现有组重名的默认名（设备 N）。"""
    _j = 1
    while _gnorm('设备 %d' % _j) in used_names:
        _j += 1
    used_names.add(_gnorm('设备 %d' % _j))
    return '设备 %d' % _j


def device_build(sessions, k=3, sens='均衡', old=None, max_boxes=None,
                 shapes=None, thr=None, cons=True, train=True):
    """重新分组（2.1.2 语义）：固定组只增不改，非固定组可重排，偏离/无法判定单列。

    1. 固定组成员按当前模型冻结（全部钉进 manual），名称/中心/约束一律不改，
       任何路径都删改不了已有成员；
    2. 未归箱 + 非固定组旧成员作为待判池，用「固定组成员现训 kNN + 全库学习模型
       + 固定组约束」三路判定，只允许往固定组新增成员；
    3. 明显偏离固定组特征（到固定组成员中心 > 2.2×P90）的进「偏离项」；
       两路模型冲突且约束无法裁决的进「无法判定」；两者都是普通组，下次重分组
       会连未归箱一起重判，不会长期沉淀；
    4. 其余记录按旧非固定组数重聚类，旧组按最近中心一对一迁移名字（可并入、
       可新建），样本不足时如实保留旧组并说明；
    5. 用最终归属全量重训，返回前后条数、固定组是否零删除、置信度变化。
    """
    rows = [s for s in sessions or [] if isinstance(s, dict)]
    try:
        k = max(2, min(8, int(k or 3)))
    except Exception:
        k = 3
    if thr is None:
        thr = 1.6
    eps = DEV_EPS.get(sens, DEV_EPS['均衡'])
    ob = [b for b in ((old or {}).get('boxes') or []) if isinstance(b, dict)]
    fixed = [b for b in ob if group_is_fixed(b)]
    norm0 = [b for b in ob if not group_is_fixed(b) and not group_special(b)]
    fid = {str(b.get('id') or '') for b in fixed}
    box_by_id = {str(b.get('id') or ''): b for b in fixed}
    manual0 = dict((old or {}).get('manual') or {})
    old_model = old if isinstance(old, dict) and ob else {'boxes': ob}
    _learn0 = learn_load() or {}
    _cap = max(DEV_CAP_MIN, min(DEV_CAP_MAX, int(max_boxes or DEV_MAX_BOXES)))
    if _cap - len(fixed) < 1 and any(session_key(s) not in manual0
                                     for s in rows):
        return {'ok': False, 'eps': eps, 'k': 0, 'max_boxes': _cap,
                'msg': '分组总数已达上限（当前固定组 %d 个 / 上限 %d 个）：'
                       '请先删除或合并分组，或在「进阶配置 → 设备识别 → 设备分组上限」调高上限'
                       % (len(fixed), _cap)}
    at = datetime.now().strftime('%Y-%m-%d %H:%M')
    by_key = {session_key(s): s for s in rows}

    # ---- 1. 冻结固定组：当前模型判进固定组的记录全部算成员，人工移出的保持移出
    before_of = {}
    fix_sets = {bid: set() for bid in fid}
    kept_out = {}
    pool = []
    for s in rows:
        kk = session_key(s)
        _b = group_resolve(s, old_model, sens, manual=manual0, shapes=shapes,
                           thr=thr, cons=cons, learn=_learn0)
        before_of[kk] = _b or ''
        if _b in fid:
            fix_sets[_b].add(kk)
        elif str(manual0.get(kk) or '') == UNASSIGNED:
            kept_out[kk] = s
        else:
            pool.append(s)

    # 固定组成员中心 / 离散度：用于「偏离项」判定（成员少于 5 条不判偏离）
    fix_stats = {}
    fix_feats = []
    for b in fixed:
        _bid = str(b.get('id') or '')
        _keys = fix_sets.get(_bid) or set()
        _vs = [dev_vec(by_key[x]) for x in _keys if x in by_key]
        if not _vs:
            continue
        _c = dev_center(_vs)
        _ds = sorted(_dev_dist(v, _c) for v in _vs)
        _p90 = _ds[min(len(_ds) - 1, int(math.ceil(len(_ds) * 0.9)) - 1)]
        fix_stats[_bid] = {'center': _c, 'p90': _p90, 'n': len(_vs),
                           'out': max(0.18, round(2.2 * _p90, 4))}
        for x in _keys:
            _s = by_key.get(x)
            if _s is not None:
                fix_feats.append({'key': x, 'bid': _bid,
                                  'vec': learn_feats(_s), 'w': 1.0})
    _fcnt = {}
    for p in fix_feats:
        _fcnt[p['bid']] = _fcnt.get(p['bid'], 0) + 1
    _fsamples = [p for p in fix_feats
                 if _fcnt.get(p['bid'], 0) >= LEARN_MIN_PER_CLASS
                 and _vec_ok(p['vec'])]

    # ---- 2. 三路判定：只往固定组「新增」，冲突/偏离分别落袋
    add_fixed = {bid: [] for bid in fid}
    outlier_keys, undecided_keys, leftover = [], [], []
    for s in pool:
        kk = session_key(s)
        feat = learn_feats(s)
        hits = [str(b.get('id') or '') for b in fixed
                if box_cons_ok(s, b, cons)]
        _lf, _lcf, _ = _knn_vote(feat, _fsamples, skip_key=kk)
        _lg, _lgc = learn_predict(feat, _learn0, skip_key=kk)
        votes = []
        if _lf in fid and _lcf >= LEARN_MIN_CONF:
            votes.append(_lf)
        if _lg in fid and _lgc >= LEARN_MIN_CONF and box_cons_ok(s, box_by_id[_lg], cons):
            votes.append(_lg)
        _uniq = sorted(set(votes))
        _tgt = ''
        if len(_uniq) == 1 and (not hits or _uniq[0] in hits):
            _tgt = _uniq[0]
        elif len(_uniq) == 1 and hits:
            _tgt = hits[0] if len(hits) == 1 else ''
        elif len(_uniq) > 1:
            # 多路冲突：唯一硬约束能裁决就用它，否则不硬塞
            _tgt = hits[0] if len(hits) == 1 else ''
            if not _tgt:
                undecided_keys.append(kk)
                continue
        elif len(hits) == 1:
            _tgt = hits[0]
        if not _tgt:
            leftover.append(s)
            continue
        _st = fix_stats.get(_tgt)
        if _st and _st['n'] >= 5 and _dev_dist(dev_vec(s), _st['center']) > _st['out']:
            outlier_keys.append(kk)          # 明显偏离固定组特征：剔除，不硬塞
            continue
        add_fixed[_tgt].append(kk)

    # ---- 3. 非固定组重排：旧组数决定箱数，名字按最近中心迁移，可并入/新建
    _special_n = (1 if outlier_keys else 0) + (1 if undecided_keys else 0)
    _max_non = max(1, _cap - len(fixed) - _special_n)
    _want = len(norm0) if norm0 else max(2, min(4, len(leftover) // 3 or 2))
    _kk = max(1, min(8, int(k or 3), _max_non, max(1, _want)))
    new_boxes = []
    cluster_members = {}
    merged_old = []
    if leftover:
        if len(leftover) < max(3, _kk):
            # 样本不足：能并进旧非固定组就并，否则无法判定（不造空箱）
            _oc = []
            for b in norm0:
                _mem = [by_key[x] for x, v in manual0.items()
                        if str(v) == str(b.get('id')) and x in by_key]
                _vs = [dev_vec(x) for x in _mem]
                _c = dev_center(_vs) if _vs else (b.get('center') or {})
                if isinstance(_c, dict) and _c:
                    _oc.append((b, _c))
            if _oc:
                for s in leftover:
                    b, _c = min(_oc, key=lambda t: _dev_dist(dev_vec(s), t[1]))
                    cluster_members.setdefault(str(b.get('id') or ''), []).append(
                        session_key(s))
                    if str(b.get('id') or '') not in [x.get('id') for x in new_boxes]:
                        _nb = copy.deepcopy(b)
                        _nb['n'] = 0
                        _nb['src'] = 'regroup'
                        new_boxes.append(_nb)
                merged_old = [str(b.get('id') or '') for b in norm0
                              if str(b.get('id') or '') not in cluster_members]
            else:
                undecided_keys.extend(session_key(s) for s in leftover)
        else:
            centers = _kmeans([dev_vec(s) for s in leftover], _kk)
            buckets = [[] for _ in centers]
            for s in leftover:
                _j = min(range(len(centers)),
                         key=lambda t: _dev_dist(dev_vec(s), centers[t]))
                buckets[_j].append(s)
            _cl = [(c, m) for c, m in zip(centers, buckets) if m]
            # 旧组中心（成员均值优先，退回保存的中心）
            _old = []
            for b in norm0:
                _bid = str(b.get('id') or '')
                _mem = [by_key[x] for x, v in manual0.items()
                        if str(v) == _bid and x in by_key]
                _vs = [dev_vec(x) for x in _mem]
                _c = dev_center(_vs) if _vs else (b.get('center') or {})
                if isinstance(_c, dict) and _c:
                    _old.append((b, _c))
            _pairs = sorted(((_dev_dist(oc, c), oi, ni)
                             for oi, (obx, oc) in enumerate(_old)
                             for ni, (c, mem) in enumerate(_cl)),
                            key=lambda x: x[0])
            _uo, _un = set(), set()
            used_names = {_gnorm(x.get('name')) for x in ob}
            _slots = [None] * len(_cl)      # 按簇索引预占，避免「匹配先后的 append」错位
            for d, oi, ni in _pairs:
                if oi in _uo or ni in _un:
                    continue
                _uo.add(oi)
                _un.add(ni)
                obx = _old[oi][0]
                _bid = str(obx.get('id') or '')
                _name = str(obx.get('name') or '') or _fresh_dev_name(used_names)
                used_names.add(_gnorm(_name))
                _slots[ni] = {'id': _bid, 'name': _name, 'parent': '',
                              'center': _cl[ni][0], 'fixed': False,
                              'src': 'regroup', 'n': len(_cl[ni][1]),
                              'review': bool(d > DEV_MOVE), 'move': round(d, 3),
                              'prev': _bid, 'at': at}
                cluster_members[_bid] = [session_key(x) for x in _cl[ni][1]]
            for ni, (c, mem) in enumerate(_cl):
                if _slots[ni] is not None:
                    continue
                _name = _fresh_dev_name(used_names)
                _used_ids = (fid | {b.get('id') for b in new_boxes}
                             | {b.get('id') for b in _slots if b})
                _bid = _new_group_id([{'id': x} for x in _used_ids], 'DEV')
                _slots[ni] = {'id': _bid, 'name': _name, 'parent': '',
                              'center': c, 'fixed': False, 'src': 'regroup',
                              'n': len(mem), 'review': True, 'move': 0.0,
                              'prev': '', 'at': at}
                cluster_members[_bid] = [session_key(x) for x in mem]
            new_boxes.extend([b for b in _slots if b is not None])
            # 旧组没被任何新中心接住 = 已并入其他非固定组 / 已被固定组吸收
            _used_old = {str(_old[oi][0].get('id') or '') for oi in _uo}
            merged_old = [str(b.get('id') or '') for b in norm0
                          if str(b.get('id') or '') not in _used_old]
    else:
        merged_old = [str(b.get('id') or '') for b in norm0]

    # 同名去重：旧模型里可能已有两个「设备 1」，重排后保留先出现者，其余换默认名
    _seen_nm = {_gnorm(b.get('name')) for b in fixed}
    for nb in new_boxes:
        _nm = str(nb.get('name') or '')
        if not _nm or _gnorm(_nm) in _seen_nm:
            nb['name'] = _fresh_dev_name(_seen_nm)
        else:
            _seen_nm.add(_gnorm(_nm))

    # ---- 4. 成员钉死 + 固定组只增不改
    manual = {}
    meta = {}
    for bid in fid:
        for kk in fix_sets[bid]:
            manual[kk] = bid
            meta[kk] = {'at': at, 'by': 'regroup', 'src': 'regroup', 'to': bid}
        for kk in add_fixed.get(bid, []):
            manual[kk] = bid
            meta[kk] = {'at': at, 'by': 'regroup', 'src': 'regroup-add', 'to': bid}
    for kk in kept_out:
        manual[kk] = UNASSIGNED
    for bid, keys in cluster_members.items():
        for kk in keys:
            manual[kk] = bid
            meta[kk] = {'at': at, 'by': 'regroup', 'src': 'regroup', 'to': bid}
    for kk in outlier_keys:
        manual[kk] = SPECIAL_IDS['outlier']
        meta[kk] = {'at': at, 'by': 'regroup', 'src': 'outlier',
                    'to': SPECIAL_IDS['outlier']}
    for kk in undecided_keys:
        manual[kk] = SPECIAL_IDS['undecided']
        meta[kk] = {'at': at, 'by': 'regroup', 'src': 'undecided',
                    'to': SPECIAL_IDS['undecided']}

    boxes = []
    for b in fixed:
        nb = copy.deepcopy(b)
        _bid = str(b.get('id') or '')
        nb['n'] = len(fix_sets.get(_bid) or set()) + len(add_fixed.get(_bid, []))
        boxes.append(nb)
    for nb in new_boxes:
        nb['n'] = len(cluster_members.get(str(nb.get('id') or ''), []))
        boxes.append(nb)
    if outlier_keys:
        boxes.append({'id': SPECIAL_IDS['outlier'], 'name': SPECIAL_ROLES['outlier'],
                      'parent': '', 'fixed': False, 'special': 'outlier',
                      'src': 'regroup', 'n': len(outlier_keys), 'review': False,
                      'move': 0.0, 'prev': '', 'at': at})
    if undecided_keys:
        boxes.append({'id': SPECIAL_IDS['undecided'],
                      'name': SPECIAL_ROLES['undecided'],
                      'parent': '', 'fixed': False, 'special': 'undecided',
                      'src': 'regroup', 'n': len(undecided_keys), 'review': False,
                      'move': 0.0, 'prev': '', 'at': at})

    # ---- 5. 全量重训 + 前后指标（固定组成员标签不变，置信度可直接对比）
    def _cvec(s):
        _sh = (shapes or {}).get(session_key(s))
        return cluster_vec(s, _sh) if _sh else None

    changes = []
    for bid in fid:
        for kk in list(fix_sets[bid]) + list(add_fixed.get(bid, [])):
            _s = by_key.get(kk)
            if _s is not None:
                changes.append((kk, bid, learn_feats(_s), _cvec(_s), 1.0))
    for bid, keys in cluster_members.items():
        for kk in keys:
            _s = by_key.get(kk)
            if _s is not None:
                changes.append((kk, bid, learn_feats(_s), _cvec(_s), 0.6))
    if train:
        try:
            _tm = learn_apply(changes, 'regroup', full=True)
        except Exception as exc:
            learn_log('REGROUP-TRAIN-FAIL n=%d err=%r' % (len(changes), exc))
            _tm = _learn0
    else:
        _tm = _learn0

    def _conf_match(model, pairs):
        vals = []
        for kk, bid in pairs:
            _s = by_key.get(kk)
            if _s is None:
                continue
            pb, pc = learn_predict(learn_feats(_s), model, skip_key=kk)
            vals.append(float(pc) if pb == bid else 0.0)
        return round(sum(vals) / len(vals), 3) if vals else 0.0

    _fix_pairs = [(kk, bid) for bid in fid for kk in fix_sets[bid]]
    _all_pairs = _fix_pairs + [(kk, bid) for bid, keys in cluster_members.items()
                               for kk in keys]
    _before = {}
    for kk in before_of:
        _before[before_of[kk]] = _before.get(before_of[kk], 0) + 1
    _after = {}
    for kk, bid in manual.items():
        if bid != UNASSIGNED:
            _after[str(bid)] = _after.get(str(bid), 0) + 1
    _kept_out_n = sum(1 for kk in kept_out)
    _fixed_after = {bid: set() for bid in fid}
    for kk, bid in manual.items():
        if str(bid) in _fixed_after:
            _fixed_after[str(bid)].add(kk)
    _fixed_removed = sum(len(fix_sets[bid] - _fixed_after[bid]) for bid in fid)
    _new_names = [str(b.get('name')) for b in new_boxes
                  if str(b.get('id') or '') not in
                  {str(x.get('id') or '') for x in norm0}]
    _merged_names = [str(b.get('name')) for b in norm0
                     if str(b.get('id') or '') in (merged_old or [])]
    _h0 = _learn0.get('holdout') or {}
    _h1 = (_tm or {}).get('holdout') or {}
    _lr = {'v0': int((_tm or {}).get('v0') or 0),
           'version': int((_tm or {}).get('version') or 0),
           'n_train': int((_tm or {}).get('n_train') or 0),
           'conf_before': _conf_match(_learn0, _fix_pairs),
           'conf_after': _conf_match(_tm, _fix_pairs),
           'conf_all_before': _conf_match(_learn0, _all_pairs),
           'conf_all_after': _conf_match(_tm, _all_pairs),
           'holdout_before': {'model': _h0.get('model'), 'n': _h0.get('n')},
           'holdout_after': {'model': _h1.get('model'), 'n': _h1.get('n')}}
    summary = {
        'at': at, 'rows': len(rows),
        'before': {'counts': _before, 'unassigned': _before.get('', 0)},
        'after': {'counts': _after, 'unassigned': _kept_out_n},
        'fixed': {'boxes': len(fixed),
                  'before': {bid: len(fix_sets[bid]) for bid in fid},
                  'added': {bid: len(add_fixed.get(bid, [])) for bid in fid},
                  'removed': _fixed_removed,
                  'intact': _fixed_removed == 0},
        'added_fixed': sum(len(v) for v in add_fixed.values()),
        'new_boxes': _new_names, 'merged_boxes': _merged_names,
        'outliers': len(outlier_keys), 'undecided': len(undecided_keys),
        'kept_out': _kept_out_n, 'learn': _lr}
    _added_n = summary['added_fixed']
    _new_n = len(_new_names)
    _note = ('未归箱/非固定组 %d 条 → 固定组新增 %d、非固定组 %d 个（新建 %d、并入旧组 %d）、'
             '偏离项 %d、无法判定 %d；固定组原成员 0 条变动；模型置信度 %.2f→%.2f'
             % (len(pool), _added_n, len(new_boxes), _new_n, len(merged_old),
                len(outlier_keys), len(undecided_keys),
                _lr['conf_before'], _lr['conf_after']))
    return {'ok': True, 'eps': eps, 'sens': sens, 'k': len(new_boxes),
            'k_req': _kk, 'seed': DEV_SEED, 'note': _note, 'updated': at,
            'boxes': boxes, 'unassigned': _kept_out_n,
            'manual': manual, 'manual_meta': meta,
            'summary': summary, 'regroup': summary,
            'review': [b['id'] for b in new_boxes if b.get('review')]}


def analyze_sessions(sessions, merge_gap=120, sens='均衡', model=None,
                     shapes=None, thr=None):
    """会话切分 + 设备指纹聚类 + 断开原因 + 电压体检（纯本地规则，无网络）。"""
    rows = [s for s in (sessions or []) if isinstance(s, dict)]
    rows.sort(key=lambda s: _f(s.get('start_time')))
    merged = []
    for s in rows:
        if merged:
            prev = merged[-1]
            same_port = str(prev.get('port')) == str(s.get('port'))
            gap = _f(s.get('start_time')) - _f(prev.get('end_time'), _f(prev.get('start_time')))
            if same_port and 0 <= gap <= merge_gap:
                prev['_merged'] = prev.get('_merged', 1) + 1
                prev['_ids'] = list(prev.get('_ids') or [prev.get('id')]) + [s.get('id')]
                prev['end_time'] = s.get('end_time') or prev.get('end_time')
                prev['total_wh'] = _f(prev.get('total_wh')) + _f(s.get('total_wh'))
                prev['peak_power_w'] = max(_f(prev.get('peak_power_w')), _f(s.get('peak_power_w')))
                # 合并会话的断开归因取最后一段（真正结束的那次）
                for _k in ('end_source_kind', 'end_source', 'end_actor',
                           'end_evidence', 'end_attrib'):
                    if s.get(_k):
                        prev[_k] = s.get(_k)
                continue
        _row = dict(s)
        _row['_ids'] = [s.get('id')]
        merged.append(_row)

    eps = DEV_EPS.get(sens, DEV_EPS['均衡'])
    boxes = [b for b in ((model or {}).get('boxes') or [])
             if isinstance(b, dict)
             and (isinstance(b.get('center'), dict) or group_is_fixed(b))]
    unassigned = 0
    clusters = []
    if boxes:
        # 1.5.7：与「设备分组管理」共用同一归属口径（人工>形态>学习>规则>约束兜底）。
        # 固定组即使 0 条也保留在结果里，分析页不得把它隐藏。
        for b in boxes:
            clusters.append({'id': b.get('id') or 'DEV-0000',
                             'name': b.get('name') or b.get('id'),
                             'feat': b.get('center') or {}, 'items': [], 'score': []})
        _by_id = {str(c['id']): c for c in clusters}
        _learn = learn_load()
        for s in merged:
            _bid0, _src0, _cf0 = group_resolve(s, model, sens, None, shapes, thr,
                                               True, _learn, True)
            if _bid0 in _by_id:
                _by_id[_bid0]['items'].append(s)
                _by_id[_bid0]['score'].append(_cf0)
            else:
                unassigned += 1
        _fix = {str(b.get('id') or '') for b in boxes if group_is_fixed(b)}
        clusters = [c for c in clusters if c['items'] or str(c['id']) in _fix]
        for c in clusters:
            if c['items']:
                c['confidence'] = round(sum(c['score']) / len(c['score']), 2)
                c['single'] = len(c['items']) < 2
                c['suspect'] = (not c['single']) and eps <= c['confidence'] < 0.82
            else:
                c['confidence'] = 0.0
                c['single'] = True
                c['suspect'] = False
                c['empty'] = True
    else:
        for s in merged:
            feat = _fingerprint(s)
            best, best_score = None, 0.0
            for c in clusters:
                sc = _similarity(feat, c['feat'])
                if sc > best_score:
                    best, best_score = c, sc
            if best is not None and best_score >= eps:
                best['items'].append(s)
                best['feat'] = {k: (best['feat'][k] * (len(best['items']) - 1) + feat[k])
                                / len(best['items']) for k in feat}
                best['score'].append(best_score)
            else:
                clusters.append({'id': 'DEV-%04d' % (len(clusters) + 1), 'feat': feat,
                                 'items': [s], 'score': [1.0]})
        for c in clusters:
            # 单会话簇没有可比对样本，不能标 1.00 高置信
            c['single'] = len(c['items']) < 2
            conf = 0.5 if c['single'] else sum(c['score']) / max(1, len(c['score']))
            c['confidence'] = round(conf, 2)
            c['suspect'] = (not c['single']) and eps <= conf < 0.82
    _rrules = end_rules_load()
    return {'sessions': merged, 'clusters': clusters,
            'reasons': [_disconnect_reason(s, _rrules) for s in merged],
            'eps': eps, 'sens': sens, 'unassigned': unassigned}


def _fingerprint(s):
    peak = _f(s.get('peak_power_w'))
    dur = _f(s.get('duration_sec'))
    volt = _f(s.get('avg_voltage'))
    port = _f(s.get('port'))
    try:
        hour = datetime.fromtimestamp(_f(s.get('start_time'))).hour
    except Exception:
        hour = 0
    return {
        'peak_bucket': min(12, int(peak // 10)),
        'dur_bucket': min(8, int(dur // 900)),
        'volt_bucket': min(5, int(volt // 4)),
        'port': port,
        'hour_bucket': hour // 4,
    }


_W = {'peak_bucket': 0.34, 'dur_bucket': 0.22, 'volt_bucket': 0.18,
      'port': 0.14, 'hour_bucket': 0.12}


def _similarity(a, b):
    score = 0.0
    for k, w in _W.items():
        av, bv = a.get(k, 0), b.get(k, 0)
        if k == 'port':
            score += w if av == bv else 0.0
        elif k == 'hour_bucket':
            d = abs(av - bv) % 6
            d = min(d, 6 - d)
            score += w * max(0.0, 1 - d / 3.0)
        else:
            d = abs(av - bv)
            score += w * max(0.0, 1 - d / 2.0)
    return score


def _plain_reason_label(text):
    """显示层：去掉括号注释（全/半角）与首尾空白，内部 code/落库字段不动。"""
    return re.sub(r'\s*[（(][^）)]*[）)]\s*', '', str(text or '')).strip()


# --------------------------------------------------- 断开原因规则（本地可配置）
# 单条规则：{'id','label','on','match'}；按顺序首个命中即采用，分布统计随规则变化。
# match 支持：kind / src_has / src_ne / ev_has / tail_lt / head_ge / v_drop /
#            p_ratio_lt / dur_lt / dur_gt / peak_gt / avg_v_lt / port_in
END_RULES_DEFAULT = [
    {'id': 'auto', 'label': '{src}', 'on': True, 'match': {'kind': 'auto'}},
    {'id': 'manual', 'label': '人工关闭', 'on': True, 'match': {'kind': 'manual'}},
    {'id': 'device', 'label': '{device}', 'on': True, 'match': {'kind': 'device'}},
    {'id': 'link', 'label': '链路丢失', 'on': True, 'match': {'kind': 'link'}},
    {'id': 'app', 'label': '应用停止', 'on': True, 'match': {'kind': 'app'}},
    {'id': 'unknown_named', 'label': '{src}', 'on': True,
     'match': {'kind': 'unknown', 'src_ne': '未识别'}},
    {'id': 'cable', 'label': '线缆/接触断开', 'on': True,
     'match': {'v_drop': 1.0, 'p_ratio_lt': 0.6}},
    {'id': 'full', 'label': '设备充满', 'on': True,
     'match': {'tail_lt': 2.0, 'dur_gt': 600, 'peak_gt': 5}},
    {'id': 'sudden', 'label': '供电侧断电或应用停止', 'on': True,
     'match': {'tail_lt': 1.0, 'head_ge': 5}},
    {'id': 'contact', 'label': '短接触/握手失败', 'on': True, 'match': {'dur_lt': 60}},
    {'id': 'undervoltage', 'label': '欠压保护断开', 'on': True, 'match': {'avg_v_lt': 4.5}},
    {'id': 'unknown', 'label': '未识别', 'on': True, 'match': {}},
]
_END_RULES = {'path': '', 'mtime': 0.0, 'rules': None}


def end_rules_path():
    return os.path.join(learn_dir(), 'end_rules.json')


def end_rules_load(force=False):
    """读规则（带 mtime 缓存）；文件缺失/损坏时回退默认并保留原文件不动。"""
    p = end_rules_path()
    try:
        mt = os.path.getmtime(p)
    except OSError:
        mt = 0.0
    if (not force and _END_RULES.get('rules') is not None
            and _END_RULES.get('path') == p and _END_RULES.get('mtime') == mt):
        return _END_RULES['rules']
    rules = None
    if mt:
        try:
            with open(p, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            _r = data.get('rules') if isinstance(data, dict) else data
            if isinstance(_r, list) and _r:
                rules = [r for r in _r if isinstance(r, dict) and r.get('id')]
        except Exception:
            rules = None
    if not rules:
        rules = copy.deepcopy(END_RULES_DEFAULT)
    _END_RULES.update({'path': p, 'mtime': mt, 'rules': rules})
    return rules


def end_rules_save(rules):
    """原子落盘 + 审计（改了什么：条数、启用数、显示名摘要）。"""
    _r = [r for r in (rules or []) if isinstance(r, dict) and r.get('id')]
    if not _r:
        return False
    p = end_rules_path()
    tmp = p + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'version': int(time.time()),
                       'rules': _r}, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as exc:
        learn_log('END-RULES-SAVE-FAIL err=%r' % (exc,))
        return False
    _END_RULES.update({'path': p, 'mtime': os.path.getmtime(p), 'rules': _r})
    learn_log('END-RULES n=%d on=%d labels=%s' % (
        len(_r), sum(1 for x in _r if x.get('on')),
        '|'.join(str(x.get('label') or '') for x in _r[:6])))
    return True


def end_rules_reset():
    r = copy.deepcopy(END_RULES_DEFAULT)
    end_rules_save(r)
    learn_log('END-RULES-RESET n=%d' % len(r))
    return r


def _end_ctx(s):
    pts = s.get('_points') or []
    tail = []
    for p in pts[-6:]:
        if isinstance(p, dict):
            tail.append((_f(p.get('power') or p.get('p')),
                         _f(p.get('voltage') or p.get('v'))))
    p0, v0 = tail[0] if tail else (0.0, 0.0)
    pn, vn = tail[-1] if tail else (0.0, 0.0)
    return {'kind': str(s.get('end_source_kind') or ''),
            'src': str(s.get('end_source') or ''),
            'ev': str(s.get('end_evidence') or ''),
            'dur': _f(s.get('duration_sec')),
            'peak': _f(s.get('peak_power_w')),
            'avg_v': _f(s.get('avg_voltage')),
            'port': str(s.get('port') or ''),
            'tail': tail, 'p0': p0, 'pn': pn, 'vdrop': (v0 - vn) if tail else 0.0,
            'ratio': (pn / p0) if p0 > 0 else 1.0}


def _end_match(m, c):
    if not isinstance(m, dict):
        return True
    _kinds = m.get('kind')
    if _kinds:
        _kinds = [_kinds] if isinstance(_kinds, str) else list(_kinds)
        if c['kind'] not in _kinds:
            return False
    if 'src_has' in m and str(m['src_has']) not in c['src']:
        return False
    if 'src_ne' in m and c['src'] == str(m['src_ne']):
        return False
    if 'ev_has' in m and str(m['ev_has']) not in c['ev']:
        return False
    if 'port_in' in m and c['port'] not in [str(x) for x in (m['port_in'] or [])]:
        return False
    if not c['tail']:
        for _k in ('tail_lt', 'head_ge', 'v_drop', 'p_ratio_lt'):
            if _k in m:
                return False
        return True
    if 'tail_lt' in m and not (c['pn'] < float(m['tail_lt'])):
        return False
    if 'head_ge' in m and not (c['p0'] >= float(m['head_ge'])):
        return False
    if 'v_drop' in m and not (c['vdrop'] >= float(m['v_drop'])):
        return False
    if 'p_ratio_lt' in m and not (c['ratio'] < float(m['p_ratio_lt'])):
        return False
    if 'dur_lt' in m and not (c['dur'] and c['dur'] < float(m['dur_lt'])):
        return False
    if 'dur_gt' in m and not (c['dur'] > float(m['dur_gt'])):
        return False
    if 'peak_gt' in m and not (c['peak'] > float(m['peak_gt'])):
        return False
    if 'avg_v_lt' in m and not (c['avg_v'] and c['avg_v'] < float(m['avg_v_lt'])):
        return False
    return True


def _end_label(tpl, c):
    """显示名模板：{src} 原始来源 / {device} 设备侧标签 / {kind} 来源类别。"""
    if tpl == '{device}':
        return _plain_reason_label(
            c['src'] if c['src'].startswith('设备') else ('设备侧' + (c['src'] or '结束')))
    return _plain_reason_label(str(tpl or '').replace(
        '{src}', c['src'] or '未命名规则').replace(
        '{kind}', c['kind'] or '未知').replace('{port}', c['port'] or ''))


def _end_evidence(code, c, s):
    if code == 'auto':
        return c['ev'] or '规则触发值缺失（end_evidence 为空）'
    if code == 'manual':
        return c['ev'] or '调用方记录缺失（end_actor/end_evidence 为空）'
    if code in ('device', 'link', 'app', 'unknown_named'):
        return c['ev'] or '现场信号与调用方记录缺失'
    if code == 'cable':
        return '断开前沿电压骤降 %.1fV、功率 %.0f%%' % (
            c['vdrop'], 100 * max(c['pn'], 0) / max(c['p0'], 1))
    if code == 'full':
        return '峰值 %.0fW、时长 %.0f 分钟，末端功率 <2W' % (c['peak'], c['dur'] / 60)
    if code == 'sudden':
        return '末端功率 1 个采样内从 %.1fW 掉到 0W' % c['p0']
    if code == 'contact':
        return '会话仅 %.0f 秒' % c['dur']
    if code == 'undervoltage':
        return '平均电压 %.2fV 低于 4.5V' % c['avg_v']
    if c['ev']:
        return c['ev']
    if not c['tail']:
        return '缺末端采样点；现有证据仅峰值 %.0fW、时长 %.0f 分钟' % (
            c['peak'], c['dur'] / 60)
    return '无匹配的自动化规则事件与人工调用记录；现有证据仅峰值 %.0fW、时长 %.0f 分钟' % (
        c['peak'], c['dur'] / 60)


def _disconnect_reason(s, rules=None):
    """按本地可配置规则判定断开原因（默认规则与 1.5.5 行为一致）。"""
    if s.get('is_active'):
        return {'code': 'active', 'label': '进行中', 'evidence': '会话仍在输出'}
    c = _end_ctx(s)
    for r in (rules if rules is not None else end_rules_load()):
        if not r.get('on'):
            continue
        if _end_match(r.get('match') or {}, c):
            return {'code': str(r.get('id') or 'rule'),
                    'label': _end_label(r.get('label') or r.get('id'), c),
                    'evidence': _end_evidence(str(r.get('id') or ''), c, s)}
    return {'code': 'unknown', 'label': '未识别',
            'evidence': _end_evidence('unknown', c, s)}


def voltage_metrics(points):
    """电压体系化指标：波动率、跌落次数、最大 dV/dt、欠压/过压计数。"""
    vs = []
    for p in points or []:
        if isinstance(p, dict):
            v = _f(p.get('voltage') or p.get('v'))
            if v > 0:
                vs.append(v)
    if len(vs) < 2:
        return {'samples': len(vs), 'ripple': 0.0, 'drops': 0,
                'max_dvdt': 0.0, 'under': 0, 'over': 0}
    mean = sum(vs) / len(vs)
    var = sum((v - mean) ** 2 for v in vs) / len(vs)
    drops = sum(1 for a, b in zip(vs, vs[1:]) if a - b >= 1.0)
    dvdt = max(abs(b - a) for a, b in zip(vs, vs[1:]))
    return {'samples': len(vs), 'ripple': round((var ** 0.5) / max(mean, 0.1), 4),
            'drops': drops, 'max_dvdt': round(dvdt, 2),
            'under': sum(1 for v in vs if v < 4.5),
            'over': sum(1 for v in vs if v > 21.5)}


def rule_report(stats, analysis):
    """无 AI 时的结构化日报（本地规则引擎）。"""
    st = stats or {}
    total_wh = _f(st.get('total_wh'))
    sessions = st.get('session_count') or len((analysis or {}).get('sessions', []))
    peak = _f(st.get('peak_power_w'))
    dur = _f(st.get('total_duration_sec'))
    lines = ['【充电日报 · 本地规则引擎】',
             '统计范围：%s' % st.get('period', '今日'),
             '充电总量：%.1f Wh｜会话 %s 次｜峰值 %.0f W｜总时长 %.0f 分钟'
             % (total_wh, sessions, peak, dur / 60)]
    if peak >= 100:
        lines.append('· 峰值功率较高（%.0fW），请确认线缆与端口规格支持。' % peak)
    if total_wh and dur:
        eff = total_wh / (dur / 60.0)
        lines.append('· 平均输入 %.1f W；若明显低于峰值，多为后段涓流或设备限流。' % eff)
    an = analysis or {}
    if an.get('clusters'):
        lines.append('设备识别：%d 个匿名设备（本地生成，不含可识别信息）'
                     % len(an['clusters']))
        for c in an['clusters'][:3]:
            n = len(c['items'])
            p = max((_f(i.get('peak_power_w')) for i in c['items']), default=0)
            lines.append('  - %s：%d 次会话，峰值 %.0f W，置信度 %.2f%s'
                         % (device_box_name(c.get('id'), c.get('name')), n, p,
                            c['confidence'],
                            '（疑似同设备，可手动确认）' if c.get('suspect') else ''))
    if an.get('reasons'):
        from collections import Counter
        cnt = Counter(r['label'] for r in an['reasons'])
        lines.append('断开原因：' + '、'.join('%s×%d' % (k, v) for k, v in cnt.items()))
    lines.append('建议：保持通风，避免长时间满功率；异常掉速先检查线缆。')
    return '\n'.join(lines)


# ---------------------------------------------------------------- AI 客户端
def ai_config(prefs, secrets):
    provider = prefs.get('aiProvider')
    base = str(prefs.get('aiBaseUrl') or '').strip().rstrip('/')
    if provider == '本地端点' and ('api.deepseek.com' in base or not base):
        base = 'http://127.0.0.1:1234/v1'
    try:
        _rt = int(prefs.get('aiRetries', 1))
    except (TypeError, ValueError):
        _rt = 1
    return {'provider': provider, 'base': base,
            'model': str(prefs.get('aiModel') or 'deepseek-chat').strip(),
            'key': secrets.load_key(), 'proxy': str(prefs.get('proxy') or '').strip(),
            'redact': bool(prefs.get('aiRedact')),
            # R026：参数在「小源 → AI 配置」里可调，默认值与旧行为一致
            'temperature': float(prefs.get('aiTemperature', 0.3) or 0.3),
            'max_tokens': int(prefs.get('aiMaxTokens', 700) or 700),
            # 1.5.6：请求超时默认 30s，重试 0–3 次（默认 1）
            'timeout': int(prefs.get('aiTimeout', 30) or 30),
            'retries': max(0, min(3, _rt))}


def _ai_attempts(cfg):
    try:
        _r = int(cfg.get('retries', 1))
    except (TypeError, ValueError):
        _r = 1
    return 1 + max(0, min(3, _r))


class _UiBridge(QObject):
    call = Signal(object)


def _run_ui(fn):
    try:
        fn()
    except Exception:
        pass


_BRIDGE = _UiBridge()
_BRIDGE.call.connect(_run_ui, Qt.QueuedConnection)


def _ui(on_done, *args):
    """把回调排队回主线程（AI 线程不得直接操作控件）。"""
    _BRIDGE.call.emit(lambda: on_done(*args))


# ---------------------------------------------------------------- AI 调用公共段
# ai_run / ai_stream 原先各自重复「Key 判定 / 代理 opener / 请求构造 /
# 401·429·URLError 文案」。以下四个 helper 只做搬运，参数与文案逐字保留；
# 超时、重试次数、SSE 解析仍留在各自函数内（两者语义不同，不合并）。
def _ai_need_key(cfg):
    """未配 Key 且指向 deepseek：直接回落本地规则引擎。"""
    return not cfg.get('key') and 'deepseek' in (cfg.get('base') or '')


def _ai_opener(cfg):
    """按 cfg['proxy'] 建 opener（无代理即默认 opener）。"""
    proxy = cfg.get('proxy') or None
    handlers = [urllib.request.ProxyHandler({'http': proxy, 'https': proxy})] if proxy else []
    return urllib.request.build_opener(*handlers)


def _ai_request(cfg, body):
    """OpenAI 兼容 /chat/completions 请求对象。"""
    return urllib.request.Request(cfg['base'] + '/chat/completions', data=body,
                                  headers={'Content-Type': 'application/json',
                                           'Authorization': 'Bearer ' + cfg['key']})


def _ai_err(exc, attempt):
    """urllib 异常 → (文案, 是否重试, 是否致命提示)。

    致命提示由调用方直接回调并 return；重试由调用方 continue。
    429 的 2 秒退避仍在这里发生，与旧实现同一位置同一时长。
    """
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        if code == 401:
            return '', False, 'API Key 无效或已过期（401）'
        if code == 429 and attempt == 0:
            time.sleep(2.0)
            return '触发限流（429），已退避重试', True, None
        return '服务返回 HTTP %d' % code, False, None
    if isinstance(exc, urllib.error.URLError):
        return '网络不可达：%s' % getattr(exc, 'reason', exc), False, None
    return '请求失败：%s' % exc, False, None


def ai_run(cfg, payload, on_done, timeout=None):
    """异步调用 OpenAI 兼容 /chat/completions；失败回调明确错误码。

    R026：temperature / max_tokens / timeout 由「小源 → AI 配置」提供，
    system 提示词可由调用方通过 cfg['system'] 覆盖（对话页用自己的口径）。
    1.4.0：只接受 ai_payload.build() 的产物，裸字符串/字典直接拒绝。
    """
    prompt = ai_payload.text_of(payload, 'ai_run')
    _to = int(timeout or cfg.get('timeout') or 30)
    _sys = str(cfg.get('system') or '你是充电设备数据分析助手，只依据给定聚合数据作答，'
                                   '不臆造设备信息；输出简洁中文。')
    def worker():
        if _ai_need_key(cfg):
            _ui(on_done, None, '未配置 API Key：已使用本地规则引擎')
            return
        body = json.dumps({
            'model': cfg['model'],
            'messages': [
                {'role': 'system', 'content': _sys},
                {'role': 'user', 'content': prompt},
            ],
            'temperature': float(cfg.get('temperature', 0.3)),
            'max_tokens': int(cfg.get('max_tokens', 700)),
        }).encode('utf-8')
        req = _ai_request(cfg, body)
        opener = _ai_opener(cfg)
        last = ''
        for attempt in range(_ai_attempts(cfg)):
            try:
                with opener.open(req, timeout=_to) as r:
                    d = json.loads(r.read().decode('utf-8'))
                _msg = (d.get('choices') or [{}])[0].get('message', {})
                txt = _msg.get('content', '')
                if txt:
                    _ui(on_done, txt, None)
                    return
                # R027：本地推理模型会先输出 reasoning_content 吃掉 token 预算，
                # 首次为空时按 3 倍预算重试一次（上限 4096），并在重试时尝试关闭思考开关。
                if _msg.get('reasoning_content') and attempt == 0:
                    _mt = min(4096, max(800, int(cfg.get('max_tokens', 700)) * 3))
                    _body2 = json.loads(body.decode('utf-8'))
                    _body2['max_tokens'] = _mt
                    _body2['chat_template_kwargs'] = {'enable_thinking': False}
                    body = json.dumps(_body2).encode('utf-8')
                    req = _ai_request(cfg, body)
                    last = '模型仅返回思考过程，已扩大 token 预算重试'
                    continue
                last = '返回为空'
            except Exception as e:
                last, _retry, _fatal = _ai_err(e, attempt)
                if _fatal:
                    _ui(on_done, None, _fatal)
                    return
                if _retry:
                    continue
                break
        _ui(on_done, None, last + '（已降级本地规则引擎）')

    threading.Thread(target=worker, daemon=True).start()


def ai_stream(cfg, payload, on_delta, on_done, timeout=None):
    """R027：流式调用（SSE），增量回调 on_delta(piece)，结束回调 on_done(full, err)。

    与 ai_run 共用配置口径；1.4.0 起同样只接受 ai_payload.build() 的产物。
    本地推理模型只吐 reasoning_content 时，自动按 3 倍预算（上限 4096）非流式重试一次。
    """
    prompt = ai_payload.text_of(payload, 'ai_stream')
    _to = int(timeout or cfg.get('timeout') or 30)
    _sys = str(cfg.get('system') or '你是充电设备数据分析助手，只依据给定聚合数据作答，'
                                   '不臆造设备信息；输出简洁中文。')

    def worker():
        if _ai_need_key(cfg):
            _ui(on_done, None, '未配置 API Key：已使用本地规则引擎')
            return
        opener = _ai_opener(cfg)
        last = ''
        for attempt in range(_ai_attempts(cfg)):
            _mt = (int(cfg.get('max_tokens', 700)) if attempt == 0
                   else min(4096, max(800, int(cfg.get('max_tokens', 700)) * 3)))
            _extra = ({} if attempt == 0
                      else {'chat_template_kwargs': {'enable_thinking': False}})
            body = json.dumps({
                'model': cfg['model'],
                'messages': [
                    {'role': 'system', 'content': _sys},
                    {'role': 'user', 'content': prompt},
                ],
                'temperature': float(cfg.get('temperature', 0.3)),
                'max_tokens': _mt,
                'stream': True,
                **_extra,
            }).encode('utf-8')
            req = _ai_request(cfg, body)
            acc = []
            reasoning = []
            try:
                with opener.open(req, timeout=_to) as r:
                    for raw in r:
                        line = raw.decode('utf-8', 'ignore').strip()
                        if not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if data == '[DONE]':
                            break
                        try:
                            j = json.loads(data)
                        except Exception:
                            continue
                        ch = (j.get('choices') or [{}])[0]
                        dd = ch.get('delta') or {}
                        if dd.get('reasoning_content'):
                            reasoning.append(dd['reasoning_content'])
                        piece = dd.get('content') or ''
                        if piece:
                            acc.append(piece)
                            _ui(on_delta, piece)
                txt = ''.join(acc).strip()
                if txt:
                    _ui(on_done, txt, None)
                    return
                if reasoning and attempt == 0:
                    last = '模型仅返回思考过程，已扩大预算并尝试关闭思考重试'
                    continue
                if reasoning:
                    last = ('模型思考超出 token 预算、未产出正文；'
                            '可在「AI 配置」把最大长度调到 4096 后重试')
                    continue
                last = '返回为空'
            except Exception as e:
                last, _retry, _fatal = _ai_err(e, attempt)
                if _fatal:
                    _ui(on_done, None, _fatal)
                    return
                if _retry:
                    continue
                break
        _ui(on_done, None, last + '（已降级本地规则引擎）')

    threading.Thread(target=worker, daemon=True).start()


def ai_tool(cfg, prompt, tools, on_done, timeout=None):
    """1.5.6：function calling 入口。on_done(message_dict|None, err)。

    message_dict 形如 {'content': '...', 'tool_calls': [...]}；
    模型不可用/超时/无 key 时 err 非空，调用方用关键词兜底。
    """
    _to = int(timeout or cfg.get('timeout') or 30)
    _sys = str(cfg.get('system') or '你是充电管家，可用工具直接操作系统；先判断意图再调用工具。')

    def worker():
        if _ai_need_key(cfg):
            _ui(on_done, None, '未配置 API Key')
            return
        body = json.dumps({
            'model': cfg['model'],
            'messages': [
                {'role': 'system', 'content': _sys},
                {'role': 'user', 'content': prompt},
            ],
            'tools': tools,
            'tool_choice': 'auto',
            'temperature': float(cfg.get('temperature', 0.3)),
            'max_tokens': int(cfg.get('max_tokens', 700)),
        }).encode('utf-8')
        req = _ai_request(cfg, body)
        opener = _ai_opener(cfg)
        last = ''
        for attempt in range(_ai_attempts(cfg)):
            try:
                with opener.open(req, timeout=_to) as r:
                    d = json.loads(r.read().decode('utf-8'))
                msg = (d.get('choices') or [{}])[0].get('message') or {}
                if msg.get('tool_calls') or msg.get('content'):
                    _ui(on_done, msg, None)
                    return
                last = '返回为空'
            except Exception as e:
                last, _retry, _fatal = _ai_err(e, attempt)
                if _fatal:
                    _ui(on_done, None, _fatal)
                    return
                if _retry:
                    continue
                break
        _ui(on_done, None, last)

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- 文本内容
def _legal_file(fn):
    """随包 legal/<fn> 的定稿全文；文件缺失或为空返回 None（不抛错）。"""
    try:
        base = getattr(sys, '_MEIPASS', '') or os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, 'legal', fn), 'r', encoding='utf-8') as f:
            return f.read().strip() or None
    except OSError:
        return None


def _legal_doc(fn, fallback=''):
    """优先读随包 legal/<fn> 的定稿全文；缺失时退回内置摘要（不空白）。"""
    return _legal_file(fn) or fallback


_PRIVACY_SUMMARY = """《隐私说明》（版本 %APPVER%）

一、我们采集什么
· 设备信息：充电器型号、固件版本、端口协议能力（仅用于展示与兼容）。
· 运行数据：各端口功率、电压、电流、温度、充电会话起止时间与电量。
· 诊断日志：应用运行日志、错误堆栈（不含账号、密钥与文件内容）。

二、用途
· 本地展示实时状态、生成充电记录与健康趋势；
· 在你主动开启 AI 分析并同意后，生成充电解读与日报。

三、存储位置与期限
· 全部数据保存在本机（%LOCALAPPDATA% 与本地 SQLite），不上传服务器。
· 默认保留 90 天，可在「设置 → 进阶配置 → 数据、日志与诊断」修改保留期限，
  或用该页底部的「一键清理数据」立即清理。

四、第三方共享
· 默认不共享。仅当你开启 AI 且选择云端通道时，才会发送「数据脱敏」开启后的聚合特征
  （时长、总量、峰值、异常计数）至你填写的 API 服务商（默认 DeepSeek）。
· 不发送设备序列号、MAC、位置与文件内容；原始高频曲线在任何情况下都不上传。
· 关闭「数据脱敏」后会改发会话明细，请谨慎。

五、你的权利
· 随时在「小源 → AI 配置」关闭 AI；在「设置 → 进阶配置 → 隐私与协议」
  撤回同意，或用底部「一键清理数据」删除全部数据；
· 撤回同意后，AI 功能立即停用，本地规则分析仍可用。

六、安全
· API Key 使用 Windows DPAPI（当前用户）加密保存，不写入日志、不打包进程序。
""".replace('%APPVER%', APP_VERSION)

_EULA_SUMMARY = """《用户协议》（版本 %APPVER%）

1. 本应用为本地工具，按"现状"提供；充电安全保护以充电器硬件为准，
   应用内的阈值与自动化仅作辅助，不替代原厂保护。
2. 你需自行确保所用 AI 服务商账号、额度与合规性；AI 输出仅供参考。
3. 不得将本应用用于违法用途或绕过设备安全限制。
4. 数据与配置归你所有；卸载应用不会自动删除历史库，请在「设置 → 进阶配置」中清理。
5. 功能可能随版本更新调整，具体变更以对应版本的发布说明为准。
""".replace('%APPVER%', APP_VERSION)

PRIVACY = _legal_doc('隐私说明.md', _PRIVACY_SUMMARY)
EULA = _legal_doc('用户协议.md', _EULA_SUMMARY)

GUIDE = """《使用说明》（版本 %APPVER%）

一、快速开始
1. 启动后自动拉起本地服务；「状态」页显示实时功率、端口与曲线。
2. 首次连接充电器：设置 → 连接设置 → 填写 MAC 地址、Token、蓝牙密钥 →
   点「连接设备」，确认后点「保存蓝牙配置」。
3. 日常控制：「端口控制」页逐口开关、协议开关、倒计时；「充电记录」页按范围查看会话与曲线。

二、界面结构
· 左侧导航 5 项：状态、端口控制、充电记录、自动化、设置。
· 导航最下方是独立入口「小源」，不占主导航；在其页内按 Ctrl+K 可直接聚焦输入框。
· 设置分 6 个子页：连接设置、模式与显示、充电限制与保护、服务与数据、端口统计、进阶配置。

三、设置子页说明
· 连接设置：蓝牙 MAC 地址、Token、蓝牙密钥、扫描超时；底部为「连接设备」与「保存蓝牙配置」。
· 模式与显示：充电模式（AI模式 / 数码生态 / 单口模式 / 均衡模式）、息屏时间、
  设备语言、空闲息屏、屏幕方向锁、USB-A 小电流。
· 充电限制与保护：C口1 / C口2 的最大充电时长；智能保护（低功率阈值与持续时长）。
  C口3 与 USB-A 的保护暂未启用，对应输入框为置灰状态。
· 服务与数据：服务端口、日志级别、记录保留天数；开机自启、每日定时重启与重启时间；
  数据导出与记录存储路径。
· 端口统计：按端口汇总的累计数据。
· 进阶配置（共 4 组）：
  充电与保护 —— 过温保护阈值、过功率保护阈值、设备识别灵敏度、抖动合并阈值；
  自动化与通知 —— AI 自动充电管理、系统通知、免打扰、开机自启、关闭时最小化到托盘；
  数据、日志与诊断 —— 数据保留期限、自动清理、采样间隔、日志级别、开发者调试入口、网络代理；
  显示、性能与曲线 —— 主题配色、强调色、动画与特效强度、减少动效、性能模式、
  实验性功能、端口轮询频率、图表点数、曲线平滑。

四、小源
· 入口在左侧导航最下方，页内 4 个子页：对话、AI 配置、数据授权、隐私与日志。
· 对话可做：解读充电日报与异常、解释掉速/过热/断开原因、回答端口与功率问题。
· 固定说法会直接生成操作卡片：「今天充电概况」「关闭 C1」「全部关闭」等；
  单口开关可在卡片上一键撤销，全部关闭需二次确认。
· 「数据授权」决定哪些统计可以进入上下文；「隐私与日志」可查看并导出调用记录。
· 能力边界：当前不能发短信、不能拨打电话、不能主动推送通知，也不能修改设置项
  （如定时、协议开关、主题）。这类请求会明确告知不支持，不会执行。

五、说明在哪里看
· 鼠标在设置项上停留 3 秒，浮出该项「做什么 / 何时用 / 影响什么」；移开即收。
· 完整的分模块说明见「进阶配置 → 查看说明与协议 → 功能说明」。

六、AI 失败时
· 未配置 Key、断网、超时、触发限流：对话页显示失败原因并保留问题，可点「重新生成」；
  充电日报保留本地规则引擎的结果，不丢内容。
· AI 请求在后台线程执行，主界面不阻塞；API Key 仅存本机加密存储，
  可在「小源 → AI 配置」中清除。

七、数据与隐私
· 历史与日志均在本机；默认保留 90 天，可在「设置 → 进阶配置 → 数据、日志与诊断」
  修改保留期限，或用该页底部的「一键清理数据」立即清理。
· 详见《隐私说明》与《用户协议》。

八、常见现象（可能被问到）
· 刷新节奏：端口状态按「端口轮询频率」轮询（默认 2 秒），数字带 500 毫秒平滑动画，
  因此读数变化会在屏上平滑落定；功率快速波动时会看到数字「追赶」而不是逐点跳变，
  属正常表现，并非卡顿。
· 「已充满」判定：先出现 >5 W 充电，随后功率 ≤0.5 W 连续保持 120 秒，
  且距最近一次高功率不超过 10 分钟。插着本就满电的设备开机（没有高功率前序）时，
  显示「充电器在位 · 未充电」而非「已充满」。
· 设备电量与满电标志的通道已预留；协议确认后优先采用设备上报值。
""".replace('%APPVER%', APP_VERSION)


# ---------------------------------------------------------------- 说明与协议（唯一视图）
# 目录六条：显示名 → legal 文件名（None = 内联《使用说明》）。
# 运行期缺文件只把该条置灰，不炸整页；构建期 legal_gate 仍拦截缺文件出包。
LEGAL_VIEW_ITEMS = (('使用说明', None),
                    ('功能说明', '功能说明.md'),
                    ('隐私说明', '隐私说明.md'),
                    ('用户协议', '用户协议.md'),
                    ('安全与免责', '安全与免责提示.md'),
                    ('第三方许可', '第三方组件与开源许可.md'))

# 正文容器：允许内部滚动，滚动条极细低对比、悬停才显现
DOC_BROWSER_QSS = (
    'QTextBrowser{border-radius:12px; padding:10px; font-size:13px;'
    'background:rgba(255,255,255,0.04);}'
    'QScrollBar:vertical{background:transparent;width:6px;margin:0;}'
    'QScrollBar::handle:vertical{background:rgba(255,255,255,0);'
    'border-radius:4px;min-height:28px;}'
    'QScrollBar::handle:vertical:hover{background:rgba(255,255,255,0.30);}'
    'QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;'
    'background:transparent;}'
    'QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{'
    'background:transparent;}'
    'QScrollBar:horizontal{background:transparent;height:6px;margin:0;}'
    'QScrollBar::handle:horizontal{background:rgba(255,255,255,0);'
    'border-radius:4px;min-width:28px;}'
    'QScrollBar::handle:horizontal:hover{background:rgba(255,255,255,0.30);}'
    'QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0;'
    'background:transparent;}'
    'QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal{'
    'background:transparent;}')

LEGAL_NAV_QSS = (
    'QListWidget{background:%s; border:1px solid %s; border-radius:10px;'
    ' padding:4px; outline:0;}'
    'QListWidget::item{padding:9px 10px; border-radius:7px; color:%s;}'
    'QListWidget::item:hover{background:rgba(255,255,255,0.06); color:%s;}'
    'QListWidget::item:selected{background:rgba(3,169,244,0.22); color:#ffffff;'
    ' font-weight:700;}'
    'QListWidget::item:disabled{color:rgba(159,180,208,0.35);}'
    % (TOKENS['surface'], TOKENS['border_soft'], TOKENS['text_sub'], TOKENS['text']))


def legal_view_text(fn):
    """目录项正文；None = 内联《使用说明》缺失（理论上不会）。"""
    return GUIDE if fn is None else _legal_file(fn)


class LegalView(QWidget):
    """说明与协议的唯一视图：左侧目录 + 右侧一次只显示一份正文。

    宽度 >= COMPACT_WIDTH 显示纵向目录，更窄退化为下拉选择，不做横向挤压。
    首启同意卡、进阶配置的「隐私与协议」子页、独立的「说明与协议」页面
    共用本类；三处各写一份 tab 叠 tab 的旧做法已删除。
    """

    COMPACT_WIDTH = 700

    def __init__(self, parent=None):
        super().__init__(parent)
        self._compact = None
        self._cur = -1
        self._syncing = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.nav = QListWidget()
        self.nav.setObjectName('legalNav')
        self.nav.setFixedWidth(150)
        self.nav.setStyleSheet(LEGAL_NAV_QSS)
        row.addWidget(self.nav)

        right = QVBoxLayout()
        right.setSpacing(4)
        self.combo = QComboBox()
        self.combo.setObjectName('legalNavCompact')
        self.combo.hide()
        right.addWidget(self.combo)
        # 正文自带 H1（doc_html 生成），此处不再重复一行标题
        self.body = QTextBrowser()
        self.body.setObjectName('docBody')
        self.body.setStyleSheet(DOC_BROWSER_QSS)
        right.addWidget(self.body, 1)
        row.addLayout(right, 1)
        root.addLayout(row, 1)

        self._texts = []
        for i, (name, fn) in enumerate(LEGAL_VIEW_ITEMS):
            text = legal_view_text(fn)
            self._texts.append(text)
            ok = bool(text)
            label = name if ok else '%s（不可用）' % name
            self.nav.addItem(label)
            self.combo.addItem(label)
            if not ok:
                it = self.nav.item(i)
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
                ci = self.combo.model().item(i)
                if ci is not None:
                    ci.setEnabled(False)

        self.nav.currentRowChanged.connect(self._on_row)
        self.combo.currentIndexChanged.connect(self._on_combo)
        self.select(next((i for i, t in enumerate(self._texts) if t), 0))

    def select(self, idx):
        """只显示第 idx 份正文；缺失项不切换（目录里已标不可用）。"""
        if not (0 <= idx < len(self._texts)) or not self._texts[idx]:
            return
        self._cur = idx
        self._syncing = True
        try:
            self.nav.setCurrentRow(idx)
            self.combo.setCurrentIndex(idx)
        finally:
            self._syncing = False
        self.body.setHtml(doc_html(self._texts[idx]))
        self.body.verticalScrollBar().setValue(0)

    def stats(self):
        """自检用：每一条的正文长度（0 = 读不到）。"""
        return [{'name': n, 'chars': len(t or '')}
                for (n, _fn), t in zip(LEGAL_VIEW_ITEMS, self._texts)]

    def is_compact(self):
        return bool(self._compact)

    def _on_row(self, row):
        if not self._syncing and row != self._cur:
            self.select(row)

    def _on_combo(self, idx):
        if not self._syncing and idx != self._cur:
            self.select(idx)

    def _apply_compact(self, compact):
        if compact == self._compact:
            return
        self._compact = compact
        self.nav.setVisible(not compact)
        self.combo.setVisible(compact)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._apply_compact(ev.size().width() < self.COMPACT_WIDTH)


def proto_names(ext):
    """把协议扩展字段（dict / list / str / None）规整成去重后的协议名列表。

    兼容三种后端形态：{"protocols": ["PD", "PPS"]}、{"pd": 1, "pps": 1}、
    以及 "PD,PPS" 这种逗号串；子项可为字符串或带 name/label/id/key 的 dict。
    短名称统一大写（pd → PD），去重不区分大小写。
    """
    if isinstance(ext, dict):
        raw = ext.get('protocols')
        if raw is None:
            raw = ext.get('list')
        if raw is None:
            raw = list(ext.keys())
    else:
        raw = ext
    if isinstance(raw, dict):
        raw = list(raw.keys())
    if isinstance(raw, str):
        raw = (raw.replace(',', ' ').replace('/', ' ')
                  .replace('|', ' ').replace(';', ' ').split())
    out = []
    seen = set()
    for _it in (raw or []):
        if isinstance(_it, dict):
            _nm = (_it.get('name') or _it.get('label')
                   or _it.get('id') or _it.get('key'))
        else:
            _nm = _it
        _nm = str(_nm or '').strip()
        if not _nm:
            continue
        if len(_nm) <= 6 and _nm.replace('.', '').isalnum():
            _nm = _nm.upper()
        if _nm.lower() in seen:
            continue
        seen.add(_nm.lower())
        out.append(_nm)
    return out


# ---------------------------------------------------------------- 控件
class _HintSpot(QWidget):
    """零占地说明点（1.5.9）：界面上不再常驻「?」图标。

    说明挂在所在行/卡片上：鼠标停留 3 秒才浮出，移开或点击即收。
    说明正文同时收进「说明与协议 → 功能说明」，可离线查阅。
    """

    DELAY_MS = 3000

    def __init__(self, title, body, parent=None):
        super().__init__(parent)
        self._title = str(title or '')
        self._body = str(body or '')
        self._host = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DELAY_MS)
        self._timer.timeout.connect(self._popup)
        self.setFixedSize(0, 0)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def _popup(self):
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QToolTip
        if self._host is not None and self._body:
            QToolTip.showText(QCursor.pos(), self._body, self._host)

    def _bind(self):
        p = self.parentWidget()
        if p is None or p is self._host:
            return
        if self._host is not None:
            try:
                self._host.removeEventFilter(self)
            except RuntimeError:
                pass
        self._host = p
        p.installEventFilter(self)

    def showEvent(self, ev):
        super().showEvent(ev)
        self._bind()

    def event(self, ev):
        if ev.type() == QEvent.ParentChange:
            self._host = None
            QTimer.singleShot(0, self._bind)
        return super().event(ev)

    def eventFilter(self, obj, ev):
        if obj is self._host:
            t = ev.type()
            if t == QEvent.Enter:
                self._timer.start()
            elif t in (QEvent.Leave, QEvent.MouseButtonPress, QEvent.Wheel):
                self._timer.stop()
                from PySide6.QtWidgets import QToolTip
                QToolTip.hideText()
        return False


def help_badge(key, parent=None, size=26):
    """配置项说明（1.5.9）：不再显示「?」，改为所在行停留 3 秒浮出。

    size 参数仅为兼容旧调用保留：说明点零占地，不参与排版。
    """
    text = HELP.get(key, ('', ''))
    body = ('<b>%s</b><br>做什么：%s<br>何时用/影响：%s' %
            (text[0], text[1], text[2]) if len(text) == 3 else '<b>%s</b>' % (text[0] or key))
    return _HintSpot(text[0] or key, body, parent)


def info_badge(title, body, parent=None):
    """任意说明（1.5.9）：同 help_badge，零占地 + 停留 3 秒浮出，无「?」。"""
    return _HintSpot(title or '说明', body or '', parent)


def labeled_row(label, key, control):
    """表单行：控件 + 问号，问号不挤压文字。"""
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(6)
    h.addWidget(control)
    h.addWidget(help_badge(key, w))
    h.addStretch(1)
    return w


# ---------------------------------------------------------------- 配置中心
class AdvancedConfigDialog(QDialog):
    """独立进阶配置中心页面（两个应用共用）。"""

    def __init__(self, parent, prefs, secrets, title='进阶配置中心',
                 on_apply=None, on_clear=None, on_diag=None, extra_tabs=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 620)
        self.setStyleSheet(_qss())
        self.p = prefs
        self.sec = secrets
        self.on_apply = on_apply
        self.on_clear = on_clear
        self.on_diag = on_diag
        self.controls = {}
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)
        head = QHBoxLayout()
        t = QLabel(title)
        t.setStyleSheet('font-size:17px; font-weight:700;')
        head.addWidget(t)
        head.addStretch(1)
        ver = QLabel('v' + APP_VERSION)
        ver.setObjectName('hint')
        head.addWidget(ver)
        v.addLayout(head)
        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)
        for sec_name, items in SPEC:
            self._build_tab(sec_name, items)
        self._build_legal_tab()
        if extra_tabs:
            for name, widget in extra_tabs:
                self.tabs.addTab(widget, name)
        self._build_footer(v)
        self._refresh_state()

    def _build_tab(self, name, items):
        page = QWidget()
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        form.setContentsMargins(12, 14, 12, 12)
        form.setSpacing(10)
        for key, label, kind, arg in items:
            ctl = None
            if kind == 'choice':
                ctl = QComboBox()
                ctl.addItems(arg)
                ctl.setCurrentText(str(self.p.get(key)))
            elif kind == 'bool':
                ctl = QCheckBox('开启' if key not in ('reduceMotion', 'autoCleanup',
                                                     'notifyEnabled',
                                                     'minimizeTray', 'dnd', 'aiRedact')
                                else '启用')
                ctl.setChecked(bool(self.p.get(key)))
            elif kind == 'int':
                ctl = QSpinBox()
                ctl.setRange(arg[0], arg[1])
                ctl.setValue(int(self.p.get(key)))
            elif kind == 'secret':
                ctl = QLineEdit()
                ctl.setEchoMode(QLineEdit.Password)
                ctl.setPlaceholderText('仅本机加密保存；留空表示清除')
                ctl.setText(self.sec.load_key())
            else:
                ctl = QLineEdit(str(self.p.get(key) or ''))
            ctl.setMinimumHeight(30)
            self.controls[key] = (ctl, kind)
            form.addRow(label, labeled_row(label, key, ctl))
        sa.setWidget(inner)
        pv = QVBoxLayout(page)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(sa)
        self.tabs.addTab(page, name)

    def _build_legal_tab(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 10)
        v.addWidget(LegalView(page), 1)
        consent_row = QHBoxLayout()
        self.consent_lbl = QLabel('')
        self.consent_lbl.setObjectName('hint')
        consent_row.addWidget(self.consent_lbl, 1)
        self.withdraw_b = QPushButton('撤回同意并停用 AI')
        self.withdraw_b.clicked.connect(self._withdraw)
        consent_row.addWidget(self.withdraw_b)
        v.addLayout(consent_row)
        self.tabs.addTab(page, '隐私与协议')

    def _build_footer(self, v):
        row = QHBoxLayout()
        self.status = QLabel('')
        self.status.setObjectName('hint')
        row.addWidget(self.status, 1)
        b_reset = QPushButton('恢复默认')
        b_reset.clicked.connect(self._reset)
        row.addWidget(b_reset)
        b_test = QPushButton('测试 AI 连接')
        b_test.clicked.connect(self._test_ai)
        row.addWidget(b_test)
        b_diag = QPushButton('导出诊断')
        b_diag.clicked.connect(self._diag)
        row.addWidget(b_diag)
        b_clear = QPushButton('一键清理数据')
        b_clear.clicked.connect(self._clear)
        row.addWidget(b_clear)
        b_save = QPushButton('保存并应用')
        b_save.clicked.connect(self._save)
        row.addWidget(b_save)
        b_close = QPushButton('关闭')
        b_close.setObjectName('ghost')
        b_close.clicked.connect(self.reject)
        row.addWidget(b_close)
        v.addLayout(row)

    def _refresh_state(self):
        at = self.p.consent_at()
        ok = self.p.consent_ok()
        self.consent_lbl.setText(('已同意隐私说明与用户协议（%s）' % at) if ok
                                 else '尚未同意隐私说明：AI 功能不可用')
        self.withdraw_b.setEnabled(ok)

    def _collect(self):
        out = {}
        for key, (ctl, kind) in self.controls.items():
            if kind == 'choice':
                out[key] = ctl.currentText()
            elif kind == 'bool':
                out[key] = ctl.isChecked()
            elif kind == 'int':
                out[key] = ctl.value()
            elif kind == 'secret':
                out[key] = ctl.text().strip()
            else:
                out[key] = ctl.text().strip()
        return out

    def _save(self):
        vals = self._collect()
        # 开启 AI 前必须显式同意隐私说明；拒绝则强制关闭 AI，其余设置照常保存
        if vals.get('aiEnabled') and not self.p.consent_ok():
            dlg = ConsentDialog(self, self.p)
            if dlg.exec() != QDialog.Accepted:
                vals['aiEnabled'] = False
                self.controls['aiEnabled'][0].setChecked(False)
                self.status.setText('未同意隐私说明：AI 保持关闭，本地分析可用')
            else:
                self._refresh_state()
        key = vals.pop('aiApiKey', '')
        old_key = self.sec.load_key()
        if key != old_key:
            self.sec.save_key(key)
        for k, v in vals.items():
            self.p.set(k, v)
        if callable(self.on_apply):
            try:
                self.on_apply(vals)
            except Exception as e:
                self.status.setText('应用失败：%s' % e)
                return
        self.status.setText('已保存（%s）' % datetime.now().strftime('%H:%M:%S'))
        self._refresh_state()

    def _reset(self):
        if QMessageBox.question(self, '恢复默认', '恢复全部进阶配置为默认值？\n'
                                '（API Key 也会被清除）') != QMessageBox.Yes:
            return
        self.p.reset()
        self.sec.save_key('')
        for key, (ctl, kind) in self.controls.items():
            d = DEFAULTS.get(key)
            if kind == 'choice':
                ctl.setCurrentText(str(d))
            elif kind == 'bool':
                ctl.setChecked(bool(d))
            elif kind == 'int':
                ctl.setValue(int(d))
            elif kind == 'secret':
                ctl.setText('')
            else:
                ctl.setText(str(d or ''))
        self.status.setText('已恢复默认')

    def _withdraw(self):
        self.p.set_consent(False)
        self.controls['aiEnabled'][0].setChecked(False)
        self.status.setText('已撤回同意，AI 已停用')
        self._refresh_state()

    def _test_ai(self):
        cfg = {'provider': self.controls['aiProvider'][0].currentText(),
               'base': self.controls['aiBaseUrl'][0].text().strip().rstrip('/'),
               'model': self.controls['aiModel'][0].text().strip(),
               'key': self.controls['aiApiKey'][0].text().strip(),
               'proxy': self.controls['proxy'][0].text().strip(),
               'redact': True}
        if cfg['provider'] == '本地端点' and not cfg['base']:
            cfg['base'] = 'http://127.0.0.1:1234/v1'
        if not cfg['key'] and 'deepseek' in cfg['base']:
            self.status.setText('未填 API Key：将使用本地规则引擎')
            return
        self.status.setText('正在测试连接…')

        def done(txt, err):
            self.status.setText(('连接成功：' + (txt or '')[:40]) if not err else err)
        ai_run(cfg, ai_payload.build('conn_test'), done, timeout=20)

    def _diag(self):
        if callable(self.on_diag):
            try:
                p = self.on_diag()
                self.status.setText('诊断已导出：%s' % p)
                return
            except Exception as e:
                self.status.setText('导出失败：%s' % e)
                return
        self.status.setText('当前应用未提供诊断导出')

    def _clear(self):
        if QMessageBox.question(self, '清理数据', '将删除本机历史充电数据与日志，不可恢复。继续？') \
                != QMessageBox.Yes:
            return
        if callable(self.on_clear):
            try:
                self.on_clear()
                self.status.setText('已清理')
            except Exception as e:
                self.status.setText('清理失败：%s' % e)


class IntField(QLineEdit):
    """整数输入框：替代 QSpinBox（应用级 QSS 下 QSpinBox 不绘制数字）。"""

    def __init__(self, lo, hi, val, parent=None):
        super().__init__(parent)
        self._lo, self._hi = int(lo), int(hi)
        self.setValidator(QIntValidator(self._lo, self._hi, self))
        self.setText(str(int(val)))
        self.setMinimumHeight(28)

    def value(self):
        try:
            v = int(self.text() or 0)
        except Exception:
            v = 0
        return max(self._lo, min(self._hi, v))

    def setValue(self, v):
        self.setText(str(max(self._lo, min(self._hi, int(v)))))


class DoubleField(QLineEdit):
    """小数输入框：替代 QDoubleSpinBox（应用级 QSS 下 QDoubleSpinBox 不绘制数字）。"""

    def __init__(self, lo, hi, val=0.0, decimals=1, parent=None):
        super().__init__(parent)
        self._lo = float(lo)
        self._hi = float(hi)
        self._dec = int(decimals)
        _dv = QDoubleValidator(self._lo, self._hi, self._dec, self)
        _dv.setNotation(QDoubleValidator.StandardNotation)
        self.setValidator(_dv)
        self.setMinimumHeight(28)
        self.setAlignment(Qt.AlignRight)
        self.setText(('%.' + str(self._dec) + 'f') % float(val))

    def value(self):
        try:
            v = float(self.text() or 0)
        except Exception:
            v = 0.0
        return max(self._lo, min(self._hi, v))

    def setValue(self, v):
        _txt = ('%.' + str(self._dec) + 'f') % max(self._lo, min(self._hi, float(v)))
        self.setText(_txt)


class AdvPanel(QWidget):
    """R024：进阶配置面板（嵌入"设置"分区，不再是独立突兀弹窗）。

    每项包含：名称 / 一句话作用 / 控件 / 问号 / 生效时机 / 使用说明链接。
    """

    applied = Signal(dict)
    changed = Signal(str, object)

    def __init__(self, parent, prefs, secrets, on_apply=None, on_clear=None,
                 on_diag=None, on_guide=None, guide_keys=None, skip_keys=None,
                 extra_btn=None, card_factory=None, on_dev=None,
                 groups=None, flat=False, top_cards=None,
                 notes=None, immediate=()):
        super().__init__(parent)
        # R046 / C15：取证用 objectName（不影响界面）
        self.setObjectName('advPanel')
        self.p = prefs
        self.sec = secrets
        # C34-UI（骨架统一 A）：分组卡由主程序注入 GlassCard，与本设置页其它子页同骨架；
        # 传入 None 时退化为普通 QGroupBox（无卡头竖条），仅供无主程序的离屏/单测使用。
        self.card_factory = card_factory
        self.on_dev = on_dev
        # R03（25 轮）：外部按钮（如「查看说明与协议」）并入本面板同一页脚行，
        # 避免在其下方再挂一行造成 13 px 基线错位。
        self.extra_btn = extra_btn
        self.on_apply = on_apply
        self.on_clear = on_clear
        self.on_diag = on_diag
        self.on_guide = on_guide
        self.guide_keys = guide_keys or {}
        # R026：AI 相关项已收拢到「小源 → AI 配置」，进阶配置里不再出现第二份
        self.skip_keys = set(skip_keys or ())
        # 1.5.5：允许只渲染指定分组，并可关闭手风琴（flat）——
        # 三块内容迁走后本页只剩「设备识别 + 性能与曲线」，一屏全展开即可。
        self.groups = list(groups) if groups else list(SPEC)
        self.flat = bool(flat)
        # 1.5.5：允许在页标题/说明下方插入同级卡片（如「A 口小电流自动保持」），
        # 与后续分组卡同一层级、同一缩进与卡间距，不再挂在标题之上。
        self.top_cards = [c for c in (top_cards or ()) if c is not None]
        # 2.1.0：控件旁的实时说明（如设备分组上限的「已用 N · 固定 X · 自建 Y」），
        # 值是 callable，刷新时机：构建、保存、恢复默认、页面再次显示。
        self.notes = dict(notes or {})
        self._note_lbs = {}
        # 2.1.0：即时生效项——编辑结束即校验并落盘，不必等「保存并应用」。
        self.immediate = set(immediate or ())
        self.controls = {}
        self._build()

    # ---- 构建 ----
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel('进阶配置')
        t.setObjectName('advTitle')
        head.addWidget(t)
        sub = QLabel('所有开关都会即时生效或在标注的时机生效；鼠标在项目上停留 3 秒可看完整说明。')
        sub.setObjectName('hint')
        head.addWidget(sub, 1)
        v.addLayout(head)
        # R026：进阶配置不再用滚动区（红线：页面不得出现滚动条）——
        # 改为分组折叠面板，一次只展开一组，永远不需要滚动。
        self._groups = []
        holder = QWidget()
        holder.setObjectName('advHolder')
        # R046 / C15：内容列收口到 560 px（原先铺满 812/737/612，右侧大片空档）
        # 1.5.5：扁平模式只剩两组，放宽到 860 px 让两列网格铺开。
        holder.setMaximumWidth(860 if self.flat else 560)
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(4, 4, 8, 4)
        # R046 / C15：150%/900×620 下 7 张折叠卡 + 展开卡需 481 px，可用 433 px →
        # 卡片纵向内边距减半、卡间距 8→4（落在 {4,8,12,16} 节奏），展开卡不再压盖。
        hv.setSpacing(10 if self.flat else 4)
        for _tc in self.top_cards:
            _tc.setParent(holder)
            hv.addWidget(_tc, 1)
        for name, items in self.groups:
            # 允许分组用配置键列表表达（1.5.5 迁移时只写键，避免复制 SPEC 元组）
            _norm = []
            for _it in items:
                _g = _BY_KEY.get(_it) if isinstance(_it, str) else _it
                if _g is not None:
                    _norm.append(_g)
            items = [it for it in _norm if it[0] not in self.skip_keys]
            if not items:
                continue
            _cols = 2 if len(items) > 2 else 1
            # 1.5.5：卡高按内容行数分配，避免「两行卡」和「三行卡」等高后各留半屏空档
            _rows = (len(items) + _cols - 1) // _cols
            hv.addWidget(self._build_group(name, items), _rows if self.flat else 0)
        if not self.flat:
            self._restore_open()
            hv.addStretch(1)
        v.addWidget(holder, 1)
        v.addLayout(self._build_footer())
        self.apply_theme()
        self._refresh_state()
        self.refresh_notes()

    def _build_group(self, name, items):
        """分组卡：骨架与设置页其它子页一致——QGroupBox 标题 + 3×14 竖条，
        条数固定在卡头右侧；点击卡头或空格/回车展开收起（手风琴，一次一组）。"""
        if self.flat:
            return self._build_group_flat(name, items)
        if callable(self.card_factory):
            card = self.card_factory(name)
        else:
            card = QGroupBox(name)
        card.setObjectName('advGroup')
        card.setFocusPolicy(Qt.StrongFocus)
        card.setCursor(Qt.PointingHandCursor)
        card.setAccessibleName(name)
        card.setAccessibleDescription('按空格或回车展开或收起该分组')
        # B′：折叠态锁到自身 sizeHint 38 px，取消 QVBoxLayout 的松弛分配
        card.setFixedHeight(38)
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        cnt = QLabel('%d 项' % len(items), card)
        cnt.setObjectName('advCount')
        cnt.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        cnt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        card._ck_name = name
        card._ck_count = cnt
        card._ck_open = False
        card.installEventFilter(self)
        cv = QVBoxLayout(card)
        # 卡内留白交给 QGroupBox 的 padding（与其它子页一致），布局不再叠加
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(2)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(12, 10, 20, 8)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnMinimumWidth(0, 140)
        grid.setColumnStretch(1, 1)
        for r, (key, label, kind, arg) in enumerate(items):
            gr = r * 2                                   # 每项占两行：控件行 + 说明行
            nm = QLabel(label)
            nm.setObjectName('advItemName')
            nm.setFixedWidth(140)
            ctl = self._make_ctl(key, kind, arg)
            ctl.setMinimumHeight(28)
            ctl.setMaximumWidth(260)
            # R02（25 轮）：grid 行高被说明文字与 44 px「说明」按钮撑到 ~90 px，
            # 数值框垂直策略随行拉伸，实际渲染成 88 px 高的输入框（实拍可见）。
            # 定高 34 + 顶部对齐，恢复与其它表单一致的控件高度。
            ctl.setMaximumHeight(34)
            box = QWidget()
            bv = QHBoxLayout(box)
            bv.setContentsMargins(0, 0, 0, 0)
            bv.setSpacing(8)
            bv.addWidget(ctl, 0, Qt.AlignVCenter)
            bv.addWidget(help_badge(key, box), 0, Qt.AlignVCenter)
            bv.addStretch(1)
            _desc = HELP.get(key, ('', '', ''))
            d = QLabel((_desc[1] if len(_desc) > 1 else '').split('。')[0] + '。')
            d.setObjectName('desc')
            d.setWordWrap(True)
            eff = QLabel(EFFECT.get(key, '即时'))
            eff.setObjectName('eff')
            # 徽章按内容定宽：原先被 grid 列拉成等宽空壳（「即时」与「写入系统启动项」同宽）
            eff.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            # C34-UI：44×44 的文字方块比控件还高，把行高撑到 47 并挤掉说明第二行；
            # 收成 44×26 文本链接，热区仍高于 WCAG 24×24
            lk = QPushButton('说明')
            lk.setObjectName('ghost')
            lk.setFixedSize(44, 26)
            lk.setStyleSheet('font-size:11px; padding:0; border:0; background:transparent;')
            lk.clicked.connect(lambda _=False, k=key: self._open_guide(k))
            # C34-UI：说明文字移出控件竖列，横跨「控件 + 时机 + 说明」三列——
            # 原先 187 px 宽只够一行、实际需 30 px 高却只分到 21 px，第二行被判裁。
            # 标签与 28 px 控件行垂直居中对齐（原先跨两行顶端对齐，文字比控件高 ~10 px）
            grid.addWidget(nm, gr, 0, Qt.AlignVCenter)
            grid.addWidget(box, gr, 1)
            grid.addWidget(d, gr + 1, 1, 1, 3)
            grid.addWidget(eff, gr, 2, Qt.AlignVCenter)
            grid.addWidget(lk, gr, 3, Qt.AlignVCenter)
        # B′：超限组用纵向滚动兜底；起覆盖式 6 px 滚动条只在 rng>0 时出现，
        # 无溢出（静止态）隐藏、不常驻；内容按 heightForWidth 定高，
        # 避免 wordwrap 说明行按两行估算后二次裁切。
        sa = QScrollArea(card)
        sa.setObjectName('advScroll')
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        body.setAutoFillBackground(False)
        body._ck_scroll = sa
        body.installEventFilter(self)
        sa.setWidget(body)
        sa.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 覆盖式滚动条：不占视口宽度，四组内容右边线才能都落在 515
        ov = QScrollBar(Qt.Vertical, card)
        ov.setObjectName('advScrollBar')
        ov.setCursor(Qt.PointingHandCursor)
        ov.hide()
        _isb = sa.verticalScrollBar()
        _isb.valueChanged.connect(ov.setValue)
        ov.valueChanged.connect(_isb.setValue)
        _isb.rangeChanged.connect(lambda _a, _b, c=card: self._place_scrollbar(c))
        card._ck_sb = ov
        card._ck_body = sa
        cv.addWidget(sa)
        self._groups.append((card, sa))
        self._place_scrollbar(card)
        # 默认全折叠，由 _restore_open() 按上次展开组恢复
        self._set_open(card, False, exclusive=False, persist=False)
        self._place_count(card)
        return card

    def _build_group_flat(self, name, items):
        """1.5.5 扁平分组：整卡直接展开，一行一项（名称/控件/?/生效时机），
        不折叠、不滚动，也不再把说明灰字铺在页面上（全部收进「?」）。"""
        card = self.card_factory(name) if callable(self.card_factory) else QGroupBox(name)
        card.setObjectName('advGroup')
        grid = QGridLayout(card)
        grid.setContentsMargins(12, 12, 12, 10)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        cols = 2 if len(items) > 2 else 1
        for i, (key, label, kind, arg) in enumerate(items):
            grid.addWidget(self._flat_cell(key, label, kind, arg, card),
                           i // cols, i % cols)
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        # 1.5.5：剩余项本就少，纵向等分卡片高度，避免页面下半屏大片空白
        for r in range((len(items) + cols - 1) // cols):
            grid.setRowStretch(r, 1)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._flat_cards = getattr(self, '_flat_cards', [])
        self._flat_cards.append(card)
        return card

    def _flat_cell(self, key, label, kind, arg, parent):
        """一行设置：名称 + 控件 + ? + 右侧生效时机。"""
        box = QWidget(parent)
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        nm = QLabel(label)
        nm.setObjectName('advItemName')
        # 1.5.5：列宽预算按 995 宽窗口核算（名称 108 + 控件 120 + ? 26 + 时机 41
        # + 间距 24 = 319 < 单列可用 337），避免控件最小宽把「?」压出重叠。
        nm.setMinimumWidth(108)
        nm.setMaximumWidth(150)
        ctl = self._make_ctl(key, kind, arg)
        ctl.setFixedHeight(30)
        ctl.setMaximumWidth(170)
        _eff_full = EFFECT.get(key, '即时')
        _eff_short = _EFFECT_SHORT.get(_eff_full, _eff_full)
        # 1.5.9：即时生效不再逐个标注（默认行为），只在非即时项上提示时机
        eff = QLabel('' if _eff_short == '即时' else _eff_short)
        eff.setObjectName('eff')
        eff.setToolTip('' if _eff_short == '即时' else _eff_full)
        eff.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        if _eff_short == '即时':
            eff.hide()      # 空胶囊不占位、不出现
        h.addWidget(nm, 0, Qt.AlignVCenter)
        h.addWidget(ctl, 0, Qt.AlignVCenter)
        if key in self.notes:
            _nt = QLabel('')
            _nt.setObjectName('sub')
            h.addWidget(_nt, 0, Qt.AlignVCenter)
            self._note_lbs[key] = _nt
        h.addWidget(help_badge(key, box), 0, Qt.AlignVCenter)
        h.addStretch(1)
        h.addWidget(eff, 0, Qt.AlignVCenter)
        return box

    def refresh_notes(self):
        """刷新控件旁的实时说明（占用数等），不重建页面、不改几何。"""
        for key, lb in self._note_lbs.items():
            _fn = self.notes.get(key)
            try:
                lb.setText(str(_fn() if callable(_fn) else (_fn or '')))
            except Exception:
                lb.setText('')

    def _commit_now(self, key, ctl):
        """即时生效项：编辑结束就校验→落盘→刷新说明，不等「保存并应用」。

        非法值（空、越界）不写入：恢复为已保存值并在状态行说明，避免存进脏数据。
        """
        try:
            val = int(ctl.text().strip())
        except (TypeError, ValueError):
            val = None
        lo, hi = int(getattr(ctl, '_lo', 0)), int(getattr(ctl, '_hi', 0))
        if val is None or not (lo <= val <= hi):
            ctl.setValue(int(self.p.get(key) or lo))
            self.status.setText('「%s」需在 %d–%d 之间，已恢复为 %d'
                                % (self._label(key), lo, hi, ctl.value()))
            return
        if int(self.p.get(key) or 0) != val:
            self.p.set(key, val)
            self.p.sync()                      # 立即落盘：不依赖退出时的刷写
            self.changed.emit(key, val)        # 先通知（状态行可能被外部刷新）
            self.status.setText('「%s」已保存并生效：%d（重建分组按新上限执行）'
                                % (self._label(key), val))
        else:
            self.status.setText('「%s」未变化：%d' % (self._label(key), val))
        self.refresh_notes()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.refresh_notes()

    def _restore_open(self):
        """记住上次展开的分组，重进页面时恢复。

        从未记录过（advOpen 不存在）→ 展开第一组，保持改造前的默认观感；
        记录为空字符串（用户主动全收起）→ 维持五组全折叠；
        记录的组被 skip_keys 过滤掉 → 也维持全折叠，不擅自替用户选。
        """
        cards = [c for c, _b in self._groups]
        if not cards:
            return
        want = self.p.get('advOpen', None)
        if want is None:
            self._set_open(cards[0], True, persist=False)
            return
        for c in cards:
            if c._ck_name == want:
                self._set_open(c, True, persist=False)
                return

    def _set_open(self, card, on, exclusive=True, persist=True):
        """手风琴：展开一组时自动收起其它组，避免纵向溢出。"""
        if on and exclusive:
            for c, _b in self._groups:
                if c is not card:
                    self._set_open(c, False, exclusive=False, persist=False)
        card._ck_open = bool(on)
        card.setTitle(('▾  ' if on else '▸  ') + card._ck_name)
        card._ck_body.setVisible(bool(on))
        if on:
            card.setMinimumHeight(38)
            card.setMaximumHeight(16777215)
        else:
            card.setFixedHeight(38)
        if persist:
            # 记住展开的是哪一组；全折叠记空串，重进页面按原样恢复
            self.p.set('advOpen', card._ck_name if on else '')
        self._place_scrollbar(card)
        card.updateGeometry()

    def _place_scrollbar(self, card):
        """覆盖式滚动条：贴在卡片右侧内边距里（不占内容宽度），仅溢出时显示。"""
        ov = getattr(card, '_ck_sb', None)
        sa = getattr(card, '_ck_body', None)
        if ov is None or not isinstance(sa, QScrollArea):
            return
        isb = sa.verticalScrollBar()
        rng = isb.maximum()
        ov.setRange(0, max(0, rng))
        ov.setPageStep(isb.pageStep())
        ov.setValue(isb.value())
        show = bool(getattr(card, "_ck_open", False)) and rng > 0
        ov.setVisible(show)
        if show:
            ov.setGeometry(card.width() - 14, sa.y() + 2, 6,
                           max(24, sa.height() - 4))

    def _place_count(self, card):
        """条数标签贴卡头右侧（与 3×14 竖条同一水平带）。"""
        cnt = getattr(card, '_ck_count', None)
        if cnt is None:
            return
        cnt.adjustSize()
        cnt.move(max(12, card.width() - cnt.width() - 14), 9)

    def eventFilter(self, obj, ev):
        if getattr(obj, '_ck_scroll', None) is not None:
            if ev.type() == QEvent.Resize and obj.width() > 0:
                lay = obj.layout()
                if lay is not None and lay.hasHeightForWidth():
                    need = lay.heightForWidth(obj.width())
                    if need > 0 and obj.minimumHeight() != need:
                        obj.setMinimumHeight(need)
            return super().eventFilter(obj, ev)
        if getattr(obj, '_ck_name', None) is not None:
            _t = ev.type()
            if _t == QEvent.Resize:
                self._place_count(obj)
                self._place_scrollbar(obj)
            elif (_t == QEvent.MouseButtonRelease and ev.button() == Qt.LeftButton
                  and ev.position().y() <= 30):
                self._set_open(obj, not obj._ck_open)
                return True
            elif _t == QEvent.KeyPress and ev.key() in (Qt.Key_Space, Qt.Key_Return,
                                                        Qt.Key_Enter):
                self._set_open(obj, not obj._ck_open)
                return True
        return super().eventFilter(obj, ev)

    def _panel_qss(self):
        """面板级样式：全部取当前主题令牌，切主题时重挂本表即可整体跟随。

        卡头留白 32 px 与其它子页同值；折叠卡锁 38 px，超限组内容区滚动（B′），
        标题字号/字重、竖条、圆角、底色、描边仍与 GlassCard 同源。
        """
        return (
            'QLabel#advTitle { color:%(text)s; font-size:16px; font-weight:700; }'
            'QLabel#hint { color:%(text_sub)s; font-size:12px; }'
            'QLabel#advItemName { color:%(text)s; font-size:12px; }'
            'QLabel#desc { color:%(text_sub)s; font-size:11px; }'
            'QLabel#eff { color:%(text_sub)s; font-size:11px;'
            ' border:1px solid %(border)s; border-radius:8px; padding:1px 6px; }'
            'QLabel#advCount { color:%(text_sub)s; font-size:11px; }'
            'QGroupBox#advGroup { margin-top:32px; padding:2px 12px 2px 12px; }'
            'QGroupBox#advGroup:focus { border:1px solid %(accent)s; }'
            'QScrollArea#advScroll { background:transparent; border:0; }'
            'QScrollArea#advScroll > QWidget > QWidget { background:transparent; }'
            'QScrollBar#advScrollBar:vertical { background:%(surface_hi)s;'
            'width:6px; margin:0; border-radius:3px; }'
            'QScrollBar#advScrollBar::handle:vertical { background:%(accent)s;'
            'min-height:24px; border-radius:3px; }'
            'QScrollBar#advScrollBar::add-line:vertical,'
            'QScrollBar#advScrollBar::sub-line:vertical { height:0; }'
            'QScrollBar#advScrollBar::add-page:vertical,'
            'QScrollBar#advScrollBar::sub-page:vertical { background:transparent; }'
            % TOKENS
        )

    def apply_theme(self, theme=None):
        """主题切换入口：刷新令牌并重挂面板样式，本页所有颜色随主题。"""
        if theme:
            set_theme(theme)
        self.setStyleSheet(self._panel_qss())
        for card, _b in getattr(self, '_groups', ()):
            self._place_count(card)

    def _make_ctl(self, key, kind, arg):
        if kind == 'choice':
            ctl = QComboBox()
            ctl.addItems(arg)
            ctl.setCurrentText(str(self.p.get(key)))
        elif kind == 'bool':
            ctl = QCheckBox('已开启' if self.p.get(key) else '已关闭')
            ctl.setChecked(bool(self.p.get(key)))
            ctl.toggled.connect(lambda on, c=ctl: c.setText('已开启' if on else '已关闭'))
        elif kind == 'int':
            # QSpinBox 在应用级 QSS + NoButtons 下不绘制数字（实测空白），改用带校验的输入框
            ctl = IntField(arg[0], arg[1], int(self.p.get(key)))
            ctl.setMinimumWidth(96)
            if key in self.immediate:
                ctl.editingFinished.connect(
                    lambda k=key, c=ctl: self._commit_now(k, c))
        elif kind == 'action':
            # 「管理分组」类入口按钮（无配置值，collect 时跳过）
            ctl = QPushButton(str(arg or '打开'))
            ctl.setMinimumWidth(120)   # 1.5.5：140→120，保证 995 宽单行放得下「?」
            ctl.setCursor(Qt.PointingHandCursor)
            ctl.clicked.connect(
                lambda _=False: (self.on_dev() if callable(self.on_dev) else None))
        elif kind == 'secret':
            ctl = QLineEdit()
            ctl.setEchoMode(QLineEdit.Password)
            ctl.setPlaceholderText('仅本机 DPAPI 加密保存；留空=清除')
            ctl.setText(self.sec.load_key())
        else:
            ctl = QLineEdit(str(self.p.get(key) or ''))
        self.controls[key] = (ctl, kind)
        if kind == 'choice':
            ctl.currentTextChanged.connect(lambda t, k=key: self._on_change(k, t))
        elif kind == 'bool':
            ctl.toggled.connect(lambda on, k=key: self._on_change(k, on))
        elif kind == 'int':
            ctl.textChanged.connect(lambda t, k=key, c=ctl: self._on_change(k, c.value()))
        elif kind == 'action':
            pass
        else:
            ctl.textEdited.connect(lambda t, k=key: self._on_change(k, t))
        return ctl

    def _on_change(self, key, val):
        if hasattr(self, 'status'):
            self.status.setText('已修改「%s」，点击「保存并应用」后生效（%s）'
                                % (self._label(key), EFFECT.get(key, '即时')))
        self.changed.emit(key, val)
        self._refresh_state()

    def _label(self, key):
        for _n, items in SPEC:
            for k, lab, _a, _b in items:
                if k == key:
                    return lab
        return key

    def _build_footer(self):
        # R046 / C15：900×620 下状态文案与 6 个按钮同排会互相压盖——
        # 改为两行：状态一行、按钮一行（右对齐），600 px 内可完整排下。
        wrap = QVBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(8)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self.status = QLabel('')
        self.status.setObjectName('hint')
        # 单行 + 省略：避免状态文案换行 3 行挤压底部按钮（综合审查项）
        self.status.setWordWrap(False)
        self.status.setMinimumWidth(150)
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        top.addWidget(self.status, 1)
        wrap.addLayout(top)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addStretch(1)
        # 1.5.9：移除本页无实际作用的「测试 AI 连接 / 导出诊断 / 一键清理」，
        # 只留恢复默认、保存并应用与页外说明入口。
        for text, fn, name in (('恢复默认', self._reset, ''),
                               ('保存并应用', self._save, 'primary')):
            b = QPushButton(text)
            if name:
                b.setObjectName(name)
            b.setMinimumSize(84, 30)
            b.setStyleSheet('font-size:11px; padding:0 6px;')
            b.clicked.connect(fn)
            if text == '保存并应用':
                self.save_b = b
            row.addWidget(b)
        # R03（25 轮）：外部按钮并入按钮行，基线与「保存并应用」一致
        if self.extra_btn is not None:
            try:
                self.extra_btn.setMinimumSize(132, 30)
                self.extra_btn.setStyleSheet('font-size:11px; padding:0 6px;')
                row.addWidget(self.extra_btn)
            except Exception:
                pass
        wrap.addLayout(row)
        return wrap

    # ---- 行为 ----
    def collect(self):
        out = {}
        for key, (ctl, kind) in self.controls.items():
            if kind == 'action':
                continue                      # 入口按钮无配置值
            if kind == 'choice':
                out[key] = ctl.currentText()
            elif kind == 'bool':
                out[key] = ctl.isChecked()
            elif kind == 'int':
                out[key] = ctl.value()
            else:
                out[key] = ctl.text().strip()
        return out

    def _refresh_state(self):
        at = self.p.consent_at()
        ok = self.p.consent_ok()
        if hasattr(self, 'status'):
            base = ('已同意隐私说明（%s）' % at) if ok else '尚未同意隐私说明：AI 与上传相关功能不可用'
            if not self.status.text():
                self.status.setText(base)
            self.status.setToolTip(self.status.text())

    def _open_guide(self, key):
        if callable(self.on_guide):
            self.on_guide(self.guide_keys.get(key, key))
        else:
            QMessageBox.information(self, '使用说明',
                                    '%s\n\n%s\n\n%s' % (HELP.get(key, (key,))[0],
                                                        HELP.get(key, ('', ''))[1],
                                                        HELP.get(key, ('', '', ''))[2]))

    def save(self):
        # 防重入：快速连点/重复信号不重复写配置与重放生效逻辑
        if getattr(self, '_saving', False):
            return
        self._saving = True
        try:
            self._save_impl()
        finally:
            self._saving = False

    def _save_impl(self):
        vals = self.collect()
        if vals.get('aiEnabled') and not self.p.consent_ok():
            dlg = ConsentDialog(self, self.p)
            if dlg.exec() != QDialog.Accepted:
                vals['aiEnabled'] = False
                self.controls['aiEnabled'][0].setChecked(False)
        key = vals.pop('aiApiKey', '')
        if key != self.sec.load_key():
            self.sec.save_key(key)
        for k, val in vals.items():
            self.p.set(k, val)
        try:
            if callable(self.on_apply):
                self.on_apply(vals)
        except Exception as e:
            self.status.setText('应用失败：%s' % e)
            return
        self.applied.emit(vals)
        self.status.setText('已保存并应用（%s）' % datetime.now().strftime('%H:%M:%S'))
        self._refresh_state()
        self.refresh_notes()

    # 兼容旧调用
    _save = save

    def _reset(self):
        if QMessageBox.question(self, '恢复默认', '恢复全部进阶配置为默认值？\n'
                                '（API Key 也会被清除）') != QMessageBox.Yes:
            return
        self.p.reset()
        self.sec.save_key('')
        for key, (ctl, kind) in self.controls.items():
            d = DEFAULTS.get(key)
            if kind == 'action':
                continue                      # 入口按钮不参与恢复默认
            if kind == 'choice':
                ctl.setCurrentText(str(d))
            elif kind == 'bool':
                ctl.setChecked(bool(d))
            elif kind == 'int':
                ctl.setValue(int(d))
            else:
                ctl.setText('' if kind == 'secret' else str(d or ''))
        self.status.setText('已恢复默认，点击「保存并应用」写入')
        self.refresh_notes()

    def _test_ai(self):
        cfg = {'provider': self.controls['aiProvider'][0].currentText(),
               'base': self.controls['aiBaseUrl'][0].text().strip().rstrip('/'),
               'model': self.controls['aiModel'][0].text().strip(),
               'key': self.controls['aiApiKey'][0].text().strip() or self.sec.load_key(),
               'proxy': self.controls['proxy'][0].text().strip(),
               'redact': True}
        if cfg['provider'] == '本地端点' and not cfg['base']:
            cfg['base'] = 'http://127.0.0.1:1234/v1'
        if not cfg['key']:
            self.status.setText('未配置 API Key：AI 入口会提示去配置，本地规则引擎照常工作')
            return
        self.status.setText('正在测试连接…')

        def done(txt, err):
            self.status.setText(('连接成功：' + (txt or '')[:40]) if not err else ('连接失败：' + err))
        ai_run(cfg, ai_payload.build('conn_test'), done, timeout=20)

    def _diag(self):
        if callable(self.on_diag):
            try:
                self.status.setText('诊断已导出：%s' % self.on_diag())
            except Exception as e:
                self.status.setText('导出失败：%s' % e)
        else:
            self.status.setText('当前应用未提供诊断导出')

    def _clear(self):
        if QMessageBox.question(self, '清理数据', '将删除本机历史充电数据与日志，不可恢复。继续？') \
                != QMessageBox.Yes:
            return
        if callable(self.on_clear):
            try:
                self.on_clear()
                self.status.setText('已清理')
            except Exception as e:
                self.status.setText('清理失败：%s' % e)


_EFFECT_SHORT = {
    '即时（下一采样拍生效）': '即时', '即时（后端）': '即时',
    '下次分析生效': '下次分析', '重新分组后生效': '重新分组',
    '下次告警生效': '下次告警', '下次关窗生效': '下次关窗',
    '写入系统启动项': '系统启动项', '下次启动生效': '下次启动',
    '立即加密保存': '立即保存', '需重启服务': '需重启',
}

class AdvSection(QWidget):
    """1.5.5：扁平设置组，可嵌入任意二级设置页。

    一行一项：名称 / 控件 / 「?」 / 生效时机；说明文字全部在「?」里，
    页面上不再铺成段灰字。改动即时写入 Prefs 并发出 changed，宿主据此应用。
    """

    changed = Signal(str, object)

    def __init__(self, parent, prefs, secrets, title, keys, cols=2,
                 on_dev=None, card_factory=None, glyph=None,
                 labels=None, fixed=None):
        # 允许调用方直接传布局（addWidget 后会自动 reparent）
        if not isinstance(parent, QWidget):
            parent = None
        super().__init__(parent)
        self.setObjectName('advSection')
        self.p = prefs
        self.sec = secrets
        self.title = title
        self.cols = max(1, int(cols))
        self.on_dev = on_dev
        self.card_factory = card_factory
        self.controls = {}
        self.labels = dict(labels or {})
        self.fixed = tuple(fixed) if fixed else None
        self._items = []
        for k in keys:
            if k not in _BY_KEY:
                continue
            _k, _l, _kind, _arg = _BY_KEY[k]
            self._items.append((_k, self.labels.get(_k, _l), _kind, _arg))
        self._build()
        if glyph:
            self.set_glyph(glyph)

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.card = (self.card_factory(self.title) if callable(self.card_factory)
                     else QGroupBox(self.title))
        self.card.setObjectName('advSectionCard')
        grid = QGridLayout(self.card)
        grid.setContentsMargins(12, 10, 12, 8)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(6)
        for i, (key, label, kind, arg) in enumerate(self._items):
            grid.addWidget(self._cell(key, label, kind, arg), i // self.cols, i % self.cols)
        for c in range(self.cols):
            grid.setColumnStretch(c, 1)
        v.addWidget(self.card)

    def _cell(self, key, label, kind, arg):
        box = QWidget(self.card)
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        nm = QLabel(label)
        nm.setObjectName('advItemName')
        nm.setMinimumWidth(118)
        nm.setMaximumWidth(140)
        nm.setToolTip(label)
        ctl = self._make_ctl(key, kind, arg)
        ctl.setFixedHeight(30)
        # 1.5.5：控件最大宽必须 ≥ 其最小宽，否则 Qt 先按最大宽分布、
        # 控件再按最小宽外扩，右边缘会压到后面的「?」徽标（995 宽复现）。
        ctl.setMaximumWidth(160 if kind == 'action' else 112)
        _eff_full = EFFECT.get(key, '即时')
        _eff_short = _EFFECT_SHORT.get(_eff_full, _eff_full)
        # 1.5.9：即时生效不标注；定宽网格下保留空列宽，行与行仍对齐
        eff = QLabel('' if _eff_short == '即时' else _eff_short)
        eff.setObjectName('eff')
        eff.setToolTip('' if _eff_short == '即时' else _eff_full)
        eff.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        if _eff_short == '即时':
            eff.hide()      # 空胶囊不占位、不出现
        if self.fixed:
            # 1.5.5：定宽网格——名称/控件/生效列在同行与跨行都对齐，
            # 「?」紧随控件，末端列宽固定，避免不同控件类型各占一列宽。
            _nm_w, _ctl_w, _eff_w = self.fixed
            nm.setFixedWidth(_nm_w)
            ctl.setFixedWidth(_ctl_w)
            eff.setFixedWidth(_eff_w)
            eff.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        h.addWidget(nm, 0, Qt.AlignVCenter)
        h.addWidget(ctl, 0, Qt.AlignVCenter)
        h.addWidget(help_badge(key, box), 0, Qt.AlignVCenter)
        h.addStretch(1)
        h.addWidget(eff, 0, Qt.AlignVCenter)
        return box

    def refresh_theme(self):
        """令牌切换后重挂行内字色（宿主页不走 AdvPanel 的 QSS）。"""
        _name = 'color:%s; font-size:12px; background:transparent;' % TOKENS['text']
        _eff = ('color:%s; font-size:11px; border:1px solid %s;'
                'border-radius:8px; padding:1px 5px; background:transparent;'
                % (TOKENS['text_sub'], TOKENS['border']))
        for lb in self.findChildren(QLabel, 'advItemName'):
            lb.setStyleSheet(_name)
        for lb in self.findChildren(QLabel, 'eff'):
            lb.setStyleSheet(_eff)

    def set_glyph(self, kind):
        try:
            self.card.set_glyph(kind)
        except Exception:
            pass

    def _make_ctl(self, key, kind, arg):
        if kind == 'choice':
            ctl = QComboBox()
            ctl.addItems(arg)
            ctl.setCurrentText(str(self.p.get(key)))
            ctl.currentTextChanged.connect(lambda t, k=key: self._write(k, t))
        elif kind == 'bool':
            ctl = QCheckBox('已开启' if self.p.get(key) else '已关闭')
            ctl.setChecked(bool(self.p.get(key)))
            ctl.toggled.connect(lambda on, c=ctl, k=key: (
                c.setText('已开启' if on else '已关闭'), self._write(k, on)))
        elif kind == 'int':
            ctl = IntField(arg[0], arg[1], int(self.p.get(key)))
            ctl.setMinimumWidth(96)
            ctl.textChanged.connect(lambda _t, k=key, c=ctl: self._write(k, c.value()))
        elif kind == 'action':
            ctl = QPushButton(str(arg or '打开'))
            ctl.setMinimumWidth(120)
            ctl.setCursor(Qt.PointingHandCursor)
            ctl.clicked.connect(
                lambda _=False: (self.on_dev() if callable(self.on_dev) else None))
        elif kind == 'secret':
            ctl = QLineEdit()
            ctl.setEchoMode(QLineEdit.Password)
            ctl.setText(self.sec.load_key())
            ctl.textEdited.connect(lambda t, k=key: self._write(k, t))
        else:
            ctl = QLineEdit(str(self.p.get(key) or ''))
            ctl.textEdited.connect(lambda t, k=key: self._write(k, t))
        self.controls[key] = (ctl, kind)
        return ctl

    def _write(self, key, val):
        if key not in DEFAULTS:
            return
        self.p.set(key, val)
        self.changed.emit(key, val)

    def collect(self):
        out = {}
        for key, (ctl, kind) in self.controls.items():
            if kind == 'action':
                continue
            if kind == 'choice':
                out[key] = ctl.currentText()
            elif kind == 'bool':
                out[key] = ctl.isChecked()
            elif kind == 'int':
                out[key] = ctl.value()
            else:
                out[key] = ctl.text().strip()
        return out


class AdvancedConfigDialog(QDialog):
    """兼容包装：旧入口以弹窗打开同一套 AdvPanel。"""

    def __init__(self, parent, prefs, secrets, title='进阶配置',
                 on_apply=None, on_clear=None, on_diag=None, extra_tabs=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(880, 640)
        self.setStyleSheet(_qss())
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        self.panel = AdvPanel(self, prefs, secrets, on_apply=on_apply,
                              on_clear=on_clear, on_diag=on_diag)
        v.addWidget(self.panel, 1)
        bb = QDialogButtonBox()
        bb.addButton('关闭', QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        v.addWidget(bb)


class ConsentDialog(QDialog):
    """首次启动的隐私说明 + 用户协议确认。"""

    def __init__(self, parent, prefs):
        super().__init__(parent)
        self.setWindowTitle('隐私说明与用户协议')
        self.resize(760, 640)
        self.setStyleSheet(_qss())
        self.p = prefs
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        lb = QLabel('首次使用请阅读并确认以下内容（可随时在「进阶配置中心 → 隐私与协议」回看与撤回）')
        lb.setWordWrap(True)
        v.addWidget(lb)
        hi = QLabel(
            '<p style="margin:0 0 6px 0;">'
            '<b>启用 AI 分析后，你的提问会发送至你配置的第三方 AI 服务（默认 DeepSeek）。</b>'
            '不使用 AI 时，端口控制、状态查看、历史记录与自动关闭全部在本机运行，不联网。</p>'
            '<p style="margin:0;">'
            '<b>责任限制（用户协议 7.2）：</b>第 7.1 条不排除或限制我们因故意或重大过失造成的责任，'
            '也不排除或限制依法不可排除的人身损害责任。<br/>'
            '<b>争议解决（用户协议 9.2）：</b>因本协议产生的争议，双方应先行协商；'
            '协商不成的，任一方可向北京市海淀区人民法院提起诉讼。</p>')
        hi.setWordWrap(True)
        hi.setTextFormat(Qt.RichText)
        hi.setStyleSheet('QLabel{background:rgba(255,255,255,0.06);'
                         'border:1px solid rgba(255,255,255,0.13);'
                         'border-radius:8px;padding:10px;font-size:12px;}')
        v.addWidget(hi)
        self.view = LegalView(self)
        v.addWidget(self.view, 1)
        bb = QDialogButtonBox()
        self.ok_b = bb.addButton('同意并继续', QDialogButtonBox.AcceptRole)
        self.no_b = bb.addButton('暂不同意（离线功能仍可用）', QDialogButtonBox.RejectRole)
        self.ok_b.clicked.connect(self.accept)
        self.no_b.clicked.connect(self.decline)
        v.addWidget(bb)

    def accept(self):
        self.p.set_consent(True)
        super().accept()

    def decline(self):
        """暂不同意：记住「本版本已问过」，下次启动不再弹；AI 保持停用，离线功能照常。"""
        self.p.set_consent(False)
        super().reject()


def ensure_consent(parent, prefs):
    """返回是否已同意；本版本已问过（同意或拒绝）就不再弹。"""
    if prefs.consent_ok():
        return True
    if prefs.consent_decided():
        return False
    dlg = ConsentDialog(parent, prefs)
    ok = dlg.exec() == QDialog.Accepted
    return ok


def ck_notify_plan(muted, unread, kind='plain'):
    """静音策略决策（纯函数，可离线单测）。

    C34f 口径：静音时不发系统横幅，只累计未读角标（不自动弹窗）；
    解除静音后把累计未读合成一条摘要补发。

    返回 dict:
      action: 'send'  正常发送
              'count' 静音抑制，仅未读 +1
              'digest' 解除静音后的补发摘要
      unread: 处理后的未读数
      body:   digest 时的补发文案（其余为空串）
    """
    try:
        n = max(0, int(unread))
    except Exception:
        n = 0
    if muted:
        return {'action': 'count', 'unread': n + 1, 'body': ''}
    if n > 0:
        return {'action': 'digest', 'unread': 0,
                'body': '静音期间有 %d 条通知，点击查看' % n}
    return {'action': 'send', 'unread': 0, 'body': ''}
