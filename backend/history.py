"""CUKTECH BLE Server - SQLite history storage for port data."""
import asyncio
import csv
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger("cuktech_history")

DEFAULT_RETENTION_DAYS = 2
DEFAULT_DB_PATH = "port_history.db"


def period_cutoff(period: str, now: float):
    """统一 period -> (cutoff, limit_end) 解析。

    返回 (cutoff, limit_end)；limit_end 为 yesterday 区间的上界，其余为 None。
    - 24h：最近 24 小时；all/未知：全量（cutoff=0）
    - today/yesterday/week/month：自然日或滚动窗口
    """
    today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    if period == "today":
        return today_start, None
    if period == "yesterday":
        return today_start - 86400, today_start
    if period == "24h":
        return now - 86400, None
    if period == "week":
        return now - 7 * 86400, None
    if period == "month":
        return now - 30 * 86400, None
    # all 及未知值：全量
    if period not in ("all",):
        _LOGGER.debug("未知 period=%s，按全量处理", period)
    return 0, None


class PortHistory:
    """SQLite-based port history storage."""

    # 批量提交参数：降低高频采样下的写放大（每次 BLE 推送不再单独 COMMIT）
    BATCH_SIZE = 50          # 缓冲行数达到该值即强制提交
    BATCH_INTERVAL = 1.0     # 距上次提交超过该秒数即强制提交

    def __init__(self, db_path: str = DEFAULT_DB_PATH, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.db_path = db_path
        self.retention_days = retention_days
        self._conn: Optional[sqlite3.Connection] = None
        self._db_lock = threading.Lock()  # 保护所有读写操作
        self._last_cleanup = 0
        self._last_wal_checkpoint = 0
        self._pending: list[tuple] = []   # 待批量写入的 port_history 行
        self._last_commit = 0.0
        self.degraded = False              # 内存库回落标记（P1-6：DB 不可用时）

    def connect(self):
        """Open database connection and create tables."""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = self._open(str(self.db_path))
        except sqlite3.Error as e:
            # P1-6：DB 路径不可用/只读/损坏时，整个后端会起不来（面板直接没服务）。
            # 回落内存库：服务照常起，历史写入丢弃并记 ERROR，面板数据回落“未知”。
            _LOGGER.error("历史数据库不可用（%s）：%s；回落内存库，历史不落盘", self.db_path, e)
            self.degraded = True
            self._conn = self._open(":memory:")
        self._create_tables()
        self._reap_orphan_sessions()
        self._cleanup_old_data()
        _LOGGER.info("History database connected: %s", ":memory:" if self.degraded else self.db_path)

    def _open(self, path):
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")  # checkpoint every 1000 pages
        return conn

    def close(self):
        """Close database connection with checkpoint and graceful shutdown."""
        if self._conn:
            with self._db_lock:
                # 落盘缓冲区中的采样，避免关闭时丢失最近 ~1s 的数据
                self._flush_pending()
            try:
                # Try to checkpoint WAL before closing
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            _LOGGER.info("History database closed")

    def vacuum(self):
        """VACUUM 数据库：重建文件，消除删除数据后的碎片化，减小文件体积。
        仅建议在每日重启等低峰期调用，执行期间会锁定数据库。"""
        if not self._conn:
            return
        try:
            with self._db_lock:
                self._flush_pending()
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.execute("VACUUM")
                self._conn.commit()
            _LOGGER.info("Database vacuum completed")
        except Exception as e:
            _LOGGER.warning("Database vacuum failed: %s", e)

    def _create_tables(self):
        """Create database tables and run migrations."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS port_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                port INTEGER NOT NULL,
                voltage REAL,
                current REAL,
                power REAL,
                active INTEGER,
                protocol TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_port_history_port ON port_history(port);
            CREATE INDEX IF NOT EXISTS idx_port_history_timestamp ON port_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_port_history_port_time ON port_history(port, timestamp);

            CREATE TABLE IF NOT EXISTS charge_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                port INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL,
                total_wh REAL DEFAULT 0,
                avg_power_w REAL DEFAULT 0,
                peak_power_w REAL DEFAULT 0,
                avg_voltage REAL DEFAULT 0,
                avg_current REAL DEFAULT 0,
                duration_sec INTEGER DEFAULT 0,
                protocol TEXT DEFAULT '',
                device_key TEXT DEFAULT '',
                device_label TEXT DEFAULT '',
                end_source_kind TEXT DEFAULT '',
                end_source TEXT DEFAULT '',
                end_actor TEXT DEFAULT '',
                end_evidence TEXT DEFAULT '',
                end_attrib TEXT DEFAULT '',
                created_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_charge_sessions_port ON charge_sessions(port);
            CREATE INDEX IF NOT EXISTS idx_charge_sessions_start ON charge_sessions(start_time);

            CREATE TABLE IF NOT EXISTS charge_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                voltage REAL,
                current REAL,
                power REAL,
                protocol TEXT DEFAULT '',
                FOREIGN KEY (session_id) REFERENCES charge_sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_charge_points_session ON charge_points(session_id);
            CREATE INDEX IF NOT EXISTS idx_charge_points_timestamp ON charge_points(timestamp);

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # Migration: add protocol column to charge_points if missing
        try:
            self._conn.execute("SELECT protocol FROM charge_points LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute("ALTER TABLE charge_points ADD COLUMN protocol TEXT DEFAULT ''")
            self._conn.commit()

        # 归因字段迁移：旧库补列，保证查询/写入字段一致（不重写历史值）
        _cols = {r[1] for r in self._conn.execute("PRAGMA table_info(charge_sessions)")}
        for _name, _ddl in (
            ("device_key", "TEXT DEFAULT ''"),
            ("device_label", "TEXT DEFAULT ''"),
            ("end_source_kind", "TEXT DEFAULT ''"),
            ("end_source", "TEXT DEFAULT ''"),
            ("end_actor", "TEXT DEFAULT ''"),
            ("end_evidence", "TEXT DEFAULT ''"),
            ("end_attrib", "TEXT DEFAULT ''"),
        ):
            if _name not in _cols:
                self._conn.execute(
                    "ALTER TABLE charge_sessions ADD COLUMN %s %s" % (_name, _ddl))
        self._conn.commit()

        # C16: v/i 回填迁移——旧会话 avg_voltage/avg_current 为 0 的，用点集均值回填
        # （meta 标记防重复全表扫描；仅当点集确有电压/电流值时才写，避免把真 0 覆盖）
        try:
            if self.get_meta("vi_backfill_v1", "") != "done":
                self._backfill_vi()
                self.set_meta("vi_backfill_v1", "done")
        except Exception as e:
            _LOGGER.error("vi backfill meta check failed: %s", e)

    def _backfill_vi(self):
        """用 charge_points 均值回填旧会话的 avg_voltage / avg_current。"""
        with self._db_lock:
            try:
                rows = self._conn.execute(
                    "SELECT id FROM charge_sessions WHERE avg_voltage=0 AND avg_current=0"
                ).fetchall()
                _filled = 0
                for row in rows:
                    sid = row["id"]
                    agg = self._conn.execute(
                        "SELECT AVG(voltage) v, AVG(current) c FROM charge_points "
                        "WHERE session_id=? AND voltage IS NOT NULL AND current IS NOT NULL",
                        (sid,)).fetchone()
                    if agg and (agg["v"] or agg["c"]):
                        self._conn.execute(
                            "UPDATE charge_sessions SET avg_voltage=?, avg_current=? WHERE id=?",
                            (round(float(agg["v"] or 0), 2),
                             round(float(agg["c"] or 0), 2), sid))
                        _filled += 1
                self._conn.commit()
                if _filled:
                    _LOGGER.info("v/i backfill: %d sessions filled", _filled)
            except Exception as e:
                _LOGGER.error("v/i backfill failed: %s", e)

    # ── 归因（谁发起的断开 / 触发依据）──────────────────────────────

    def set_default_device(self, mac: str, model: str = ""):
        """登记当前配对设备，并给旧会话补设备归属（写入时落库，不在查询时猜）。"""
        self._device_key = str(mac or "")
        self._device_model = str(model or "")
        if not self._conn:
            return
        label = self._device_model or "CUKTECH 充电器"
        with self._db_lock:
            try:
                self._conn.execute(
                    "UPDATE charge_sessions SET device_key=? "
                    "WHERE COALESCE(device_key,'')=''", (self._device_key,))
                # 型号后到时把占位型号升级为真实型号（只动仍为默认值的行）
                self._conn.execute(
                    "UPDATE charge_sessions SET device_label=? "
                    "WHERE COALESCE(device_label,'')='' OR device_label='CUKTECH 充电器'",
                    (label,))
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("set_default_device failed: %s", e)

    @staticmethod
    def _attr_source_files():
        base = Path(os.environ.get("LOCALAPPDATA") or ".") / "CuktechPanel"
        here = Path(__file__).parent
        return {
            "auto": Path(os.environ.get("CUKTECH_AUTOOFF_LOG") or (base / "auto_off.log")),
            "bemfa": Path(os.environ.get("CUKTECH_BEMFA_LOG") or (base / "bemfa.log")),
            "guard": Path(os.environ.get("CUKTECH_GUARD_EVENTS")
                          or (here / "charge_guard.json")),
        }

    @staticmethod
    def _parse_auto_events(path: Path):
        """auto_off.log → [(ts, port, no_charge_s, window_s, thr_a)]（CUT_OK + 最近 FIRE）。"""
        out = []
        if not path.exists():
            return out
        last_fire = {}
        _ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        _fire_re = re.compile(
            r"FIRE port=(\w+) .*no_charge=(\d+)s .*thr=([\d.]+)A window=(\d+)s")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = _ts_re.match(line)
                    ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) if m else 0
                    fm = _fire_re.search(line)
                    if fm:
                        last_fire[fm.group(1)] = (ts, int(fm.group(2)),
                                                  int(fm.group(4)), float(fm.group(3)))
                    if " CUT_OK " in line:
                        pm = re.search(r"CUT_OK port=(\w+)", line)
                        if not pm:
                            continue
                        port = pm.group(1)
                        fire = last_fire.get(port)
                        if fire and abs(ts - fire[0]) <= 20:
                            out.append((ts, port, fire[1], fire[2], fire[3]))
                        else:
                            out.append((ts, port, 0, 0, 0.0))
        except Exception as e:
            _LOGGER.error("parse auto_off log failed: %s", e)
        return out

    @staticmethod
    def _parse_guard_events(path: Path):
        out = []
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                for ev in (data.get("events") or []):
                    out.append((float(ev.get("ts") or 0), str(ev.get("port") or ""),
                                str(ev.get("reason") or ""), int(ev.get("elapsed_sec") or 0),
                                float(ev.get("power_w") or 0)))
        except Exception as e:
            _LOGGER.error("parse charge_guard events failed: %s", e)
        return out

    @staticmethod
    def _parse_bemfa_offs(path: Path):
        """bemfa.log → [(ts, port, raw)]，仅取下行 off 指令（人工 MQTT 入口）。"""
        out = []
        if not path.exists():
            return out
        _line_re = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*event=downlink")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "event=downlink" not in line or "payload=off" not in line:
                        continue
                    m = _line_re.search(line)
                    pm = re.search(r"parsed=(\w+)=(\w+)", line)
                    if not (m and pm and pm.group(1) in ("c1", "c2", "c3", "a")):
                        continue
                    ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                    out.append((ts, pm.group(1), line.strip()[:180]))
        except Exception as e:
            _LOGGER.error("parse bemfa log failed: %s", e)
        return out

    def backfill_attribution(self) -> dict:
        """一次性回溯旧记录：规则日志 → 人工调用 → 功率曲线 → 未识别（写明缺什么）。

        只处理 end_source 为空的历史行；结果带 end_attrib='backfill'/'infer'/'unknown'。
        """
        stats = {"auto_rule": 0, "manual": 0, "device": 0, "unknown": 0}
        if not self._conn:
            return stats
        if self.get_meta("attrib_backfill_v1", "") == "done":
            return stats
        src = self._attr_source_files()
        autos = self._parse_auto_events(src["auto"])
        guards = self._parse_guard_events(src["guard"])
        bemfas = self._parse_bemfa_offs(src["bemfa"])
        pmap = {1: "c1", 2: "c2", 3: "c3", 4: "a"}
        with self._db_lock:
            try:
                rows = self._conn.execute(
                    "SELECT id, port, start_time, end_time, duration_sec, peak_power_w, "
                    "avg_voltage FROM charge_sessions "
                    "WHERE end_time IS NOT NULL AND COALESCE(end_source,'')=''"
                ).fetchall()
                for r in rows:
                    key = pmap.get(r["port"], "")
                    end = float(r["end_time"] or 0)
                    pick = None
                    for ts, port, nc, win, thr in autos:
                        if port == key and abs(end - ts) <= 180:
                            pick = ("auto", "无充电超时",
                                    "", "无充电累计 %ds（阈值 %.2fA，窗口 %ds）；"
                                    "auto_off.log CUT_OK %s"
                                    % (nc, thr, win, time.strftime("%m-%d %H:%M:%S",
                                                                   time.localtime(ts))))
                            break
                    if pick is None:
                        for ts, port, reason, elapsed, power in guards:
                            if port == key and abs(end - ts) <= 180:
                                rule = {"protect": "持续低功率保护",
                                        "limit": "充电时长上限"}.get(reason, reason or "充电守护")
                                pick = ("auto", rule, "",
                                        "功率 %.1fW 持续 %ds 触发充电守护；"
                                        "charge_guard.json %s"
                                        % (power, elapsed,
                                           time.strftime("%m-%d %H:%M:%S",
                                                         time.localtime(ts))))
                                break
                    if pick is None:
                        for ts, port, raw in bemfas:
                            if port == key and abs(end - ts) <= 180:
                                pick = ("manual", "MQTT 指令", "bemfa",
                                        "巴法云下行 off；bemfa.log %s"
                                        % time.strftime("%m-%d %H:%M:%S",
                                                        time.localtime(ts)))
                                break
                    if pick is None:
                        pick = self._infer_by_curve(r["id"], key, r["duration_sec"] or 0)
                    kind, source, actor, evidence = pick
                    attrib = "backfill" if kind in ("auto", "manual") else (
                        "infer" if kind == "device" else "unknown")
                    self._conn.execute(
                        "UPDATE charge_sessions SET end_source_kind=?, end_source=?, "
                        "end_actor=?, end_evidence=?, end_attrib=? WHERE id=?",
                        (kind, source, actor, evidence, attrib, r["id"]))
                    stats["auto_rule" if kind == "auto" else
                          "manual" if kind == "manual" else
                          "device" if kind == "device" else "unknown"] += 1
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("attribution backfill failed: %s", e)
        self.set_meta("attrib_backfill_v1", "done")
        _LOGGER.info("attribution backfill: %s", stats)
        return stats

    def _infer_by_curve(self, sid: int, key: str, dur: int):
        """按末端功率曲线回溯：无充电超时 → 设备充满/停止 → 未识别（写明缺口）。"""
        pts = self._conn.execute(
            "SELECT timestamp, power, current, voltage FROM charge_points "
            "WHERE session_id=? ORDER BY timestamp", (sid,)).fetchall()
        if not pts:
            return ("unknown", "未识别", "",
                    "缺末端采样点（charge_points 无该会话记录）、"
                    "缺自动化规则事件（auto_off/充电守护无匹配时间点）、"
                    "缺人工调用记录（旧版本未记录调用方）；仅有 duration=%ds" % dur)
        last = pts[-1]
        hi = None
        for i in range(len(pts) - 1, -1, -1):
            if (pts[i]["power"] or 0) > 1.0:
                hi = i
                break
        zero_span = (last["timestamp"] - pts[hi]["timestamp"]) if hi is not None else (
            last["timestamp"] - pts[0]["timestamp"])
        tail_power = (pts[hi]["power"] or 0) if hi is not None else 0.0
        if zero_span >= 300 and dur >= 600:
            return ("auto", "无充电超时（曲线回溯）", "",
                    "末端连续 %ds 功率≈0（<1W），会话时长 %d 分钟，"
                    "与无充电超时规则窗口一致；缺当日规则日志/端口关闭事件" % (zero_span, dur // 60))
        if zero_span >= 120:
            return ("device", "设备充满（涓流结束）", "",
                    "末端连续 %ds 功率≈0（<1W），峰值 %.1fW；"
                    "端口关闭发起方未记录" % (zero_span, max((p["power"] or 0) for p in pts)))
        if tail_power <= 5:
            return ("device", "设备侧停止充电", "",
                    "末次采样 %.1fW（≤5W），会话在 %.0fs 内结束；"
                    "缺端口关闭事件与调用方记录" % (tail_power, dur))
        return ("unknown", "未识别", "",
                "末次采样仍有 %.1fW（端口状态/调用方均未记录），"
                "缺 end_source 字段、缺端口关闭事件、缺人工调用审计；"
                "无法区分人工断电、设备拔出或链路丢失" % tail_power)

    # ── Runtime meta storage (single source for runtime toggles) ──

    def get_meta(self, key: str, default: str = "") -> str:
        """Read a runtime meta value from the DB. Returns default if absent/failed."""
        if not self._conn:
            return default
        with self._db_lock:
            try:
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
                return row["value"] if row else default
            except Exception as e:
                _LOGGER.error("Failed to read meta %s: %s", key, e)
                return default

    def set_meta(self, key: str, value: str):
        """Persist a runtime meta value (upsert)."""
        if not self._conn:
            return
        with self._db_lock:
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (key, value))
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to write meta %s: %s", key, e)

    def get_session_recording(self) -> bool:
        """Whether charge session recording is enabled (DB meta, default True)."""
        value = self.get_meta("session_recording", "true").strip().lower()
        return value not in ("0", "false", "no", "off")

    def set_session_recording(self, enabled: bool) -> None:
        """Persist the charge session recording toggle (DB meta, survives restart)."""
        self.set_meta("session_recording", "true" if enabled else "false")

    def get_web_language(self) -> str:
        """Web UI language preference (DB meta, default 'auto' = follow system).

        Returns 'auto' (follow system), 'zh-CN' or 'en'.
        """
        value = self.get_meta("web_language", "auto").strip().lower()
        if value in ("zh", "zh-cn", "zh-hans"):
            return "zh-CN"
        if value.startswith("en"):
            return "en"
        return "auto"

    def set_web_language(self, lang: str) -> None:
        """Persist the Web UI language preference (DB meta, survives restart)."""
        self.set_meta("web_language", lang)

    def _checkpoint_wal(self):
        """Run WAL checkpoint if enough pages have accumulated."""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    def _reap_orphan_sessions(self):
        """启动时清理崩溃遗留的未结束会话（end_time IS NULL）及其采样点。

        进程崩溃（未走 on_shutdown 的 _close_active_sessions）会留下
        end_time IS NULL / total_wh=0 的会话行；重启后内存会话状态已丢失，
        这类行不可能再被 end_session 更新，属于永久孤儿数据——get_sessions
        按 total_wh>0 过滤使它们永远不可见、也永远不被清理。
        仅在启动时执行（运行时正常进行中的会话 end_time IS NULL，不可删）。
        """
        if not self._conn:
            return
        try:
            self._conn.execute(
                """DELETE FROM charge_points WHERE session_id IN
                   (SELECT id FROM charge_sessions WHERE end_time IS NULL)""")
            removed = self._conn.execute(
                "DELETE FROM charge_sessions WHERE end_time IS NULL").rowcount
            self._conn.commit()
            if removed:
                _LOGGER.info("Reaped %d orphan charge session(s) from unclean shutdown", removed)
        except Exception as e:
            _LOGGER.error("Failed to reap orphan charge sessions: %s", e)

    def _cleanup_old_data(self):
        """Remove data older than retention period (port samples + closed sessions)."""
        cutoff = time.time() - (self.retention_days * 86400)
        self._conn.execute("DELETE FROM port_history WHERE timestamp < ?", (cutoff,))
        # 清理过期闭环会话及其采样点（charge_sessions 行此前从不删除，会无限累积）
        self._conn.execute(
            """DELETE FROM charge_points WHERE session_id IN
               (SELECT id FROM charge_sessions WHERE end_time IS NOT NULL AND end_time < ?)""",
            (cutoff,))
        self._conn.execute(
            """DELETE FROM charge_sessions WHERE end_time IS NOT NULL AND end_time < ?""",
            (cutoff,))
        self._conn.commit()

    def flush(self):
        """强制将缓冲区中的采样批量落盘提交。线程安全。"""
        if not self._conn:
            return
        with self._db_lock:
            self._flush_pending()

    def _flush_pending(self):
        """将缓冲行批量 INSERT 并提交。调用方必须已持有 _db_lock。"""
        if not self._pending:
            return
        try:
            self._conn.executemany(
                """INSERT INTO port_history (timestamp, port, voltage, current, power, active, protocol)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                self._pending,
            )
            self._pending.clear()
            self._conn.commit()
            self._last_commit = time.time()
            # 周期性清理/checkpoint 仍挂在提交路径上，但只按时间间隔触发（每 1h / 5min）
            if self._last_commit - self._last_cleanup > 3600:
                self._cleanup_old_data()
                self._last_cleanup = self._last_commit
            if self._last_commit - self._last_wal_checkpoint > 300:
                self._checkpoint_wal()
                self._last_wal_checkpoint = self._last_commit
        except Exception as e:
            _LOGGER.error("Failed to record port data: %s", e)
            self._pending.clear()
            try:
                self._conn.rollback()
            except Exception:
                pass

    def record_port_data(self, port: int, data: dict):
        """Record port data to database (synchronous, called from async via executor).

        批量缓冲：攒到 BATCH_SIZE 或距上次提交超过 BATCH_INTERVAL 再批量写入，
        大幅降低高频采样下的 COMMIT 次数与写放大。读取路径会自动 flush 未落盘
        的缓冲行，保证"写入后立即读取可见"。
        """
        if not self._conn:
            return
        row = (
            time.time(),
            port,
            data.get("voltage"),
            data.get("current"),
            data.get("power"),
            1 if data.get("active") else 0,
            data.get("protocol"),
        )
        with self._db_lock:
            self._pending.append(row)
            now = time.time()
            if (len(self._pending) >= self.BATCH_SIZE
                    or now - self._last_commit >= self.BATCH_INTERVAL):
                self._flush_pending()

    def query_history(
        self,
        port: int,
        hours: int = 24,
        interval: Optional[int] = None
    ) -> list[dict]:
        """Query port history with optional downsampling.

        Args:
            port: Port number (1-4)
            hours: Number of hours to query
            interval: Aggregation interval in seconds (None = raw data)
        """
        if not self._conn:
            return []

        # 落盘缓冲采样，保证"写入后立即读取"的一致性（批量提交引入 ≤1s 缓冲）
        self.flush()

        cutoff = time.time() - (hours * 3600)

        if interval:
            rows = self._conn.execute(
                """SELECT
                    (CAST(timestamp / ? AS INTEGER) * ?) as bucket,
                    AVG(voltage) as voltage,
                    AVG(current) as current,
                    AVG(power) as power,
                    MAX(active) as active,
                    COUNT(*) as samples
                FROM port_history
                WHERE port = ? AND timestamp >= ?
                GROUP BY bucket
                ORDER BY bucket""",
                (interval, interval, port, cutoff)
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT timestamp, voltage, current, power, active, protocol
                FROM port_history
                WHERE port = ? AND timestamp >= ?
                ORDER BY timestamp""",
                (port, cutoff)
            ).fetchall()

        return [dict(row) for row in rows]

    def get_statistics(self, port: int, hours: int = 24) -> dict:
        """Get statistical summary for a port."""
        if not self._conn:
            return {}

        self.flush()

        cutoff = time.time() - (hours * 3600)
        row = self._conn.execute(
            """SELECT
                COUNT(*) as samples,
                MIN(timestamp) as first_seen,
                MAX(timestamp) as last_seen,
                AVG(voltage) as avg_voltage,
                MAX(voltage) as max_voltage,
                MIN(voltage) as min_voltage,
                AVG(current) as avg_current,
                MAX(current) as max_current,
                AVG(power) as avg_power,
                MAX(power) as max_power,
                SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) as active_count,
                COALESCE(
                    (SELECT SUM(p.power * (p.timestamp - p.prev_ts)) / 3600.0
                     FROM (
                         SELECT timestamp, power,
                                LAG(timestamp) OVER (ORDER BY timestamp) as prev_ts
                         FROM port_history
                         WHERE port = ? AND timestamp >= ? AND active = 1
                     ) p
                     WHERE p.prev_ts IS NOT NULL),
                0) as energy_wh
            FROM port_history
            WHERE port = ? AND timestamp >= ?""",
            (port, cutoff, port, cutoff)
        ).fetchone()

        if not row or row["samples"] == 0:
            return {"port": port, "hours": hours, "samples": 0}

        return {
            "port": port,
            "hours": hours,
            "samples": row["samples"],
            "first_seen": datetime.fromtimestamp(row["first_seen"]).isoformat() if row["first_seen"] else None,
            "last_seen": datetime.fromtimestamp(row["last_seen"]).isoformat() if row["last_seen"] else None,
            "voltage": {
                "avg": round(row["avg_voltage"], 2) if row["avg_voltage"] else None,
                "min": round(row["min_voltage"], 2) if row["min_voltage"] else None,
                "max": round(row["max_voltage"], 2) if row["max_voltage"] else None,
            },
            "current": {
                "avg": round(row["avg_current"], 2) if row["avg_current"] else None,
                "max": round(row["max_current"], 2) if row["max_current"] else None,
            },
            "power": {
                "avg": round(row["avg_power"], 2) if row["avg_power"] else None,
                "max": round(row["max_power"], 2) if row["max_power"] else None,
                "total_wh": round(row["energy_wh"], 2) if row["energy_wh"] is not None else 0,
            },
            "active_ratio": round(row["active_count"] / row["samples"], 2) if row["samples"] > 0 else 0,
        }

    def export_csv(self, port: int, hours: int = 24) -> str:
        """Export port history as CSV string."""
        if not self._conn:
            return ""

        self.flush()

        cutoff = time.time() - (hours * 3600)
        rows = self._conn.execute(
            """SELECT timestamp, voltage, current, power, active, protocol
            FROM port_history
            WHERE port = ? AND timestamp >= ?
            ORDER BY timestamp""",
            (port, cutoff)
        ).fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["timestamp", "datetime", "voltage", "current", "power", "active", "protocol"])

        for row in rows:
            writer.writerow([
                row["timestamp"],
                datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
                row["voltage"],
                row["current"],
                row["power"],
                "yes" if row["active"] else "no",
                row["protocol"],
            ])

        return output.getvalue()

    def query_history_multi(self, start_port: int, end_port: int, hours: float, interval: int) -> list[dict]:
        """Query history for multiple ports in a single query."""
        if not self._conn:
            return []

        self.flush()

        cutoff = time.time() - (hours * 3600)
        rows = self._conn.execute(
            """SELECT port,
                (CAST(timestamp / ? AS INTEGER) * ?) as bucket,
                AVG(voltage) as voltage,
                AVG(current) as current,
                AVG(power) as power,
                COUNT(*) as samples
            FROM port_history
            WHERE port >= ? AND port <= ? AND timestamp >= ?
            GROUP BY port, bucket
            ORDER BY port, bucket""",
            (interval, interval, start_port, end_port, cutoff)
        ).fetchall()

        return [dict(row) for row in rows]

    # ── Charge Session Management ──

    def start_session(self, port: int, protocol: str = "",
                      device_key: str = "", device_label: str = "") -> int:
        """Start a new charge session, return session_id（同时落库设备归属）。"""
        if not self._conn:
            return 0
        with self._db_lock:
            try:
                cursor = self._conn.execute(
                    """INSERT INTO charge_sessions
                       (port, start_time, protocol, device_key, device_label)
                       VALUES (?, ?, ?, ?, ?)""",
                    (port, time.time(), protocol, device_key, device_label),
                )
                self._conn.commit()
                return cursor.lastrowid
            except Exception as e:
                _LOGGER.error("Failed to start session: %s", e)
                return 0

    def record_charge_point(self, session_id: int, voltage: float,
                            current: float, power: float, protocol: str = ""):
        """Record a single data point for a charge session."""
        if not self._conn or not session_id:
            return
        with self._db_lock:
            try:
                self._conn.execute(
                    """INSERT INTO charge_points (session_id, timestamp, voltage, current, power, protocol)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (session_id, time.time(), voltage, current, power, protocol),
                )
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to record charge point: %s", e)

    def end_session(self, session_id: int, total_wh: float, peak_power_w: float,
                    avg_voltage: float, avg_current: float, duration_sec: int,
                    end_kind: str = "", end_source: str = "", end_actor: str = "",
                    end_evidence: str = "", end_attrib: str = "live"):
        """End a charge session with final stats."""
        if not self._conn or not session_id:
            return
        avg_power = total_wh / (duration_sec / 3600.0) if duration_sec > 0 else 0
        # C8d（CUK-G4）：从 charge_points 按 sid 回填平均电压/电流，
        # 避免检测器关闭路径传 0、正常路径传末次值导致的口径错误。
        try:
            row = self._conn.execute(
                """SELECT AVG(voltage), AVG(current) FROM charge_points
                   WHERE session_id = ? AND voltage > 0 AND current > 0""",
                (session_id,)).fetchone()
            if row and row[0] and row[0] > 0:
                avg_voltage = round(float(row[0]), 2)
            if row and row[1] and row[1] > 0:
                avg_current = round(float(row[1]), 2)
        except Exception as e:
            _LOGGER.debug("end_session avg v/i backfill failed: %s", e)
        with self._db_lock:
            try:
                self._conn.execute(
                    """UPDATE charge_sessions SET
                       end_time = ?, total_wh = ?, avg_power_w = ?,
                       peak_power_w = ?, avg_voltage = ?, avg_current = ?,
                       duration_sec = ?, end_source_kind = ?, end_source = ?,
                       end_actor = ?, end_evidence = ?, end_attrib = ?
                       WHERE id = ?""",
                    (time.time(), round(total_wh, 4), round(avg_power, 2),
                     round(peak_power_w, 2), round(avg_voltage, 2),
                     round(avg_current, 2), duration_sec,
                     end_kind or "unknown", end_source or "未识别",
                     end_actor or "", end_evidence or "",
                     end_attrib or "live", session_id),
                )
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to end session: %s", e)

    def delete_session(self, session_id: int):
        """Delete a session and its points (for 0Wh sessions)."""
        if not self._conn or not session_id:
            return
        with self._db_lock:
            try:
                self._conn.execute("DELETE FROM charge_points WHERE session_id = ?", (session_id,))
                self._conn.execute("DELETE FROM charge_sessions WHERE id = ?", (session_id,))
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to delete session: %s", e)

    def get_sessions(self, port: Optional[int] = None, period: str = "today",
                     limit: int = 10, offset: int = 0) -> tuple:
        """Query charge sessions."""
        if not self._conn:
            return [], 0

        now = time.time()
        cutoff, limit_end = period_cutoff(period, now)

        query = """SELECT id, port, start_time, end_time, total_wh, avg_power_w,
                   peak_power_w, avg_voltage, avg_current, duration_sec, protocol,
                   device_key, device_label, end_source_kind, end_source, end_actor,
                   end_evidence, end_attrib,
                   COUNT(*) OVER() AS total
                   FROM charge_sessions WHERE start_time >= ? AND total_wh > 0"""
        params = [cutoff]

        if port is not None:
            query += " AND port = ?"
            params.append(port)

        if period == "yesterday" and limit_end is not None:
            query += " AND start_time < ?"
            params.append(limit_end)

        query += " ORDER BY end_time IS NULL DESC, start_time DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._conn.execute(query, params).fetchall()
        total = rows[0]["total"] if rows else 0

        return [dict(row) for row in rows], total

    def get_active_sessions(self) -> list[dict]:
        """当前未结束的会话（含设备归属）：端口卡「本次充电设备名」用。

        1.5.5 新增；只读 charge_sessions，不参与任何写入路径。
        """
        if not self._conn:
            return []
        with self._db_lock:
            rows = self._conn.execute(
                """SELECT id, port, start_time, device_key, device_label, protocol
                   FROM charge_sessions WHERE end_time IS NULL
                   ORDER BY start_time DESC""").fetchall()
        return [dict(r) for r in rows]

    def get_device_profile(self, device_key: str) -> dict:
        """某设备（充电器箱）按端口的历史汇总：画像与自动规则共用同一口径。"""
        if not self._conn or not device_key:
            return {"sessions": 0, "ports": {}}
        with self._db_lock:
            rows = self._conn.execute(
                """SELECT port, COUNT(*) n, AVG(avg_power_w) avg_w,
                          MAX(peak_power_w) peak_w, SUM(duration_sec) dur,
                          SUM(total_wh) wh
                   FROM charge_sessions
                   WHERE device_key=? AND total_wh>0
                   GROUP BY port ORDER BY n DESC""", (device_key,)).fetchall()
        ports = {}
        total = 0
        for r in rows:
            n = int(r["n"] or 0)
            total += n
            ports[str(r["port"])] = {
                "n": n, "avg_w": round(float(r["avg_w"] or 0), 1),
                "peak_w": round(float(r["peak_w"] or 0), 1),
                "dur_sec": int(r["dur"] or 0), "wh": round(float(r["wh"] or 0), 2),
            }
        return {"sessions": total, "ports": ports}

    def get_session_points(self, session_id: int) -> list[dict]:
        """Get all data points for a charge session."""
        if not self._conn:
            return []
        rows = self._conn.execute(
            """SELECT timestamp, voltage, current, power, protocol
               FROM charge_points WHERE session_id = ?
               ORDER BY timestamp""",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_energy_stats(self, period: str = "today") -> dict:
        """Get aggregated energy statistics."""
        if not self._conn:
            return {"period": period, "total_wh": 0, "session_count": 0}

        now = time.time()
        cutoff, limit_end = period_cutoff(period, now)

        params1 = [cutoff]
        params2 = [cutoff]
        extra = ""
        if period == "yesterday" and limit_end is not None:
            extra = " AND start_time < ?"
            params1.append(limit_end)
            params2.append(limit_end)

        row = self._conn.execute(
            f"""SELECT
                COUNT(*) as session_count,
                COALESCE(SUM(total_wh), 0) as total_wh,
                COALESCE(MAX(peak_power_w), 0) as peak_power_w,
                COALESCE(SUM(duration_sec), 0) as total_duration_sec
            FROM charge_sessions WHERE start_time >= ?{extra} AND total_wh > 0""",
            params1,
        ).fetchone()

        by_port = self._conn.execute(
            f"""SELECT port, COALESCE(SUM(total_wh), 0) as wh, COUNT(*) as count
               FROM charge_sessions WHERE start_time >= ?{extra} AND total_wh > 0
               GROUP BY port""",
            params2,
        ).fetchall()

        # Calculate avg power from total energy and duration (more accurate than DB avg)
        total_dur = row["total_duration_sec"] or 0
        total_wh = row["total_wh"] or 0
        avg_power = round(total_wh / (total_dur / 3600), 1) if total_dur > 0 else 0

        return {
            "period": period,
            "total_wh": round(total_wh, 2),
            "session_count": row["session_count"],
            "avg_power_w": avg_power,
            "peak_power_w": round(row["peak_power_w"], 1),
            "total_duration_sec": total_dur,
            "by_port": {str(r["port"]): {"wh": round(r["wh"], 2), "count": r["count"]}
                        for r in by_port},
        }

    def _period_cutoff(self, period):
        cutoff, _ = period_cutoff(period, time.time())
        return cutoff

    def export_sessions_csv(self, port=None, period='all'):
        """导出充电会话为 CSV"""
        if not self._conn:
            return 'start_time,end_time,port,total_wh,avg_power_w,peak_power_w,avg_voltage,avg_current,duration_sec,protocol\n'
        cutoff, limit_end = period_cutoff(period, time.time())
        params = [cutoff]
        extra = ''
        if period == 'yesterday' and limit_end is not None:
            extra = ' AND start_time < ?'
            params.append(limit_end)
        if port is not None:
            extra += ' AND port = ?'
            params.append(int(port))
        rows = self._conn.execute(
            f'''SELECT id, port, start_time, end_time, total_wh, avg_power_w,
                       peak_power_w, avg_voltage, avg_current, duration_sec, protocol
                FROM charge_sessions WHERE start_time >= ?{extra} AND total_wh > 0
                ORDER BY start_time''', params).fetchall()
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['id', 'port', 'start_time', 'end_time', 'total_wh', 'avg_power_w',
                    'peak_power_w', 'avg_voltage', 'avg_current', 'duration_sec', 'protocol'])
        for r in rows:
            w.writerow([r['id'], r['port'], r['start_time'], r['end_time'], r['total_wh'],
                        r['avg_power_w'], r['peak_power_w'], r['avg_voltage'], r['avg_current'],
                        r['duration_sec'], r['protocol']])
        return buf.getvalue()

    def export_daily_csv(self, period='all'):
        """按天/端口汇总导出日报（1.5.5：中文表头 + 单位写进表头）。"""
        if not self._conn:
            return '日期,端口,会话数,充入 Wh,峰功率 W,总时长 秒\n'
        cutoff = self._period_cutoff(period)
        rows = self._conn.execute(
            '''SELECT date(start_time,'unixepoch','localtime') AS day, port,
                      COUNT(*) AS session_count, COALESCE(SUM(total_wh),0) AS total_wh,
                      COALESCE(MAX(peak_power_w),0) AS peak_power_w,
                      COALESCE(SUM(duration_sec),0) AS duration_sec
               FROM charge_sessions WHERE start_time >= ? AND total_wh > 0
               GROUP BY day, port ORDER BY day''', (cutoff,)).fetchall()
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator='\r\n')
        w.writerow(['日期', '端口', '会话数', '充入 Wh', '峰功率 W', '总时长 秒'])
        for r in rows:
            _pv = int(r['port'] or 0)
            w.writerow([r['day'], ('C%d' % _pv) if _pv in (1, 2, 3) else 'A',
                        r['session_count'], round(r['total_wh'], 2),
                        round(r['peak_power_w'], 1), r['duration_sec']])
        return buf.getvalue()
