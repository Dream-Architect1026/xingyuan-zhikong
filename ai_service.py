# -*- coding: utf-8 -*-
"""统一 AI 服务层（1.5.6）。

一个入口负责：取数口径（调用方传入已聚合的数据）→ 组装上下文（ai_payload 字段定义）
→ 调模型 → 降级（规则结论）→ 留痕（ai_audit.log）。

场景：
- chat       小源对话（含工具执行，工具在 agent_tools）
- box_review 分箱复核
- diagnosis  诊断分析
"""
import json
import os
import time

import ai_payload
import ux_core as ux

# 场景 key → (ai_payload kind, 中文名, 默认启用)
SCENES = {
    'chat': ('chat', '对话', True),
    'agent': ('agent', 'Agent 搭建', True),
    'box_review': ('box_review', '分箱复核', True),
    'diagnosis': ('diagnosis', '诊断分析', True),
    'auto_suggest': ('diagnosis', '自动建议', False),
}

def payload(scene, question, data):
    """唯一组装点：场景 → 字段定义 → 载荷（含软裁剪与脱敏）。"""
    _kind = SCENES.get(scene, ('chat', '', True))[0]
    return ai_payload.build(_kind, question=question, **(data or {}))

DEFAULT_SYSTEM = """你是「小源」，这台充电管理面板的助手。

一、身份与职责
负责答疑、诊断分析、分箱建议，并在用户授权范围内执行端口与分组操作。

二、领域事实
- 端口：C1/C2/C3 为三个 C 口，A 为 USB-A 口。
- 安全规则：无充电满 10 分钟自动断电，仅 C1/C2/C3 参与，A 口豁免。
- 充电上限：按分钟计的端口最长充电时长；保护阈值用于低功率/过压保护。
- 断开原因常见来源：自动化、设备停止、充满、超时、人工。

三、数据口径
- 会话字段：Wh 为充入电量，W 为功率，时长为秒或分钟（按字段注明换算）。
- 满充按估算：设备标记充满，或达到该设备历史最大充入的 90%。
- 样本不足必须写「样本不足（N 条）」，不得用少量样本下结论。

四、回答规则
先给结论再给依据，引用具体会话时间与数值（如「11-12 14:03 那条峰值 28.3W」）；
算不出来的不编；不确定就说不确定。

五、行为边界
不绕过断电与保护规则；动作类操作需用户确认；失败要说明原因。

六、表达要求
中文、口语、简洁；不出现字段名、白名单、内部报错等实现细节。"""


def sys_prompt(prefs):
    _s = ''
    try:
        _s = str(prefs.get('aiSystemPrompt') or '')
    except Exception:
        _s = ''
    return _s.strip() or DEFAULT_SYSTEM


def scene_on(prefs, scene):
    _key = {'chat': 'aiEnabled', 'box_review': 'aiSceneBoxReview',
            'diagnosis': 'aiSceneDiag', 'auto_suggest': 'aiSceneAuto'}.get(scene)
    if not _key:
        return False
    if not bool(prefs.get('aiEnabled', False)):
        return False
    return bool(prefs.get(_key, SCENES[scene][2]))


def make_cfg(prefs, secrets, scene='chat', system=None):
    _c = ux.ai_config(prefs, secrets)
    _c['system'] = (system or '') + '\n\n' + sys_prompt(prefs)
    _c['scene'] = scene
    return _c


def audit_path():
    _p = os.environ.get('CUKTECH_AI_AUDIT')
    if _p:
        return _p
    _base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(_base, 'CuktechPanel', 'ai_audit.log')


def audit(scene, started, degraded=False, adopted=False, ok=True, note=''):
    _row = {'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'scene': scene,
            'ms': int((time.time() - float(started or time.time())) * 1000),
            'degraded': bool(degraded), 'adopted': bool(adopted),
            'ok': bool(ok), 'note': str(note or '')[:200]}
    try:
        _p = audit_path()
        os.makedirs(os.path.dirname(_p), exist_ok=True)
        with open(_p, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(_row, ensure_ascii=False) + '\n')
    except Exception:
        pass
    return _row


def read_audit(limit=500):
    _p = audit_path()
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
                    pass
    except Exception:
        return []
    return _out[-int(limit):]


