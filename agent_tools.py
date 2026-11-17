# -*- coding: utf-8 -*-
"""Agent 工具集（1.5.6）：能力定义、关键词兜底、参数校验、执行、审计。

设计要点：
- 每个能力一个明确函数 + 参数校验；模型可用时走 function calling，
  不可用时用关键词兜底，保证断网也能执行简单命令；
- 风险分级：查询类免确认；动作类执行前给确认卡（单条迁移视为低风险，免确认）；
- 所有执行只调用既有后端接口与既有分组入口，不绕过安全规则；
- 每次执行写 `agent_action.log`（时间 / 原话 / 工具与参数 / 结果 / 是否经确认）。
"""
import json
import os
import re
import time

PORT_KEYS = ('1', '2', '3', 'a')
# 后端端口键：c1/c2/c3/a；界面/解析用 1/2/3/a。
PORT_API = {'1': 'c1', '2': 'c2', '3': 'c3', 'a': 'a'}
PORT_NAMES = {'1': 'C1（1 号 C 口）', '2': 'C2（2 号 C 口）',
              '3': 'C3（3 号 C 口）', 'a': 'USB-A 口'}

# risk: query / action / batch；confirm=False 表示免确认（查询与单条迁移）
TOOLS = {
    'query_status': {
        'name': '查询当前功率与状态', 'risk': 'query', 'confirm': False,
        'default': True, 'params': {},
        'desc': '读取每个端口的开关与当前功率'},
    'query_energy': {
        'name': '查询某时段充入统计', 'risk': 'query', 'confirm': False,
        'default': True,
        'params': {'period': ('enum', ('today', 'week', 'month'), 'today')},
        'desc': '读取指定时段的充入电量与会话数'},
    'set_port': {
        'name': '单个端口开/关', 'risk': 'action', 'confirm': True,
        'default': True,
        'params': {'port': ('port', None, None),
                   'action': ('enum', ('on', 'off'), None)},
        'desc': '打开或关闭指定端口'},
    'all_off': {
        'name': '全部关闭', 'risk': 'action', 'confirm': True,
        'default': False, 'params': {},
        'desc': '关闭全部端口'},
    'set_limit': {
        'name': '设置充电上限（分钟）', 'risk': 'action', 'confirm': True,
        'default': False,
        'params': {'port': ('port', None, None),
                   'minutes': ('range', (1, 1440), None)},
        'desc': '设置指定端口的最长充电时长（分钟）'},
    'create_box': {
        'name': '新建分箱', 'risk': 'action', 'confirm': True,
        'default': False,
        'params': {'name': ('text', (1, 40), None)},
        'desc': '新建一个一级分组'},
    'rename_box': {
        'name': '分箱改名', 'risk': 'action', 'confirm': True,
        'default': False,
        'params': {'old': ('text', (1, 40), None),
                   'new': ('text', (1, 40), None)},
        'desc': '把已有分组改名'},
    'move_sessions': {
        'name': '迁移/移出分箱', 'risk': 'batch', 'confirm': True,
        'default': False,
        'params': {'box': ('text', (0, 40), ''),
                   'count': ('range', (1, 200), None),
                   'action': ('enum', ('move', 'remove'), 'move')},
        'desc': '把最近 N 条会话迁入指定分箱，或移出到未归箱'},
    'auto_toggle': {
        'name': '启停自动化规则', 'risk': 'action', 'confirm': True,
        'default': False,
        'params': {'on': ('bool', None, None)},
        'desc': '开启或关闭自动断电规则'},
    'export_records': {
        'name': '导出记录', 'risk': 'action', 'confirm': True,
        'default': False,
        'params': {'period': ('enum', ('today', 'week', 'month'), 'week')},
        'desc': '把会话记录导出为 CSV'},
}

_CN_NUM = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
           '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


_SHORT = {
    'query_status': '当前状态', 'query_energy': '时段充入',
    'set_port': '单口开关', 'all_off': '全部关闭',
    'set_limit': '充电上限', 'create_box': '新建分箱',
    'rename_box': '分箱改名', 'move_sessions': '迁移/移出',
    'auto_toggle': '自动化启停', 'export_records': '导出记录',
}


def short_name(tool):
    return _SHORT.get(tool, (TOOLS.get(tool) or {}).get('name', tool))


def defaults():
    return {k: bool(v.get('default')) for k, v in TOOLS.items()}


