"""C51：无充电满 10 分钟自动关端口（后端计时，与界面无关）。

阈值唯一来源是本文件的 DEFAULT_WINDOW_S：config 缺省值/示例配置、日志里的
window 字段都从这里派生。界面不显示无充电计时/倒计时，也不参与判定。

判定只在后端进程里跑：进程活着就一直按端口累计"连续无充电时长"，
面板关闭/重开、BLE 重连都不清零（除非该端口重新开始充电）。

- 端口关闭 → 立即丢弃该口计时。
- 电流 > cur_a → 视为正在充电，无充电时长归零。
- 端口刚打开的前 grace_s 秒不触发（留给握手/插入）。
- BLE 断开期间暂停累计（看不到电流，不能误判），恢复后接着算。
- 豁免名单（cfg.exempt，默认 ["a"]）内的端口完全不进计时：不建状态、
  不写 START/CHARGE_RESUME、不参与 FIRE，也不产生 CUT 尝试。
- 到点由 ha_server 走现有 /api/port 同一条开关路径断电，本模块只产出该断的口。

审计日志同时写 logger 与 %LOCALAPPDATA%\\CuktechPanel\\auto_off.log，
字段：端口、起始时刻、判定/断开时刻、当时电压电流。
"""
import logging
import os
import time

_LOGGER = logging.getLogger("cuktech_auto_off")

# 无充电自动关端口的判定窗口（秒）。10 分钟：避免涓流设备/短时空载被误断
# （历史阈值为 5 分钟＝300s，旧记录里的 300s 证据即按当时的窗口写入）。
DEFAULT_WINDOW_S = 600

PORT_KEYS = ("c1", "c2", "c3", "a")
PORT_PIID = {"c1": 1, "c2": 2, "c3": 3, "a": 4}
PORT_LABEL = {"c1": "C口1", "c2": "C口2", "c3": "C口3", "a": "USB-A"}


def _exempt_set(cfg):
    """读取豁免端口集合（cfg.exempt，缺省 A 口）；大小写不敏感，忽略未知端口。"""
    raw = getattr(cfg, "exempt", ("a",)) or ()
    if isinstance(raw, str):
        raw = [raw]
    return {str(x).strip().lower() for x in raw if str(x).strip().lower() in PORT_KEYS}


def _stamp(ts=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() if ts is None else ts))


def audit(line):
    """写一行审计：logger + 本机日志文件（失败不影响判定）。"""
    text = "%s %s" % (_stamp(), line)
    _LOGGER.info("AUTO_OFF %s", line)
    try:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                            "CuktechPanel")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "auto_off.log"), "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


class AutoOffEngine:
    """按端口维护连续无充电计时；tick() 返回本次应断电的端口列表。"""

    def __init__(self, clock=time.monotonic, wall=time.time):
        self._clock = clock
        self._wall = wall
        self._ports = {}       # key -> {since, zero, fired}
        self._last = None      # 上次 tick 的单调时钟
        self._anchor = (0.0, 0.0)  # (monotonic, wall) 用于换算 epoch

    def _epoch(self, mono):
        m, w = self._anchor
        return w - (m - mono)

    def reset(self):
        self._ports.clear()
        self._last = None

    def snapshot(self, cfg):
        mono = self._clock()
        self._anchor = (mono, self._wall())
        exempt = _exempt_set(cfg)
        out = {}
        for key, st in self._ports.items():
            if key in exempt:
                continue
            no_charge = max(0.0, mono - st["zero"])
            rem = max(0.0, float(cfg.window_s) - no_charge)
            out[key] = {
                "since": round(self._epoch(st["since"]), 2),
                "zero": round(self._epoch(st["zero"]), 2),
                "no_charge_s": int(no_charge),
                "remaining_s": int(rem + 0.999),
                "fired": bool(st["fired"]),
            }
        return {
            "enabled": bool(cfg.enabled),
            "window_s": int(cfg.window_s),
            "grace_s": int(cfg.grace_s),
            "cur_a": float(cfg.cur_a),
            "exempt": sorted(exempt),
            "server_now": round(self._wall(), 2),
            "ports": out,
        }

    def tick(self, ports, link_ok, cfg):
        """推进一拍。ports: {piid: {'enabled':bool,'current':float,'voltage':float}}。

        返回应断电的端口 key 列表（只报一次；调用方把结果回传 mark_result）。
        """
        mono = self._clock()
        dt = 0.0 if self._last is None else max(0.0, mono - self._last)
        self._last = mono
        self._anchor = (mono, self._wall())

        if not cfg.enabled:
            if self._ports:
                audit("DISABLED 计时清除 ports=%s" % ",".join(sorted(self._ports)))
                self._ports.clear()
            return []

        if not link_ok:
            # 看不到电流：暂停累计，既不误判也不清零
            for st in self._ports.values():
                st["zero"] += dt
            return []

        fire = []
        seen = set()
        exempt = _exempt_set(cfg)
        for key in PORT_KEYS:
            piid = PORT_PIID[key]
            p = ports.get(piid) or {}
            seen.add(key)
            if key in exempt:
                # 豁免端口完全不进计时：不建状态、不写任何日志、不产生 CUT。
                # 配置在运行中变更时，已存在的计时直接静默丢弃。
                self._ports.pop(key, None)
                continue
            enabled = bool(p.get("enabled"))
            if not enabled:
                if key in self._ports:
                    st = self._ports.pop(key)
                    audit("STOP port=%s reason=port_off since=%s" % (key, _stamp(self._epoch(st["since"]))))
                continue
            cur = float(p.get("current") or 0.0)
            volt = float(p.get("voltage") or 0.0)
            st = self._ports.get(key)
            if cur > float(cfg.cur_a):
                if st is not None and (mono - st["zero"]) >= 1.0:
                    audit("CHARGE_RESUME port=%s since=%s no_charge=%ds v=%.2f c=%.3f"
                          % (key, _stamp(self._epoch(st["since"])), int(mono - st["zero"]), volt, cur))
                self._ports[key] = {"since": st["since"] if st else mono, "zero": mono, "fired": False}
                continue
            if st is None:
                st = {"since": mono, "zero": mono, "fired": False}
                self._ports[key] = st
                audit("START port=%s since=%s v=%.2f c=%.3f window=%ss grace=%ss"
                      % (key, _stamp(self._epoch(mono)), volt, cur, cfg.window_s, cfg.grace_s))
            no_charge = mono - st["zero"]
            if st["fired"] or no_charge < float(cfg.window_s):
                continue
            if (mono - st["since"]) < float(cfg.grace_s):
                continue
            st["fired"] = True
            fire.append(key)
            audit("FIRE port=%s zero_at=%s now=%s no_charge=%ds v=%.2f c=%.3f thr=%.3fA window=%ss"
                  % (key, _stamp(self._epoch(st["zero"])), _stamp(self._epoch(mono)),
                     int(no_charge), volt, cur, cfg.cur_a, cfg.window_s))

        # 端口从状态里消失（设备重启等）：保留计时，不清零
        return fire

    def mark_result(self, key, ok):
        st = self._ports.get(key)
        if not st:
            return
        if ok:
            st["fired"] = True
        else:
            # 发送失败：下一拍重试，不把计时清零
            st["fired"] = False


