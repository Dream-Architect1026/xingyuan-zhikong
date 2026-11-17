# -*- coding: utf-8 -*-
"""ui_kit —— 本产品专用样式常量与辅助函数（不与其他产品共用）。

三件套：TOKENS（设计令牌）、doc_html（文档渲染）、page_head（页头构件）。
约定：只放与业务无关的纯 UI 构件，不 import 任何一侧的 launcher，避免反向依赖。
"""
import html as _html
import re as _re

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

# ---------------------------------------------------------------- 设计令牌
# 本产品专用取值（text / text_sub 已收敛）。
TOKENS = {
    'bg': '#1c1c1c',
    'surface': 'rgba(255,255,255,0.045)',
    'surface_hi': 'rgba(255,255,255,0.085)',
    'border': 'rgba(255,255,255,0.13)',
    'text': '#e8f4ff',
    'text_sub': '#9fb4d0',
    'accent': '#03a9f4',
    'accent2': '#7c4dff',
    'accent3': '#2fb6ff',
    'ok': '#34d399',
    'warn': '#fbbf24',
    'err': '#ef6a5d',
    'surface_soft': 'rgba(255,255,255,0.05)',
    'border_soft': 'rgba(255,255,255,0.10)',
    'text_muted': '#9fb4c8',
    'code_bg': 'rgba(0,0,0,0.25)',
    'font_card': '14px', 'font_h': '18px',
    'port1': '#22d3ee', 'port2': '#a78bfa', 'port3': '#34d399', 'port4': '#fbbf24',
    'font_xl': '84px', 'font_l': '26px', 'font_m': '15px',
    'font_body': '13px', 'font_s': '12px', 'font_xs': '11px',
    'space1': 4, 'space2': 8, 'space3': 12, 'space4': 16, 'space5': 24,
    'radius1': 8, 'radius2': 12, 'radius3': 16,
    'dur_fast': 150, 'dur_base': 220, 'dur_slow': 300,
}

_MD_HEAD = _re.compile(r'^(#{1,6})\s+(.*)$')
_MD_HR = _re.compile(r'^\s{0,3}([-*_])\s*(?:\1\s*){2,}$')
_MD_UL = _re.compile(r'^(\s*)[-*+]\s+(.*)$')
_MD_OL = _re.compile(r'^(\s*)(\d+)[.)]\s+(.*)$')
_MD_QUOTE = _re.compile(r'^\s{0,3}>\s?(.*)$')
_MD_TSEP = _re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$')


def is_markdown(text):
    """是否按 Markdown 渲染：出现 ATX 标题 / 强调 / 代码块 / 表格 / 引用即认定。"""
    for _ln in str(text or '').splitlines():
        if _MD_HEAD.match(_ln) or _MD_TSEP.match(_ln) or _ln.strip().startswith('```'):
            return True
        if '**' in _ln or '`' in _ln or _ln.lstrip().startswith('> '):
            return True
    return False


def _inline(text):
    """行内语法：`code` → 转义 → **粗** / *斜* / ~~删~~ / [链接](url)。"""
    _codes = []

    def _keep(m):
        _codes.append(m.group(1))
        return '\x00%d\x00' % (len(_codes) - 1)

    t = _re.sub(r'`([^`]+)`', _keep, str(text or ''))
    t = _html.escape(t, quote=False)
    t = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t)
    t = _re.sub(r'__(.+?)__', r'<b>\1</b>', t)
    t = _re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', t)
    t = _re.sub(r'(?<!_)_([^_\n]+)_(?!_)', r'<i>\1</i>', t)
    t = _re.sub(r'~~(.+?)~~', r'<s>\1</s>', t)
    t = _re.sub(
        r'\[([^\]]+)\]\(([^)\s]+)\)',
        r'<a href="\2" style="color:%s;text-decoration:none;">\1</a>' % TOKENS['accent3'],
        t)
    for _i, _c in enumerate(_codes):
        t = t.replace('\x00%d\x00' % _i,
                      '<span style="font-family:Consolas,monospace;font-size:12px;'
                      'background:%s;color:#cfe3f5;">&nbsp;%s&nbsp;</span>'
                      % (TOKENS['code_bg'], _html.escape(_c)))
    return t