def needs_confirm(tool, params=None):
    _t = TOOLS.get(tool) or {}
    if not _t.get('confirm'):
        return False
    if tool == 'move_sessions':
        try:
            return int((params or {}).get('count') or 1) > 1
        except (TypeError, ValueError):
            return True
    return True


# ---------------------------------------------------------------- 参数校验
def validate(tool, params):
    """返回 (ok, cleaned, err)。所有取值在调用后端前先卡住。"""
    _t = TOOLS.get(tool)
    if not _t:
        return False, {}, '不支持的操作'
    _p = dict(params or {})
    _out = {}
    for _k, _spec in _t['params'].items():
        _kind, _range, _dv = _spec
        _v = _p.get(_k, _dv)
        if _kind == 'port':
            _v = str(_v or '').lower().lstrip('c')
            if _v not in PORT_KEYS:
                return False, {}, '端口只能是 C1 / C2 / C3 / USB-A'
            _out[_k] = _v
        elif _kind == 'enum':
            _v = str(_v or _dv or '').lower()
            if _v not in _range:
                return False, {}, '参数 %s 取值不合法' % _k
            _out[_k] = _v
        elif _kind == 'range':
            try:
                _v = int(_v)
            except (TypeError, ValueError):
                return False, {}, '参数 %s 需要整数' % _k
            _lo, _hi = _range
            if not _lo <= _v <= _hi:
                return False, {}, '参数 %s 超出范围 %d–%d' % (_k, _lo, _hi)
            _out[_k] = _v
        elif _kind == 'bool':
            _out[_k] = bool(_v)
        else:   # text
            _v = str(_v or '').strip()
            _lo, _hi = _range
            if not _lo <= len(_v) <= _hi:
                return False, {}, '参数 %s 长度不合法' % _k
            _out[_k] = _v
    return True, _out, ''


# ---------------------------------------------------------------- 关键词兜底
def _cn2int(s):
    if s.isdigit():
        return int(s)
    return _CN_NUM.get(s)


def _find_port(text):
    _m = re.search(r'(?:c\s*口?\s*|usb\s*-?\s*)([123a])', text, re.I)
    if _m:
        return _m.group(1).lower()
    _m = re.search(r'\b([123a])\s*号?\s*(?:口|port)\b', text, re.I)
    if _m:
        return _m.group(1).lower()
    _m = re.search(r'\b([123])\b', text)
    if _m:
        return _m.group(1)
    return None


def _clean_name(s):
    return str(s or '').strip(' \t「」“”"\'《》')


