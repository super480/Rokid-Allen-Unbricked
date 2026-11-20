"""Read a P1S status snapshot over the local, authenticated MQTT connection."""
from __future__ import annotations

import ipaddress
import json
import math
import re
import os
import ssl
import threading
import time
from pathlib import Path

from .models import QueryResult, now_iso, validate_card

APP_ID = "bambu_print"
TITLE = "打印进度"
SOURCE = "bambu_lan_mqtt"
OPERATION = "print_status"
CARD_TTL_MS = 50_000
LAN_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in
                     ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
STATES = {
    "IDLE": "空闲", "RUNNING": "打印中", "PAUSE": "已暂停",
    "FINISH": "已完成", "FAILED": "打印失败", "PREPARE": "准备中",
    "INIT": "准备中", "SLICING": "切片中",
}
TASK_KEYS = ("subtask_id", "subtask_name", "gcode_file")
TASK_VALUES = ("mc_percent", "mc_remaining_time", "layer_num", "total_layer_num")
FIELDS = (*TASK_KEYS, *TASK_VALUES, "gcode_state", "nozzle_temper", "bed_temper")


class BambuError(Exception):
    pass


def _credentials(values, old):
    fields = {}
    for key in ("host", "serial", "access_code"):
        default = "" if key == "access_code" else old.get(key, "")
        value = values.get(key, default)
        if not isinstance(value, str):
            raise ValueError("连接信息必须是文本")
        fields[key] = value.strip()
    try:
        ip = ipaddress.IPv4Address(fields["host"])
        allowed = any(ip in network for network in LAN_NETWORKS)
    except ValueError:
        allowed = False
    if not allowed:
        raise ValueError("请输入打印机的局域网 IPv4 地址")
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", fields["serial"]):
        raise ValueError("请输入打印机序列号，仅支持字母和数字")
    if (not fields["access_code"] and fields["host"] == old.get("host")
            and fields["serial"] == old.get("serial")):
        fields["access_code"] = old.get("access_code", "")
    if not re.fullmatch(r"[A-Za-z0-9]{8}", fields["access_code"]):
        raise ValueError("请输入打印机屏幕上的 8 位 LAN 访问码")
    return fields


def _number(value, upper):
    if type(value) not in (int, float, str):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= upper else None


def _merge(current, report):
    fields = {key: report[key] for key in FIELDS if key in report}
    new_task = any(fields.get(key) and current.get(key) and
                   fields[key] != current[key] for key in TASK_KEYS)
    new_run = (fields.get("gcode_state") in ("PREPARE", "RUNNING")
               and current.get("gcode_state") in ("IDLE", "FINISH", "FAILED"))
    if new_task or new_run or fields.get("gcode_state") == "IDLE":
        for key in (*TASK_KEYS, *TASK_VALUES):
            current.pop(key, None)
    current.update(fields)


def _tls_context():
    # Trust the Bambu LAN CA only. Device certificates do not consistently
    # include the printer's IP as a SAN.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str((Path(os.environ["ROKID_RESOURCES"]) / "app/host/miniapps/certs/bambu-lan.cert" if "ROKID_RESOURCES" in os.environ else Path(__file__).with_name("certs") / "bambu-lan.cert")))
    ctx.check_hostname = False
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def read_snapshot(credentials):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        raise BambuError("主机缺少 MQTT 依赖，请安装 requirements-bambu.txt") from None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         reconnect_on_failure=False)
    client.connect_timeout = 3
    client.username_pw_set("bblp", credentials["access_code"])
    client.tls_set_context(_tls_context())
    client.tls_insecure_set(True)  # Skip IP hostname matching, keep CA verification.
    topic = "device/" + credentials["serial"]
    snapshot = {}
    problem = ""
    authenticated = False
    snapshot_requested = False

    def connected(client, userdata, flags, reason_code, properties):
        nonlocal authenticated, problem
        if reason_code.is_failure:
            problem = "MQTT 认证失败，请核对 LAN 访问码和打印机网络设置"
            return
        authenticated = True
        code, _ = client.subscribe(topic + "/report")
        if code != mqtt.MQTT_ERR_SUCCESS:
            problem = "无法订阅打印机状态"

    def subscribed(client, userdata, mid, reasons, properties):
        nonlocal problem, snapshot_requested
        if any(reason.is_failure for reason in reasons):
            problem = "打印机拒绝状态订阅，请检查序列号和网络设置"
            return
        snapshot_requested = True
        client.publish(topic + "/request",
                       '{"pushing":{"sequence_id":"0","command":"pushall"}}')

    def message(client, userdata, msg):
        if not snapshot_requested or msg.topic != topic + "/report" or msg.retain:
            return
        try:
            report = json.loads(msg.payload).get("print")
        except (ValueError, AttributeError):
            return
        if (isinstance(report, dict)
                and report.get("command") == "push_status"):
            _merge(snapshot, report)

    client.on_connect = connected
    client.on_subscribe = subscribed
    client.on_message = message
    try:
        client.connect(credentials["host"], 8883, keepalive=10)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not problem:
            if client.loop(timeout=0.25) != mqtt.MQTT_ERR_SUCCESS:
                break
            state = snapshot.get("gcode_state")
            if state == "IDLE" and all(key in snapshot for key in ("nozzle_temper", "bed_temper")):
                break
            if isinstance(state, str) and state != "IDLE" and all(
                key in snapshot for key in (*TASK_VALUES, "nozzle_temper", "bed_temper")
            ) and (snapshot.get("subtask_name") or snapshot.get("gcode_file")):
                break
    except ssl.SSLError:
        raise BambuError("打印机 TLS 证书校验失败") from None
    except OSError:
        raise BambuError("无法连接打印机，请检查 IP、网络和打印机电源") from None
    finally:
        client.disconnect()

    if problem:
        raise BambuError(problem)
    if isinstance(snapshot.get("gcode_state"), str) and snapshot["gcode_state"]:
        return snapshot
    if authenticated:
        raise BambuError("认证通过，未收到状态，请核对序列号和打印机网络模式")
    raise BambuError("未能建立 MQTT 连接，请检查打印机网络设置")