def _md_table(lines, i):
    """从第 i 行起读一张表；返回 (html, 下一行下标)。首行必须紧跟分隔行。"""
    if i + 1 >= len(lines) or not _MD_TSEP.match(lines[i + 1]):
        return None, i
    _split = lambda s: [c.strip() for c in s.strip().strip('|').split('|')]
    head = _split(lines[i])
    rows = []
    j = i + 2
    while j < len(lines) and '|' in lines[j] and lines[j].strip():
        rows.append(_split(lines[j]))
        j += 1
    _bd = TOKENS['border_soft']
    out = ['<table width="100%" cellspacing="0" cellpadding="4" border="0">',
           '<tr>' + ''.join(
               '<td style="border:1px solid %s;background:%s;color:%s;">'
               '<b>%s</b></td>' % (_bd, TOKENS['surface_hi'], TOKENS['text'],
                                   _inline(_c)) for _c in head) + '</tr>']
    for _r in rows:
        _r = (_r + [''] * len(head))[:len(head)]
        out.append('<tr>' + ''.join(
            '<td style="border:1px solid %s;color:%s;">%s</td>'
            % (_bd, TOKENS['text'], _inline(_c)) for _c in _r) + '</tr>')
    out.append('</table>')
    return ''.join(out), j


def _md_html(text):
    """Markdown → QTextBrowser 可用的深色 HTML（本地实现，不引外部依赖）。"""
    lines = str(text or '').splitlines()
    out = []
    i, n = 0, len(lines)
    _h = [None, '20px', '16px', '14.5px', '14px', '13.5px', '13px']
    while i < n:
        ln = lines[i]
        st = ln.strip()
        if st.startswith('```'):                       # 代码块
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith('```'):
                buf.append(lines[i])
                i += 1
            i += 1
            body = '<br>'.join(_html.escape(x).replace('  ', '&nbsp;&nbsp;')
                               for x in buf) or '&nbsp;'
            out.append('<div style="font-family:Consolas,monospace;font-size:12px;'
                       'background:%s;color:#cfe3f5;padding:8px 10px;margin:6px 0;">'
                       '%s</div>' % (TOKENS['code_bg'], body))
            continue
        if not st:
            i += 1
            continue
        if _MD_TSEP.match(ln) and i + 1 < n:
            i += 1
            continue
        m = _MD_HEAD.match(st)
        if m:
            lv = len(m.group(1))
            out.append('<div style="font-size:%s;font-weight:%s;color:%s;'
                       'margin:%dpx 0 %dpx 0;">%s</div>'
                       % (_h[lv], '700' if lv <= 2 else '600',
                          TOKENS['text'] if lv <= 2 else TOKENS['accent3'],
                          14 if lv == 1 else 12, 6, _inline(m.group(2))))
            i += 1
            continue
        if _MD_HR.match(ln):
            out.append('<div style="border-top:1px solid %s;margin:10px 0;">'
                       '</div>' % TOKENS['border_soft'])
            i += 1
            continue
        if _MD_QUOTE.match(ln):
            buf = []
            while i < n and _MD_QUOTE.match(lines[i]):
                buf.append(_inline(_MD_QUOTE.match(lines[i]).group(1)))
                i += 1
            out.append('<div style="border-left:3px solid %s;'
                       'background:rgba(255,255,255,0.04);color:%s;'
                       'padding:6px 10px;margin:6px 0;">%s</div>'
                       % (TOKENS['accent'], TOKENS['text_sub'], '<br>'.join(buf)))
            continue
        tbl, nxt = _md_table(lines, i)
        if tbl:
            out.append('<div style="margin:6px 0;">' + tbl + '</div>')
            i = nxt
            continue
        m = _MD_UL.match(ln)
        if m:
            items = []
            while i < n:
                _u = _MD_UL.match(lines[i])
                if not _u:
                    break
                _lvl = len(_u.group(1)) // 2
                items.append('<div style="margin:2px 0 4px %dpx;color:%s;">• %s</div>'
                             % (14 + _lvl * 14, TOKENS['text'], _inline(_u.group(2))))
                i += 1
            out.extend(items)
            continue
        m = _MD_OL.match(ln)
        if m:
            while i < n:
                _o = _MD_OL.match(lines[i])
                if not _o:
                    break
                _lvl = len(_o.group(1)) // 2
                out.append('<div style="margin:2px 0 4px %dpx;color:%s;">%s. %s</div>'
                           % (14 + _lvl * 14, TOKENS['text'], _o.group(2),
                              _inline(_o.group(3))))
                i += 1
            continue
        # 普通段落：连续非空行合并，行内换行保留
        buf = []
        while i < n:
            _s = lines[i]
            if (not _s.strip() or _MD_HEAD.match(_s.strip()) or _MD_UL.match(_s)
                    or _MD_OL.match(_s) or _MD_QUOTE.match(_s)
                    or _s.strip().startswith('```') or _MD_HR.match(_s)):
                break
            buf.append(_inline(_s.strip()))
            i += 1
            if i < n and _MD_TSEP.match(lines[i]) and len(buf) == 1:
                break
        if buf:
            out.append('<div style="margin:0 0 7px 0;color:%s;line-height:150%%;">'
                       '%s</div>' % (TOKENS['text'], '<br>'.join(buf)))
    return ('<div style="font-size:13px;line-height:150%%;color:%s;">%s</div>'
            % (TOKENS['text'], ''.join(out)))


