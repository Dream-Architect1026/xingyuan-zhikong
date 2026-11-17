"""CUKTECH BLE data server for the desktop panel.

"""
import asyncio
import re
import sys
import warnings
warnings.filterwarnings('ignore', message='.*default MTU.*')
import gzip
import hashlib
import io
import json
import logging
import os
import socket
import secrets
import ipaddress
import math
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path
from aiohttp import web

from config import (load_config, LOG_LEVELS, BEMFA_PORT_NAMES,
                    bemfa_topic_error, user_config_path)
from state import ChargerState, PORT_BITS, PORT_NAMES, PORT_DEFAULT, VALID_PIIDS, PIID_RANGES, PROTOCOL_SWITCH_BITS
from ble_manager import BLEManager, set_status_cache_invalidator
from history import PortHistory
from automation import AutomationManager
from charge_guard import ChargeGuard
from auto_off import AutoOffEngine, audit as ao_audit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_LOGGER = logging.getLogger("cuktech_server")

# 巴法云模式下的端口显示名（与网页控制台设备卡一致，不另起名）
BEMFA_PORT_LABELS = {"c1": "1号口", "c2": "2号口", "c3": "3号口", "a": "A口"}
# MQTT CONNACK 返回码 → 可读原因（同时覆盖 3.1.1 的 1..5 与 MQTT5 的 128+）
MQTT_RC_REASONS = {
    1: "协议版本不被服务端接受",
    2: "客户端 ID 被拒绝（巴法云需填私钥 UID）",
    3: "服务端不可用",
    4: "用户名或密码错误",
    5: "认证被拒（未授权：私钥 UID 不对，或主题未在控制台创建）",
    128: "服务端未指明原因的失败",
    132: "协议版本不被服务端接受",
    133: "客户端 ID 不合法（巴法云需填私钥 UID）",
    134: "用户名或密码错误",
    135: "认证被拒（未授权：私钥 UID 不对，或主题未在控制台创建）",
    136: "服务端不可用（维护中或已达连接上限）",
    137: "服务端繁忙",
    138: "该客户端被服务端封禁",
    139: "服务端正在关闭",
    144: "主题名不合法（只能字母数字下划线，且需先在控制台创建）",
    151: "配额超限",
}


_sse_log = logging.getLogger("cuktech_sse")


# ── Request size limit (prevent DoS via large payloads) ──
MAX_REQUEST_BODY_SIZE = 1024 * 1024  # 1 MB

# 开机自启使用的 Windows 任务计划名称（登录时触发 start_service.vbs）
AUTOSTART_TASK_NAME = "CuktechChargerAutoStart"


class SSEEmitter:
    """SSE event broadcaster — push events to all connected browser clients.

    Coalescing rules (stale data is replaced, never enqueued alongside newer):
    - status events: 'latest only' per client (a newer status replaces the old).
    - port_update events: 'latest only' per port_id per client — a burst of
      port pushes collapses to one payload per port, so a slow/full client
      queue can never lose the final state of a port.
    """

    MAX_QUEUE_SIZE = 128

    def __init__(self):
        self._clients: set[asyncio.Queue] = set()
        self._pending_status: dict[int, str] = {}  # id(queue) -> latest status payload
        self._pending_ports: dict[int, dict] = {}  # id(queue) -> {port_id: payload}
        self._lock = threading.Lock()

    def add_client(self, queue: asyncio.Queue):
        with self._lock:
            self._clients.add(queue)
        _sse_log.info("SSE client connected (total: %d)", len(self._clients))

    def remove_client(self, queue: asyncio.Queue):
        with self._lock:
            self._clients.discard(queue)
            self._pending_status.pop(id(queue), None)
            self._pending_ports.pop(id(queue), None)
        _sse_log.info("SSE client disconnected (total: %d)", len(self._clients))

    def _put_or_drop(self, q: asyncio.Queue, payload: str):
        """Put payload in queue; drop oldest if full."""
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            _sse_log.warning("SSE queue full, dropping oldest event (qsize=%s)", q.qsize())
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def _flush_locked(self, q: asyncio.Queue):
        """Flush pending status + port updates into queue. Caller holds the lock.

        Order: latest status first, then latest per-port updates, so stale
        statuses are never delivered behind newer events.
        """
        qid = id(q)
        status = self._pending_status.pop(qid, None)
        if status:
            self._put_or_drop(q, status)
        ports = self._pending_ports.pop(qid, None)
        if ports:
            for pid in sorted(ports):
                self._put_or_drop(q, ports[pid])

    def emit(self, event_type: str, data: dict):
        """Broadcast event to all connected clients."""
        payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
        with self._lock:
            if event_type == "status":
                # Status: just update pending, don't enqueue (replaces stale status)
                for q in self._clients:
                    self._pending_status[id(q)] = payload
            elif event_type == "port_update":
                # Port update: coalesce per port_id (latest wins)
                pid = data.get("port_id")
                for q in self._clients:
                    self._pending_ports.setdefault(id(q), {})[pid] = payload
            else:
                # Non-status/port: flush pending status+ports first, then enqueue
                for q in self._clients:
                    self._flush_locked(q)
                    self._put_or_drop(q, payload)

    def flush_pending(self, q: asyncio.Queue) -> list:
        """Return and clear the latest pending status + port updates for a queue.

        Called by the SSE handler before each queue read so the newest
        status/port states are always sent ahead of older queued events.
        """
        with self._lock:
            out = []
            status = self._pending_status.pop(id(q), None)
            if status:
                out.append(status)
            ports = self._pending_ports.pop(id(q), None)
            if ports:
                out.extend(ports[pid] for pid in sorted(ports))
            return out

    def flush_status(self, q: asyncio.Queue) -> str | None:
        """Return and clear the latest pending status for a queue (kept for compat)."""
        with self._lock:
            return self._pending_status.pop(id(q), None)


