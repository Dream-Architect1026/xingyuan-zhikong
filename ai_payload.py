# -*- coding: utf-8 -*-
"""AI 出站载荷的唯一构造点（1.4.0 / ；1.5.6 改为软校验）。

为什么要这个模块：此前"脱敏"只靠调用方自觉（`cfg['redact']` 谁都可不看），
新增一个功能就可能把未授权字段发出去。这里把"发什么"收敛成字段定义：

- 只有 `build(kind, **fields)` 能产出 `AIPayload`，直接实例化会被拒绝；
- 字段定义（schema）同时产出自检清单与实际载荷；未登记/敏感字段**就地剔除并继续**，
  敏感字段等同自动脱敏，绝不因数据问题拦截对话；
- 嵌套统计/会话行同样按定义裁剪，未知列静默剔除并记入 `dropped`；
- `ux_core.ai_run` / `ux_core.ai_stream` 只接受 `AIPayload`，裸字符串/字典一律拒绝。

字段口径与《隐私说明》5.2/5.3 一致：不上送原始高频曲线、逐端口开关与功率、
设备 MAC/序列号、诊断日志原文。
"""
import json


class PayloadError(ValueError):
    """载荷结构错误（缺必填字段等）；不再因未知/敏感字段拦截整条对话。"""


# 显式禁止字段：命中者一律就地剔除（自动脱敏），绝不阻断对话。
DENY = frozenset((
    'mac', 'mac_address', 'bssid', 'serial', 'serial_no', 'sn', 'device_id',
    'imei', 'iccid', 'uuid', 'raw_curve', 'curve', 'points', 'raw_points',
    'samples', 'log', 'logs', 'raw_log',
))

# 会话统计字段定义（1.5.6）：**唯一来源**——白名单、类型校验、实际载荷都从这里派生，
# 不再手写平行常量。新增字段只改这张表，`SESSION_STATS` 自动跟进；开发期自检
# `self_check()` 若发现调用方塞了表外字段会直接判红，而不是运行期拦用户。
STATS_SCHEMA = {
    'period': ('str', '统计时段（今日/近 7 天…）'),
    'total_wh': ('num', '充入电量 Wh'),
    'session_count': ('int', '会话数'),
    'avg_power_w': ('num', '平均功率 W'),
    'peak_power_w': ('num', '峰值功率 W'),
    'total_duration_sec': ('int', '总时长 秒'),
}
SESSION_STATS = tuple(STATS_SCHEMA)
_STATS_DROP = frozenset(('by_port',))

# 会话行（脱敏关闭时发送）：与后端 /api/sessions 的列一一对应，无设备标识。
SESSION_ROW = {
    'id': int, 'port': int, 'start_time': (int, float),
    'end_time': (int, float, type(None)), 'total_wh': (int, float),
    'avg_power_w': (int, float), 'peak_power_w': (int, float),
    'avg_voltage': (int, float), 'avg_current': (int, float),
    'duration_sec': int, 'protocol': str, 'total': int, 'is_active': bool,
}

# 分箱复核行（1.5.6）：会话 + 归属箱 + 置信度；不含设备标识。
BOX_ROW = {
    'id': int, 'port': int, 'start_time': (int, float),
    'total_wh': (int, float), 'avg_power_w': (int, float),
    'peak_power_w': (int, float), 'duration_sec': int,
    'box': str, 'conf': (int, float),
}
BOX_INFO = {'id': str, 'name': str, 'n': int, 'fixed': bool, 'summary': str}

# Agent 快照：端口状态只留计数与总功率，不再逐端口。
AGENT_SCHEMA = {
    'status': {'port_count': ('int', '端口数'),
               'on_count': ('int', '开启端口数'),
               'total_power_w': ('num', '总功率 W')},
    'energy': {'total_wh': ('num', '充入 Wh'),
               'session_count': ('int', '会话数')},
    'records': {'session_count': ('int', '会话数'),
                'reason_count': ('int', '断开原因条数'),
                'device_count': ('int', '设备组数')},
}
AGENT_STATUS = tuple(AGENT_SCHEMA['status'])
AGENT_ENERGY = tuple(AGENT_SCHEMA['energy'])
AGENT_RECORDS = tuple(AGENT_SCHEMA['records'])


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _str(v):
    return isinstance(v, str)