def _plain_html(text):
    """旧版纯文本排版（首行《…》=标题、「一、」=小标题、「·」/「1.」=列表）。"""
    _h1 = TOKENS.get('heading') or TOKENS['text']
    _h2c = TOKENS.get('accent3') or TOKENS['accent']
    out = []
    for _i, _raw in enumerate(str(text or '').splitlines()):
        _s = _raw.rstrip()
        _t = _s.strip()
        if not _t:
            continue
        _e = _html.escape(_t)
        _h2 = (len(_t) > 1 and _t[1] == '、'
               and _t[0] in '一二三四五六七八九十')
        _num = _t.split('.', 1)[0].isdigit() and '.' in _t
        if _i == 0 and _t.startswith('《'):
            out.append('<div style="font-size:18px;font-weight:700;'
                       'color:%s;margin:0 0 12px 0;">%s</div>'
                       % (_h1, _e))
        elif _h2:
            out.append('<div style="font-size:14px;font-weight:700;'
                       'color:%s;margin:16px 0 6px 0;">%s</div>'
                       % (_h2c, _e))
        elif _t.startswith('·'):
            out.append('<div style="margin:2px 0 6px 12px;color:%s;">%s</div>'
                       % (TOKENS['text'], _e))
        elif _num:
            out.append('<div style="margin:2px 0 6px 4px;color:%s;">%s</div>'
                       % (TOKENS['text'], _e))
        elif _s[:1].isspace():
            out.append('<div style="margin:0 0 6px 20px;color:%s;">%s</div>'
                       % (TOKENS['text_sub'], _e))
        else:
            out.append('<div style="margin:0 0 6px 0;color:%s;">%s</div>'
                       % (TOKENS['text'], _e))
    return ('<div style="font-size:13px;">' + ''.join(out) + '</div>')


# ---------------------------------------------------------------- 文档渲染
def doc_html(text):
    """说明/协议正文 → 深色 HTML。

    协议正文按 Markdown 维护（标题/列表/强调/行内代码/代码块/引用/分隔线/链接/表格）；
    内联《使用说明》仍是纯文本排版，自动按 `is_markdown` 选择渲染器，不出现原始符号。
    """
    _t = str(text or '')
    return _md_html(_t) if is_markdown(_t) else _plain_html(_t)


# ---------------------------------------------------------------- 页内头
def page_head(text=None, meta=None, status=None, badge=None):
    """48 px 页内头，一个函数两种内容模型（不许拆成两个函数）。

    文档模型：text = 18 px/700 标题，meta = 11 px 右侧「最后更新 … · 版本 …」
    状态模型：status = 11 px 左侧状态短句，badge = 右侧徽章控件
    返回 (header, refs)：refs 暴露 title/meta/status/badge，供调用方就地刷新。
    """
    hdr = QWidget()
    hdr.setFixedHeight(48)
    hl = QHBoxLayout(hdr)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(8)
    _t = QLabel(text or '')
    _t.setStyleSheet('font-size:18px; font-weight:700;')
    _t.setVisible(bool(text))
    _s = QLabel(status or '')
    _s.setObjectName('sub')
    _s.setStyleSheet('font-size:11px;')
    _s.setVisible(bool(status))
    _m = QLabel(meta or '')
    _m.setObjectName('sub')
    _m.setStyleSheet('font-size:11px;')
    _m.setVisible(bool(meta))
    hl.addWidget(_t)
    hl.addWidget(_s)
    hl.addStretch(1)
    hl.addWidget(_m)
    if badge is not None:
        hl.addWidget(badge)
    return hdr, {'title': _t, 'meta': _m, 'status': _s, 'badge': badge}