def parse(text):
    """关键词兜底：返回 (tool, params) 或 None。"""
    _t = str(text or '')
    _n = re.sub(r'\s+', '', _t)
    if re.search(r'(全部关闭|全部关掉|全关|所有端口关闭|都关掉)', _n):
        return ('all_off', {})
    _m = re.search(r'(限到|限制到|设到|设置到|设为|上限)\s*(\d{1,4})\s*(%|分钟|分|min)?',
                   _t, re.I)
    if _m:
        _port = _find_port(_t)
        if _port:
            _mins = int(_m.group(2))
            # 本机保护套件按「分钟」计上限；百分比说法取同一数值并在回执说明
            return ('set_limit', {'port': _port, 'minutes': max(1, _mins)})
    _m = re.search(r'(关闭|关掉|断开|停用|打开|开启|关|开)\s*'
                   r'(?:c\s*口?\s*([123])|usb\s*-?\s*a|a\s*口)', _t, re.I)
    if _m:
        _kw = _m.group(1)
        _port = _m.group(2).lower() if _m.group(2) else 'a'
        return ('set_port', {'port': _port,
                             'action': 'on' if _kw in ('打开', '开启', '开') else 'off'})
    _m = re.search(r'(新建|创建|建)(?:一个|个)?\s*[「“"《]([^」”"》]{1,40})[」”"》]', _t)
    if _m:
        return ('create_box', {'name': _clean_name(_m.group(2))})
    _m = re.search(r'(新建|创建|建)(?:一个|个)?\s*([^\s，。]{1,40}?)(?:分箱|箱|分组)', _t)
    if _m and _clean_name(_m.group(2)):
        return ('create_box', {'name': _clean_name(_m.group(2))})
    _m = re.search(r'(?:把|将)?[「“"]?(.+?)[」”"]?\s*(?:改名为|改名成|重命名为|改名)\s*'
                   r'[「“"]?(.+?)[」”"]?$', _t)
    if _m and _clean_name(_m.group(2)):
        return ('rename_box', {'old': _clean_name(_m.group(1)),
                              'new': _clean_name(_m.group(2))})
    _m = re.search(r'(?:最近|最后|近)\s*([0-9一二两三四五六七八九十]+)\s*条', _n)
    if _m and re.search(r'(移|挪|放|归)', _n):
        _cnt = _cn2int(_m.group(1)) or 1
        _bm = (re.search(r'(?:进|到|入)\s*[「“"《]([^」”"》]{1,40})[」”"》]', _t)
               or re.search(r'(?:进|到|入)\s*([^\s，。]{1,40})$', _t))
        if re.search(r'(移出|拿出来|出箱|移出分箱)', _n):
            return ('move_sessions', {'box': '', 'count': _cnt, 'action': 'remove'})
        _nm = _clean_name(_bm.group(1)) if _bm else ''
        if _nm in ('去', '进', '来', '里面', '其中', '它'):
            _nm = ''
        if _nm or re.search(r'(移进去|放进去|归进去)', _n):
            return ('move_sessions', {'box': _nm, 'count': _cnt,
                                      'action': 'move'})
    if re.search(r'(自动化|自动断电|定时规则)', _n) and re.search(r'(开|关|启|停)', _n):
        _on = bool(re.search(r'(开启|启动|打开|开)', _n)) and \
            not re.search(r'(关闭|停用|关掉|停止)', _n)
        return ('auto_toggle', {'on': _on})
    if '导出' in _n:
        _pd = ('today' if ('今天' in _n or '今日' in _n)
               else 'month' if ('月' in _n) else 'week')
        return ('export_records', {'period': _pd})
    if re.search(r'(今天|今日|本周|这周|本月|充了多少|充入|用电)', _n):
        _pd = ('today' if ('今天' in _n or '今日' in _n)
               else 'month' if ('月' in _n) else 'week')
        return ('query_energy', {'period': _pd})
    if re.search(r'(当前功率|功率|状态|在充|充电情况|端口)', _n):
        return ('query_status', {})
    return None


def parse_all(text):
    """复合指令：按标点/连接词拆句，逐句解析，按顺序返回多个意图。"""
    _segs = [s for s in re.split(r'[，,；;。]|然后|接着|并且|并', text or '') if s.strip()]
    _out = []
    for _s in _segs:
        _p = parse(_s)
        if _p and (_p not in _out):
            _out.append(_p)
    if not _out:
        _p = parse(text)
        if _p:
            _out.append(_p)
    return _out


# ---------------------------------------------------------------- 模型工具描述
def openai_tools():
    """生成 OpenAI 兼容 function calling 的 tools 列表。"""
    _out = []
    for _k, _t in TOOLS.items():
        _props, _req = {}, []
        for _p, (_kind, _r, _dv) in _t['params'].items():
            if _kind == 'port':
                _props[_p] = {'type': 'string', 'enum': list(PORT_KEYS),
                              'description': '端口：1/2/3 为 C 口，a 为 USB-A'}
            elif _kind == 'enum':
                _props[_p] = {'type': 'string', 'enum': list(_r)}
            elif _kind == 'range':
                _props[_p] = {'type': 'integer', 'minimum': _r[0],
                              'maximum': _r[1]}
            elif _kind == 'bool':
                _props[_p] = {'type': 'boolean'}
            else:
                _props[_p] = {'type': 'string'}
            if _dv is None:
                _req.append(_p)
        _out.append({'type': 'function', 'function': {
            'name': _k, 'description': _t['name'] + '：' + _t['desc'],
            'parameters': {'type': 'object', 'properties': _props,
                           'required': _req}}})
    return _out


# ---------------------------------------------------------------- 审计
def log_path():
    _p = os.environ.get('CUKTECH_AGENT_LOG')
    if _p:
        return _p
    _base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(_base, 'CuktechPanel', 'agent_action.log')


def log_action(text, tool, params, result, confirmed, ok=True):
    _row = {'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'text': str(text or ''),
            'tool': tool, 'params': params or {}, 'ok': bool(ok),
            'confirmed': bool(confirmed), 'result': str(result or '')[:300]}
    try:
        _p = log_path()
        os.makedirs(os.path.dirname(_p), exist_ok=True)
        with open(_p, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(_row, ensure_ascii=False) + '\n')
    except Exception:
        pass
    return _row


