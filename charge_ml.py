# -*- coding: utf-8 -*-
"""星源智控控制面板 · 充电会话离线推理（纯 Python 标准库实现，禁止依赖 torch/numpy/sklearn）。

模型：规则弱标签训练的线性 softmax（LogisticRegression），权重来自 models/charge_model.json。
用法：
    from charge_ml import available, infer, MODEL_VERSION
    result = infer(session, points)   # points 为 [{'timestamp','voltage','current','power','protocol'}, ...]
    # 返回 None 时调用方应回落到规则判定。
"""
import json
import os
import math
import datetime

MODEL_VERSION = "ck2026.11-r07-v1"

_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "charge_model.json")
_CACHE = {"loaded": False, "model": None}

_READABLE = {
    "duration_sec": "充电时长",
    "total_wh": "总能量",
    "peak_power_w": "峰值功率",
    "avg_power_w": "平均功率",
    "avg_voltage": "电压中位数",
    "avg_current": "电流中位数",
    "tail10_ratio": "末段10%功率/峰值",
    "peak_pos_ratio": "峰值位置(相对)",
    "stage_switch_count": "阶段切换数",
    "power_drop_ratio": "功率骤降比",
    "n_points": "采样点数",
    "max_gap_s": "最大采样间隔",
    "start_hour_sin": "开始小时(sin)",
    "start_hour_cos": "开始小时(cos)",
}


def _load():
    if _CACHE["loaded"]:
        return _CACHE["model"]
    _CACHE["loaded"] = True
    try:
        with open(_MODEL_PATH, "r", encoding="utf-8") as f:
            m = json.load(f)
        if m.get("arch") != "logistic":
            raise ValueError("unsupported arch")
        names = m["feature_names"]
        classes = m["classes"]
        coef = m["coef"]
        if len(m["mean"]) != len(names) or len(m["std"]) != len(names):
            raise ValueError("normalization dim mismatch")
        if len(coef) != len(classes) or any(len(row) != len(names) for row in coef):
            raise ValueError("weight dim mismatch")
        if len(m["intercept"]) != len(classes):
            raise ValueError("intercept dim mismatch")
        _CACHE["model"] = m
    except Exception:
        _CACHE["model"] = None
    return _CACHE["model"]


def available():
    """模型文件存在且结构可解析时返回 True。"""
    return _load() is not None


def _resample(vals, n=32):
    m = len(vals)
    if m == 0:
        return [0.0] * n
    if m == 1:
        return [float(vals[0])] * n
    out = []
    for j in range(n):
        u = j / (n - 1.0) * (m - 1)
        i0 = int(u)
        i1 = min(i0 + 1, m - 1)
        w = u - i0
        out.append(float(vals[i0]) * (1 - w) + float(vals[i1]) * w)
    return out


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(x) or math.isinf(x):
        return 0.0
    return x


def _features(session, points):
    """与训练脚本完全一致的 114 维特征；任何关键字段缺失/异常返回 None。

    2.1.1：协议类别（one-hot / unknown 位）已从特征向量中撤回，
    协议只用于界面展示与可选约束，不参与训练；保留的均为数值型口径
    （功率/能量/时长/电压/电流/时段/端口/曲线形态）。
    """
    if not session or not points:
        return None
    try:
        port = int(session.get("port"))
    except (TypeError, ValueError):
        return None
    if port not in (1, 2, 3, 4):
        return None
    try:
        pts = sorted(points, key=lambda p: _num(p.get("timestamp", 0)))
    except Exception:
        return None
    n = len(pts)
    if n < 3:
        return None
    vals_p = [_num(p.get("power")) for p in pts]
    vals_v = [_num(p.get("voltage")) for p in pts]
    vals_i = [_num(p.get("current")) for p in pts]
    ts = [_num(p.get("timestamp")) for p in pts]
    pk = max(vals_p)
    if pk <= 0:
        return None
    duration = _num(session.get("duration_sec"))
    if duration <= 0:
        duration = max(0.0, ts[-1] - ts[0])
    if duration <= 0:
        return None
    tail = vals_p[max(0, int(n * 0.9)):]
    tail_ratio = (sum(tail) / len(tail)) / pk
    ipk = max(i for i, v in enumerate(vals_p) if v >= pk * 0.98)
    peak_pos = ipk / (n - 1.0)
    stage = 0
    prev_proto = None
    for p in pts:
        pr = p.get("protocol")
        if pr and pr != prev_proto:
            if prev_proto is not None:
                stage += 1
            prev_proto = pr
    drop = 0.0
    for i in range(1, n):
        d = (vals_p[i - 1] - vals_p[i]) / pk
        if d > drop:
            drop = d
    gaps = [ts[i + 1] - ts[i] for i in range(n - 1)]
    max_gap = max(gaps) if gaps else 0.0
    sv = sorted(vals_v)
    si = sorted(vals_i)
    med_v = sv[len(sv) // 2] if len(sv) % 2 else (sv[len(sv) // 2 - 1] + sv[len(sv) // 2]) / 2.0
    med_i = si[len(si) // 2] if len(si) % 2 else (si[len(si) // 2 - 1] + si[len(si) // 2]) / 2.0
    start = _num(session.get("start_time"))
    if start <= 0:
        start = ts[0]
    hour = datetime.datetime.fromtimestamp(start).hour

    feats = []
    feats += _resample(vals_p)
    feats += _resample(vals_v)
    feats += _resample(vals_i)
    feats += [
        duration, _num(session.get("total_wh")), round(pk, 2),
        round(sum(vals_p) / n, 3), round(med_v, 2), round(med_i, 2),
        round(tail_ratio, 4), peak_pos, float(stage), round(drop, 4), float(n),
        round(max_gap, 2),
        math.sin(2 * math.pi * hour / 24.0), math.cos(2 * math.pi * hour / 24.0),
    ]
    feats += [1.0 if port == pn else 0.0 for pn in (1, 2, 3, 4)]
    return feats


def _softmax(logits):
    mx = max(logits)
    exps = [math.exp(v - mx) for v in logits]
    s = sum(exps)
    if s <= 0:
        return [1.0 / len(logits)] * len(logits)
    return [e / s for e in exps]


def infer(session, points):
    """返回 dict；模型缺失或特征异常时返回 None（调用方回落规则判定）。"""
    m = _load()
    if m is None:
        return None
    f = _features(session, points)
    if f is None or len(f) != len(m["feature_names"]):
        return None
    try:
        z = [(f[i] - m["mean"][i]) / (m["std"][i] if abs(m["std"][i]) > 1e-12 else 1.0)
             for i in range(len(f))]
        logits = []
        for k in range(len(m["classes"])):
            w = m["coef"][k]
            logits.append(sum(w[i] * z[i] for i in range(len(z))) + m["intercept"][k])
        probs = _softmax(logits)
        best = max(range(len(probs)), key=lambda i: probs[i])
        w = m["coef"][best]
        contrib = sorted(((m["feature_names"][i], w[i] * z[i]) for i in range(len(z))),
                         key=lambda kv: abs(kv[1]), reverse=True)
        basis = [{"feature": _READABLE.get(name, name), "score": round(float(v), 4)}
                 for name, v in contrib[:5]]
        return {
            "device_cluster": None,
            "disconnect_reason": m["classes"][best],
            "confidence": round(float(probs[best]), 4),
            "basis": basis,
            "label_source": "model",
        }
    except Exception:
        return None