def _selftest():
    class Cfg:
        enabled = True
        window_s = DEFAULT_WINDOW_S
        grace_s = 60
        cur_a = 0.05
        exempt = ["a"]

    class CfgNoExempt:
        enabled = True
        window_s = DEFAULT_WINDOW_S
        grace_s = 60
        cur_a = 0.05
        exempt = []

    def run(seq, piid=1, cfg=None):
        cfg = cfg or Cfg()
        t = [0.0]
        eng = AutoOffEngine(clock=lambda: t[0], wall=lambda: 1000.0 + t[0])
        fired = []
        for dt, cur, link, en in seq:
            t[0] += dt
            fired += eng.tick({piid: {"enabled": en, "current": cur, "voltage": 5.0}},
                              link, cfg)
        return fired, t[0]

    w = DEFAULT_WINDOW_S
    # 每拍 1s；首拍只建立起点（since=zero），累计秒数 = 拍数-1。
    a, ta = run([(1, 0, True, True)] * w)                  # 累计 599s 不应触发
    b, tb = run([(1, 0, True, True)] * (w + 1))            # 累计 600s 应触发
    c, tc = run([(1, 0, True, True)] * 200 + [(1, 0, False, True)] * 300
                + [(1, 0, True, True)] * 400)              # 断链 300s 暂停，恢复累计 599s
    d, td = run([(1, 0, True, True)] * 200 + [(1, 0, False, True)] * 300
                + [(1, 0, True, True)] * 401)              # 恢复后累计满 600s 才触发
    e, te = run([(1, 0, True, True)] * 400 + [(1, 0.5, True, True)]
                + [(1, 0, True, True)] * (w - 1))          # 中途充电归零，再等 599s
    f, tf = run([(1, 0, True, True)] * 400 + [(1, 0, True, False)]
                + [(1, 0, True, True)] * (w - 1))          # 端口关闭清零，重开 599s

    # 第 7 条：A 口豁免——连续无充电 15 分钟不触发、日志里不出现该口任何事件；
    # 同一份数据下 C1 第 600 秒照常断电；豁免名单清空后 A 口恢复计时。
    _logs = []
    _real_audit = globals()["audit"]
    globals()["audit"] = lambda line: (_logs.append(line), _real_audit(line))[0]
    try:
        g, tg = run([(1, 0, True, True)] * (15 * 60), piid=PORT_PIID["a"])
        h, th = run([(1, 0, True, True)] * (w + 1), piid=PORT_PIID["c1"])
    finally:
        globals()["audit"] = _real_audit
    import re as _re
    a_events = [ln for ln in _logs if _re.search(r"\bport=a\b", ln)]
    i, ti = run([(1, 0, True, True)] * (w + 1), piid=PORT_PIID["a"], cfg=CfgNoExempt())

    checks = [
        ("未到点(累计599s)不触发", a == [], "累计599s fired=%s" % a),
        ("到点(累计600s)触发", b == ["c1"], "累计600s fired=%s" % b),
        ("断链300s暂停累计(恢复后累计599s)", c == [], "断链暂停 fired=%s" % c),
        ("重连接着算(恢复后累计600s)", d == ["c1"], "恢复后累计600s fired=%s" % d),
        ("中途恢复充电归零(再599s)", e == [], "充电归零 fired=%s" % e),
        ("端口关闭清零(重开599s)", f == [], "端口关闭 fired=%s" % f),
        ("A口豁免(15分钟无触发无日志)且名单可配",
         g == [] and not a_events and h == ["c1"] and i == ["a"],
         "A口15分钟 fired=%s log=%d ；C1 600s fired=%s ；豁免=[] 时A口 fired=%s"
         % (g, len(a_events), h, i)),
    ]
    print("AUTOOFF_SELFTEST window_s=%d thr=%.2fA grace=%ds exempt=%s"
          % (w, Cfg.cur_a, Cfg.grace_s, ",".join(Cfg.exempt)))
    for name, ok, observed in checks:
        print("AUTOOFF_SELFTEST", "PASS" if ok else "FAIL", name, "| observed:", observed)
    _passed = sum(1 for _, ok, _ in checks if ok)
    return (all(ok for _, ok, _ in checks), _passed, len(checks))


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest()[0] else 1)