def read_log(limit=200):
    _p = log_path()
    if not os.path.exists(_p):
        return []
    _out = []
    try:
        with open(_p, 'r', encoding='utf-8') as fh:
            for _ln in fh:
                _ln = _ln.strip()
                if not _ln:
                    continue
                try:
                    _out.append(json.loads(_ln))
                except Exception:
                    continue
    except Exception:
        return []
    return _out[-int(limit):]


# ---------------------------------------------------------------- 执行
def _status_line(d):
    _ps = (d or {}).get('ports')
    if not isinstance(_ps, dict):
        return ''
    _bits, _total = [], 0.0
    for _k, _v in _ps.items():
        if not isinstance(_v, dict):
            continue
        _on = _v.get('on', _v.get('active'))
        _pw = _v.get('power', _v.get('w'))
        _pw = float(_pw) if isinstance(_pw, (int, float)) else 0.0
        if _on:
            _total += max(0.0, _pw)
        _bits.append('%s %s%s' % (
            str(_k).upper() if str(_k) != 'a' else 'USB-A',
            '开' if _on else '关',
            (' %.1fW' % _pw) if isinstance(_pw, (int, float)) else ''))
    return '；'.join(_bits) + ('；合计 %.1fW' % _total)


def execute(tool, params, ctx):
    """执行一个能力，返回 (ok, message, extra)。ctx: api/get_bytes/prefs/ux。"""
    _ok, _p, _err = validate(tool, params)
    if not _ok:
        return False, _err, {}
    _api = ctx.get('api')
    try:
        if tool == 'query_status':
            _c, _d = _api('GET', '/api/status')
            if _c != 200:
                return False, '读取状态失败（HTTP %s）' % _c, {}
            return True, '当前状态：' + (_status_line(_d) or '暂无端口明细'), _d
        if tool == 'query_energy':
            _c, _d = _api('GET', '/api/energy/stats?period=%s' % _p['period'])
            if _c != 200:
                return False, '读取统计失败（HTTP %s）' % _c, {}
            _s = _d.get('stats') if isinstance(_d.get('stats'), dict) else _d
            _wh = _s.get('total_wh', 0)
            _nn = _s.get('session_count', 0)
            _lb = {'today': '今天', 'week': '本周', 'month': '本月'}[_p['period']]
            return True, '%s一共充入 %.1f Wh，会话 %d 次' % (
                _lb, float(_wh or 0), int(_nn or 0)), _s
        if tool == 'set_port':
            _c, _d = _api('POST', '/api/port',
                          {'port': PORT_API.get(_p['port'], _p['port']),
                           'action': _p['action'],
                           'client': 'Agent 工具'})
            if _c != 200:
                return False, '端口接口返回 %s，未执行' % _c, {}
            _c2, _d2 = _api('GET', '/api/status')
            _st = _status_line(_d2) if _c2 == 200 else ''
            return True, '已%s %s；%s' % (
                '开启' if _p['action'] == 'on' else '关闭',
                PORT_NAMES.get(_p['port'], _p['port']), _st or '状态已刷新'), _d2
        if tool == 'all_off':
            _c, _d = _api('POST', '/api/port',
                          {'port': 'all', 'action': 'off', 'client': 'Agent 工具'})
            if _c != 200:
                return False, '全部关闭失败（HTTP %s）' % _c, {}
            _c2, _d2 = _api('GET', '/api/status')
            return True, '4 个端口已全部断电，当前 0 W；需手动重新开启', _d2
        if tool == 'set_limit':
            _pk = _p['port']
            _c, _d = _api('POST', '/api/config',
                          {'charge_limit': {PORT_API.get(_pk, _pk):
                                            int(_p['minutes'])}})
            if _c != 200:
                return False, '设置上限失败（HTTP %s）' % _c, {}
            _tail = ('（本机按分钟计时长上限，不支持电量百分比）'
                     if _pk in ('3', 'a') else '，低功率保护同时生效')
            _note = ('；该口保护套件未启用，上限仅记录'
                     if _pk in ('3', 'a') else '')
            return True, '%s 充电上限已设 %d 分钟%s%s' % (
                PORT_NAMES.get(_pk, _pk), int(_p['minutes']), _tail, _note), {}
        if tool in ('create_box', 'rename_box', 'move_sessions'):
            _ux = ctx.get('ux')
            _prefs = ctx.get('prefs')
            if tool == 'create_box':
                _r = _ux.group_mutate(_prefs, 'create', name=_p['name'])
            elif tool == 'rename_box':
                _bid = _box_id_by_name(_ux, _prefs, _p['old'])
                if not _bid:
                    return False, '没找到名为「%s」的分箱' % _p['old'], {}
                _r = _ux.group_mutate(_prefs, 'rename', bid=_bid, name=_p['new'])
            else:
                _r = _move_sessions(ctx, _p)
            if not (_r or {}).get('ok'):
                return False, (_r or {}).get('msg') or '操作未完成', _r or {}
            if tool == 'create_box':
                _msg = '已新建分箱「%s」' % _p['name']
            elif tool == 'rename_box':
                _msg = '已把「%s」改名为「%s」' % (_p['old'], _p['new'])
            else:
                _msg = _r.get('msg') or '已完成'
            return True, _msg + '（版本 %s，%s）' % (
                _r.get('version'), '全量' if _r.get('mode') == 'full' else '增量'), _r
        if tool == 'auto_toggle':
            _c, _d = _api('POST', '/api/auto_rules', {'enabled': bool(_p['on'])})
            if _c != 200:
                return False, '自动化规则设置失败（HTTP %s）' % _c, {}
            return True, '自动化规则已%s' % ('开启' if _p['on'] else '关闭'), _d
        if tool == 'export_records':
            _gb = ctx.get('get_bytes')
            _raw = _gb('/api/export/sessions?period=%s' % _p['period'], timeout=60)
            if not isinstance(_raw, (bytes, bytearray)) or not _raw:
                return False, '导出失败：没有取得数据', {}
            _dir = os.path.join(os.path.expanduser('~'), 'Documents', 'Cuktech导出')
            os.makedirs(_dir, exist_ok=True)
            _fn = os.path.join(_dir, 'sessions_%s.csv'
                               % time.strftime('%Y%m%d_%H%M%S'))
            with open(_fn, 'wb') as _fh:
                _fh.write(_raw)
            return True, '已导出 %d 字节：%s' % (len(_raw), _fn), {'path': _fn}
    except Exception as _exc:
        return False, '执行异常：%s' % _exc, {}
    return False, '该能力暂无执行器', {}