def _bool(v):
    return isinstance(v, bool)


def _dict(v):
    return isinstance(v, dict)


def _list(v):
    return isinstance(v, (list, tuple))


def _str_list(v):
    return isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v)


_KINDS = {
    'chat': {
        'question': (_str, True),
        'session_stats': (_dict, False),
        'curve_authorized': (_bool, False),
        'device_groups': (_int, False),
        'device_suspect': (_bool, False),
        'disconnect_reasons': (_dict, False),
        'data_gaps': (_str_list, False),
    },
    'agent': {
        'question': (_str, True),
        'status': (_dict, False),
        'energy': (_dict, False),
        'report': (_str, False),
        'records': (_dict, False),
        'skipped': (_str_list, False),
        'notes': (_str_list, False),
    },
    'report_daily': {
        'period': (_str, False),
        'total_wh': (_num, False),
        'session_count': (_int, False),
        'peak_power_w': (_num, False),
        'total_duration_sec': (_int, False),
        'device_count': (_int, False),
        'disconnect_reasons': (_str_list, False),
        'sessions': (_list, False),
    },
    'conn_test': {},
    # 1.5.6：分箱复核 / 诊断分析共用同一构造点，避免各写一套提示词。
    'box_review': {
        'question': (_str, True),
        'sessions': (_list, False),
        'boxes': (_list, False),
        'notes': (_str_list, False),
    },
    'diagnosis': {
        'question': (_str, True),
        'stats': (_dict, False),
        'reasons': (_dict, False),
        'sessions': (_list, False),
        'notes': (_str_list, False),
    },
}

# report_daily 的聚合字段：与 sessions（会话明细）互斥。
_REPORT_AGG = ('period', 'total_wh', 'session_count', 'peak_power_w',
               'total_duration_sec', 'device_count', 'disconnect_reasons')


class AIPayload:
    """已按字段定义裁剪的出站载荷。只能由 build() 产出。"""

    __slots__ = ('kind', 'text', 'fields', 'dropped')

    _SEAL = object()

    def __init__(self, kind, text, fields, dropped=None, _seal=None):
        if _seal is not AIPayload._SEAL:
            raise PayloadError('AIPayload 只能由 ai_payload.build() 产出')
        self.kind = kind
        self.text = text
        self.fields = fields
        self.dropped = tuple(dropped or ())

    def __repr__(self):
        return '<AIPayload %s fields=%s dropped=%s>' % (
            self.kind, sorted(self.fields), list(self.dropped))


def is_payload(obj):
    return isinstance(obj, AIPayload)


def text_of(obj, who='ai_run'):
    """取载荷文本；裸字符串/字典一律拒绝（fail closed）。"""
    if not is_payload(obj):
        raise TypeError('%s 只接受 ai_payload.build() 的产物，不接受 %s'
                        % (who, type(obj).__name__))
    return obj.text


# ---------------------------------------------------------------- 二级字段定义
def _sub(d, allowed, where, drop=(), gaps=None):
    """软裁剪：未知列与敏感列一律就地剔除（自动脱敏），剔除的列名记入 gaps。"""
    if not isinstance(d, dict):
        raise PayloadError('%s 必须是对象' % where)
    bad = [k for k in d if k not in allowed or k in DENY]
    bad = [k for k in bad if k not in drop]
    if gaps is not None:
        gaps.extend('%s.%s' % (where, k) for k in bad)
    return {k: d[k] for k in allowed if k in d and k not in DENY}


_TYPES = {'num': _num, 'int': _int, 'str': _str, 'bool': _bool}


def _check_map(d, schema, where, drop=()):
    """按字段定义裁剪：表外/敏感/类型不符一律剔除并记录，不阻断对话。"""
    if not isinstance(d, dict):
        raise PayloadError('%s 必须是对象' % where)
    dropped = [k for k in d if k not in schema and k not in drop]
    dropped.extend(k for k in d if k in DENY and k not in dropped)
    out = {}
    for k, (t, _desc) in schema.items():
        if k not in d or k in DENY:
            continue
        if not _TYPES[t](d[k]):
            dropped.append(k)
            continue
        out[k] = d[k]
    return out, dropped


