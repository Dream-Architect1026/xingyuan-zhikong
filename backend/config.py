"""CUKTECH BLE Server - Configuration management.

Supports YAML config file and environment variables.
YAML file takes precedence over environment variables.
"""
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# 无充电自动关端口阈值唯一来源（auto_off.py）；config 缺省值与之一致
try:
    from auto_off import DEFAULT_WINDOW_S
except Exception:  # 单独拷用 config.py 时兜底，数值与 auto_off.DEFAULT_WINDOW_S 相同
    DEFAULT_WINDOW_S = 600

LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# 巴法云四路端口名，与控制台设备卡一一对应（1号口/2号口/3号口/A口）
BEMFA_PORT_NAMES = ("c1", "c2", "c3", "a")
# 巴法云主题只接受字母、数字、下划线（文档「主题名」规则）；本地再卡 48 字符上限
BEMFA_TOPIC_RE = r"^[0-9A-Za-z_]+$"
BEMFA_TOPIC_MAX_LEN = 48


def bemfa_topic_error(name, topic):
    """校验单个巴法云主题名，返回中文错误；空串表示通过。"""
    t = str(topic or "").strip()
    if not t:
        return "%s：主题名为空，去巴法云控制台复制主题名粘贴到这里" % name
    if "/" in t:
        return "%s：斜杠不允许，只能字母数字下划线" % name
    import re
    if not re.match(BEMFA_TOPIC_RE, t):
        return "%s：只能字母数字下划线（不能有空格、连字符、汉字）" % name
    if len(t) > BEMFA_TOPIC_MAX_LEN:
        return "%s：主题名过长（%d > %d）" % (name, len(t), BEMFA_TOPIC_MAX_LEN)
    return ""


def user_config_path() -> Path:
    """config.yaml 的唯一落盘路径（保存/读取/迁移共用）。

    1.5.5：默认放 %LOCALAPPDATA%\\CuktechPanel\\config.yaml，不再写进程序目录——
    便携包换目录或覆盖升级时不会把用户填的 MQTT/蓝牙配置一起换掉。
    首次运行若用户目录没有，则从随包 config.yaml 复制一份（只补缺，不覆盖）。
    CUKTECH_CONFIG_PATH 仍作为显式覆盖（自检/临时实例用）。
    """
    env = os.environ.get("CUKTECH_CONFIG_PATH")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return Path(__file__).parent / "config.yaml"
    _dir = Path(base) / "CuktechPanel"
    try:
        _dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(__file__).parent / "config.yaml"
    target = _dir / "config.yaml"
    if not target.exists():
        for cand in (Path(__file__).parent / "config.yaml", Path.cwd() / "config.yaml"):
            try:
                if cand.exists() and cand.resolve() != target.resolve():
                    import shutil
                    shutil.copyfile(cand, target)
                    _LOGGER.info("config.yaml 迁移到用户目录：%s", target)
                    break
            except OSError:
                continue
    return target


def _load_yaml_config():
    """Load config from YAML file if exists."""
    config_path = user_config_path()
    if not config_path.exists():
        config_path = Path.cwd() / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml
        # Windows 下 open() 默认 GBK，读 UTF-8 的 config.yaml 会抛
        # UnicodeDecodeError（服务启动即崩）。显式用 UTF-8 读取，与系统
        # locale/环境变量无关。
        with open(config_path, encoding="utf-8-sig") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        return {}


@dataclass
class BLEConfig:
    mac: str = ""
    token: str = ""
    ble_key: str = ""
    scan_timeout: int = 10

    def __post_init__(self):
        if not self.mac or self.mac == "XX:XX:XX:XX:XX:XX":
            _LOGGER.warning("CUKTECH_DEVICE_MAC 未配置，BLE 连接不可用。可通过 config.html 配置或设置环境变量")
        if not self.token:
            _LOGGER.warning("CUKTECH_DEVICE_TOKEN 未配置，BLE 连接不可用。可通过 config.html 配置或设置环境变量")


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = field(default_factory=lambda: 18199 if sys.platform == "win32" else 8199)
    lan_mode: bool = False  # 显式开启才允许 0.0.0.0，且强制本机 token 校验
    command_timeout: float = 10.0
    settings_refresh_interval: float = 10.0
    log_level: str = "info"
    history_retention_days: int = 2
    history_db_path: str = "port_history.db"
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 300.0
    idle_shutdown_seconds: int = 0  # 0=常驻; N=网页全部关闭 N 秒后自动退出服务