def usage():
    _rows = read_audit(2000)
    return {'calls': len(_rows),
            'degraded': sum(1 for r in _rows if r.get('degraded')),
            'adopted': sum(1 for r in _rows if r.get('adopted')),
            'avg_ms': (int(sum(r.get('ms') or 0 for r in _rows) / len(_rows))
                       if _rows else 0)}


def ask(scene, question, data, cfg, on_done, fallback=None):
    """统一调用：组装 → 调模型 → 失败降级 → 留痕。on_done(text, degraded, err)。"""
    _kind = SCENES.get(scene, ('chat', '', True))[0]
    _t0 = time.time()

    def _cb(txt, err):
        _deg = not bool(txt)
        if _deg:
            txt = fallback() if callable(fallback) else (fallback or '')
        audit(scene, _t0, degraded=_deg, ok=bool(txt), note=str(err or '')[:120])
        on_done(txt or '', _deg, err)

    try:
        _pl = ai_payload.build(_kind, question=question, **data)
    except Exception as _exc:
        _txt = fallback() if callable(fallback) else (fallback or '')
        audit(scene, _t0, degraded=True, ok=bool(_txt), note=repr(_exc)[:120])
        on_done(_txt, True, _exc)
        return
    ux.ai_run(cfg, _pl, _cb)