def stats_payload(values):
    """由 STATS_SCHEMA 产出 session_stats → (载荷, 被剔除字段名)。"""
    return _check_map(values, STATS_SCHEMA, 'session_stats', _STATS_DROP)


def self_check():
    """冒烟自检：字段定义 / 实际载荷一致；返回问题列表（空 = 通过）。

    失败只记录并降级（不拦用户）；打包门禁与 `CUKTECH_SELFCHECK=1` 会打印结果。
    """
    problems = []
    if set(SESSION_STATS) != set(STATS_SCHEMA):
        problems.append('SESSION_STATS 与 STATS_SCHEMA 不一致')
    if 'period' not in SESSION_STATS:
        problems.append('session_stats 缺 period（1.5.6 回归项）')
    for _sec, _sch in AGENT_SCHEMA.items():
        _tup = {'status': AGENT_STATUS, 'energy': AGENT_ENERGY,
                'records': AGENT_RECORDS}[_sec]
        if tuple(_sch) != tuple(_tup):
            problems.append('AGENT_%s 与 schema 不一致' % _sec.upper())
    for _k in list(STATS_SCHEMA) + list(SESSION_ROW):
        if _k in DENY:
            problems.append('字段 %s 同时在白名单与禁止表' % _k)
    _sample = {'period': '今日', 'total_wh': 12.5, 'session_count': 3,
               'avg_power_w': 20.0, 'peak_power_w': 45.0, 'total_duration_sec': 1800}
    try:
        st, _gap = stats_payload(_sample)
        if _gap:
            problems.append('合法统计字段被误剔除：%s' % _gap)
        build('chat', question='自检', session_stats=st,
              curve_authorized=True, device_groups=2, device_suspect=False,
              disconnect_reasons={'人工关闭': 1},
              data_gaps=['部分数据暂缺，本次未纳入'])
        build('agent', question='自检',
              status={'port_count': 4, 'on_count': 1, 'total_power_w': 20.0},
              energy={'total_wh': 12.5, 'session_count': 3},
              records={'session_count': 3, 'reason_count': 1, 'device_count': 2},
              skipped=['x'], notes=['n'])
        build('report_daily', period='today', total_wh=12.5, session_count=3,
              peak_power_w=45.0, total_duration_sec=1800, device_count=2,
              disconnect_reasons=['人工关闭'])
        build('report_daily', sessions=[{
            'id': 1, 'port': 1, 'start_time': 1.0, 'end_time': 2.0,
            'total_wh': 1.0, 'avg_power_w': 5.0, 'peak_power_w': 9.0,
            'avg_voltage': 5.0, 'avg_current': 1.0, 'duration_sec': 60,
            'protocol': 'PD', 'total': 0, 'is_active': False}])
        build('conn_test')
        build('box_review', question='复核',
              boxes=[{'id': 'GRP-VIVO', 'name': 'vivo X200 Pro Mini', 'n': 5,
                      'fixed': True, 'summary': '峰值 15–100W'}],
              sessions=[{'id': 1, 'port': 1, 'start_time': 1.0, 'total_wh': 12.0,
                         'avg_power_w': 20.0, 'peak_power_w': 45.0,
                         'duration_sec': 1800, 'box': 'GRP-VIVO', 'conf': 0.55}])
        build('diagnosis', question='诊断',
              stats={'total_wh': 12.0}, reasons={'人工关闭': 1},
              sessions=[{'id': 1, 'port': 1, 'start_time': 1.0, 'total_wh': 12.0,
                         'avg_power_w': 20.0, 'peak_power_w': 45.0,
                         'duration_sec': 1800}])
    except Exception as exc:
        problems.append('样例载荷构造失败：%r' % (exc,))
    try:
        _p = build('chat', question='自检', unknown_field=1)
        if 'unknown_field' not in _p.dropped:
            problems.append('表外字段未被剔除并记录')
    except Exception as exc:
        problems.append('表外字段不应阻断对话：%r' % (exc,))
    try:
        _p = build('chat', question='自检', session_stats={'period': '今日', 'mac': 'x'})
        if 'mac' in json.dumps(_p.fields, ensure_ascii=False):
            problems.append('敏感字段未脱敏')
    except Exception as exc:
        problems.append('敏感字段不应阻断对话：%r' % (exc,))
    return problems