@dataclass
class ServiceConfig:
    """服务管理：开机自启、每日定时重启。"""
    auto_start: bool = True               # 开机（登录）后自动后台常驻
    daily_restart_enabled: bool = True    # 每日定时重启开关
    daily_restart_time: str = "06:30"     # HH:MM


@dataclass
class ChargeLimitConfig:
    """各端口最大充电时长（分钟），0 表示不限制。"""
    c1: int = 120
    c2: int = 120
    c3: int = 120
    a: int = 120


@dataclass
class ProtectionConfig:
    """智能充电保护：仅 C1/C2 生效。持续低功率自动断电。"""
    enabled: bool = True
    low_power_w: float = 15.0       # 低功率触发阈值（W）
    low_power_minutes: int = 30     # 低功率持续时长（分钟）


@dataclass
class MQTTConfig:
    """MQTT 集成（可选，出厂关闭）。broker 地址由用户填写，本机回环为默认。

    client_id 用于需要固定客户端标识的 broker（巴法云把私钥 UID 当 client_id）。
    """
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    client_id: str = ""
    username: str = ""
    password: str = ""
    topic_prefix: str = "cuktech/charger"
    keepalive: int = 60
    remote_acked_host: str = ""
    # generic：原有 JSON 主题布局（行为不变）；bemfa：巴法云设备协议（纯文本）
    mode: str = "generic"
    # 巴法云只接受 QoS 0/1，QoS2 会被强制下线
    qos: int = 1
    # 巴法云四路主题：控制台里已建好的完整主题名，原样粘贴，不做任何拼接。
    # 控制台的设备卡分别叫 1号口 / 2号口 / 3号口 / A口。
    topic_c1: str = ""   # 1号口
    topic_c2: str = ""   # 2号口
    topic_c3: str = ""   # 3号口
    topic_a: str = ""    # A口

    @property
    def topic_status(self):
        return "%s/status" % self.topic_prefix

    @property
    def topic_ports(self):
        return "%s/port" % self.topic_prefix

    @property
    def topic_settings(self):
        return "%s/settings" % self.topic_prefix

    @property
    def topic_charge(self):
        return "%s/charge_event" % self.topic_prefix

    @property
    def topic_set(self):
        return "%s/set" % self.topic_prefix

    @property
    def topic_set_port(self):
        return "%s/set_port" % self.topic_prefix

    @property
    def topic_set_ble(self):
        return "%s/set_ble" % self.topic_prefix

    def bemfa_topic(self, port_name):
        """巴法云：返回该端口在控制台里建好的完整主题名（原样，不拼接）。"""
        return str(getattr(self, "topic_%s" % port_name, "") or "").strip()

    def bemfa_topics(self):
        """四路主题 {端口名: 主题}；值为空串表示尚未填写。"""
        return {n: self.bemfa_topic(n) for n in BEMFA_PORT_NAMES}

    def bemfa_topic_errors(self):
        """巴法云模式下四路主题的校验错误列表（空列表表示通过）。"""
        if self.mode != "bemfa":
            return []
        return [e for e in (bemfa_topic_error(n, self.bemfa_topic(n))
                            for n in BEMFA_PORT_NAMES) if e]


@dataclass
class AutoOffConfig:
    """后端无充电自动关端口（C51）：进程存活即计时，与界面无关。

    1.5.5：新装默认开启（enabled=True）；关闭方式见 README「无充电自动断电」。
    """
    enabled: bool = True
    window_s: int = DEFAULT_WINDOW_S   # 连续无充电多久后断电（唯一来源见 auto_off.py）
    grace_s: int = 60            # 端口开启后的宽限期（插入设备/握手）
    cur_a: float = 0.05          # 判定"有充电"的电流阈值（A）
    # 豁免名单：这些端口完全不进计时（不累计、不写 START/FIRE、不产生 CUT）。
    # 默认 A 口豁免——A 口常接小电流设备（耳机/手环），10 分钟断电极易误伤。
    exempt: list = field(default_factory=lambda: ["a"])