class BambuPrinter:
    def __init__(self, engine):
        self.engine = engine
        self._lock = threading.Lock()
        self._last_read_at = 0.0
        self._last_snapshot = None

    def settings(self):
        cfg = self.engine.config.get(APP_ID) or {}
        return {key: cfg.get(key, "") for key in ("host", "serial")} | {
            "has_access_code": bool(cfg.get("access_code")),
        }

    def save(self, values):
        with self._lock:
            config = self.engine.config
            previous = config.get(APP_ID, {})
            config[APP_ID] = _credentials(values, previous)
            try:
                self.engine._save_config(strict=True)
            except OSError:
                config[APP_ID] = previous
                raise BambuError("保存打印机设置失败") from None
            self._last_read_at = 0.0
            self._last_snapshot = None
            return self.settings()

    def read(self):
        if not self._lock.acquire(blocking=False):
            raise BambuError("正在读取打印机，请稍后重试")
        try:
            try:
                credentials = _credentials({}, self.engine.config.get(APP_ID) or {})
            except ValueError:
                raise BambuError("请在应用商店设置打印机连接") from None
            if self._last_snapshot is not None and time.monotonic() - self._last_read_at < 10:
                return self._last_snapshot
            snapshot = read_snapshot(credentials)
            self._last_snapshot = snapshot
            self._last_read_at = time.monotonic()
            return snapshot
        finally:
            self._lock.release()

    def test_connection(self):
        snapshot = self.read()
        state = STATES.get(snapshot["gcode_state"], "已收到状态")
        return {"ok": True, "message": "连接成功 · " + state}


def result(snapshot, request_id="", error=""):
    def temperature(key):
        value = _number(snapshot.get(key), 500)
        return "—" if value is None else "%d°C" % round(value)

    state = snapshot.get("gcode_state")
    printing = state in ("RUNNING", "PAUSE", "PREPARE", "INIT", "SLICING")
    progress = _number(snapshot.get("mc_percent"), 100) if printing or state == "FINISH" else None
    remaining = _number(snapshot.get("mc_remaining_time"), 100000) if printing else None
    layer = _number(snapshot.get("layer_num"), 100000)
    layers = _number(snapshot.get("total_layer_num"), 100000)

    status_text = "暂不可用" if error else STATES.get(state, "状态未知")
    progress_text = "—" if progress is None else "%d%%" % round(progress)
    layer_text = "—"
    if (printing or state == "FINISH") and layer is not None and layers and layer <= layers:
        layer_text = "第 %d / %d 层" % (layer, layers)
    remaining_text = "—"
    if remaining is not None:
        hours, minutes = divmod(int(remaining), 60)
        remaining_text = ("%d 小时 %d 分钟" % (hours, minutes) if hours
                          else "%d 分钟" % minutes)
    task = snapshot.get("subtask_name") or snapshot.get("gcode_file")
    task = (" ".join(task.split())[:80]
            if state != "IDLE" and isinstance(task, str) else "")
    # The generic lines renderer on older bridges still shows useful data.
    rows = [
        {"label": "进度", "value": progress_text + " · " + status_text},
        {"label": "温度", "value": "喷嘴 " + temperature("nozzle_temper")
         + "   热床 " + temperature("bed_temper")},
        {"label": "打印", "value": layer_text + "   剩余 " + remaining_text},
    ]
    status = "error" if error else ("partial" if printing and progress is None else "ok")
    card = {
        "template": "lines", "layout": APP_ID, "title": TITLE, "status": status,
        "rows": rows, "footer": "拓竹 · 局域网", "notice": error[:120],
        "printer": {
            "progress": progress, "progress_text": progress_text, "state": status_text,
            "nozzle": temperature("nozzle_temper"), "bed": temperature("bed_temper"),
            "layers": layer_text, "task": task, "remaining": remaining_text,
        },
    }
    validate_card(card)
    spoken = status_text
    if printing and progress is not None:
        spoken = "打印到百分之%d" % round(progress)
    if remaining is not None:
        spoken += "，剩余" + remaining_text.replace(" ", "")
    return QueryResult(
        APP_ID, OPERATION, status=status, source=SOURCE, request_id=request_id,
        data={"card": card}, speech=(error or spoken)[:100],
        data_through=now_iso() if not error else None,
        error_code="unavailable" if error else None,
    )