class Server:
    def __init__(self):
        self.config = load_config()
        # R015（领导）：每日自动重启不做记忆——每次启动强制默认关闭
        try:
            if getattr(self.config.service, "daily_restart_enabled", False):
                # 只改内存：config.yaml 不落盘，下次启动照旧强制关闭（不做记忆）。
                self.config.service.daily_restart_enabled = False
                _LOGGER.info("daily_restart forced off on startup (no memory)")
        except Exception:
            pass
        self.state = ChargerState()
        self.ble = BLEManager(self.config.ble.mac, self.config.ble.token, self.state, self.config)
        self.mqtt_client = None
        self._mqtt_connect_time = 0.0
        self._mqtt_disconnect_count = 0
        self._mqtt_publish_failures = 0
        # 连接状态机（界面状态行 / bemfa.log / 测试连接共用）
        self._mqtt_state = "disabled"   # disabled/connecting/connected/failed/disconnected
        self._mqtt_reason = ""
        self._mqtt_rc = None
        self._mqtt_last_event = 0.0
        # C51：后端无充电 10 分钟自动关端口（与界面无关，进程存活即计时）
        self._auto_off = AutoOffEngine()
        self._auto_off_task = None
        self.loop = None
        self._start_lock = asyncio.Lock()
        self._status_cache_bytes = None
        self._status_cache_valid = False
        self._chart_cache: OrderedDict = OrderedDict()
        self._chart_cache_ttl = 10
        self._chart_cache_max = 10
        self.sse = SSEEmitter()
        self.automation = AutomationManager(str(Path(__file__).parent / "automation.json"), self.sse)
        self.automation.on_fire = self._fire_schedule
        # PRD 需求2/4：充电时长上限 + 智能充电保护引擎
        self.guard = ChargeGuard(str(Path(__file__).parent / "charge_guard.json"), self.sse)
        self.guard.on_cutoff = self._guard_cutoff
        self._daily_restart_last = ""  # 每日定时重启去重（YYYY-MM-DD）
        self._start_time = time.time()
        self.history = PortHistory(
            db_path=self.config.server.history_db_path,
            retention_days=self.config.server.history_retention_days,
        )
        # 归因：登记本机设备 + 一次性回溯旧记录的断开来源（规则/人工/曲线/未识别）
        try:
            self.history.set_default_device(self.config.ble.mac, "")
            self._attrib_backfill = self.history.backfill_attribution()
            if any(self._attrib_backfill.values()):
                _LOGGER.info("旧记录断开来源回溯: %s", self._attrib_backfill)
        except Exception as e:
            _LOGGER.error("旧记录归因回溯失败: %s", e)
        # 1.5.5：自动执行规则接线。引擎只依赖注入的只读查询 + 现有 set 通道；
        # 审计落在 %LOCALAPPDATA%\CuktechPanel\auto_rules.log（JSONL）。
        try:
            _base = Path(os.environ.get("LOCALAPPDATA") or ".") / "CuktechPanel"
            _base.mkdir(parents=True, exist_ok=True)
            self.auto_rule_log_path = str(_base / "auto_rules.log")
            self.automation.active_session = self._active_sessions_by_port
            self.automation.device_profile = self.history.get_device_profile
            self.automation.rule_set_piid = self._rule_set_piid
            self.automation.rule_log = self._rule_log_write
        except Exception as e:
            _LOGGER.error("自动规则接线失败: %s", e)
        effective_level = self.config.server.log_level
        logging.getLogger().setLevel(LOG_LEVELS.get(effective_level, logging.INFO))
        env_var = os.environ.get("CUKTECH_LOG_LEVEL")
        source = "环境变量 CUKTECH_LOG_LEVEL" if env_var else "config.yaml"
        _LOGGER.info("日志级别: %s (来源: %s)", effective_level, source)

    def mqtt_publish(self, topic, payload, retain=False):
        """向 MQTT broker 发布一条消息（未连接时静默跳过）。

        generic：原有 JSON 主题布局，行为不变。
        bemfa：巴法云设备协议——只把端口状态映射成纯文本 on/off，
        发到该端口自己的主题 /up；其余主题不在巴法云设备协议内，跳过。
        """
        if self.mqtt_client and self.mqtt_client.is_connected():
            try:
                if self.config.mqtt.mode == "bemfa":
                    self._bemfa_publish(topic, payload)
                    return
                self.mqtt_client.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)
            except Exception:
                self._mqtt_publish_failures += 1

    def _bemfa_publish(self, topic, payload):
        """巴法云上行：{端口主题}/up，纯文本 on/off，QoS 只取 0/1。"""
        prefix = "%s/" % self.config.mqtt.topic_ports
        if not isinstance(topic, str) or not topic.startswith(prefix):
            return
        port_name = topic[len(prefix):]
        if port_name not in BEMFA_PORT_NAMES:
            return
        enabled = payload.get("enabled") if isinstance(payload, dict) else None
        if not isinstance(enabled, bool):
            return
        base = self.config.mqtt.bemfa_topic(port_name)
        if not base:
            self._mqtt_log("uplink_skipped", topic="(空)", payload="", result="该端口主题未填")
            return
        text = "on" if enabled else "off"
        info = self.mqtt_client.publish(
            "%s/up" % base, text,
            qos=min(1, max(0, int(self.config.mqtt.qos))),
            retain=False,
        )
        try:
            ok = int(info.rc) == 0
        except Exception:
            ok = True
        self._mqtt_log("uplink", topic="%s/up" % base, payload=text,
                       result="已发送" if ok else "发送失败 rc=%s" % getattr(info, "rc", "?"))

    def _bemfa_port_from_topic(self, topic):
        """把收到的 主题 或 主题/set 还原成端口名，未知主题返回 None。"""
        base = topic[:-4] if topic.endswith("/set") else topic
        for name in BEMFA_PORT_NAMES:
            if base == self.config.mqtt.bemfa_topic(name):
                return name
        return None

    def _bemfa_publish_current(self):
        """连接/重连后把四路当前开关状态补给云端（断线期间的变化）。"""
        try:
            ctl = self.ble.state.settings.get("16", 0x0F)
        except Exception:
            return
        for pid, name in PORT_NAMES.items():
            self._bemfa_publish(
                "%s/%s" % (self.config.mqtt.topic_ports, name),
                {"enabled": bool(ctl & (1 << (pid - 1)))},
            )

    def _config_path(self) -> Path:
        # 1.5.5：统一走用户目录（%LOCALAPPDATA%\CuktechPanel\config.yaml），
        # 覆盖升级/换便携目录不丢 MQTT 与蓝牙配置。
        return user_config_path()

    async def handle_health(self, request):
        """GET /api/health - health check endpoint with runtime stats."""
        ble_connected = self.ble.ctrl is not None and self.state.connected
        mqtt_connected = self.mqtt_client is not None and self.mqtt_client.is_connected()
        all_healthy = bool(ble_connected or self.config.server.port > 0)
        # 运行时统计：队列长度、客户端数、活跃会话、缓存大小
        try:
            cmd_queue_size = self.ble.cmd_queue.qsize() if self.ble.cmd_queue else 0
        except Exception:
            cmd_queue_size = 0
        try:
            sse_clients = len(self.sse._clients) if self.sse else 0
        except Exception:
            sse_clients = 0
        try:
            active_sessions = len(self.ble._active_sessions) if self.ble else 0
        except Exception:
            active_sessions = 0
        try:
            static_cache_files = len(_static_cache)
            static_cache_bytes = sum(len(v.get("raw", b"")) for v in _static_cache.values())
        except Exception:
            static_cache_files = 0
            static_cache_bytes = 0
        try:
            db_size = os.path.getsize(self.config.server.history_db_path) if os.path.exists(self.config.server.history_db_path) else 0
        except Exception:
            db_size = 0
        return web.json_response({
            "ok": True,
            "healthy": all_healthy,
            "components": {
                "ble": ble_connected,
                "mqtt": mqtt_connected,
            },
            "uptime_ms": int((time.time() - getattr(self, '_start_time', time.time())) * 1000),
            "runtime": {
                "ble_cmd_queue": cmd_queue_size,
                "sse_clients": sse_clients,
                "active_sessions": active_sessions,
                "static_cache_files": static_cache_files,
                "static_cache_kb": round(static_cache_bytes / 1024, 1),
                "db_kb": round(db_size / 1024, 1),
            },
        })

    def invalidate_status_cache(self):
        self._status_cache_valid = False

    # ── MQTT 集成（C51 恢复；出厂关闭，地址由用户在"连接设置"填写）──
    def _mqtt_log_path(self):
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                            "CuktechPanel")
        return os.path.join(base, "bemfa.log")

    def _mqtt_log(self, event, **fields):
        """追加一行 bemfa.log（时间 + event + k=v）；client_id 只留前4后4。"""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + \
            ".%03d" % int((time.time() % 1) * 1000)
        mq = self.config.mqtt
        parts = [stamp, "event=%s" % event,
                 "host=%s" % (fields.pop("host", mq.host) or "-"),
                 "port=%s" % fields.pop("port", mq.port),
                 "mode=%s" % fields.pop("mode", mq.mode),
                 "client_id=%s" % (self._mask(mq.client_id) if mq.client_id else "-")]
        for k, v in fields.items():
            if v is None:
                continue
            sv = str(v).replace('"', "'")
            parts.append('%s="%s"' % (k, sv) if (" " in sv or not sv) else "%s=%s" % (k, sv))
        line = " ".join(parts)
        _LOGGER.info("MQTT %s", line)
        # 自动重连会周期性重复同一条失败：每种事件内容相同就只留第一行，避免刷屏
        _key = " ".join(parts[1:])
        _last = getattr(self, "_mqtt_log_last", None)
        if not isinstance(_last, dict):
            _last = self._mqtt_log_last = {}
        if event in ("connect_refused", "disconnect_early") and _last.get(event) == _key:
            return
        _last[event] = _key
        try:
            path = self._mqtt_log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # 简单轮转：超过 512KB 就把旧文件留一份 .1，避免重试刷爆磁盘
            try:
                if os.path.getsize(path) > 512 * 1024:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    @staticmethod
    def _mqtt_rc_reason(code, mode="generic"):
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = -1
        if code == 0:
            return ""
        if code in MQTT_RC_REASONS:
            return MQTT_RC_REASONS[code]
        if mode == "bemfa":
            return ("被服务端拒绝（rc=%d）：检查私钥 UID 是否正确、四个主题是否已在"
                    "控制台创建" % code)
        return "被服务端拒绝（rc=%d）" % code

    @staticmethod
    def _mqtt_exc_reason(exc):
        import ssl as _ssl
        if isinstance(exc, socket.gaierror):
            return "域名解析失败（检查 Broker 地址）"
        if isinstance(exc, ConnectionRefusedError):
            return "拒绝连接（端口不对或服务端未监听）"
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "连接超时（网络不通或被防火墙拦截）"
        if isinstance(exc, _ssl.SSLError):
            return "TLS 握手失败（9503 才是 TLS 端口，9501 为明文）"
        if isinstance(exc, OSError) and getattr(exc, "errno", None):
            return "连接失败（errno=%s：%s）" % (exc.errno, exc.strerror or exc)
        return "连接失败（%s）" % (str(exc) or type(exc).__name__)

    def mqtt_status(self):
        """连接状态 + 可读原因，供界面状态行与 /api/mqtt/status 使用。"""
        mq = self.config.mqtt
        connected = self.mqtt_client is not None and self.mqtt_client.is_connected()
        state = "connected" if connected else self._mqtt_state
        labels = {"disabled": "未开启", "connecting": "连接中",
                  "connected": "已连接", "failed": "失败", "disconnected": "未连接"}
        return {
            "state": state,
            "label": labels.get(state, state),
            "reason": "" if connected else self._mqtt_reason,
            "rc": self._mqtt_rc,
            "mode": mq.mode,
            "host": mq.host,
            "port": mq.port,
            "client_id": self._mask(mq.client_id) if mq.client_id else "",
            "qos": mq.qos,
            "topics": {BEMFA_PORT_LABELS[n]: mq.bemfa_topic(n) for n in BEMFA_PORT_NAMES},
            "uptime": (int(time.time() - self._mqtt_connect_time)
                       if connected and self._mqtt_connect_time else 0),
            "last_event": int(self._mqtt_last_event) if self._mqtt_last_event else 0,
        }

    async def setup_mqtt(self):
        mq = self.config.mqtt
        if not mq.enabled:
            self._mqtt_state = "disabled"
            self._mqtt_reason = ""
            _LOGGER.info("MQTT disabled, running in standalone web server mode")
            return

        # 巴法云：主题必须先在控制台建好并原样粘贴；不合规就不连，直接给出原因
        if mq.mode == "bemfa":
            errs = mq.bemfa_topic_errors()
            if errs:
                self._mqtt_state = "failed"
                self._mqtt_reason = "；".join(errs)
                self._mqtt_log("config_invalid", reason=self._mqtt_reason)
                _LOGGER.error("MQTT 巴法云主题配置无效：%s", self._mqtt_reason)
                return

        import paho.mqtt.client as mqtt
        self._mqtt_state = "connecting"
        self._mqtt_reason = ""
        self._mqtt_rc = None
        self._mqtt_log("connect_begin")
        self.mqtt_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=(mq.client_id or None),
        )
        if mq.username:
            self.mqtt_client.username_pw_set(mq.username, mq.password)
        self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)

        def on_connect(client, userdata, flags, rc, properties=None):
            s = get_server()
            code = int(getattr(rc, "value", rc))
            s._mqtt_rc = code
            s._mqtt_last_event = time.time()
            if code == 0:
                s._mqtt_state = "connected"
                s._mqtt_reason = ""
                s._mqtt_connect_time = time.time()
                s._mqtt_log("connected", rc=code)
                _LOGGER.info("MQTT connected (rc=0)")
                if s.ble:
                    s.ble.set_mqtt_publisher(s.mqtt_publish)
                s.setup_mqtt_subscriptions()
            else:
                s._mqtt_state = "failed"
                s._mqtt_reason = s._mqtt_rc_reason(code, s.config.mqtt.mode)
                s._mqtt_log("connect_refused", rc=code, reason=s._mqtt_reason)
                _LOGGER.error("MQTT connect refused rc=%s: %s", code, s._mqtt_reason)

        def on_disconnect(client, userdata, flags, rc, properties=None):
            s = get_server()
            code = int(getattr(rc, "value", rc))
            s._mqtt_disconnect_count += 1
            s._mqtt_last_event = time.time()
            if s._mqtt_state != "failed":
                # 被拒的 CONNACK 后紧跟的断开会带 rc=128，不要盖掉真实返回码
                s._mqtt_rc = code
            if s._mqtt_state == "connected":
                if code == 0:
                    s._mqtt_state = "disconnected"
                    s._mqtt_reason = (
                        "连接已断开（服务端关闭；巴法云同一私钥 UID 多客户端会互踢）"
                        if s.config.mqtt.mode == "bemfa" else "连接已断开")
                else:
                    s._mqtt_state = "failed"
                    s._mqtt_reason = s._mqtt_rc_reason(code, s.config.mqtt.mode)
                s._mqtt_log("disconnected", rc=code, reason=s._mqtt_reason)
            else:
                # 已被拒绝的 CONNACK 后紧跟一次 disconnect：保留首次可读原因
                if s._mqtt_state != "failed":
                    s._mqtt_reason = (s._mqtt_rc_reason(code, s.config.mqtt.mode)
                                      or s._mqtt_reason)
                s._mqtt_log("disconnect_early", rc=code, reason=s._mqtt_reason)
            _LOGGER.warning("MQTT disconnected (rc=%s)", code)

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_disconnect = on_disconnect
        if mq.mode == "bemfa":
            # 巴法云 9503 是 TLS 端口（9501 为明文）；不套 JSON 遗嘱，
            # 遗嘱写主题会让云端误判开关状态。
            if int(mq.port) == 9503:
                try:
                    self.mqtt_client.tls_set()
                except Exception as e:
                    self._mqtt_state = "failed"
                    self._mqtt_reason = "TLS 初始化失败（%s）" % e
                    self._mqtt_log("tls_error", reason=self._mqtt_reason)
                    _LOGGER.error("MQTT TLS 初始化失败: %s", e)
                    self.mqtt_client = None
                    return
        else:
            self.mqtt_client.will_set(
                mq.topic_status,
                json.dumps({"connected": False}),
                retain=True, qos=1,
            )

        for attempt in range(3):
            try:
                self.mqtt_client.connect(mq.host, mq.port, mq.keepalive)
                self.mqtt_client.loop_start()
                _LOGGER.info("MQTT connecting to %s:%s", mq.host, mq.port)
                break
            except Exception as e:
                self._mqtt_state = "failed"
                self._mqtt_reason = self._mqtt_exc_reason(e)
                self._mqtt_log("connect_error", attempt=attempt + 1,
                               errno=getattr(e, "errno", None),
                               reason=self._mqtt_reason)
                _LOGGER.error("MQTT connection failed (attempt %d/3): %s:%s (%s)",
                              attempt + 1, mq.host, mq.port, self._mqtt_reason)
                if attempt < 2:
                    await asyncio.sleep(3)
                else:
                    self.mqtt_client = None

        if self.mqtt_client:
            self.ble.set_mqtt_publisher(self.mqtt_publish)

    def setup_mqtt_subscriptions(self):
        if not self.mqtt_client:
            return
        server = self

        if self.config.mqtt.mode == "bemfa":
            # 巴法云设备侧：订阅「控制台里建好的主题本身」，对方发 主题/set，
            # 载荷是纯文本 on/off。主题不做任何拼接，原样使用。
            qos = min(1, max(0, int(self.config.mqtt.qos)))

            def on_bemfa_message(client, userdata, msg):
                raw = (msg.payload or b"").decode("utf-8", "replace")
                try:
                    port_name = server._bemfa_port_from_topic(msg.topic)
                    if port_name is None:
                        server._mqtt_log("downlink_ignored", topic=msg.topic,
                                         payload=raw[:40], parsed="未知主题")
                        _LOGGER.debug("Bemfa: 未映射主题 %s，忽略", msg.topic)
                        return
                    text = raw.strip().lower()
                    if text not in ("on", "off"):
                        # 非法载荷只告警，绝不抛异常把客户端带崩
                        server._mqtt_log("downlink_invalid", topic=msg.topic,
                                         payload=raw[:40], parsed="非 on/off，已忽略")
                        _LOGGER.warning("Bemfa: 非法载荷 %r（主题 %s），已忽略",
                                        text[:40], msg.topic)
                        return
                    if server.loop is None:
                        server._mqtt_log("downlink_dropped", topic=msg.topic,
                                         payload=raw[:40], parsed="%s=%s" % (port_name, text),
                                         result="事件循环未就绪")
                        return
                    if text == "off":
                        server.ble.note_end_cause(
                            port_name, "manual", "MQTT 指令", "巴法云下行",
                            "主题 %s 载荷 off（巴法云 MQTT 人工指令）" % msg.topic)
                    server.loop.call_soon_threadsafe(
                        server.ble.cmd_queue.put_nowait,
                        ("port", (port_name, text), None))
                    server._mqtt_log("downlink", topic=msg.topic, payload=raw[:40],
                                     parsed="%s=%s" % (port_name, text), result="已入队")
                    _LOGGER.info("Bemfa command: %s <- %s", port_name, text)
                except Exception as e:
                    server._mqtt_log("downlink_error", topic=msg.topic,
                                     payload=raw[:40], reason=str(e))
                    _LOGGER.error("Bemfa cmd error: %s", e)

            self.mqtt_client.on_message = on_bemfa_message
            for name in BEMFA_PORT_NAMES:
                topic = self.config.mqtt.bemfa_topic(name)
                if not topic:
                    continue
                self.mqtt_client.subscribe(topic, qos=qos)
                self._mqtt_log("subscribe", topic=topic,
                               label=BEMFA_PORT_LABELS[name], qos=qos)
                _LOGGER.info("Bemfa subscribe: %s (qos=%d)", topic, qos)
            # 断线期间的状态变化，重连后补发一次
            self._bemfa_publish_current()
            return

        def on_mqtt_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload)
                if server.loop is None:
                    return
                if msg.topic == server.config.mqtt.topic_set:
                    piid = payload.get("piid")
                    value = payload.get("value")
                    if piid is None or value is None:
                        _LOGGER.warning("Invalid set command: missing piid or value")
                        return
                    try:
                        piid_int = int(piid)
                        value_int = int(value)
                    except (ValueError, TypeError):
                        _LOGGER.warning("Invalid set command: piid/value must be integers")
                        return
                    if piid_int not in VALID_PIIDS:
                        _LOGGER.warning("Invalid set command: piid %d not valid", piid_int)
                        return
                    min_val, max_val = PIID_RANGES[piid_int]
                    if not (min_val <= value_int <= max_val):
                        _LOGGER.warning("Invalid set command: value %d out of range [%d, %d]",
                                        value_int, min_val, max_val)
                        return
                    server.loop.call_soon_threadsafe(
                        server.ble.cmd_queue.put_nowait,
                        ("set", (piid_int, value_int), None))
                    _LOGGER.info("MQTT set command: piid=%d value=%d", piid_int, value_int)
                elif msg.topic == server.config.mqtt.topic_ports:
                    port = payload.get("port")
                    action = payload.get("action")
                    if not port or action not in ("on", "off"):
                        _LOGGER.warning("Invalid port command: port=%s action=%s", port, action)
                        return
                    if port not in PORT_BITS and port != "all":
                        _LOGGER.warning("Invalid port command: unknown port %s", port)
                        return
                    if action == "off":
                        for _p in (("c1", "c2", "c3", "a") if port == "all" else (port,)):
                            server.ble.note_end_cause(
                                _p, "manual", "MQTT 指令", "MQTT 客户端",
                                "主题 %s 载荷 port=%s action=off（通用 MQTT 人工指令）"
                                % (msg.topic, port))
                    server.loop.call_soon_threadsafe(
                        server.ble.cmd_queue.put_nowait,
                        ("port", (port, action), None))
                    _LOGGER.info("MQTT port command: port=%s action=%s", port, action)
                elif msg.topic == "%s/ble" % server.config.mqtt.topic_prefix:
                    enabled = payload.get("enabled")
                    if enabled is None or not isinstance(enabled, bool):
                        _LOGGER.warning("Invalid BLE command: 'enabled' must be a bool")
                        return
                    _LOGGER.info("MQTT BLE command: %s", "enable" if enabled else "disable")
                    try:
                        if enabled:
                            asyncio.run_coroutine_threadsafe(server.ble.start(), server.loop)
                        else:
                            asyncio.run_coroutine_threadsafe(server.ble.request_stop(), server.loop)
                    except Exception as e:
                        _LOGGER.error("MQTT BLE cmd failed: %s", e)
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                _LOGGER.error("MQTT cmd parse error: %s", e)
            except Exception as e:
                _LOGGER.error("MQTT cmd error: %s", e)

        self.mqtt_client.on_message = on_mqtt_message
        self.mqtt_client.subscribe(self.config.mqtt.topic_set)
        self.mqtt_client.subscribe(self.config.mqtt.topic_ports)
        self.mqtt_client.subscribe("%s/ble" % self.config.mqtt.topic_prefix)
        _LOGGER.info("MQTT subscriptions ready")

    def mqtt_quality(self) -> dict:
        """Return MQTT connection quality metrics."""
        if not self.mqtt_client:
            return {"score": 0, "uptime": 0, "disconnects": 0, "publish_failures": 0}
        if not self.mqtt_client.is_connected():
            return {"score": 0, "uptime": 0, "disconnects": self._mqtt_disconnect_count,
                    "publish_failures": self._mqtt_publish_failures}
        uptime = int(time.time() - self._mqtt_connect_time) if self._mqtt_connect_time else 0
        dc_score = max(0, 100 - self._mqtt_disconnect_count * 15)
        pf_score = max(0, 100 - self._mqtt_publish_failures * 5)
        return {"score": round(dc_score * 0.6 + pf_score * 0.4), "uptime": uptime,
                "disconnects": self._mqtt_disconnect_count,
                "publish_failures": self._mqtt_publish_failures}

    # ── MQTT 状态 / 测试连接（不落盘、不动现有连接）──
    async def handle_mqtt_status(self, request):
        """GET /api/mqtt/status — 界面状态行轮询用。"""
        return web.json_response({"ok": True, **self.mqtt_status()})

    async def handle_mqtt_test(self, request):
        """POST /api/mqtt/test — 按传入（或当前）参数试一次握手，结果原样回显。"""
        try:
            body = await request.json() if request.can_read_body else {}
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        m = body.get("mqtt") if isinstance(body.get("mqtt"), dict) else body
        cfg = self.config.mqtt
        host = str(m.get("host") or cfg.host or "").strip()
        try:
            port = int(m.get("port") or cfg.port)
        except (TypeError, ValueError):
            port = cfg.port
        cid = str(m.get("client_id") if m.get("client_id") is not None else cfg.client_id or "").strip()
        user = str(m.get("username") if m.get("username") is not None else cfg.username or "")
        pwd = str(m.get("password") if m.get("password") is not None else cfg.password or "")
        if not host:
            return web.json_response(
                {"ok": False, "state": "failed", "reason": "Broker 地址为空",
                 "host": host, "port": port, "rc": None, "elapsed_ms": 0})
        if not cid and cfg.mode == "bemfa":
            return web.json_response(
                {"ok": False, "state": "failed",
                 "reason": "客户端 ID 为空：巴法云要填私钥 UID（见控制台）",
                 "host": host, "port": port, "rc": None, "elapsed_ms": 0})
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None, self._mqtt_probe_sync, host, port, cid, user, pwd,
            int(cfg.port) == 9503 and cfg.mode == "bemfa", 6.0)
        self._mqtt_log("TEST", host=host, port=port,
                       rc=res.get("rc"), errno=res.get("errno"),
                       reason=res.get("reason") or "ok",
                       elapsed_ms=res.get("elapsed_ms"))
        return web.json_response(res)

    def _mqtt_probe_sync(self, host, port, client_id, username, password,
                         use_tls, timeout):
        """一次性握手（阻塞，跑在线程池里）：只判断能否 CONNACK，改了不影响保存。"""
        import paho.mqtt.client as mqtt
        import threading as _threading
        t0 = time.time()
        res = {"ok": False, "state": "failed", "reason": "", "rc": None,
               "errno": None, "elapsed_ms": 0, "host": host, "port": port,
               "mode": self.config.mqtt.mode, "client_id": self._mask(client_id) if client_id else ""}
        done = _threading.Event()
        box = {}
        client = None
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id=(client_id or None))
            if username:
                client.username_pw_set(username, password or None)
            if use_tls:
                client.tls_set()

            def _on_connect(c, userdata, flags, rc, properties=None):
                box["rc"] = int(getattr(rc, "value", rc))
                done.set()

            client.on_connect = _on_connect
            client.connect(host, port, 15)
            client.loop_start()
            if not done.wait(timeout):
                res["reason"] = "连接超时（%d ms 内没有收到 CONNACK）" % int(timeout * 1000)
                res["state"] = "failed"
            else:
                code = box.get("rc", -1)
                res["rc"] = code
                if code == 0:
                    res["ok"] = True
                    res["state"] = "connected"
                    res["reason"] = ""
                else:
                    res["reason"] = self._mqtt_rc_reason(code, self.config.mqtt.mode)
        except Exception as e:
            res["errno"] = getattr(e, "errno", None)
            res["reason"] = self._mqtt_exc_reason(e)
            res["state"] = "failed"
        finally:
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    pass
                try:
                    client.loop_stop()
                except Exception:
                    pass
            res["elapsed_ms"] = int((time.time() - t0) * 1000)
        return res

    # ── C51：后端无充电自动关端口（与界面无关）──
    async def _auto_off_loop(self):
        while True:
            await asyncio.sleep(1.0)
            try:
                await self._auto_off_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _LOGGER.warning("auto_off tick failed: %s", e)

    def _auto_off_ports(self):
        ports = {}
        try:
            ctl = int(self.state.settings.get("16", 0x0F) or 0)
        except (TypeError, ValueError):
            ctl = 0x0F
        for piid in range(1, 5):
            ps = self.state.ports.get(piid)
            ports[piid] = {
                "enabled": bool(ctl & (1 << (piid - 1))),
                "current": float(getattr(ps, "current", 0) or 0),
                "voltage": float(getattr(ps, "voltage", 0) or 0),
            }
        return ports

    async def _auto_off_tick(self):
        link_ok = bool(self.state.connected and self.state.authenticated)
        fire = self._auto_off.tick(self._auto_off_ports(), link_ok, self.config.auto_off)
        for key in fire:
            await self._auto_off_cut(key)
        if fire:
            self.invalidate_status_cache()

    async def _auto_off_cut(self, key):
        cfg = self.config.auto_off
        piid = {"c1": 1, "c2": 2, "c3": 3, "a": 4}.get(key)
        ps = self.state.ports.get(piid) if piid else None
        volt = float(getattr(ps, "voltage", 0) or 0)
        cur = float(getattr(ps, "current", 0) or 0)
        # 断电前最后复核：此刻已恢复充电则不误断，并让计时归零
        if cur > float(cfg.cur_a):
            self._auto_off.mark_result(key, True)
            ao_audit("CUT_SKIP port=%s reason=charging_resumed v=%.2f c=%.3f"
                     % (key, volt, cur))
            return
        ao_audit("CUT_ATTEMPT port=%s at=%s v=%.2f c=%.3f"
                 % (key, time.strftime("%Y-%m-%d %H:%M:%S"), volt, cur))
        # 归因：规则名 + 触发值一起落库（无充电累计/阈值/窗口）
        try:
            _nc = float((self._auto_off.snapshot(cfg)["ports"].get(key, {})
                         or {}).get("no_charge_s") or 0)
        except Exception:
            _nc = float(cfg.window_s)
        self.ble.note_end_cause(
            key, "auto", "无充电超时", "",
            "无充电累计 %ds ≥ 阈值窗口 %ds（电流 %.3fA < %.2fA），规则 auto_off"
            % (int(_nc), int(cfg.window_s), cur, float(cfg.cur_a)))
        result = await self.ble.send_command("port", (key, "off"))
        ok = bool(result and result.get("ok", True))
        self._auto_off.mark_result(key, ok)
        if ok:
            self._remember_ports(key, "off")
            ao_audit("CUT_OK port=%s off_at=%s v=%.2f c=%.3f"
                     % (key, time.strftime("%Y-%m-%d %H:%M:%S"), volt, cur))
        else:
            ao_audit("CUT_FAIL port=%s at=%s v=%.2f c=%.3f result=%r"
                     % (key, time.strftime("%Y-%m-%d %H:%M:%S"), volt, cur, result))
        self.invalidate_status_cache()

    async def handle_status(self, request):
        if self._status_cache_valid and self._status_cache_bytes:
            return web.Response(
                body=self._status_cache_bytes,
                content_type="application/json",
            )
        try:
            data = await self.state.to_dict()
            # C11：清洗设备信息中的控制字符/不可见字符（不只依赖前端显示层）
            for _k in ("device_model", "firmware_version", "protocol_version"):
                _v = data.get(_k)
                if isinstance(_v, str):
                    _clean = "".join(ch for ch in _v if ch.isprintable() or ch in " \t")
                    if _clean != _v:
                        data[_k] = _clean
            data["session_recording"] = bool(self.ble.record_sessions)
            data["mqtt_connected"] = self.mqtt_client is not None and self.mqtt_client.is_connected()
            # C51：无充电计时由后端计算，仅用于审计与统计——界面不显示计时/倒计时
            data["auto_off"] = self._auto_off.snapshot(self.config.auto_off)
            now_ts = time.time()
            data["server_now"] = round(now_ts, 2)
            # PRD 需求2/4：每端口本次充电时长 / 上限 / 预警 / 保护状态
            data["charge"] = self.guard.snapshot_with_cfg(self.config, now_ts)
            # R024-A3（领导）：端口倒计时剩余时间（设定值+设定时刻→剩余秒）
            data["countdowns"] = self._countdown_snapshot()
            self._status_cache_bytes = await asyncio.to_thread(
                lambda: json.dumps(data, ensure_ascii=False).encode()
            )
            self._status_cache_valid = True
        except Exception as e:
            # BLE 重连/状态对象暂未就绪：降级为「最近一次成功快照」，
            # 避免重启窗口期该接口静默 500；无快照时返回最小重连占位。
            _LOGGER.warning("handle_status degraded during reconnect: %s", e)
            if self._status_cache_bytes:
                return web.Response(
                    body=self._status_cache_bytes,
                    content_type="application/json",
                )
            fallback = {
                "connected": False, "authenticated": False,
                "session_recording": False,
                "mqtt_connected": False,
                "auto_off": self._auto_off.snapshot(self.config.auto_off),
                "server_now": round(time.time(), 2),
                "device_model": "", "firmware_version": "",
                "protocol_version": "",
                "ports": {}, "settings": {}, "charge": {},
                "reconnecting": True,
            }
            self._status_cache_bytes = json.dumps(
                fallback, ensure_ascii=False).encode()
            return web.Response(
                body=self._status_cache_bytes,
                content_type="application/json",
            )
        return web.Response(
            body=self._status_cache_bytes,
            content_type="application/json",
        )

    async def handle_set(self, request):
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        piid = data.get("piid")
        value = data.get("value")
        if piid is None or value is None:
            return web.json_response({"ok": False, "error": "missing piid or value"}, status=400)
        try:
            piid_int = int(piid)
            value_int = int(value)
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "piid and value must be integers"}, status=400)
        if piid_int not in VALID_PIIDS:
            return web.json_response({"ok": False, "error": f"invalid piid: {piid_int}"}, status=400)
        min_val, max_val = PIID_RANGES[piid_int]
        if not (min_val <= value_int <= max_val):
            return web.json_response({"ok": False, "error": f"value must be between {min_val} and {max_val}"}, status=400)
        # R15-C18：连接态守卫（对齐 /api/port 的 C3 409），离线不假装成功
        if not (self.state.connected and self.state.authenticated):
            return web.json_response({"ok": False, "error": "device not connected/authenticated"}, status=409)
        result = await self.ble.send_command("set", (piid_int, value_int))
        # R024-A3：倒计时 piid（9-12）成功写入即记录设定时刻，供端口卡显示剩余时间
        if piid_int in self._CD_PIID_PORT and isinstance(result, dict) and result.get("ok", True):
            self._remember_countdown(piid_int, value_int)
        self.invalidate_status_cache()
        return web.json_response(result)

    async def handle_port(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        # C3：未连接/未认证时端口开关直接 409——不再对空设备假装成功
        if not (self.state.connected and self.state.authenticated):
            return web.json_response({"ok": False, "error": "device not connected/authenticated"},
                                     status=409)
        port = str(body.get("port", "")).lower()
        action = str(body.get("action", "")).lower()
        if action not in ("on", "off"):
            return web.json_response({"ok": False, "error": "action must be on/off"}, status=400)
        if port not in PORT_BITS and port != "all":
            return web.json_response({"ok": False, "error": f"unknown port: {port}"}, status=400)
        # 归因：人工关端口先登记发起入口与调用方，供会话结束时原样落库
        if action == "off":
            _entry = str(body.get("client") or
                         request.headers.get("X-Cuktech-Client") or "API 调用").strip()
            _actor = str(body.get("actor") or request.remote or "local").strip()
            _ev = ("POST /api/port port=%s action=off 入口=%s 调用方=%s"
                   % (port, _entry, _actor))
            for _p in (("c1", "c2", "c3", "a") if port == "all" else (port,)):
                self.ble.note_end_cause(_p, "manual", _entry, _actor, _ev)
        result = await self.ble.send_command("port", (port, action))
        # 手动重新开启端口时，清除充电守护的「已达上限/已保护」锁定，接受新会话
        if action == "on" and result and result.get("ok", True):
            if port == "all":
                for pid in range(1, 5):
                    self.guard.resume(pid)
            else:
                _pid = {"c1": 1, "c2": 2, "c3": 3, "a": 4}.get(port)
                if _pid:
                    self.guard.resume(_pid)
        if result and result.get("ok", True):
            self._remember_ports(port, action)
        self.invalidate_status_cache()
        return web.json_response(result)

    def _remember_ports(self, port, action):
        """R015（领导）：端口开关变更即持久化，重连后自动恢复上次状态。"""
        try:
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                                "CuktechPanel", "data")
            os.makedirs(base, exist_ok=True)
            pth = os.path.join(base, "port_states.json")
            try:
                cur = json.load(open(pth, encoding="utf-8"))
            except Exception:
                cur = {}
            if port == "all":
                for _k in ("c1", "c2", "c3", "a"):
                    cur[_k] = (action == "on")
            else:
                cur[port] = (action == "on")
            with open(pth, "w", encoding="utf-8") as _f:
                json.dump(cur, _f)
        except Exception as e:
            _LOGGER.error("save port states failed: %s", e)

    def _save_proto_extend(self, val):
        """R018：协议开关持久化（protocol_extend 整型），重连后自动恢复。"""
        try:
            _base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                                 "CuktechPanel", "data")
            os.makedirs(_base, exist_ok=True)
            with open(os.path.join(_base, "protocol_states.json"), "w") as _f:
                json.dump({"protocol_extend": int(val)}, _f)
        except Exception as e:
            _LOGGER.error("save protocol state failed: %s", e)

    # R024-A3（领导）：端口倒计时显示——记录每口倒计时的设定值与设定时刻，/api/status 暴露剩余秒数
    _CD_PIID_PORT = {9: "c1", 10: "c2", 11: "c3", 12: "a"}

    def _remember_countdown(self, piid, value):
        try:
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                                "CuktechPanel", "data")
            os.makedirs(base, exist_ok=True)
            pth = os.path.join(base, "countdown_set.json")
            try:
                cur = json.load(open(pth, encoding="utf-8"))
            except Exception:
                cur = {}
            key = self._CD_PIID_PORT.get(int(piid))
            if not key:
                return
            v = int(value)
            if v <= 0:
                cur.pop(key, None)
            else:
                cur[key] = {"value": v, "set_ts": round(time.time(), 1)}
            with open(pth, "w", encoding="utf-8") as _f:
                json.dump(cur, _f)
            self._countdown_set = cur
        except Exception as e:
            _LOGGER.error("save countdown set failed: %s", e)

    def _load_countdown(self):
        try:
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                                "CuktechPanel", "data")
            pth = os.path.join(base, "countdown_set.json")
            cur = json.load(open(pth, encoding="utf-8"))
            if not isinstance(cur, dict):
                cur = {}
        except Exception:
            cur = {}
        now = time.time()
        out = {}
        for key, item in cur.items():
            try:
                rem = int(item["value"]) * 60 - int(now - float(item["set_ts"]))
                if rem > 0:
                    out[key] = {"value": int(item["value"]), "remaining": rem}
            except Exception:
                continue
        self._countdown_set = cur
        return out

    def _countdown_snapshot(self):
        """R024-A3：带剩余秒数的倒计时快照（过期自动剔除）。"""
        now = time.time()
        out = {}
        stale = False
        for key, item in (getattr(self, "_countdown_set", {}) or {}).items():
            try:
                rem = int(item["value"]) * 60 - int(now - float(item["set_ts"]))
            except Exception:
                continue
            if rem > 0:
                out[key] = {"value": int(item["value"]), "remaining": rem}
            else:
                stale = True
        if stale:
            try:
                self._countdown_set = {k: v for k, v in self._countdown_set.items()
                                       if k in out}
                base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                                    "CuktechPanel", "data")
                with open(os.path.join(base, "countdown_set.json"), "w", encoding="utf-8") as _f:
                    json.dump(self._countdown_set, _f)
            except Exception:
                pass
        return out

    async def handle_protocol(self, request):
        """处理协议开关 (PIID 21)。

        请求体:
          {"port": "c1", "protocol": "pd"}           # toggle
          {"port": "c1", "protocol": "pd", "action": "on"}   # 显式开关
          {"switches": {"c1": {"pd": true, ...}}}     # 批量设置
          {"value": 50532111}                         # 直接写原始值
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)

        try:
            state = self.ble.state
            if "value" in body:
                try:
                    new_val = int(body["value"])
                except (TypeError, ValueError):
                    return web.json_response({"ok": False, "error": "value must be integer"}, status=400)
                if not (0 <= new_val <= 0xFFFFFFFF):
                    return web.json_response({"ok": False, "error": "value out of range (0-0xFFFFFFFF)"}, status=400)
            elif "switches" in body:
                if not isinstance(body["switches"], dict):
                    return web.json_response({"ok": False, "error": "switches must be an object"}, status=400)
                # R016-CK04：局部 switches 与现有状态合并——未提及端口保持原值
                async with state.lock:
                    merged = {pk: dict(ps) for pk, ps in state.protocol_switches.items()}
                for pk, protos in body["switches"].items():
                    if not isinstance(protos, dict):
                        return web.json_response({"ok": False, "error": "switches values must be objects"}, status=400)
                    merged.setdefault(pk, {}).update(protos)
                new_val = ChargerState.encode_protocol_extend(merged)
            elif "port" in body and "protocol" in body:
                port = str(body["port"]).lower()
                proto = str(body["protocol"]).lower()
                action = body.get("action", "toggle")

                # 加锁确保 read-modify-write-send 原子性，防止竞态
                async with state.lock:
                    current_val = state.protocol_extend
                    switches = dict(state.protocol_switches)

                    if port not in switches or proto not in switches[port]:
                        return web.json_response({"ok": False, "error": f"unknown {port}.{proto}"}, status=400)

                    cur = switches[port][proto]
                    if action == "toggle":
                        new_state = not cur
                    elif action == "on":
                        new_state = True
                    elif action == "off":
                        new_state = False
                    else:
                        return web.json_response({"ok": False, "error": f"invalid action: {action}"}, status=400)

                    _LOGGER.info("Protocol switch: %s.%s %s->%s (current 0x%08X)",
                                 port, proto, cur, new_state, current_val)

                    # 构建新的开关状态
                    new_switches = dict(switches)
                    new_switches[port] = dict(switches[port])
                    new_switches[port][proto] = new_state

                    new_val = ChargerState.encode_protocol_extend(new_switches)

                # 锁外发送 SET (send_command 内部也有 async 操作)
                result = await self.ble.send_command("set", (21, new_val))
                # 同步本地状态，确保后续 GET 读到最新值
                if result and result.get("ok"):
                    await state.update_protocol_extend(new_val)
                    self._save_proto_extend(new_val)
                    if hasattr(self, 'sse'):
                        self.sse.emit("protocol", {"switches": state.protocol_switches,
                                                    "protocol_extend": new_val})
                self.invalidate_status_cache()
                return web.json_response(result)
            else:
                return web.json_response({"ok": False, "error": "missing port/protocol or value"}, status=400)

            # 批量/原始值路径：锁外发送
            result = await self.ble.send_command("set", (21, new_val))
            if result and result.get("ok"):
                await state.update_protocol_extend(new_val)
                self._save_proto_extend(new_val)
                if hasattr(self, 'sse'):
                    self.sse.emit("protocol", {"switches": state.protocol_switches,
                                               "protocol_extend": new_val})
            return web.json_response(result)
        except Exception as e:
            # R15-S02：500 不回显内部异常文本（防信息泄露，契约见 PRD 附录）
            _LOGGER.error("Protocol switch error: %s", e)
            return web.json_response({"ok": False, "error": "internal error"}, status=500)

    async def handle_enable(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        enabled = body.get("enabled", True)
        if enabled:
            async with self._start_lock:
                if self.ble.is_running:
                    return web.json_response({"ok": True, "enabled": True, "note": "already running"})
                app_ = request.app
                if "ble_task" in app_:
                    old = app_["ble_task"]
                    if old and not old.done():
                        old.cancel()
                        try:
                            await old
                        except asyncio.CancelledError:
                            pass
                app_["ble_task"] = asyncio.create_task(self.ble.start())
        else:
            async with self._start_lock:
                await self.ble.request_stop()
                app_ = request.app
                if "ble_task" in app_ and app_["ble_task"] and not app_["ble_task"].done():
                    try:
                        await asyncio.wait_for(app_["ble_task"], timeout=10)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                # _disconnect() (在 start() 的 finally 中) 已进行完整的 Bleak 清理，
                # 不需要额外调用 _force_disconnect_bluetooth()（仅用于错误恢复/关机）
            for piid in range(1, 5):
                await self.state.update_port(piid, PORT_DEFAULT)
        self.invalidate_status_cache()
        return web.json_response({"ok": True, "enabled": enabled})

    async def handle_log_level(self, request):
        """Get or set log level."""
        if request.method == "GET":
            current = logging.getLogger().level
            level_name = logging.getLevelName(current).lower()
            return web.json_response({
                "level": level_name,
                "available": list(LOG_LEVELS.keys()),
            })

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)

        level = str(data.get("level", "")).lower()
        if level not in LOG_LEVELS:
            return web.json_response({"ok": False, "error": f"invalid level: {level}"}, status=400)

        logging.getLogger().setLevel(LOG_LEVELS[level])
        # Persist to config.yaml so the change survives restart
        try:
            import yaml
            config_path = self._config_path()
            cfg = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            if "server" not in cfg:
                cfg["server"] = {}
            cfg["server"]["log_level"] = level
            # Atomic write: write to temp file then rename
            tmp_path = config_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
            os.replace(tmp_path, config_path)
        except Exception:
            _LOGGER.warning("Failed to persist log level to config.yaml", exc_info=True)
        _LOGGER.info("Log level changed to %s", level)
        return web.json_response({"ok": True, "level": level})

    async def handle_session_recording(self, request):
        """GET/POST /api/session-recording — 充电会话记录开关。

        状态持久化在 history.db 的 meta 表（单源，随数据库备份/清除），
        即时生效，无需重启服务。关闭后不再创建新的充电会话记录，但充电
        完成事件照常发布。
        """
        if request.method == "GET":
            return web.json_response({"ok": True, "enabled": bool(self.ble.record_sessions)})
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return web.json_response({"ok": False, "error": "enabled must be boolean"}, status=400)
        was_enabled = bool(self.ble.record_sessions)
        self.ble.record_sessions = enabled
        if self.history:
            self.history.set_session_recording(enabled)
        if enabled and not was_enabled:
            # 打开瞬间：把关闭期间仍在充电的占位会话立即转为真实记录，
            # 使正在充电的端口从此刻起恢复曲线/历史（无需等到下次充电）
            self.ble.resume_recording_sessions()
        self.invalidate_status_cache()
        _LOGGER.info("Charge session recording %s", "enabled" if enabled else "disabled")
        return web.json_response({"ok": True, "enabled": enabled})

    async def handle_web_language(self, request):
        """GET/POST /api/web-language — Web UI language (DB meta, instant).

        GET 返回当前语言偏好（'auto' | 'zh-CN' | 'en'），POST 持久化到
        history.db 的 meta 表（单源，随数据库备份/清除），即时生效、无需重启。
        'auto' 表示跟随浏览器/系统语言（回退中文）。
        """
        if request.method == "GET":
            lang = self.history.get_web_language() if self.history else "auto"
            return web.json_response({"ok": True, "language": lang})
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        raw = str(data.get("language", "")).strip().lower()
        if raw in ("zh", "zh-cn", "zh-hans"):
            canonical = "zh-CN"
        elif raw == "en":
            canonical = "en"
        elif raw == "auto":
            canonical = "auto"
        else:
            return web.json_response({"ok": False, "error": "invalid language"}, status=400)
        if self.history:
            self.history.set_web_language(canonical)
        _LOGGER.info("Web UI language set to %s", canonical)
        return web.json_response({"ok": True, "language": canonical})

    async def handle_chart(self, request):
        """Get chart-ready data for all ports with caching and ETag."""
        # C5：hours 正整数/正浮点、上界 720；interval 下界 5；NaN 显式拒绝
        try:
            hours = float(request.query.get("hours", 1))
            interval = float(request.query.get("interval", 30))
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "invalid hours/interval parameter"}, status=400)
        if not (0.0 < hours <= 720.0):
            return web.json_response({"ok": False, "error": "hours must be 0<hours<=720"}, status=400)
        if not (interval >= 5.0) or interval != interval or not math.isfinite(interval):
            return web.json_response({"ok": False, "error": "interval must be >=5"}, status=400)
        interval = int(interval)
        cache_key = f"{hours}:{interval}"

        # Check cache
        now = time.time()
        entry = self._chart_cache.get(cache_key)
        if entry:
            cached_time, cached_etag, cached_body, _ = entry
            if now - cached_time < self._chart_cache_ttl:
                if_none_match = request.headers.get("If-None-Match")
                if if_none_match == cached_etag:
                    return web.Response(status=304)
                return web.Response(
                    body=cached_body,
                    content_type="application/json",
                    headers={"ETag": cached_etag},
                )

        # Generate data in thread pool (strftime + loops + json.dumps + sha256 are all CPU-bound)
        now_ts = time.time()
        start_ts = now_ts - hours * 3600
        aligned_start = (int(start_ts) // interval) * interval
        use_date = hours > 12
        aligned_now = (int(now_ts) // interval) * interval
        epochs = list(range(aligned_start, aligned_now, interval))
        # SQLite 查询为同步阻塞调用，放入线程池执行，避免阻塞事件循环
        try:
            raw_rows = await asyncio.to_thread(self.history.query_history_multi, 1, 4, hours, interval)
        except Exception as e:
            # 历史库在重连/重启窗口暂不可用：返回最近一次成功图表，避免 500。
            _LOGGER.warning("handle_chart degraded (history) during reconnect: %s", e)
            cached = self._chart_cache.get(cache_key)
            if cached:
                _, cached_etag, cached_body, _ = cached
                return web.Response(
                    body=cached_body, content_type="application/json",
                    headers={"ETag": cached_etag})
            return web.json_response(
                {"ok": True, "labels": [], "datasets": {
                    "power": [], "voltage": [], "current": []}})

        def _build_chart(epochs_, labels, raw_rows_):
            port_data = {p: {} for p in range(1, 5)}
            for row in raw_rows_:
                port_data[row["port"]][int(row["bucket"])] = (
                    row["power"], row["voltage"], row["current"]
                )

            n = len(labels)
            power = [[0.0] * n for _ in range(5)]
            voltage = [[0.0] * n for _ in range(4)]
            current = [[0.0] * n for _ in range(4)]

            for i, epoch in enumerate(epochs_):
                total = 0.0
                for port in range(1, 5):
                    entry_ = port_data[port].get(epoch)
                    if entry_ is not None:
                        p, v, c = entry_
                        power[port - 1][i] = round(p, 1)
                        voltage[port - 1][i] = round(v, 2)
                        current[port - 1][i] = round(c, 2)
                        total += p
                power[4][i] = round(total, 1)

            port_names = ["C1", "C2", "C3", "A"]
            result = {
                "ok": True,
                "labels": labels,
                "datasets": {
                    "power": [{"label": port_names[p], "data": power[p]} for p in range(4)]
                           + [{"label": "Total", "data": power[4]}],
                    "voltage": [{"label": port_names[p], "data": voltage[p]} for p in range(4)],
                    "current": [{"label": port_names[p], "data": current[p]} for p in range(4)],
                },
            }
            body = json.dumps(result, ensure_ascii=False).encode()
            etag = hashlib.sha256(body).hexdigest()
            return body, etag

        if use_date:
            all_labels = [time.strftime('%m-%d %H:%M', time.localtime(t)) for t in epochs]
        else:
            all_labels = [time.strftime('%H:%M', time.localtime(t)) for t in epochs]

        try:
            body, etag = await asyncio.to_thread(_build_chart, epochs, all_labels, raw_rows)
        except Exception as e:
            _LOGGER.warning("handle_chart degraded (build) during reconnect: %s", e)
            cached = self._chart_cache.get(cache_key)
            if cached:
                _, cached_etag, cached_body, _ = cached
                return web.Response(
                    body=cached_body, content_type="application/json",
                    headers={"ETag": cached_etag})
            return web.json_response(
                {"ok": True, "labels": [], "datasets": {
                    "power": [], "voltage": [], "current": []}})

        # Update cache: OrderedDict O(1) eviction
        self._chart_cache[cache_key] = (now, etag, body, now)
        if len(self._chart_cache) > self._chart_cache_max:
            self._chart_cache.popitem(last=False)

        return web.Response(
            body=body,
            content_type="application/json",
            headers={"ETag": etag},
        )

    async def handle_live(self, request):
        """R19：/api/status + /api/chart 合并为一次请求（服务端字节级拼接，零 JSON 解析）。

        - status 段照旧走 handle_status（缓存命中时直接复用 bytes）；
        - 客户端把上一拍的 chartTag 放进 X-Chart-Tag：tag 未变时 chart 字段为 null，
          客户端连曲线 JSON 都不用下载/解析（后台桶 5s 才变一次）；
        - X-Live-Ms / X-Live-Bytes 回传服务端耗时与体积，供客户端做五项耗时对照。
        """
        t0 = time.perf_counter()
        st = await self.handle_status(request)
        st_body = st.body or b'{}'
        chart_body = b''
        tag = request.headers.get('X-Chart-Tag') or ''
        if request.query.get('hours') is not None and request.query.get('interval') is not None:
            ch = await self.handle_chart(request)
            if ch.status == 200 and ch.body:
                tag = ch.headers.get('ETag') or ''
                # 空 tag = 客户端首拍（或 tag 丢失）：必须回整段曲线，否则客户端永远拿不到数据
                if not (request.headers.get('X-Chart-Tag') or '') or \
                        tag != (request.headers.get('X-Chart-Tag') or ''):
                    chart_body = ch.body
        parts = [b'{"status":', st_body]
        if chart_body:
            parts += [b',"chart":', chart_body]
        parts += [b',"chartTag":', json.dumps(tag).encode(), b'}']
        body = b''.join(parts)
        ms = (time.perf_counter() - t0) * 1000.0
        return web.Response(
            body=body, content_type="application/json",
            headers={"X-Live-Ms": "%.2f" % ms, "X-Live-Bytes": str(len(body))})

    @staticmethod
    def _q_float(request, key, default, lo, hi):
        """安全解析 query 浮点参数并夹取范围；非法值回退默认。"""
        try:
            x = float(request.query.get(key, default))
        except (TypeError, ValueError):
            x = float(default)
        return min(max(x, lo), hi)

    async def handle_statistics(self, request):
        """Get port statistics."""
        try:
            port = int(request.match_info.get("port", 1))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)
        if port not in range(1, 5):
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)
        hours = self._q_float(request, "hours", 24, 1, 720)
        try:
            stats = await asyncio.to_thread(self.history.get_statistics, port, int(hours))
        except Exception as e:
            _LOGGER.warning("statistics unavailable: %s", e)
            return web.json_response({"ok": True, "data": None, "degraded": True})
        return web.json_response({"ok": True, "data": stats})

    async def handle_export(self, request):
        """Export port history as CSV."""
        try:
            port = int(request.match_info.get("port", 1))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)
        if port not in range(1, 5):
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)
        hours = self._q_float(request, "hours", 24, 1, 720)
        try:
            csv_data = await asyncio.to_thread(self.history.export_csv, port, int(hours))
        except Exception as e:
            _LOGGER.warning("export unavailable: %s", e)
            return web.json_response({"ok": False, "error": "历史数据暂不可用"}, status=503)
        return web.Response(
            body=csv_data,
            content_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=port_{port}_history.csv"},
        )

    # ── 本地自动化（通知 / 告警阈值 / 定时控制） ──

    _PORT_KEY_MAP = {1: "c1", 2: "c2", 3: "c3", 4: "a"}

    # ── 1.5.5 自动规则：注入给 AutomationManager 的三个回调 ──
    def _active_sessions_by_port(self):
        out = {}
        try:
            for s in self.history.get_active_sessions():
                try:
                    out[int(s.get("port") or 0)] = s
                except (TypeError, ValueError):
                    continue
        except Exception as e:
            _LOGGER.warning("active sessions query failed: %s", e)
        return out

    def _rule_set_piid(self, port, value):
        """小电流模式 piid=15（0/1）；走现有 BLE set 通道，不新开协议。"""
        if not (self.loop and not self.loop.is_closed()):
            return
        asyncio.run_coroutine_threadsafe(
            self.ble.send_command("set", (15, int(value))), self.loop)
        self.invalidate_status_cache()

    def _rule_log_write(self, ev):
        """追加一行 JSONL：触发/结束都留痕（触发时间、设备、端口、前后模式、保持到何时）。"""
        try:
            with open(self.auto_rule_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception as e:
            _LOGGER.error("auto rule log write failed: %s", e)

    def _fire_schedule(self, port, action):
        """定时任务触发：向 BLE 发送端口开关指令。"""
        try:
            port = self._PORT_KEY_MAP.get(int(port), "all")
            if action == "off":
                # 归因：定时计划属于自动化规则，记规则名与触发时刻
                _ev = ("定时计划触发 port=%s action=off at %s（automation.json）"
                       % (port, time.strftime("%Y-%m-%d %H:%M:%S")))
                for _p in (("c1", "c2", "c3", "a") if port == "all" else (port,)):
                    self.ble.note_end_cause(_p, "auto", "定时计划", "", _ev)
            if self.loop and not self.loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self.ble.send_command("port", (port, action)), self.loop)
                _LOGGER.info("Schedule fire: %s %s", port, action)
        except Exception as e:
            _LOGGER.error("schedule fire error: %s", e)

    def _guard_cutoff(self, port_key, reason):
        """充电守护触发自动断电：向 BLE 下发端口关闭（独立于网页，硬性断开）。"""
        try:
            if reason == "limit":
                _src = "充电时长上限"
            elif reason == "protect":
                _src = "持续低功率保护"
            else:
                _src = "充电守护"
            # 归因：取守护会话里的触发值（功率/持续秒数），用户要能追到规则与数值
            _pid = {"c1": 1, "c2": 2, "c3": 3, "a": 4}.get(port_key)
            _s = self.guard.sessions.get(_pid) if _pid else None
            _pw = float(getattr(_s, "cut_power", 0) or 0)
            _el = int(getattr(_s, "cut_elapsed", 0) or 0)
            if reason == "limit":
                _ev = ("连续充电 %ds 达到端口上限（规则 limit，触发功率 %.1fW）"
                       % (_el, _pw))
            else:
                _ev = ("功率 %.1fW 低于保护阈值并持续 %ds（规则 protect），触发硬断电"
                       % (_pw, _el))
            self.ble.note_end_cause(port_key, "auto", _src, "", _ev)
            if self.loop and not self.loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self.ble.send_command("port", (port_key, "off")), self.loop)
                _LOGGER.warning("Guard cutoff: %s (%s)", port_key, reason)
                self.invalidate_status_cache()
        except Exception as e:
            _LOGGER.error("guard cutoff error: %s", e)

    def _daily_marker_path(self) -> Path:
        return Path(__file__).parent / ".daily_restart_date"

    def _check_daily_restart(self, now):
        """每日定时重启（进程级 os.execv，真正释放累积资源），同一天只触发一次。

        去重凭据落文件而非仅存内存：os.execv 重启后的新进程内存被清空，若仍处于
        目标分钟内，纯内存标记会让它再次触发、在该分钟内连环重启。文件标记可跨重启存活。
        """
        svc = getattr(self.config, "service", None)
        if svc is None or not getattr(svc, "daily_restart_enabled", False):
            return
        lt = time.localtime(now)
        hhmm = f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
        day = f"{lt.tm_year}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
        if hhmm != getattr(svc, "daily_restart_time", "06:30"):
            return
        try:
            last = self._daily_marker_path().read_text(encoding="ascii").strip()
        except Exception:
            last = ""
        if last == day or self._daily_restart_last == day:
            return
        self._daily_restart_last = day
        try:
            self._daily_marker_path().write_text(day, encoding="ascii")
        except Exception as e:
            _LOGGER.error("write daily restart marker failed: %s", e)
        _LOGGER.info("Daily restart triggered at %s", hhmm)
        asyncio.create_task(self._restart())

    async def _restore_port_states(self):
        """重连稳定后按 port_states.json 重放上次的端口开关状态。"""
        await asyncio.sleep(3.0)
        try:
            if not (self.state.connected and self.state.authenticated):
                return
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                                "CuktechPanel", "data")
            with open(os.path.join(base, "port_states.json"), encoding="utf-8") as _f:
                cur = json.load(_f)
            for pk, want in cur.items():
                if pk not in ("c1", "c2", "c3", "a"):
                    continue
                try:
                    await self.ble.send_command("port", (pk, "on" if want else "off"))
                except Exception as e:
                    _LOGGER.error("restore port %s failed: %s", pk, e)
            self.invalidate_status_cache()
            _LOGGER.info("port states restored on reconnect: %s", cur)
            # R018：协议开关一并恢复（此前重启即全关，曾影响充电器）
            try:
                with open(os.path.join(base, "protocol_states.json"), encoding="utf-8") as _f:
                    _pv = int(json.load(_f).get("protocol_extend") or 0)
                if _pv > 0:
                    await self.state.update_protocol_extend(_pv)
                    self._save_proto_extend(_pv)
                    self.invalidate_status_cache()
                    _LOGGER.info("protocol_extend restored on reconnect: %s", _pv)
            except FileNotFoundError:
                pass
            except Exception as _e2:
                _LOGGER.error("restore protocol_extend failed: %s", _e2)
        except FileNotFoundError:
            pass
        except Exception as e:
            _LOGGER.error("restore port states failed: %s", e)
        finally:
            self._restoring_ports = False

    async def _automation_loop(self):
        """后台监控：每秒检查充电状态、告警阈值、定时计划、充电守护与每日重启。"""
        while True:
            now = time.time()
            # R024-A3：启动/循环时惰性加载倒计时设定记录
            if not hasattr(self, "_countdown_set"):
                self._countdown_set = self._load_countdown()
            # R015（领导）：重连后恢复上次端口开关状态（False->True 沿触发一次）
            try:
                _conn = bool(getattr(self.state, "connected", False))
                _prev = getattr(self, "_last_connected", None)
                if _prev is not None and _conn and not _prev:
                    if not getattr(self, "_restoring_ports", False):
                        self._restoring_ports = True
                        asyncio.create_task(self._restore_port_states())
                self._last_connected = _conn
            except Exception as _e:
                _LOGGER.error("port state watch error: %s", _e)
            try:
                self.automation.tick(self.state, now)
            except Exception as e:
                _LOGGER.error("automation tick error: %s", e)
            try:
                cutoffs = self.guard.tick(self.state, self.config, now)
                # 充电进行中让 /api/status 每秒拿到新的计时基准；空闲时保留缓存零开销
                if cutoffs or any(s.active or s.stopped for s in self.guard.sessions.values()):
                    self.invalidate_status_cache()
            except Exception as e:
                _LOGGER.error("charge guard tick error: %s", e)
            try:
                self._check_daily_restart(now)
            except Exception as e:
                _LOGGER.error("daily restart check error: %s", e)
            await asyncio.sleep(1.0)

    async def handle_automation_get(self, request):
        return web.json_response({"ok": True, "data": self.automation.get()})

    async def handle_automation_notify(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        # C6/R015：notify 三字段——charge_start/charge_end 为布尔（UI/网页发 true/false）；
        # threshold 数值 0-240。原 HH:MM 校验误判字段语义导致保存 100% 失败（F-02）。
        for _k in ("charge_start", "charge_end"):
            if _k in body:
                _v = body[_k]
                if isinstance(_v, bool):
                    continue
                if isinstance(_v, str) and _v.lower() in ("true", "false"):
                    body[_k] = (_v.lower() == "true")
                    continue
                return web.json_response({"ok": False, "error": f"{_k} must be boolean"}, status=400)
        if "threshold" in body:
            _th = body["threshold"]
            if isinstance(_th, bool) or not isinstance(_th, (int, float)):
                return web.json_response({"ok": False, "error": "threshold must be numeric"}, status=400)
            if float(_th) != float(_th):
                return web.json_response({"ok": False, "error": "threshold must not be NaN"}, status=400)
            body = dict(body)
            body["threshold"] = min(max(float(_th), 0.0), 240.0)
        patch = {k: body[k] for k in ("charge_start", "charge_end", "threshold") if k in body}
        data = self.automation.set_notify(patch)
        return web.json_response({"ok": True, "data": data})

    async def handle_automation_threshold(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        port = body.get("port")
        # R024 领导裁定：智能充电保护仅对 C口1/C口2 生效
        if str(port) not in ("1", "2"):
            return web.json_response(
                {"ok": False, "error": "protection only supports C口1/C口2"}, status=400)
        try:
            min_w = float(body.get("min_w", 0) or 0)
            max_w = float(body.get("max_w", 0) or 0)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid value"}, status=400)
        # C6：NaN 显式拒绝；阈值钳到 0-240
        if min_w != min_w or max_w != max_w:
            return web.json_response({"ok": False, "error": "value must not be NaN"}, status=400)
        min_w = min(max(min_w, 0.0), 240.0)
        max_w = min(max(max_w, 0.0), 240.0)
        # R15-C02：反逻辑区间拒绝（0/0 清除组合除外）；钳制生效值随响应回显
        if min_w > max_w and not (min_w == 0.0 and max_w == 0.0):
            return web.json_response({"ok": False, "error": "min_w must be <= max_w"}, status=400)
        data = self.automation.set_threshold(int(port), min_w, max_w)
        return web.json_response({"ok": True, "data": data, "effective": {"min_w": min_w, "max_w": max_w}})

    async def handle_automation_schedule_add(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        port = body.get("port")
        if str(port) not in ("1", "2", "3", "4"):
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)
        tstr = str(body.get("time", ""))
        if not re.match(r"^\d{2}:\d{2}$", tstr) or int(tstr[:2]) > 23 or int(tstr[3:]) > 59:
            return web.json_response({"ok": False, "error": "time must be valid HH:MM (00:00-23:59)"}, status=400)
        action = str(body.get("action", "off"))
        if action not in ("on", "off"):
            return web.json_response({"ok": False, "error": "action must be on/off"}, status=400)
        try:
            data = self.automation.add_schedule(int(port), tstr, action)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True, "data": data})

    async def handle_automation_schedule_delete(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        # C6：id 安全解析（非法 id 400 而非 500）
        try:
            _sid = int(body.get("id", 0))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "id must be integer"}, status=400)
        # R15-A03：未知 id 显式 400（原静默 no-op）
        if not any(s.get("id") == _sid for s in self.automation.data.get("schedules", [])):
            return web.json_response({"ok": False, "error": f"schedule not found: {_sid}"}, status=400)
        data = self.automation.delete_schedule(_sid)
        return web.json_response({"ok": True, "data": data})

    async def handle_automation_schedule_toggle(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        # C6：id 安全解析；enabled 严格布尔
        try:
            _sid = int(body.get("id", 0))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "id must be integer"}, status=400)
        _en = body.get("enabled", True)
        if not isinstance(_en, bool):
            return web.json_response({"ok": False, "error": "enabled must be boolean"}, status=400)
        # R15-A03：未知 id 显式 400（原静默 no-op）
        if not any(s.get("id") == _sid for s in self.automation.data.get("schedules", [])):
            return web.json_response({"ok": False, "error": f"schedule not found: {_sid}"}, status=400)
        data = self.automation.toggle_schedule(_sid, _en)
        return web.json_response({"ok": True, "data": data})

    # ── 网络 / 二维码 / 导出 ──

    @staticmethod
    def _lan_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip

    async def handle_network(self, request):
        return web.json_response({"ok": True, "ip": self._lan_ip(), "port": self.config.server.port})

    async def handle_qrcode(self, request):
        try:
            import qrcode
            url = f"http://{self._lan_ip()}:{self.config.server.port}/"
            img = qrcode.make(url, box_size=8, border=2)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return web.Response(body=buf.getvalue(), content_type="image/png")
        except Exception as e:
            _LOGGER.error("qrcode error: %s", e)
            return web.json_response(
                {"ok": False, "error": "qrcode generator unavailable"}, status=503)

    async def handle_export_sessions(self, request):
        period = request.query.get("period", "all")
        port_str = request.query.get("port", "")
        port = int(port_str) if port_str.isdigit() else None
        csv_data = await asyncio.to_thread(self.history.export_sessions_csv, port, period)
        return web.Response(body=csv_data, content_type="text/csv",
                            headers={"Content-Disposition": "attachment; filename=charge_sessions.csv"})

    async def handle_export_energy(self, request):
        period = request.query.get("period", "all")
        csv_data = await asyncio.to_thread(self.history.export_daily_csv, period)
        if isinstance(csv_data, str):
            csv_data = csv_data.encode("utf-8")
        # UTF-8 BOM：Excel 双击直接打开不乱码
        return web.Response(body=b"\xef\xbb\xbf" + csv_data, content_type="text/csv",
                            headers={"Content-Disposition": "attachment; filename=charge_daily.csv"})

    MOBILE_UA = re.compile(r'Android|iPhone|iPod|webOS|BlackBerry|Windows Phone', re.I)

    async def handle_index(self, request):
        ua = request.headers.get('User-Agent', '')
        path = '/phone.html' if self.MOBILE_UA.search(ua) else '/index.html'
        entry = _static_cache.get(path)
        if entry:
            accept_gzip = request.headers.get("Accept-Encoding", "").find("gzip") != -1
            if accept_gzip and entry["gzipped"]:
                body = entry["gzipped"]
                headers = {
                    "Content-Type": "text/html",
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(body)),
                    "Cache-Control": "public, max-age=604800, immutable",
                }
            else:
                body = entry["raw"]
                headers = {
                    "Content-Type": "text/html",
                    "Content-Length": str(len(body)),
                    "Cache-Control": "public, max-age=604800, immutable",
                }
            return _attach_session(web.Response(body=body, headers=headers), request)
        # 缓存未命中（如打包树缺 web/static 导致根 HTML 未预加载）时仍要走票据通道
        return _attach_session(web.FileResponse(WEB_DIR / path.lstrip('/')), request)

    # ── Charge Session API ──

    async def handle_sessions(self, request):
        """GET /api/sessions?port=c1&period=today&limit=10&page=1"""
        port_str = request.query.get("port", "")
        period = request.query.get("period", "today")
        # C5：limit 钳 1-50（下界 1，杜绝除零）；page 钳到合法页码并回显实际生效值
        try:
            limit = int(request.query.get("limit", "10"))
        except (ValueError, TypeError):
            limit = 10
        limit = min(max(limit, 1), 50)
        try:
            page = int(request.query.get("page", "1"))
        except (ValueError, TypeError):
            page = 1
        page = max(1, page)

        # R15-C04：period/port 白名单（对齐 energy/stats 口径），未知枚举 400 而非静默兜底
        if period not in ("24h", "today", "yesterday", "week", "month", "all"):
            return web.json_response({"ok": False, "error": f"invalid period: {period}"}, status=400)
        port = None
        if port_str:
            port_map = {"c1": 1, "c2": 2, "c3": 3, "a": 4}
            if port_str not in port_map:
                return web.json_response({"ok": False, "error": f"unknown port: {port_str}"}, status=400)
            port = port_map[port_str]

        loop = asyncio.get_running_loop()
        try:
            sessions, total = await loop.run_in_executor(
                None, self.history.get_sessions, port, period, limit, (page - 1) * limit)
        except Exception as e:
            # 历史库在重连/重启窗口暂不可用：返回空列表，避免 500。
            _LOGGER.warning("handle_sessions degraded during reconnect: %s", e)
            return web.json_response({
                "sessions": [], "total": 0, "page": page,
                "limit": limit, "pages": 1})

        # Merge live energy data for active sessions
        now = time.time()
        live = self.ble.get_live_session_data()
        db_session_ids = {s.get("id") for s in sessions}
        for port_id, ld in live.items():
            sid = ld.get("session_id")
            matched = False
            for s in sessions:
                if s.get("id") == sid:
                    s["total_wh"] = ld["session_wh"]
                    s["peak_power_w"] = max(s.get("peak_power_w", 0), ld["max_power"])
                    # Recalculate avg from live data (DB values are stale for active sessions)
                    start_time = ld.get("start_time") or now
                    dur_sec = max(1, int(now - start_time))
                    dur_h = dur_sec / 3600.0
                    s["avg_power_w"] = round(ld["session_wh"] / dur_h, 1) if dur_h > 0 else 0
                    port_state = self.ble.state.ports.get(port_id)
                    if port_state:
                        s["avg_voltage"] = round(port_state.voltage, 2)
                        s["avg_current"] = round(port_state.current, 2)
                    s["duration_sec"] = dur_sec
                    s["is_active"] = True
                    matched = True
                    break
            if not matched and sid and page == 1:
                # 1.5.3：活跃会话只在第 1 页补一次。旧实现每页都 insert，
                # 逐页取全（limit=50 × 7 页）会在明细里出现 7~8 条重复的「进行中」。
                # Active session not in DB results (total_wh=0, filtered out) — add it
                start_time = ld.get("start_time") or now
                dur_sec = max(1, int(now - start_time))
                dur_h = dur_sec / 3600.0
                avg_p = round(ld["session_wh"] / dur_h, 1) if dur_h > 0 else 0
                # Current V/I from port state as approximate averages for active session
                port_state = self.ble.state.ports.get(port_id)
                avg_v = round(port_state.voltage, 2) if port_state else 0
                avg_i = round(port_state.current, 2) if port_state else 0
                sessions.insert(0, {
                    "id": sid, "port": port_id, "start_time": start_time,
                    "end_time": None, "total_wh": ld["session_wh"],
                    "avg_power_w": avg_p, "peak_power_w": ld["max_power"],
                    "avg_voltage": avg_v, "avg_current": avg_i,
                    "duration_sec": dur_sec,
                    "protocol": port_state.protocol if port_state else "", "is_active": True,
                })
                total += 1

        # Mark unmatched sessions as inactive
        live_sids = {ld.get("session_id") for ld in live.values()}
        for s in sessions:
            if s.get("id") not in live_sids:
                s["is_active"] = False

        return web.json_response({
            "sessions": sessions,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, (total + limit - 1) // limit),
        })

    async def handle_session_points(self, request):
        """GET /api/sessions/{id}/points?downsample=600"""
        try:
            sid = int(request.match_info["id"])
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid session id"}, status=400)
        try:
            target = int(request.query.get("downsample", "0"))
        except (TypeError, ValueError):
            target = 0
        loop = asyncio.get_running_loop()
        try:
            points = await loop.run_in_executor(
                None, self.history.get_session_points, sid)
        except Exception as e:
            _LOGGER.warning("session points unavailable: %s", e)
            return web.json_response({"points": [], "degraded": True})
        if target > 0 and len(points) > target:
            from downsample import lttb_downsample
            points = await loop.run_in_executor(
                None, lttb_downsample, points, target)
        return web.json_response({"points": points})

    async def handle_sessions_active(self, request):
        """GET /api/sessions/active —— 端口卡「本次充电设备名」。"""
        loop = asyncio.get_running_loop()
        try:
            sess = await loop.run_in_executor(None, self.history.get_active_sessions)
        except Exception as e:
            _LOGGER.warning("active sessions unavailable: %s", e)
            return web.json_response({"ok": False, "sessions": [], "degraded": True})
        return web.json_response({"ok": True, "sessions": sess})

    async def handle_auto_rules_get(self, request):
        """GET /api/auto_rules —— 开关/保持时长/当前保持中的规则。"""
        return web.json_response({"ok": True, **self.automation.get_auto_rule()})

    async def handle_auto_rules_set(self, request):
        """POST /api/auto_rules —— {enabled, hold_minutes} 热生效（无重启）。"""
        try:
            body = await request.json() if request.can_read_body else {}
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        try:
            return web.json_response({"ok": True, **self.automation.set_auto_rule(body or {})})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_auto_rules_log(self, request):
        """GET /api/auto_rules/log —— 最近 200 条自动执行留痕。"""
        events = []
        try:
            if os.path.exists(self.auto_rule_log_path):
                with open(self.auto_rule_log_path, encoding="utf-8") as f:
                    for ln in f.readlines()[-200:]:
                        try:
                            events.append(json.loads(ln))
                        except Exception:
                            continue
        except Exception as e:
            _LOGGER.warning("auto rule log read failed: %s", e)
        return web.json_response({"ok": True, "events": events})

    async def handle_energy_stats(self, request):
        """GET /api/energy/stats?period=today"""
        period = request.query.get("period", "today")
        # C6：period 白名单校验
        if period not in ("24h", "today", "yesterday", "week", "month", "all"):
            return web.json_response({"ok": False, "error": "invalid period"}, status=400)
        loop = asyncio.get_running_loop()
        try:
            stats = await loop.run_in_executor(
                None, self.history.get_energy_stats, period)
        except Exception as e:
            # 历史库在重连/重启窗口暂不可用：返回零值统计，避免 500。
            _LOGGER.warning("handle_energy degraded during reconnect: %s", e)
            return web.json_response({
                "total_wh": 0, "session_count": 0, "peak_power_w": 0,
                "avg_power_w": 0, "total_duration_sec": 0, "by_port": {}})

        # Merge live data from active sessions
        now = time.time()
        live = self.ble.get_live_session_data()
        live_count = len(live)
        for port, ld in live.items():
            stats["total_wh"] = round(stats["total_wh"] + ld["session_wh"], 2)
            stats["peak_power_w"] = max(stats.get("peak_power_w", 0), ld["max_power"])
            # Add live duration for active sessions
            if ld.get("start_time"):
                live_duration = int(now - ld["start_time"])
                stats["total_duration_sec"] = stats.get("total_duration_sec", 0) + live_duration
            port_key = str(port)
            if port_key not in stats.get("by_port", {}):
                stats.setdefault("by_port", {})[port_key] = {"wh": 0, "count": 0}
            stats["by_port"][port_key]["wh"] = round(
                stats["by_port"][port_key]["wh"] + ld["session_wh"], 2)
            stats["by_port"][port_key]["count"] = stats["by_port"][port_key].get("count", 0) + 1
            stats["by_port"][port_key]["is_active"] = True

        # DB count excludes active sessions (total_wh=0), add them back
        stats["session_count"] = stats.get("session_count", 0) + live_count

        # Recalculate avg_power_w from total energy and total duration
        total_dur = stats.get("total_duration_sec", 0)
        total_wh = stats.get("total_wh", 0)
        stats["avg_power_w"] = round(total_wh / (total_dur / 3600), 1) if total_dur > 0 else 0

        return web.json_response(stats)

    async def handle_sse(self, request):
        """GET /api/events — Server-Sent Events stream."""
        response = web.StreamResponse(
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
        response.content_type = "text/event-stream"
        await response.prepare(request)

        queue: asyncio.Queue = asyncio.Queue(maxsize=SSEEmitter.MAX_QUEUE_SIZE)
        self.sse.add_client(queue)

        # Send full state on connect so client can initialize
        try:
            full_state = await self.state.to_dict()
            full_state["type"] = "init"
            _now_ts = time.time()
            full_state["server_now"] = round(_now_ts, 2)
            full_state["charge"] = self.guard.snapshot_with_cfg(self.config, _now_ts)
            await response.write(
                f"data: {json.dumps(full_state, ensure_ascii=False)}\n\n".encode()
            )
        except Exception as e:
            _sse_log.error("Failed to send SSE init event: %s", e)

        try:
            while True:
                # Send latest pending status + port updates before each event
                # (stale statuses are replaced, never delivered behind newer events)
                for pending in self.sse.flush_pending(queue):
                    await response.write(f"data: {pending}\n\n".encode())
                # Event wait with 2s granularity: coalesced pending events
                # (status / port_update) are delivered within <=2s even when the
                # queue is otherwise idle; the timeout doubles as SSE keepalive.
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=2.0)
                    await response.write(f"data: {msg}\n\n".encode())
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
        except asyncio.CancelledError:
            _sse_log.debug("SSE handler cancelled")
        except (ConnectionError, OSError, RuntimeError) as e:
            _sse_log.debug("SSE client disconnected (%s: %s)", type(e).__name__, e)
        except asyncio.TimeoutError:
            _sse_log.debug("SSE keepalive timeout, closing connection")
        except Exception as e:
            _sse_log.warning("SSE handler error: %s: %s", type(e).__name__, e)
        finally:
            self.sse.remove_client(queue)
            _sse_log.info("SSE client cleaned up (total: %d)", len(self.sse._clients))
        return response

    # ── Configuration API ──

    @staticmethod
    def _mask(val):
        """Mask sensitive value: show first 4 + **** + last 4."""
        if not val or len(val) <= 8:
            return "****"
        return val[:4] + "****" + val[-4:]

    async def handle_config_get(self, request):
        """GET /api/config — return current config with masked sensitive fields."""
        cfg = self.config
        return web.json_response({
            "ok": True,
            "config": {
                "ble": {
                    "mac": cfg.ble.mac,
                    "token": self._mask(cfg.ble.token),
                    "ble_key": self._mask(cfg.ble.ble_key),
                    "scan_timeout": cfg.ble.scan_timeout,
                },
                "mqtt": {
                    "enabled": cfg.mqtt.enabled,
                    "mode": cfg.mqtt.mode,
                    "host": cfg.mqtt.host,
                    "port": cfg.mqtt.port,
                    "client_id": cfg.mqtt.client_id,
                    "username": cfg.mqtt.username,
                    "password": self._mask(cfg.mqtt.password),
                    "topic_prefix": cfg.mqtt.topic_prefix,
                    "topic_c1": cfg.mqtt.topic_c1,
                    "topic_c2": cfg.mqtt.topic_c2,
                    "topic_c3": cfg.mqtt.topic_c3,
                    "topic_a": cfg.mqtt.topic_a,
                    "qos": cfg.mqtt.qos,
                    "remote_acked_host": cfg.mqtt.remote_acked_host,
                },
                "auto_off": {
                    "enabled": cfg.auto_off.enabled,
                    "window_s": cfg.auto_off.window_s,
                    "exempt": list(cfg.auto_off.exempt),
                },
                "server": {
                    "port": cfg.server.port,
                    "log_level": cfg.server.log_level,
                    "history_retention_days": cfg.server.history_retention_days,
                },
                "service": {
                    "auto_start": cfg.service.auto_start,
                    "daily_restart_enabled": cfg.service.daily_restart_enabled,
                    "daily_restart_time": cfg.service.daily_restart_time,
                },
                "charge_limit": {
                    "c1": cfg.charge_limit.c1,
                    "c2": cfg.charge_limit.c2,
                    "c3": cfg.charge_limit.c3,
                    "a": cfg.charge_limit.a,
                },
                "protection": {
                    "enabled": cfg.protection.enabled,
                    "low_power_w": cfg.protection.low_power_w,
                    "low_power_minutes": cfg.protection.low_power_minutes,
                },
            },
        })

    def _validate_config_data(self, data):
        """校验配置段，返回错误列表（空列表表示通过）。"""
        errors = []
        if not isinstance(data, dict):
            return ["config 必须是对象"]
        # R016-CK01：已登记节值必须为对象，否则合并循环 500+异常回显
        for _sec in ("ble", "mqtt", "auto_off", "server",
                     "service", "charge_limit", "protection"):
            if _sec in data and not isinstance(data[_sec], dict):
                errors.append(f"{_sec} 必须是对象")
        # R016-CK02：未知节不再静默接受
        for _sec in data:
            if _sec not in ("ble", "mqtt", "auto_off", "server",
                            "service", "charge_limit", "protection"):
                errors.append(f"未知配置节: {_sec}")
        if errors:
            return errors

        def _num(v, lo, hi, name, integer=False):
            try:
                x = float(v)
                if integer and not float(x).is_integer():
                    errors.append(f"{name} 必须是整数")
                elif not (lo <= x <= hi):
                    errors.append(f"{name} 必须在 {lo}~{hi}")
            except (TypeError, ValueError):
                errors.append(f"{name} 必须是数字")

        if "server" in data and isinstance(data["server"], dict):
            s = data["server"]
            if "port" in s:
                _num(s["port"], 1, 65535, "server.port", integer=True)
            if "log_level" in s and s["log_level"] not in LOG_LEVELS:
                errors.append(f"server.log_level 必须是 {sorted(LOG_LEVELS)}")
            if "host" in s:
                try:
                    ipaddress.ip_address(str(s["host"]))
                except ValueError:
                    errors.append("server.host 必须是合法 IP")
            for k in ("idle_shutdown_seconds", "history_retention_days"):
                if k in s:
                    _num(s[k], 0, 100000, f"server.{k}", integer=True)
            if "command_timeout" in s:
                _num(s["command_timeout"], 0.1, 600, "server.command_timeout")
            for k in ("reconnect_base_delay", "reconnect_max_delay"):
                if k in s:
                    _num(s[k], 0, 3600, f"server.{k}")

        if "ble" in data and isinstance(data["ble"], dict):
            b = data["ble"]
            if "mac" in b and b["mac"] and not re.fullmatch(
                    r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", str(b["mac"])):
                errors.append("ble.mac 必须是形如 AA:BB:CC:DD:EE:FF 的 MAC")
            for k in ("token", "ble_key"):
                if b.get(k):
                    try:
                        bytes.fromhex(str(b[k]))
                    except ValueError:
                        errors.append(f"ble.{k} 必须是合法 hex")
            if "scan_timeout" in b:
                _num(b["scan_timeout"], 1, 300, "ble.scan_timeout", integer=True)

        if "mqtt" in data and isinstance(data["mqtt"], dict):
            m = data["mqtt"]
            if "port" in m:
                _num(m["port"], 1, 65535, "mqtt.port", integer=True)
            if "enabled" in m and not isinstance(m["enabled"], bool):
                errors.append("mqtt.enabled 必须是布尔")
            if "mode" in m and m["mode"] not in ("generic", "bemfa"):
                errors.append("mqtt.mode 只能是 generic 或 bemfa")
            if "qos" in m:
                try:
                    if int(m["qos"]) not in (0, 1):
                        errors.append("mqtt.qos 只能是 0 或 1（巴法云 QoS2 会强制下线）")
                except (TypeError, ValueError):
                    errors.append("mqtt.qos 必须是整数 0 或 1")
            if m.get("topic_prefix") and len(str(m["topic_prefix"])) > 120:
                errors.append("mqtt.topic_prefix 过长")
            # 巴法云：四路主题必须原样对应控制台里已建好的主题名，
            # 不允许空、不允许斜杠、只允许字母数字下划线。
            _mode = m.get("mode", self.config.mqtt.mode)
            if _mode == "bemfa":
                for _n in BEMFA_PORT_NAMES:
                    _key = "topic_%s" % _n
                    _val = m[_key] if _key in m else self.config.mqtt.bemfa_topic(_n)
                    _err = bemfa_topic_error(BEMFA_PORT_LABELS[_n], _val)
                    if _err:
                        errors.append("mqtt." + _err)

        if "auto_off" in data and isinstance(data["auto_off"], dict):
            ao = data["auto_off"]
            if "enabled" in ao and not isinstance(ao["enabled"], bool):
                errors.append("auto_off.enabled 必须是布尔")
            if "window_s" in ao:
                _num(ao["window_s"], 10, 86400, "auto_off.window_s", integer=True)
            if "exempt" in ao:
                _ex = ao["exempt"]
                if not isinstance(_ex, list) or any(
                        str(x).strip().lower() not in ("c1", "c2", "c3", "a") for x in _ex):
                    errors.append("auto_off.exempt 必须是端口名数组（c1/c2/c3/a）")

        if "charge_limit" in data and isinstance(data["charge_limit"], dict):
            for k in ("c1", "c2", "c3", "a"):
                if k in data["charge_limit"]:
                    _num(data["charge_limit"][k], 0, 100000, f"charge_limit.{k}", integer=True)

        if "protection" in data and isinstance(data["protection"], dict):
            p = data["protection"]
            if "enabled" in p and not isinstance(p["enabled"], bool):
                errors.append("protection.enabled 必须是布尔")
            if "low_power_w" in p:
                _num(p["low_power_w"], 0, 10000, "protection.low_power_w")
            if "low_power_minutes" in p:
                _num(p["low_power_minutes"], 0, 10000, "protection.low_power_minutes", integer=True)

        if "service" in data and isinstance(data["service"], dict):
            sv = data["service"]
            for k in ("auto_start", "daily_restart_enabled"):
                if k in sv and not isinstance(sv[k], bool):
                    errors.append(f"service.{k} 必须是布尔")
            if "daily_restart_time" in sv and not re.fullmatch(
                    r"[0-2]\d:[0-5]\d", str(sv["daily_restart_time"])):
                errors.append("service.daily_restart_time 必须形如 HH:MM")
        return errors

    _RESTART_SECTIONS = ("ble",)   # 1.5.5：MQTT 改为热重连，不再要求整包重启
    _RESTART_SERVER_KEYS = ("port", "host", "lan_mode")

    def _need_restart_for(self, config_data):
        """判断本次配置变更是否必须重启/重连。

        - ble（MAC/Token/Key）：涉及连接 → 重启
        - server.port/host/lan_mode：监听地址变化 → 重启
        - 其余（charge_limit/protection/service 与 server 数值）：可热生效
        """
        for sec in self._RESTART_SECTIONS:
            if sec in config_data:
                return True
        sv = config_data.get("server")
        if isinstance(sv, dict) and any(k in sv for k in self._RESTART_SERVER_KEYS):
            return True
        return False

    def _apply_hot_config(self):
        """热生效：重新加载配置并替换非连接类子配置，不中断 BLE 连接。"""
        import config as config_mod
        new_cfg = config_mod.load_config()
        self.config.server = new_cfg.server
        self.config.service = new_cfg.service
        self.config.charge_limit = new_cfg.charge_limit
        self.config.protection = new_cfg.protection
        self.config.auto_off = new_cfg.auto_off
        # 日志级别热生效
        try:
            level = LOG_LEVELS.get(self.config.server.log_level, logging.INFO)
            logging.getLogger().setLevel(level)
        except Exception:
            pass

    async def handle_config_save(self, request):
        """POST /api/config — 保存配置；连接类变更重启，数值类热生效。"""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)

        config_data = data.get("config", {})
        config_path = self._config_path()

        _cerr = self._validate_config_data(config_data)
        if _cerr:
            return web.json_response(
                {"ok": False, "error": "配置校验失败：" + "；".join(_cerr)}, status=400)

        try:
            import yaml
            # Load existing config to preserve unknown fields
            existing = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    existing = yaml.safe_load(f) or {}

            # Merge — skip masked placeholder values (****)
            SENSITIVE_KEYS = {"token", "ble_key", "password", "uid"}
            for section in ("ble", "mqtt", "auto_off", "server",
                            "service", "charge_limit", "protection"):
                if section in config_data:
                    if section not in existing:
                        existing[section] = {}
                    for k, v in config_data[section].items():
                        if k in SENSITIVE_KEYS and (not v or "****" in str(v)):
                            continue  # R2-CK02：掩码值与空串均跳过——误清空不再抹掉已存凭证
                        existing[section][k] = v

            # Validate server.log_level value (prevent config corruption from frontend)
            if "server" in existing and "log_level" in existing["server"]:
                level = existing["server"]["log_level"]
                if level not in LOG_LEVELS:
                    _LOGGER.warning(
                        "Ignoring invalid log_level '%s' in config save, fallback to 'info'",
                        level,
                    )
                    existing["server"]["log_level"] = "info"

            # Atomic write: write to temp file then rename
            tmp_path = config_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
            os.replace(tmp_path, config_path)

            # 1.5.5：MQTT 配置热生效——停旧客户端 → 重载 mqtt 段 → 重新连接/订阅，
            # 不重启进程、不断 BLE。失败的连接也照样落盘，下次启动自动回填重试。
            _mq_changed = isinstance(config_data.get("mqtt"), dict)
            if _mq_changed:
                import config as _cfgmod
                try:
                    if self.mqtt_client is not None:
                        try:
                            self.mqtt_client.loop_stop()
                        except Exception:
                            pass
                        try:
                            self.mqtt_client.disconnect()
                        except Exception:
                            pass
                        self.mqtt_client = None
                    self.config.mqtt = _cfgmod.load_config().mqtt
                    if not self.config.mqtt.enabled and self.ble:
                        self.ble.set_mqtt_publisher(None)
                    await self.setup_mqtt()
                except Exception as _e:
                    self._mqtt_state = "failed"
                    self._mqtt_reason = "MQTT 热重连失败：%s" % _e
                    _LOGGER.error("MQTT hot reconnect failed: %s", _e)

            if self._need_restart_for(config_data):
                _LOGGER.info("Config saved to %s, restarting...", config_path)
                # Schedule restart after response (use asyncio.sleep instead of deprecated call_later+ensure_future)
                loop = asyncio.get_running_loop()
                loop.create_task(self._delayed_restart())
                # C15：前端据此区分「已保存·需重启 / 需重连」
                return web.json_response(
                    {"ok": True, "message": "配置已保存，服务将在 1 秒后重启", "restart": True,
                     "restart_required": True,
                     "reconnect_required": bool("ble" in config_data)})

            # 热生效：数值类变更不重启、不中断 BLE 连接
            self._apply_hot_config()
            _LOGGER.info("Config saved to %s (hot applied, no restart)", config_path)
            return web.json_response(
                {"ok": True,
                 "message": ("配置已保存，MQTT 已按新设置重连" if _mq_changed
                             else "配置已热生效，无需重启"),
                 "restart": False, "restart_required": False,
                 "mqtt_reapplied": bool(_mq_changed),
                 "reconnect_required": False})
        except ImportError:
            return web.json_response({"ok": False, "error": "yaml module not installed"}, status=500)
        except Exception as e:
            _LOGGER.error("Config save failed: %s", e)
            _LOGGER.error("config save failed: %s", e)
            return web.json_response({"ok": False, "error": "internal error"}, status=500)

    async def _delayed_restart(self):
        """Delay 1 second then restart."""
        await asyncio.sleep(1.0)
        await self._restart()

    # ── 服务管理（PRD 需求1：开机自启 / 运行状态 / 每日定时重启） ──
    _CREATE_NO_WINDOW = 0x08000000  # Windows: 子进程不弹黑窗

    # 当前用户登录自启使用的 HKCU Run 键（免管理员、不落地脚本，最不易被安全策略拦截）
    _AUTOSTART_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def _autostart_command(self) -> str:
        # 架构 A01：开机自启启动主程序并隐藏到托盘（--tray），
        # 由主程序以 QProcess 托管 headless，实现同生共死。
        # 冻结时直接用当前 exe 路径（位置无关，换部署目录也正确）。
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}" --tray'
        app_exe = Path(__file__).parent / "app" / "酷态科控制面板.exe"
        if app_exe.exists():
            return f'"{app_exe}" --tray'
        launcher = str(Path(__file__).parent / "start_service.vbs")
        return f'wscript.exe "{launcher}"'

    def _autostart_task_exists(self) -> bool:
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._AUTOSTART_RUN_KEY,
                               0, winreg.KEY_QUERY_VALUE)
            try:
                winreg.QueryValueEx(k, AUTOSTART_TASK_NAME)
                return True
            except FileNotFoundError:
                return False
            finally:
                k.Close()
        except Exception:
            return False

    def _set_autostart(self, enabled: bool):
        """在 HKCU Run 键创建/删除登录自启项（隐藏运行 start_service.vbs，后者自带 8080 端口探测防重复）。

        选型：本机非提权上下文 schtasks /Create 被“拒绝访问”，向启动文件夹写 .vbs 又被
        安全策略拦截（典型持久化行为）；HKCU Run 为当前用户私有、无需管理员、不落地脚本，
        是该环境下最稳的用户级登录自启。
        """
        try:
            import winreg
            k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self._AUTOSTART_RUN_KEY, 0,
                                   winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
            try:
                if enabled:
                    winreg.SetValueEx(k, AUTOSTART_TASK_NAME, 0,
                                      winreg.REG_SZ, self._autostart_command())
                else:
                    try:
                        winreg.DeleteValue(k, AUTOSTART_TASK_NAME)
                    except FileNotFoundError:
                        pass
            finally:
                k.Close()
            if enabled and not self._autostart_task_exists():
                return False, "registry value not persisted"
            return True, ""
        except Exception as e:
            _LOGGER.error("set autostart exception: %s", e)
            return False, str(e)

    def _sync_autostart_with_config(self):
        """启动时按 config.service.auto_start 幂等同步任务计划（配置即真相）。"""
        try:
            want = bool(self.config.service.auto_start)
            has = self._autostart_task_exists()
            if want != has:
                ok, msg = self._set_autostart(want)
                _LOGGER.info("sync autostart -> %s: ok=%s %s", want, ok, msg or "")
        except Exception as e:
            _LOGGER.error("sync autostart failed: %s", e)

    def _patch_service_yaml(self, patch: dict):
        """即时把 service 段写回 config.yaml（不触发整包重启），保持与内存一致。"""
        try:
            import yaml
            p = self._config_path()
            existing = {}
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    existing = yaml.safe_load(f) or {}
            sec = existing.get("service", {}) or {}
            sec.update(patch)
            existing["service"] = sec
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
            os.replace(tmp, p)
        except Exception as e:
            _LOGGER.error("patch service yaml failed: %s", e)

    async def handle_service(self, request):
        """GET /api/service：查询自启/运行状态；POST：op=autostart 即时开关开机自启。"""
        if request.method == "GET":
            exists = await asyncio.to_thread(self._autostart_task_exists)
            return web.json_response({"ok": True, "service": {
                "auto_start": exists,
                "ble_running": bool(getattr(self.ble, "is_running", False)),
                "connected": bool(self.state.connected),
                "daily_restart_enabled": self.config.service.daily_restart_enabled,
                "daily_restart_time": self.config.service.daily_restart_time,
            }})
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        # C2：op/action 双名兼容；新增 restart/stop 分支
        op = body.get("op") or body.get("action")
        if op == "autostart":
            enabled = bool(body.get("enabled"))
            ok, msg = await asyncio.to_thread(self._set_autostart, enabled)
            if ok:
                self._patch_service_yaml({"auto_start": enabled})
                self.config.service.auto_start = enabled
            exists = await asyncio.to_thread(self._autostart_task_exists)
            return web.json_response({"ok": ok, "error": msg, "auto_start": exists})
        if op == "restart":
            # 先应答再延迟重启：_restart() 是 os.execv 进程替换，响应等不到重启完成
            async def _delayed_restart():
                await asyncio.sleep(0.4)
                await self._restart()
            asyncio.ensure_future(_delayed_restart())
            return web.json_response({"ok": True, "msg": "restart scheduled"})
        if op == "stop":
            # 优雅停服（BLE 先停再退出；GUI 侧 8s 自愈 watchdog 会接管重拉）
            async def _delayed_stop():
                await asyncio.sleep(0.4)
                try:
                    await asyncio.wait_for(self.ble.request_stop(), timeout=5.0)
                except Exception:
                    pass
                _LOGGER.info("Service stop requested; exiting")
                os._exit(0)
            asyncio.ensure_future(_delayed_stop())
            return web.json_response({"ok": True, "msg": "stop scheduled"})
        return web.json_response(
            {"ok": False, "error": "unknown op, expect autostart/restart/stop"}, status=400)

    async def _restart(self):
        """Gracefully restart the server by re-exec-ing the process."""
        _LOGGER.info("Restarting server...")
        s = get_server()
        try:
            await asyncio.wait_for(s.ble.request_stop(), timeout=5.0)
        except Exception:
            pass
        # 每日重启前 VACUUM 数据库，消除碎片化、减小文件体积
        try:
            s.history.vacuum()
        except Exception:
            pass
        s.history.close()
        # Re-exec: replace current process with fresh server
        os.execv(sys.executable, [sys.executable, str(Path(__file__).parent / "ha_server.py")])


WEB_DIR = Path(__file__).parent / "web"
_server = None

# ── 静态文件缓存（启动时预加载 + 预压缩） ──
_static_cache = {}
_GZIP_TYPES = (".js", ".css", ".html", ".svg", ".json", ".txt")


def _cache_static_files():
    """递归扫描 static 目录，预加载并预压缩所有文件到内存。"""
    static_dir = WEB_DIR / "static"
    if not static_dir.is_dir():
        return
    for fpath in static_dir.rglob("*"):
        if not fpath.is_file():
            continue
        rel = fpath.relative_to(static_dir).as_posix()
        key = f"/static/{rel}"
        raw = fpath.read_bytes()
        ext = fpath.suffix.lower()
        # 按后缀推断 content-type
        mime_map = {
            ".js": "application/javascript",
            ".css": "text/css",
            ".html": "text/html",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".svg": "image/svg+xml",
            ".json": "application/json",
            ".txt": "text/plain",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".webp": "image/webp",
        }
        ct = mime_map.get(ext, "application/octet-stream")
        # 预计算 gzip 版本
        gzipped = None
        if ext in _GZIP_TYPES and len(raw) >= 1024:
            compressed = gzip.compress(raw)
            if len(compressed) < len(raw):
                gzipped = compressed
        _static_cache[key] = {
            "raw": raw,
            "gzipped": gzipped,
            "content_type": ct,
        }
    # 根目录 HTML 文件也加入缓存
    for html_name in ("index.html", "phone.html", "config.html"):
        html_path = WEB_DIR / html_name
        if html_path.is_file():
            raw = html_path.read_bytes()
            compressed = gzip.compress(raw)
            gzipped = compressed if len(compressed) < len(raw) else None
            _static_cache[f"/{html_name}"] = {
                "raw": raw,
                "gzipped": gzipped,
                "content_type": "text/html",
            }


async def handle_cached_static(request):
    """从内存缓存响应静态文件，避免磁盘 I/O 和运行时 gzip。"""
    entry = _static_cache.get(request.path)
    if entry is None:
        raise web.HTTPNotFound()
    accept_gzip = request.headers.get("Accept-Encoding", "").find("gzip") != -1
    if accept_gzip and entry["gzipped"]:
        body = entry["gzipped"]
        headers = {
            "Content-Type": entry["content_type"],
            "Content-Encoding": "gzip",
            "Content-Length": str(len(body)),
            "Cache-Control": "public, max-age=604800, immutable",
        }
    else:
        body = entry["raw"]
        headers = {
            "Content-Type": entry["content_type"],
            "Content-Length": str(len(body)),
            "Cache-Control": "public, max-age=604800, immutable",
        }
    resp = web.Response(body=body, headers=headers)
    # 仅 HTML 页面签发网页会话票据（静态资源保持 immutable 缓存，不参与）
    if str(entry.get("content_type", "")).startswith("text/html"):
        _attach_session(resp, request)
    return resp


def get_server():
    """获取全局 Server 单例 (惰性初始化)。"""
    global _server
    if _server is None:
        _server = Server()
    return _server


# ============================ 本机访问 token 鉴权 ============================
def _runtime_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "CuktechPanel")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _runtime_token_file():
    return os.path.join(_runtime_dir(), "runtime.json")


def load_or_create_token():
    """启动时读取或生成一次性本机 token，并落盘供原生客户端读取。"""
    f = _runtime_token_file()
    token = None
    try:
        if os.path.exists(f):
            token = json.load(open(f, encoding="utf-8")).get("token")
    except Exception:
        token = None
    if not token:
        token = secrets.token_urlsafe(24)
        try:
            json.dump({"token": token, "wall": time.time()},
                      open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception:
            pass
    return token


LOCAL_TOKEN = load_or_create_token()


def _is_loopback_request(request):
    """只认 socket 对端地址。**不再回落到 Host 头**（Host 可伪造，LAN 模式下形同无校验）。"""
    peername = request.remote or ""
    try:
        return ipaddress.ip_address(peername).is_loopback
    except ValueError:
        return False


# ── 本机网页会话（HttpOnly cookie）──
# 背景：网页版 JS 从不发 X-Local-Token，若直接删掉 loopback+same-origin 旁路会全 401。
# 方案：页面请求在「对端回环 + 非 LAN 模式 + 非跨站导航」时签发一次性会话票据，
#       存内存（进程重启即失效），HttpOnly + SameSite=Strict，API 同时接受票据或 X-Local-Token。
# 取舍（相对「页面注入 token」）：免改既有 HTML/JS（当前打包树根本没有 web 目录），
#       HttpOnly 让页面脚本读不到票据；代价是必须让页面响应可重新协商 Cookie（页面改为 no-cache）。
_SESSION_COOKIE = "ck_sess"
_SESSION_TTL = 12 * 3600
_SESSIONS = {}
_SESSIONS_MAX = 64


def _lan_mode():
    try:
        return bool(get_server().config.server.lan_mode)
    except Exception:
        return False


def _issue_session():
    now = time.time()
    for tok, exp in list(_SESSIONS.items()):
        if exp <= now:
            _SESSIONS.pop(tok, None)
    while len(_SESSIONS) >= _SESSIONS_MAX:
        _SESSIONS.pop(next(iter(_SESSIONS)))
    tok = secrets.token_urlsafe(24)
    _SESSIONS[tok] = now + _SESSION_TTL
    return tok


def _valid_session(request):
    tok = request.cookies.get(_SESSION_COOKIE) or ""
    if not tok:
        return False
    exp = _SESSIONS.get(tok)
    if not exp:
        return False
    if exp <= time.time():
        _SESSIONS.pop(tok, None)
        return False
    return True


def _page_may_issue_session(request):
    """仅「回环 + 非 LAN + 非跨站 + 真·页面导航」签发网页会话票据。"""
    if _lan_mode() or not _is_loopback_request(request):
        return False
    if request.method != "GET":
        return False
    # 浏览器导航：none / 同源；老浏览器不带该头。跨站/同站（别的本机端口）一律不签。
    if request.headers.get("Sec-Fetch-Site", "") not in ("", "none", "same-origin"):
        return False
    # 裸脚本（curl / urllib）不带 text/html，拿不到票据
    return "text/html" in (request.headers.get("Accept", "") or "")


def _attach_session(response, request):
    if _page_may_issue_session(request):
        response.set_cookie(
            _SESSION_COOKIE, _issue_session(), max_age=_SESSION_TTL,
            httponly=True, samesite="Strict", path="/",
        )
        # 票据要能刷新：页面不能被 immutable 缓存住，否则 Cookie 过期后拿旧页面调 API 会 401
        response.headers["Cache-Control"] = "no-cache"
    return response


# 敏感 GET（POST 全部需要 token）
_TOKEN_GET_PATHS = (
    "/api/config", "/api/sessions", "/api/energy/stats", "/api/automation",
    "/api/network", "/api/service", "/api/status", "/api/export",
    "/api/auto_rules",
)


def _needs_token(request):
    p = request.path
    if request.method == "POST":
        return True
    if request.method == "GET":
        return any(p == t or p.startswith(t + "/") for t in _TOKEN_GET_PATHS)
    return False


def _has_token(request):
    # R15-A02：仅请求头通道；query token 易落入代理/日志，已移除
    supplied = request.headers.get("X-Local-Token", "")
    if supplied and secrets.compare_digest(str(supplied), LOCAL_TOKEN):
        return True
    # LAN 模式：旁路与网页票据全部关闭，必须带 token（对齐 config.py 注释）
    if _lan_mode():
        return False
    # 网页版：只认服务端签发的 HttpOnly 会话票据（不再有 Origin/same-origin 旁路，
    # 因为任何本机进程都能伪造 Origin 头，等于 token 校验后门）
    return _valid_session(request)


@web.middleware
async def local_token_middleware(request, handler):
    if request.path.startswith("/api/") and _needs_token(request) and not _has_token(request):
        return web.json_response(
            {"ok": False, "error": "unauthorized: missing or invalid local token"},
            status=401,
        )
    return await handler(request)


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    origin = request.headers.get("Origin", "")
    s = get_server()
    allowed_origins = {
        f"http://localhost:{s.config.server.port}",
        f"http://127.0.0.1:{s.config.server.port}",
    }
    if origin and origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@web.middleware
async def request_size_limit_middleware(request, handler):
    """Reject requests with body exceeding MAX_REQUEST_BODY_SIZE."""
    if request.method in ("POST", "PUT", "PATCH"):
        content_length = request.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BODY_SIZE:
                    return web.json_response(
                        {"ok": False, "error": "Request body too large"},
                        status=413,
                    )
            except ValueError:
                pass
    return await handler(request)


@web.middleware
async def request_timeout_middleware(request, handler):
    """Apply a per-request timeout (30s for API, 120s for SSE)."""
    if request.path == "/api/events":
        timeout = 120.0
    elif request.path.startswith("/api/"):
        timeout = 30.0
    else:
        # Static files / HTML — no timeout
        return await handler(request)
    try:
        return await asyncio.wait_for(handler(request), timeout=timeout)
    except asyncio.TimeoutError:
        _LOGGER.warning("Request timeout: %s %s", request.method, request.path)
        return web.json_response(
            {"ok": False, "error": "Request timeout"},
            status=504,
        )


@web.middleware
async def gzip_middleware(request, handler):
    response = await handler(request)
    # 已预压缩的静态文件（Content-Encoding 已设置）跳过
    if response.headers.get("Content-Encoding") == "gzip":
        return response
    if not hasattr(response, 'body'):
        return response
    if request.headers.get("Accept-Encoding", "").find("gzip") == -1:
        return response
    if response.content_length is not None and response.content_length < 1024:
        return response
    if response.content_type and "text" not in response.content_type and "json" not in response.content_type:
        return response
    body = response.body
    if isinstance(body, bytes) and len(body) >= 1024:
        compressed = await asyncio.to_thread(gzip.compress, body)
        if len(compressed) < len(body):
            response.body = compressed
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
    return response


@web.middleware
async def cache_middleware(request, handler):
    response = await handler(request)
    # 已由 handle_cached_static / handle_index 设置缓存头的文件跳过
    if response.headers.get("Cache-Control"):
        return response
    if request.path.startswith("/static/"):
        if request.path.endswith((".js", ".css", ".png", ".ico", ".woff", ".woff2")):
            if os.environ.get("CUKTECH_ENV") == "development":
                response.headers["Cache-Control"] = "no-cache"
            else:
                response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response


app = web.Application(middlewares=[
    request_size_limit_middleware,
    local_token_middleware,
    cors_middleware,
    gzip_middleware,
    cache_middleware,
    request_timeout_middleware,
])
app.router.add_get("/", lambda r: get_server().handle_index(r))
app.router.add_get("/phone.html", handle_cached_static)
app.router.add_get("/config.html", handle_cached_static)
app.router.add_get("/static/{tail:.*}", handle_cached_static)
app.router.add_get("/api/health", lambda r: get_server().handle_health(r))
app.router.add_get("/api/status", lambda r: get_server().handle_status(r))
app.router.add_post("/api/set", lambda r: get_server().handle_set(r))
app.router.add_post("/api/port", lambda r: get_server().handle_port(r))
app.router.add_post("/api/enable", lambda r: get_server().handle_enable(r))
app.router.add_post("/api/protocol", lambda r: get_server().handle_protocol(r))
app.router.add_get("/api/log-level", lambda r: get_server().handle_log_level(r))
app.router.add_post("/api/log-level", lambda r: get_server().handle_log_level(r))
app.router.add_get("/api/session-recording", lambda r: get_server().handle_session_recording(r))
app.router.add_post("/api/session-recording", lambda r: get_server().handle_session_recording(r))
app.router.add_get("/api/web-language", lambda r: get_server().handle_web_language(r))
app.router.add_post("/api/web-language", lambda r: get_server().handle_web_language(r))
app.router.add_get("/api/chart", lambda r: get_server().handle_chart(r))
app.router.add_get("/api/live", lambda r: get_server().handle_live(r))
app.router.add_get("/api/statistics/{port}", lambda r: get_server().handle_statistics(r))
app.router.add_get("/api/sessions", lambda r: get_server().handle_sessions(r))
app.router.add_get("/api/sessions/active", lambda r: get_server().handle_sessions_active(r))
app.router.add_get("/api/auto_rules", lambda r: get_server().handle_auto_rules_get(r))
app.router.add_post("/api/auto_rules", lambda r: get_server().handle_auto_rules_set(r))
app.router.add_get("/api/auto_rules/log", lambda r: get_server().handle_auto_rules_log(r))
app.router.add_get("/api/sessions/{id}/points", lambda r: get_server().handle_session_points(r))
app.router.add_get("/api/energy/stats", lambda r: get_server().handle_energy_stats(r))
app.router.add_get("/api/events", lambda r: get_server().handle_sse(r))
app.router.add_get("/api/config", lambda r: get_server().handle_config_get(r))
app.router.add_post("/api/config", lambda r: get_server().handle_config_save(r))
app.router.add_get("/api/mqtt/status", lambda r: get_server().handle_mqtt_status(r))
app.router.add_post("/api/mqtt/test", lambda r: get_server().handle_mqtt_test(r))
app.router.add_get("/api/service", lambda r: get_server().handle_service(r))
app.router.add_post("/api/service", lambda r: get_server().handle_service(r))
app.router.add_get("/api/automation", lambda r: get_server().handle_automation_get(r))
app.router.add_post("/api/automation/notify", lambda r: get_server().handle_automation_notify(r))
app.router.add_post("/api/automation/threshold", lambda r: get_server().handle_automation_threshold(r))
app.router.add_post("/api/automation/schedule", lambda r: get_server().handle_automation_schedule_add(r))
app.router.add_post("/api/automation/schedule/delete", lambda r: get_server().handle_automation_schedule_delete(r))
app.router.add_post("/api/automation/schedule/toggle", lambda r: get_server().handle_automation_schedule_toggle(r))
app.router.add_get("/api/network", lambda r: get_server().handle_network(r))
app.router.add_get("/api/qrcode", lambda r: get_server().handle_qrcode(r))
app.router.add_get("/api/export/sessions", lambda r: get_server().handle_export_sessions(r))
app.router.add_get("/api/export/energy", lambda r: get_server().handle_export_energy(r))
# 通配路由最后注册，避免抢先匹配 /api/export/sessions、/api/export/energy
app.router.add_get("/api/export/{port}", lambda r: get_server().handle_export(r))


async def on_startup(app_):
    _cache_static_files()
    _LOGGER.info("Static files cached: %d files (%.1f KB raw / %.1f KB gzip)",
                 len(_static_cache),
                 sum(len(v["raw"]) for v in _static_cache.values()) / 1024,
                 sum(len(v["gzipped"] or v["raw"]) for v in _static_cache.values()) / 1024)
    s = get_server()
    async with s._start_lock:
        s.loop = asyncio.get_running_loop()
        set_status_cache_invalidator(s.invalidate_status_cache)
        s.ble.set_sse_emitter(s.sse)
        s.ble.set_quality_provider(lambda: {"ble": s.ble.connection_quality()})
        s.history.connect()
        s.ble.set_history(s.history)
        # 加载充电会话记录开关（history.db meta 单源，默认开启，即时切换无需重启）
        s.ble.record_sessions = s.history.get_session_recording()
        app_["ble_task"] = asyncio.create_task(s.ble.start())
        app_["automation_task"] = asyncio.create_task(s._automation_loop())
        app_["mqtt_task"] = asyncio.create_task(s.setup_mqtt())
        app_["auto_off_task"] = asyncio.create_task(s._auto_off_loop())
        # 按配置幂等同步「开机自启」任务计划（后台执行，不阻塞端口监听）
        app_["autostart_task"] = asyncio.create_task(asyncio.to_thread(s._sync_autostart_with_config))


async def on_shutdown(app_):
    _LOGGER.info("Shutting down...")
    s = get_server()
    # Close active sessions first
    s.ble._close_active_sessions()
    # BLE disconnect with timeout
    try:
        await asyncio.wait_for(s.ble.request_stop(), timeout=5.0)
    except asyncio.TimeoutError:
        _LOGGER.warning("BLE stop timed out, forcing disconnect")
        await asyncio.wait_for(s.ble._disconnect(), timeout=3.0)
    except Exception as e:
        _LOGGER.error("BLE stop error: %s", e)
    automation_task = app_.get("automation_task")
    if automation_task:
        automation_task.cancel()
        try:
            await automation_task
        except asyncio.CancelledError:
            pass
    for _tk in ("auto_off_task", "mqtt_task"):
        task = app_.get(_tk)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    # MQTT 优雅断开（如果不是被取消的 connect 任务）
    try:
        if s.mqtt_client is not None:
            s.mqtt_client.loop_stop()
            s.mqtt_client.disconnect()
    except Exception:
        pass
    ble_task = app_.get("ble_task")
    if ble_task:
        ble_task.cancel()
        try:
            await ble_task
        except asyncio.CancelledError:
            pass
    s.history.close()


app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    s = get_server()

    async def _idle_shutdown_monitor():
        # 0 = 常驻；N>0 = 网页(SSE 客户端)全部关闭 N 秒后自动退出服务
        secs = int(getattr(s.config.server, "idle_shutdown_seconds", 0) or 0)
        if secs <= 0:
            while True:
                await asyncio.sleep(3600)
        last_active = time.time()
        _LOGGER.info("Idle shutdown enabled: exit after %ss with no web client", secs)
        while True:
            n = len(s.sse._clients)
            if n == 0:
                if time.time() - last_active >= secs:
                    _LOGGER.info("No web client for %ss, shutting down service", secs)
                    return
            else:
                last_active = time.time()
            await asyncio.sleep(2)

    async def _run():
        runner = web.AppRunner(app)
        await runner.setup()
        _host = s.config.server.host
        _port = s.config.server.port
        # C1：绑定前端口占用预检。真第二实例在 5s 内自行退出（后端兜底，双保险）；
        # 合法重启（os.execv 旧进程释放窗口）等待放行，最多 15s。
        import socket as _socket
        _deadline = time.time() + 15.0
        while True:
            _pre = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            _pre.settimeout(0.5)
            try:
                if _pre.connect_ex((_host, _port)) != 0:
                    break  # 端口空闲，可绑定
            finally:
                _pre.close()
            if time.time() > _deadline:
                _LOGGER.error("Port %s:%s still in use after 15s; "
                              "second instance -> exit(2)", _host, _port)
                sys.exit(2)
            await asyncio.sleep(0.5)
        # 不再复用 TIME_WAIT 端口：杜绝第二实例双监听（reuse_address=True 会掩盖
        # 端口占用，让两个进程同时绑定同一端口）
        site = web.TCPSite(runner, host=_host, port=_port, reuse_address=False)
        await site.start()
        _LOGGER.info("======== Running on http://%s:%s ========", _host, _port)
        await _idle_shutdown_monitor()
        _LOGGER.info("Shutting down web server, cleaning up BLE...")
        await runner.cleanup()

    asyncio.run(_run())