@dataclass
class Config:
    ble: BLEConfig = field(default_factory=BLEConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    charge_limit: ChargeLimitConfig = field(default_factory=ChargeLimitConfig)
    protection: ProtectionConfig = field(default_factory=ProtectionConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    auto_off: AutoOffConfig = field(default_factory=AutoOffConfig)

def load_config() -> Config:
    """Load config from YAML file, then override with environment variables."""
    ycfg = _load_yaml_config()

    ble_cfg = ycfg.get("ble", {})
    server_cfg = ycfg.get("server", {})

    ble = BLEConfig(
        mac=os.environ.get("CUKTECH_DEVICE_MAC", ble_cfg.get("mac", "")),
        token=os.environ.get("CUKTECH_DEVICE_TOKEN", ble_cfg.get("token", "")),
        ble_key=os.environ.get("CUKTECH_DEVICE_BLE_KEY", ble_cfg.get("ble_key", "")),
        scan_timeout=ble_cfg.get("scan_timeout", 10),
    )

    # 校验 token/ble_key 为合法 hex，畸形则清空并降级（防止 bytes.fromhex 崩溃）
    def _clean_hex(v, name):
        v = str(v or "").strip()
        if not v:
            return ""
        try:
            bytes.fromhex(v)
            return v
        except ValueError:
            _LOGGER.warning("BLE %s 不是合法 hex（%r），已忽略；BLE 功能将不可用", name, v)
            return ""
    ble.token = _clean_hex(ble.token, "token")
    ble.ble_key = _clean_hex(ble.ble_key, "ble_key")

    try:
        history_retention = int(os.environ.get("CUKTECH_HISTORY_RETENTION_DAYS", server_cfg.get("history_retention_days", 2)))
    except (ValueError, TypeError) as e:
        raise ValueError(f"History retention days must be an integer: {e}")
    try:
        reconnect_base_delay = float(server_cfg.get("reconnect_base_delay", 1.0))
    except (ValueError, TypeError) as e:
        raise ValueError(f"Reconnect base delay must be a number: {e}")
    try:
        reconnect_max_delay = float(server_cfg.get("reconnect_max_delay", 300.0))
    except (ValueError, TypeError) as e:
        raise ValueError(f"Reconnect max delay must be a number: {e}")

    _lan_mode = bool(server_cfg.get("lan_mode", False))
    _raw_host = str(server_cfg.get("host", "0.0.0.0"))
    if _lan_mode:
        _host = "0.0.0.0"
    elif _raw_host in ("0.0.0.0", "::", ""):
        _host = "127.0.0.1"  # 非局域网模式强制回环，忽略对外开放的配置
    else:
        _host = _raw_host
    server = ServerConfig(
        host=_host,
        lan_mode=_lan_mode,
        port=int(os.environ.get("CUKTECH_SERVER_PORT", server_cfg.get("port", 18199 if sys.platform == "win32" else 8199))),
        command_timeout=server_cfg.get("command_timeout", 10.0),
        settings_refresh_interval=server_cfg.get("settings_refresh_interval", 60.0),
        log_level=os.environ.get("CUKTECH_LOG_LEVEL", server_cfg.get("log_level", "info")),
        history_retention_days=history_retention,
        history_db_path=os.environ.get("CUKTECH_HISTORY_DB_PATH", server_cfg.get("history_db_path", "port_history.db")),
        reconnect_base_delay=reconnect_base_delay,
        reconnect_max_delay=reconnect_max_delay,
        idle_shutdown_seconds=int(server_cfg.get("idle_shutdown_seconds") or 0),
    )

    # ── 服务管理 / 最大充电时长 / 智能保护（PRD 新增，均带默认值与类型清洗）──
    def _as_bool(v, default=False):
        if isinstance(v, bool):
            return v
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on", "开启")

    def _as_int(v, default, minimum=0, maximum=None):
        try:
            iv = int(float(v))
        except (ValueError, TypeError):
            return default
        if minimum is not None:
            iv = max(minimum, iv)
        if maximum is not None:
            iv = min(maximum, iv)
        return iv

    def _as_float(v, default, minimum=0.0):
        try:
            fv = float(v)
        except (ValueError, TypeError):
            return default
        return max(minimum, fv)

    _PORT_KEYS = ("c1", "c2", "c3", "a")

    def _as_port_names(v):
        """清洗端口名单（auto_off.exempt）：仅保留 c1/c2/c3/a，小写去重；
        未配置时默认 ["a"]；显式配置 [] 表示不豁免任何端口。"""
        if v is None:
            return ["a"]
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            return ["a"]
        out = []
        for x in v:
            k = str(x or "").strip().lower()
            if k in _PORT_KEYS and k not in out:
                out.append(k)
        return out

    svc_cfg = ycfg.get("service", {}) or {}
    import re as _re
    drt = str(svc_cfg.get("daily_restart_time", "06:30") or "06:30").strip()
    if not _re.match(r"^\d{2}:\d{2}$", drt):
        drt = "06:30"
    service = ServiceConfig(
        auto_start=_as_bool(svc_cfg.get("auto_start", True), True),
        daily_restart_enabled=_as_bool(svc_cfg.get("daily_restart_enabled", True), True),
        daily_restart_time=drt,
    )

    limit_cfg = ycfg.get("charge_limit", {}) or {}
    charge_limit = ChargeLimitConfig(
        c1=_as_int(limit_cfg.get("c1", 120), 120),
        c2=_as_int(limit_cfg.get("c2", 120), 120),
        c3=_as_int(limit_cfg.get("c3", 120), 120),
        a=_as_int(limit_cfg.get("a", 120), 120),
    )

    prot_cfg = ycfg.get("protection", {}) or {}
    protection = ProtectionConfig(
        enabled=_as_bool(prot_cfg.get("enabled", True), True),
        low_power_w=_as_float(prot_cfg.get("low_power_w", 15.0), 15.0),
        low_power_minutes=_as_int(prot_cfg.get("low_power_minutes", 30), 30),
    )

    mq_cfg = ycfg.get("mqtt", {}) or {}
    if not isinstance(mq_cfg, dict):
        mq_cfg = {}
    mq_mode = str(mq_cfg.get("mode", "generic") or "generic").strip().lower()
    if mq_mode not in ("generic", "bemfa"):
        _LOGGER.warning("MQTT mode=%r 不支持，按 generic 处理", mq_mode)
        mq_mode = "generic"
    mq_qos = _as_int(mq_cfg.get("qos", 1), 1, 0, 2)
    if mq_qos > 1:
        # 巴法云只接受 QoS 0/1，QoS2 会被强制下线
        _LOGGER.warning("MQTT QoS=%d 不被巴法云支持（会强制下线），已收敛为 1", mq_qos)
        mq_qos = 1
    mqtt = MQTTConfig(
        enabled=_as_bool(os.environ.get("MQTT_ENABLED", mq_cfg.get("enabled", False)), False),
        host=str(os.environ.get("MQTT_HOST", mq_cfg.get("host", "localhost")) or "localhost"),
        port=_as_int(os.environ.get("MQTT_PORT", mq_cfg.get("port", 1883)), 1883, 1, 65535),
        client_id=str(os.environ.get("MQTT_CLIENT_ID", mq_cfg.get("client_id", "")) or ""),
        username=str(os.environ.get("MQTT_USERNAME", mq_cfg.get("username", "")) or ""),
        password=str(os.environ.get("MQTT_PASSWORD", mq_cfg.get("password", "")) or ""),
        topic_prefix=str(mq_cfg.get("topic_prefix", "cuktech/charger") or "cuktech/charger").strip("/"),
        keepalive=_as_int(mq_cfg.get("keepalive", 60), 60, 10, 3600),
        remote_acked_host=str(mq_cfg.get("remote_acked_host", "") or ""),
        mode=mq_mode,
        qos=mq_qos,
        topic_c1=str(mq_cfg.get("topic_c1", "") or "").strip(),
        topic_c2=str(mq_cfg.get("topic_c2", "") or "").strip(),
        topic_c3=str(mq_cfg.get("topic_c3", "") or "").strip(),
        topic_a=str(mq_cfg.get("topic_a", "") or "").strip(),
    )

    ao_cfg = ycfg.get("auto_off", {}) or {}
    if not isinstance(ao_cfg, dict):
        ao_cfg = {}
    auto_off = AutoOffConfig(
        enabled=_as_bool(ao_cfg.get("enabled", True), True),
        window_s=_as_int(ao_cfg.get("window_s", DEFAULT_WINDOW_S), DEFAULT_WINDOW_S, 10, 86400),
        grace_s=_as_int(ao_cfg.get("grace_s", 60), 60, 0, 3600),
        cur_a=_as_float(ao_cfg.get("cur_a", 0.05), 0.05),
        exempt=_as_port_names(ao_cfg.get("exempt", ["a"])),
    )

    return Config(ble=ble, server=server,
                  service=service, charge_limit=charge_limit, protection=protection,
                  mqtt=mqtt, auto_off=auto_off)