# ---------------------------------------------------------------- 规则版结论
def rule_diagnosis(analysis, rows):
    """模型不可用时的诊断结论：只写能算出来的，逐条附引用。"""
    _rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not _rows:
        return ['样本不足（0 条），暂不下结论。']
    _n = len(_rows)
    _cl = []
    _pk = max(_rows, key=lambda r: float(r.get('peak_power_w') or 0))
    _av = max(_rows, key=lambda r: float(r.get('duration_sec') or 0))
    _cl.append('峰值最高的一次：%s，峰值 %.1fW、均功率 %.1fW。' % (
        _when(_pk), float(_pk.get('peak_power_w') or 0),
        float(_pk.get('avg_power_w') or 0)))
    _cl.append('持续最久的一次：%s，%.0f 分钟、充入 %.1fWh。' % (
        _when(_av), float(_av.get('duration_sec') or 0) / 60.0,
        float(_av.get('total_wh') or 0)))

    def _hour(r):
        try:
            return time.localtime(float(r.get('start_time') or 0)).tm_hour
        except Exception:
            return None

    _night = [r for r in _rows if _hour(r) is not None
              and (_hour(r) >= 22 or _hour(r) < 6)]
    if _night:
        _ex = _night[0]
        _cl.append('过夜充电 %d/%d 次（%.0f%%），如 %s 那条 %.0f 分钟、%.1fWh。' % (
            len(_night), _n, 100.0 * len(_night) / _n, _when(_ex),
            float(_ex.get('duration_sec') or 0) / 60.0,
            float(_ex.get('total_wh') or 0)))
    else:
        _cl.append('没有过夜充电记录（22:00–06:00 起充 0/%d 次）。' % _n)
    _low = [r for r in _rows if float(r.get('avg_power_w') or 0) < 5.0]
    if _low:
        _ex = _low[0]
        _cl.append('低功率充电（均功率 < 5W）%d/%d 次（%.0f%%），如 %s 那条均功率 '
                   '%.1fW。' % (len(_low), _n, 100.0 * len(_low) / _n,
                                 _when(_ex), float(_ex.get('avg_power_w') or 0)))
    _wh = sorted(float(r.get('total_wh') or 0) for r in _rows)
    _dm = sorted(float(r.get('duration_sec') or 0) / 60.0 for r in _rows)
    _cl.append('单次平均充入 %.1fWh（中位 %.1fWh），中位时长 %.0f 分钟。' % (
        sum(_wh) / _n, _wh[_n // 2], _dm[_n // 2]))
    _hs = {}
    for r in _rows:
        _h = _hour(r)
        if _h is not None:
            _hs[_h] = _hs.get(_h, 0) + 1
    if _hs:
        _h, _c = max(_hs.items(), key=lambda x: x[1])
        _cl.append('充电时段最集中在 %02d:00–%02d:00，%d/%d 次（%.0f%%）。' % (
            _h, (_h + 1) % 24, _c, _n, 100.0 * _c / _n))
    _rs = (analysis or {}).get('reasons') or []
    if _rs:
        from collections import Counter
        _c = Counter((r or {}).get('label') or '未分类' for r in _rs)
        _k, _v = _c.most_common(1)[0]
        _cl.append('断开原因最多的是「%s」，占 %d/%d 次。' % (_k, _v, _n))
    if _n < 10:
        _cl.append('样本不足（%d 条），以上仅作参考。' % _n)
    return _cl


def rule_box_review(model, rows, shapes, thr=None):
    """规则版分箱复核：低置信度分箱 / 偏离箱中心的会话 / 约束过宽过窄。"""
    _rows = list(rows or [])
    if not _rows:
        return {'items': [], 'cons': [], 'notes': ['样本不足（0 条），暂不复核']}
    _model = model or {}
    _boxes = [b for b in _model.get('boxes') or [] if isinstance(b, dict)]
    _thr = float(thr or 1.6)
    _as = ux.device_assign(_rows, _model, '均衡',
                           manual=_model.get('manual'), shapes=shapes,
                           cons=True)
    _gmap = {str(g.get('id')): g for g in _as.get('groups') or []}
    _mem = {str(b.get('id')): ux.group_members(_model, b.get('id'), _rows,
                                               shapes, _thr / 10.0)
            for b in _boxes}
    _items = []
    for _b in _boxes:
        _bid = str(_b.get('id'))
        _g = _gmap.get(_bid) or {}
        _n = int(_g.get('n') or len(_mem.get(_bid) or []))
        _conf = float(_g.get('confidence') or 0)
        if _n >= 3 and _conf < 0.6:
            _items.append({'key': '', 'id': None, 'from': _bid, 'to': '',
                           'why': '「%s」整体置信度 %.2f（%d 条），建议人工抽查'
                                  % (_b.get('name'), _conf, _n)})
        _ctr = _b.get('center') if isinstance(_b.get('center'), dict) else {}
        if not _ctr:
            continue
        _dists = []
        for _s in _mem.get(_bid) or []:
            try:
                _dists.append((ux.dev_dist(ux.dev_vec(_s), _ctr), _s))
            except Exception:
                continue
        if len(_dists) < 2:
            continue
        _dists.sort(key=lambda x: -x[0])
        _med = sorted(d for d, _s in _dists)[len(_dists) // 2]
        for _d, _s in _dists[:2]:
            if _d <= max(_med * 2.0, 1e-6):
                continue
            _to = _nearest_other(_b, _s, _boxes)
            _items.append({'key': ux.session_key(_s), 'id': _s.get('id'),
                           'from': _bid, 'to': _to,
                           'why': '与「%s」中心偏离 %.2f（箱内中位 %.2f），'
                                  '疑似分错' % (_b.get('name'), _d, _med)})
    # 逐条：与当前箱中心距离明显大于最近其它箱中心 → 疑似分错（带数值理由）
    _manual = _model.get('manual') or {}
    _seen = {i.get('key') for i in _items if i.get('key')}
    for _s in _rows[:160]:
        _key = ux.session_key(_s)
        if _key in _seen:
            continue
        _cur = _manual.get(_key)
        if _cur in (None, '', ux.UNASSIGNED):
            continue
        _cb = next((b for b in _boxes if str(b.get('id')) == str(_cur)), None)
        if not _cb:
            continue
        try:
            _v = ux.dev_vec(_s)
            _dc = ux.dev_dist(_v, _cb.get('center') or {})
        except Exception:
            continue
        _to, _db = _nearest_other(_cb, _s, _boxes), float('inf')
        if _to:
            _tb = next((b for b in _boxes if str(b.get('id')) == str(_to)), None)
            try:
                _db = ux.dev_dist(_v, (_tb or {}).get('center') or {})
            except Exception:
                _db = float('inf')
        if _to and _db < _dc * 0.9:
            _items.append({'key': _key, 'id': _s.get('id'), 'from': str(_cur),
                           'to': _to,
                           'why': '到「%s」中心 %.2f，到「%s」中心 %.2f，'
                                  '后者更近' % (_cb.get('name'), _dc,
                                              (_tb or {}).get('name'), _db)})

    _cons = []
    # 未归箱候选：约束命中的会话建议归组（固定组锁定但仍可迁入，属"归入"不算改写）
    _manual = _model.get('manual') or {}
    _valid = {str(b.get('id')) for b in _boxes}
    _seen_keys = {i.get('key') for i in _items if i.get('key')}
    _cn = 0
    for _s in _rows:
        _key = ux.session_key(_s)
        if _key in _seen_keys:
            continue
        _mk = _manual.get(_key)
        if _mk not in (None, ''):          # 已人工归属/已移出：尊重，不打扰
            continue
        _hits = [b for b in _boxes if ux.box_cons_ok(_s, b)]
        if len(_hits) != 1 or str(_hits[0].get('id')) not in _valid:
            continue
        _b = _hits[0]
        _items.append({
            'key': _key, 'id': _s.get('id'), 'from': '', 'to': str(_b.get('id')),
            'why': '未归箱：%s 峰值 %.1fW、均功率 %.1fW、充入 %.1fWh，'
                   '落在「%s」约束区间内' % (
                       _when(_s), float(_s.get('peak_power_w') or 0),
                       float(_s.get('avg_power_w') or 0),
                       float(_s.get('total_wh') or 0), _b.get('name'))})
        _seen_keys.add(_key)
        _cn += 1
        if _cn >= 20:
            break
    _notes = []
    for _b in _boxes:
        if not _mem.get(str(_b.get('id'))) and not any(
                i.get('to') == str(_b.get('id')) for i in _items):
            _notes.append('「%s」库内暂无可匹配会话，约束区间可能偏窄'
                          % _b.get('name'))

    _cons = []
    for _b in _boxes:
        _cons_rows = _mem.get(str(_b.get('id'))) or []
        _consd = _b.get('cons') if isinstance(_b.get('cons'), dict) else {}
        for _k, _label, _kind, _unit in ux.BOX_CONS_DIMS:
            _c = _consd.get(_k)
            if _kind != 'num' or not isinstance(_c, dict) or not _c.get('on'):
                continue
            _dist = ux.box_dim_dist(_cons_rows, _k)
            if not _dist:
                continue
            _lo, _mid, _hi = _dist
            _cmin, _cmax = float(_c.get('min')), float(_c.get('max'))
            if _lo < _cmin * 0.9 or _hi > _cmax * 1.1:
                _cons.append({'id': _b.get('id'), 'dim': _k,
                              'text': '「%s」%s 约束过窄：设 %.0f–%.0f%s，'
                                      '实测 %.0f–%.0f%s（中位 %.0f）' % (
                                          _b.get('name'), _label, _cmin, _cmax,
                                          _unit, _lo, _hi, _unit, _mid)})
            elif (_cmin < _lo * 0.6) and (_cmax > _hi * 1.4):
                _cons.append({'id': _b.get('id'), 'dim': _k,
                              'text': '「%s」%s 约束过宽：设 %.0f–%.0f%s，'
                                      '实测 %.0f–%.0f%s（中位 %.0f），可收窄' % (
                                          _b.get('name'), _label, _cmin, _cmax,
                                          _unit, _lo, _hi, _unit, _mid)})
    return {'items': _items[:60], 'cons': _cons[:20], 'notes': _notes[:8]}


def _nearest_other(cur, s, boxes):
    _best, _bd = '', float('inf')
    try:
        _v = ux.dev_vec(s)
    except Exception:
        return ''
    for _b in boxes:
        if str(_b.get('id')) == str(cur.get('id')):
            continue
        _c = _b.get('center') if isinstance(_b.get('center'), dict) else {}
        if not _c:
            continue
        try:
            _d = ux.dev_dist(_v, _c)
        except Exception:
            continue
        if _d < _bd:
            _best, _bd = str(_b.get('id')), _d
    return _best


def _when(r):
    try:
        _t = time.localtime(float(r.get('start_time') or 0))
        return time.strftime('%m-%d %H:%M', _t)
    except Exception:
        return '时间未知'