def _type_ok(v, t):
    if t is int:
        return _int(v)
    if t is float:
        return _num(v)
    if t is str:
        return isinstance(v, str)
    if t is bool:
        return isinstance(v, bool)
    if t is type(None):
        return v is None
    if isinstance(t, tuple):
        return any(_type_ok(v, x) for x in t)
    return False


def _rows(rows, schema=None):
    """按行定义裁剪：表外/敏感/类型不符的列静默剔除（自动脱敏）。"""
    _schema = schema or SESSION_ROW
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        bad = [k for k in r if k not in _schema or k in DENY
               or not _type_ok(r[k], _schema.get(k))]
        if bad:
            r = {k: v for k, v in r.items() if k not in bad}
        out.append(dict(r))
    return out


# ---------------------------------------------------------------- 渲染
def _reasons(rs):
    return '、'.join('%s×%d' % (k, v) for k, v in rs.items())


def _render_chat(f):
    ctx = []
    if f.get('session_stats'):
        ctx.append('会话统计：%s' % json.dumps(
            f['session_stats'], ensure_ascii=False)[:400])
    if f.get('curve_authorized'):
        ctx.append('采样窗口：最近 24 小时（仅聚合统计，不含采样点）')
    if f.get('device_groups'):
        ctx.append('设备识别：%d 组（%s）' % (
            f['device_groups'],
            '含疑似同设备' if f.get('device_suspect') else '无重合'))
    if f.get('disconnect_reasons'):
        ctx.append('断开原因：' + _reasons(f['disconnect_reasons']))
    body = '\n'.join(ctx) or ('（本次未包含应用内数据：仅按通用充电知识回答，'
                              '不做设备结论）')
    gaps = list(f.get('data_gaps') or [])
    if gaps:
        body += '\n' + '\n'.join(gaps)
    return '【应用内数据上下文】\n%s\n\n【用户问题】\n%s' % (body, f['question'])


def _render_agent(f):
    out = []
    if f.get('status'):
        s = _sub(f['status'], AGENT_STATUS, 'status')
        out.append('端口状态：共 %d 个端口，%d 个开启，总功率 %.1fW'
                   % (int(s.get('port_count') or 0),
                      int(s.get('on_count') or 0),
                      float(s.get('total_power_w') or 0)))
    if f.get('energy'):
        e = _sub(f['energy'], AGENT_ENERGY, 'energy')
        out.append('今日能量：%s Wh · 会话 %s 次'
                   % (e.get('total_wh', '—'), e.get('session_count', '—')))
    if f.get('report'):
        out.append('充电日报（本地规则）：\n' + f['report'][:800])
    if f.get('records'):
        r = _sub(f['records'], AGENT_RECORDS, 'records')
        out.append('记录页摘要：会话 %s 次 · 断开原因 %s 条 · 设备组 %s'
                   % (r.get('session_count', 0), r.get('reason_count', 0),
                      r.get('device_count', 0)))
    for s in f.get('skipped') or []:
        out.append('（能力 %s 未获授权，已跳过）' % s)
    for n in f.get('notes') or []:
        out.append(n)
    body = '\n'.join(out) or '（未挂载任何能力）'
    return '【应用内数据上下文·已挂载能力】\n%s\n\n【用户输入】\n%s' % (
        body, f['question'])


def _render_report(f):
    if 'sessions' in f:
        head = '以下是会话明细（不含设备标识）：\n'
        data = _rows(f['sessions'])
    else:
        head = '以下是聚合后的充电统计（不含设备标识）：\n'
        data = {k: f[k] for k in _REPORT_AGG if k in f}
    return (head + json.dumps(data, ensure_ascii=False)
            + '\n请给出效率评估、异常识别、健康趋势与三条建议，200 字内。')


def _render_conn_test(_f):
    return '只回复两个字：正常'


