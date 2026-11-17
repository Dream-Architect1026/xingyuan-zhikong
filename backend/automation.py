# -*- coding: utf-8 -*-
"""本地自动化管理：事件通知、告警阈值、定时控制。

- 充电开始/结束、端口功率越限 → 通过 SSE 推送 notify 事件
- 定时控制：到点自动开关端口
- 配置持久化到 automation.json
"""
import json
import logging
import os
import time

_LOGGER = logging.getLogger("automation")

PORT_NAMES = {1: "C1", 2: "C2", 3: "C3", 4: "USB-A"}

DEFAULT_DATA = {
    "notify": {"charge_start": True, "charge_end": True, "threshold": True},
    "thresholds": {},   # "1" -> {"min_w": 0, "max_w": 0}
    "schedules": [],    # [{id, port, time, action, enabled}]
    # 1.5.5：自动执行规则（默认关闭，建议用户按需开启）
    "auto_rule": {"enabled": False, "hold_minutes": 90},
}

THRESHOLD_DEBOUNCE_SEC = 60  # 越限通知最小间隔（秒）

MAX_SCHEDULES = 50  # 定时任务数量上限


class AutomationManager:
    def __init__(self, path, sse):
        self.path = path
        self.sse = sse
        self.on_fire = None  # 由 Server 注入：def callback(schedule_dict)
        # 1.5.5 自动规则注入点（全部由 Server 提供，引擎本身不碰 BLE/DB）
        self.active_session = None   # fn() -> {port_int: session_dict}
        self.device_profile = None   # fn(device_key) -> {"sessions": n, "ports": {...}}
        self.rule_set_piid = None    # fn(port_int, value) -> None
        self.rule_log = None         # fn(event_dict) -> None
        self._holds = {}             # port -> hold dict
        self._rule_done = set()      # 已触发过的 session id（避免到期后立刻重触发）
        self._last_rule_tick = 0.0
        self.data = self._load()
        self._prev_active = {}
        self._threshold_emit = {}
        self._schedule_last = {}

    # ── 持久化 ──
    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                base = json.loads(json.dumps(DEFAULT_DATA))
                base.update(data)
                # 加载期修复历史反区间脏数据（min_w>max_w 且非全零 → 禁用并告警）
                try:
                    for _pk, _r in list((base.get("thresholds") or {}).items()):
                        if not isinstance(_r, dict):
                            continue
                        _mn = float(_r.get("min_w") or 0)
                        _mx = float(_r.get("max_w") or 0)
                        if _mn > _mx and not (_mn == 0.0 and _mx == 0.0):
                            _LOGGER.warning("automation threshold %s inverted (min %s > max %s) -> disabled",
                                            _pk, _mn, _mx)
                            base["thresholds"][_pk] = {"min_w": 0.0, "max_w": 0.0}
                except Exception:
                    pass
                return base
            except Exception as e:
                _LOGGER.error("automation load failed: %s", e)
        return json.loads(json.dumps(DEFAULT_DATA))

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            _LOGGER.error("automation save failed: %s", e)

    # ── 配置读写 ──
    def get(self):
        return self.data

    def set_notify(self, patch):
        nf = self.data["notify"]
        for k in ("charge_start", "charge_end", "threshold"):
            if k in patch:
                nf[k] = bool(patch[k])
        self._save()
        return self.data

    def set_threshold(self, port, min_w, max_w):
        min_w = max(0, float(min_w or 0))
        max_w = max(0, float(max_w or 0))
        key = str(int(port))
        if min_w == 0 and max_w == 0:
            self.data["thresholds"].pop(key, None)
        else:
            self.data["thresholds"][key] = {"min_w": min_w, "max_w": max_w}
        self._threshold_emit.pop(int(port), None)
        self._save()
        return self.data

    def add_schedule(self, port, time_str, action):
        if len(self.data["schedules"]) >= MAX_SCHEDULES:
            raise ValueError("schedule limit reached")
        sid = 1
        for s in self.data["schedules"]:
            if s.get("id", 0) >= sid:
                sid = s["id"] + 1
        self.data["schedules"].append({
            "id": sid, "port": port, "time": time_str,
            "action": action if action in ("on", "off") else "off",
            "enabled": True,
        })
        self._save()
        return self.data

    def delete_schedule(self, sid):
        self.data["schedules"] = [s for s in self.data["schedules"] if s.get("id") != sid]
        self._save()
        return self.data

    def toggle_schedule(self, sid, enabled):
        for s in self.data["schedules"]:
            if s.get("id") == sid:
                s["enabled"] = bool(enabled)
                break
        self._save()
        return self.data

    # ── 自动执行规则（1.5.5）──
    def get_auto_rule(self):
        r = self.data.setdefault("auto_rule", {"enabled": False, "hold_minutes": 90})
        r["hold_minutes"] = max(30, min(240, int(r.get("hold_minutes") or 90)))
        r["enabled"] = bool(r.get("enabled", False))
        return {"enabled": r["enabled"], "hold_minutes": r["hold_minutes"],
                "holds": list(self._holds.values()),
                "rule": {"name": "惯用 A 口小电流设备 → 自动保持小电流",
                         "condition": "该设备历史会话 ≥3 条、A 口占比 ≥60%、"
                                      "A 口平均功率 <15 W",
                         "action": "将 USB-A 切到小电流模式并保持 N 分钟",
                         "expire": "到期后回到默认策略（关闭小电流）；"
                                   "A 口不受「无充电 10 分钟自动断」约束，"
                                   "保持期内不会因无充电被断电",
                         "scope": "仅 USB-A（4 号口）"}}

    def set_auto_rule(self, patch):
        r = self.data.setdefault("auto_rule", {"enabled": False, "hold_minutes": 90})
        if "enabled" in patch:
            r["enabled"] = bool(patch.get("enabled"))
        if "hold_minutes" in patch:
            try:
                r["hold_minutes"] = max(30, min(240, int(patch.get("hold_minutes"))))
            except (TypeError, ValueError):
                r["hold_minutes"] = 90
        if not r.get("enabled") and self._holds:
            for pid in list(self._holds):
                self._end_hold(pid, "规则关闭")
        self._save()
        return self.get_auto_rule()

    def _emit_rule(self, ev):
        ev = dict(ev)
        ev.setdefault("ts", time.time())
        if callable(self.rule_log):
            try:
                self.rule_log(ev)
            except Exception as e:
                _LOGGER.error("auto rule log failed: %s", e)

    def _end_hold(self, pid, reason):
        h = self._holds.pop(pid, None)
        if not h:
            return
        # 到期/结束时回到默认策略：关闭小电流（默认值，不改用户其它设置）
        if callable(self.rule_set_piid):
            try:
                self.rule_set_piid(pid, 0)
            except Exception as e:
                _LOGGER.error("auto rule restore failed: %s", e)
        h["ended"] = time.time()
        h["end_reason"] = reason
        self._emit_rule({"event": "auto_hold_end", **h})
        try:
            self.sse.emit("notify", {
                "level": "info", "port": pid,
                "title": "自动规则已结束",
                "message": "%s 口自动保持结束：%s" % (PORT_NAMES.get(pid, pid), reason)})
        except Exception:
            pass

    def _eval_auto_rule(self, ports, now, settings=None):
        """每 10 s 评估一次：保持期到期/断开收尾，再判断是否触发。

        与「无充电 10 分钟自动断」的关系：A 口已在 auto_off 豁免名单内，
        保持期内不会因无充电被断电；保持期只维持小电流模式。
        端口连续 2 分钟无功率即视为设备断开，立即结束保持。
        """
        if now - self._last_rule_tick < 10:
            return
        self._last_rule_tick = now
        cfg = self.data.get("auto_rule") or {}
        hold_min = max(30, min(240, int(cfg.get("hold_minutes") or 90)))

        for pid, h in list(self._holds.items()):
            p = float(getattr(ports.get(pid), "power", 0) or 0)
            if now >= float(h.get("until") or 0):
                self._end_hold(pid, "到期，恢复默认策略")
                continue
            if p <= 0:
                h["idle_since"] = h.get("idle_since") or now
                if now - float(h["idle_since"]) > 120:
                    self._end_hold(pid, "设备断开，提前结束")
            else:
                h["idle_since"] = 0

        if not bool(cfg.get("enabled")):
            return
        if not (callable(self.active_session) and callable(self.device_profile)
                and callable(self.rule_set_piid)):
            return
        try:
            sess = self.active_session() or {}
        except Exception as e:
            _LOGGER.error("auto rule session query failed: %s", e)
            return
        s = sess.get(4)
        if not s or 4 in self._holds:
            return
        sid = int(s.get("id") or 0)
        if sid and sid in self._rule_done:
            return
        key = str(s.get("device_key") or "")
        if not key:
            return
        try:
            prof = self.device_profile(key) or {}
        except Exception as e:
            _LOGGER.error("auto rule profile failed: %s", e)
            return
        total = int(prof.get("sessions") or 0)
        pp = prof.get("ports") or {}
        if total < 3 or not pp:
            return
        best_pid, best = max(((int(k), v) for k, v in pp.items()),
                             key=lambda kv: int(kv[1].get("n") or 0))
        ratio = float(best.get("n") or 0) / float(total)
        if best_pid != 4 or ratio < 0.6 or float(best.get("avg_w") or 0) >= 15:
            return
        cur = float(getattr(ports.get(4), "power", 0) or 0)
        if cur <= 0:
            return
        try:
            before = int((settings or {}).get("15", 0) or 0)
        except (TypeError, ValueError):
            before = 0
        # 触发：A 口切小电流并保持
        try:
            self.rule_set_piid(4, 1)
        except Exception as e:
            _LOGGER.error("auto rule apply failed: %s", e)
            return
        h = {"port": 4, "port_name": PORT_NAMES.get(4, "USB-A"),
             "device_key": key, "device_label": str(s.get("device_label") or ""),
             "session_id": sid, "before": before, "after": 1,
             "started": now, "until": now + hold_min * 60,
             "hold_minutes": hold_min, "ratio": round(ratio, 2),
             "avg_w": round(float(best.get("avg_w") or 0), 1),
             "mode": "A 口小电流"}
        self._holds[4] = h
        if sid:
            self._rule_done.add(sid)
        self._emit_rule({"event": "auto_rule_fire", **h})

    # ── 周期性检查（由服务端循环调用）──
    def tick(self, state, now=None):
        now = now or time.time()
        if not getattr(state, "connected", False):
            return
        ports = getattr(state, "ports", {}) or {}

        # 1. 充电开始 / 结束
        for pid, ps in ports.items():
            # power>0 即视为带电
            active = bool(getattr(ps, "power", 0) or 0) > 0
            prev = self._prev_active.get(pid)
            if prev is not None and prev != active:
                if active:
                    if self.data["notify"].get("charge_start", True):
                        self.sse.emit("notify", {
                            "level": "info", "port": pid,
                            "title": f"端口 {PORT_NAMES.get(pid, pid)} 开始充电",
                            "message": f"{PORT_NAMES.get(pid, pid)} 检测到充电电流",
                        })
                else:
                    if self.data["notify"].get("charge_end", True):
                        self.sse.emit("notify", {
                            "level": "success", "port": pid,
                            "title": f"端口 {PORT_NAMES.get(pid, pid)} 停止充电",
                            "message": f"{PORT_NAMES.get(pid, pid)} 已无输出功率",
                        })
            self._prev_active[pid] = active

        # 1.5：自动执行规则（惯用 A 口小电流设备 → 保持）
        self._eval_auto_rule(ports, now, getattr(state, "settings", None))

        # 2. 告警阈值
        for key, th in self.data["thresholds"].items():
            try:
                pid = int(key)
            except (TypeError, ValueError):
                continue
            ps = ports.get(pid)
            if not ps:
                continue
            p = float(getattr(ps, "power", 0) or 0)
            max_w = float(th.get("max_w", 0) or 0)
            min_w = float(th.get("min_w", 0) or 0)
            level = None
            msg = ""
            if max_w > 0 and p > max_w:
                level = "danger"
                msg = f"端口 {PORT_NAMES.get(pid, pid)} 功率 {p:.1f}W 超过上限 {max_w:g}W"
            elif min_w > 0 and p > 0 and p < min_w:
                level = "warning"
                msg = f"端口 {PORT_NAMES.get(pid, pid)} 功率 {p:.1f}W 低于下限 {min_w:g}W"
            if level:
                last = self._threshold_emit.get(pid, 0)
                if now - last >= THRESHOLD_DEBOUNCE_SEC:
                    self._threshold_emit[pid] = now
                    if self.data["notify"].get("threshold", True):
                        self.sse.emit("notify", {
                            "level": level, "port": pid,
                            "title": f"端口 {PORT_NAMES.get(pid, pid)} 功率告警",
                            "message": msg,
                        })
            else:
                self._threshold_emit[pid] = 0  # 恢复正常，重置去抖

        # 3. 定时控制
        lt = time.localtime(now)
        hhmm = f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
        day = f"{lt.tm_year}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
        for sch in self.data["schedules"]:
            if not sch.get("enabled", True):
                continue
            if sch.get("time") != hhmm:
                continue
            key = sch.get("id")
            stamp = f"{day} {hhmm}"
            if self._schedule_last.get(key) == stamp:
                continue
            self._schedule_last[key] = stamp
            port = sch.get("port")
            action = sch.get("action", "off")
            pname = PORT_NAMES.get(int(port), port) if str(port).isdigit() else port
            self.sse.emit("notify", {
                "level": "info", "port": port,
                "title": f"定时任务执行",
                "message": f"按计划在 {hhmm} {'开启' if action == 'on' else '关闭'} 端口 {pname}",
            })
            if self.on_fire:
                try:
                    self.on_fire(port, action)
                except Exception as e:
                    _LOGGER.error("schedule fire failed: %s", e)