def _box_id_by_name(ux, prefs, name):
    try:
        _m = ux.device_model_load(prefs) or {}
        for _b in _m.get('boxes') or []:
            if str(_b.get('name') or '') == str(name or ''):
                return _b.get('id')
    except Exception:
        pass
    return None


def _move_sessions(ctx, p):
    _ux = ctx.get('ux')
    _prefs = ctx.get('prefs')
    _api = ctx.get('api')
    _c, _d = _api('GET', '/api/sessions?period=week&limit=200')
    if _c != 200 or not isinstance(_d, dict):
        return {'ok': False, 'msg': '读取会话失败（HTTP %s）' % _c}
    _rows = _d.get('sessions') or _d.get('items') or []
    _rows = sorted(_rows, key=lambda r: float(r.get('start_time') or 0),
                   reverse=True)
    _sel = _rows[:int(p['count'])]
    _keys = ['id:%s' % r.get('id') for r in _sel if r.get('id') is not None]
    if not _keys:
        return {'ok': False, 'msg': '没有可迁移的会话'}
    if p['action'] == 'remove':
        _r = _ux.group_mutate(_prefs, 'remove', keys=_keys)
        if (_r or {}).get('ok'):
            _r['msg'] = '已把最近 %d 条记录移出分组' % len(_keys)
        return _r or {'ok': False, 'msg': '移出失败'}
    _bid = _box_id_by_name(_ux, _prefs, p['box'])
    if not _bid:
        return {'ok': False, 'msg': '没找到名为「%s」的分箱' % p['box']}
    _r = _ux.group_mutate(_prefs, 'move', target=_bid, keys=_keys)
    if (_r or {}).get('ok'):
        _r['msg'] = '已把最近 %d 条记录迁入「%s」' % (len(_keys), p['box'])
    return _r or {'ok': False, 'msg': '迁移失败'}