def _render_box_review(f):
    _b = json.dumps(f.get('boxes') or [], ensure_ascii=False)
    _s = json.dumps(f.get('sessions') or [], ensure_ascii=False)
    _n = '\n'.join(f.get('notes') or [])
    return ('【现有分组】\n%s\n\n【待复核会话（含当前归属与置信度）】\n%s\n%s\n\n'
            '【任务】\n%s\n只依据以上数据给出建议；样本不足请注明条数，不臆造。\n'
            '输出格式：每条一行「会话 id：建议归入 X｜理由（引用数值）」；'
            '再给 1–3 条约束收窄/放宽建议，附实测分布。'
            % (_b[:1200], _s[:2400], _n, f['question']))


def _render_diagnosis(f):
    _st = json.dumps(f.get('stats') or {}, ensure_ascii=False)
    _rs = json.dumps(f.get('reasons') or {}, ensure_ascii=False)
    _s = json.dumps(f.get('sessions') or [], ensure_ascii=False)
    _n = '\n'.join(f.get('notes') or [])
    return ('【聚合统计】%s\n【断开原因】%s\n【会话样本】%s\n%s\n\n'
            '【任务】\n%s\n每个结论后必须附引用（时间/峰值/均功率等实际数值）；'
            '样本不足写「样本不足（N 条）」，不空口断言、不编造。'
            % (_st[:600], _rs[:400], _s[:2000], _n, f['question']))


_RENDER = {
    'chat': _render_chat,
    'agent': _render_agent,
    'report_daily': _render_report,
    'conn_test': _render_conn_test,
    'box_review': _render_box_review,
    'diagnosis': _render_diagnosis,
}

# 按 kind 选择会话行定义（分箱复核用 BOX_ROW，其余用 SESSION_ROW）。
_ROW_SCHEMA = {'report_daily': SESSION_ROW, 'box_review': BOX_ROW,
               'diagnosis': SESSION_ROW}


# 嵌套结构裁剪：与顶层字段同一份定义，保证"实际载荷 ⊆ 白名单"对嵌套同样成立。
_NESTED_CLEAN = {
    'chat': {'session_stats': lambda v: stats_payload(v)},
    'agent': {
        'status': lambda v: _check_map(v, AGENT_SCHEMA['status'], 'status'),
        'energy': lambda v: _check_map(v, AGENT_SCHEMA['energy'], 'energy'),
        'records': lambda v: _check_map(v, AGENT_SCHEMA['records'], 'records'),
    },
}


def build(kind, **fields):
    """构造出站载荷（全软校验，永不因数据问题拦截对话）。

    - 表外字段、敏感字段（DENY）、类型不符：一律剔除并记入 `dropped`，**继续构造**；
    - 敏感字段等同自动脱敏，不报错、不中止；
    - 只有必填字段（如 question）缺失/类型不符、report_daily 互斥冲突抛
      `PayloadError`——调用方应据此回落本地回答，而不是告诉用户"未发送"。
    """
    spec = _KINDS.get(kind)
    if spec is None:
        raise PayloadError('未登记的载荷 kind：%r（可用：%s）'
                           % (kind, sorted(_KINDS)))
    clean, dropped = {}, []
    for k, v in fields.items():
        if k in DENY:
            dropped.append(k)
            continue
        if k not in spec:
            dropped.append(k)
            continue
        chk = spec[k][0]
        if not chk(v):
            dropped.append(k)
            continue
        clean[k] = v
    for k, fn in _NESTED_CLEAN.get(kind, {}).items():
        if k in clean:
            out, bad = fn(clean[k])
            dropped.extend('%s.%s' % (k, b) for b in bad)
            clean[k] = out
    if 'sessions' in clean:
        clean['sessions'] = _rows(clean['sessions'],
                                  _ROW_SCHEMA.get(kind))
    if 'boxes' in clean:
        clean['boxes'] = _rows(clean['boxes'], BOX_INFO)
    for k, (_chk, required) in spec.items():
        if required and k not in clean:
            raise PayloadError('%s 缺少必填字段：%s' % (kind, k))
    if kind == 'report_daily':
        agg = [k for k in _REPORT_AGG if k in clean]
        if 'sessions' in clean and agg:
            raise PayloadError('report_daily: sessions 与聚合字段互斥（%s）'
                               % agg)
        if 'sessions' not in clean and not agg:
            raise PayloadError('report_daily: 需要聚合字段或 sessions')
    return AIPayload(kind, _RENDER[kind](clean), dict(clean), dropped,
                     _seal=AIPayload._SEAL)
