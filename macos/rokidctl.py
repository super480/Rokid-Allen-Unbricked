"""First-run controller for a single, explicitly selected Rokid on a home LAN."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser

SOURCE = Path(__file__).resolve().parents[1]
FROZEN = getattr(sys, "frozen", False)
RESOURCES = Path(sys.executable).parent.parent if FROZEN else SOURCE
DATA = Path(os.environ.get("ROKID_DATA", Path.home() / "Library/Application Support/Rokid-Allen-Unbricked"))
CONFIG = DATA / "config.local.json"
PORT = 18765
PACKAGE = "cn.rokid.bridge"
ROOTS = [ipaddress.ip_network(x) for x in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
if not FROZEN:
    sys.path.insert(0, str(SOURCE / "app/host"))


def resolve_adb():
    for candidate in (os.environ.get("ROKID_ADB"), shutil.which("adb"),
                      Path.home() / "Library/Android/sdk/platform-tools/adb",
                      "/opt/homebrew/bin/adb", "/usr/local/bin/adb"):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return str(candidate)
    raise ValueError("请选择 Android 官方 Platform-Tools 的 adb 文件。")


class Device:
    def __init__(self, ip):
        address = ipaddress.IPv4Address(ip)
        if not any(address in network for network in ROOTS):
            raise ValueError("请输入家庭局域网的若琪 IPv4 地址。")
        self.ip = str(address)
        self.serial = self.ip + ":5555"
        self.adb = resolve_adb()

    def command(self, *args, global_cmd=False, timeout=12):
        prefix = [self.adb] if global_cmd else [self.adb, "-s", self.serial]
        result = subprocess.run(prefix + list(args), capture_output=True, text=True, timeout=timeout)
        output = (result.stdout + result.stderr).strip()
        if result.returncode or "Failure [" in output or output.startswith(("Error:", "Exception")):
            raise ValueError("ADB 操作未完成：" + output[:260])
        return result.stdout.strip()

    def inspect(self):
        self.command("connect", self.serial, global_cmd=True)
        if self.command("get-state") != "device":
            raise ValueError("设备尚未授权或处于离线状态。")
        def prop(key):
            return self.command("shell", "getprop", key)
        model, sdk = prop("ro.product.model").upper(), prop("ro.build.version.sdk")
        if model not in ("ROKID-R2", "RH01CW") or sdk != "19" or prop("ro.product.manufacturer").upper() != "ROKID":
            raise ValueError("当前仅支持 Rokid R2 / RH01CW，API 19。未写入设备。")
        identity = "|".join((model, prop("ro.serialno"), prop("ro.build.fingerprint")))
        return {"identity": hashlib.sha256(identity.encode()).hexdigest(),
                "boot": self.command("shell", "cat", "/proc/sys/kernel/random/boot_id"),
                "port": prop("persist.adb.tcp.port"), "secure": prop("ro.adb.secure")}


def load_record():
    try:
        return json.loads((DATA / "adb-state.json").read_text())
    except FileNotFoundError:
        return {}


def guard(device, apply):
    from config import atomic_json
    state = device.inspect()
    old = load_record()
    same = old.get("ip") == device.ip and old.get("identity") == state["identity"]
    if not apply and (not same or state["port"] != "5555"):
        raise ValueError("请先输入此设备的 IP 并完成 ADB 固化校验。")
    if state["port"] != "5555":
        if state["secure"] != "1":
            raise ValueError("固件未报告 ADB 密钥认证，未开启持久网络调试。请核对固件与恢复通道。")
        device.command("shell", "setprop", "persist.adb.tcp.port", "5555")
        if device.command("shell", "getprop", "persist.adb.tcp.port") != "5555":
            raise ValueError("持久属性回读失败。需要该固件已授权的属性写权限；安装已停止。")
        same = False
    rebooted = bool(same and (old.get("reboot_verified") or
                    (old.get("boot") and state["boot"] and old["boot"] != state["boot"])))
    if apply:
        atomic_json(DATA / "adb-state.json", dict(ip=device.ip, identity=state["identity"],
                    boot=state["boot"], reboot_verified=rebooted))
    print("ADB 持久端口已回读为 5555。" + ("已验证跨重启回连。" if rebooted else "尚未验证重启回连。"), flush=True)
    if state["secure"] != "1":
        print("此固件未报告 ADB 密钥认证，现有调试端口已开启。请仅在隔离可信的家庭网络使用。", flush=True)


def install(device, apk):
    if not apk.is_file():
        raise ValueError("缺少同版 Bridge APK，请重新构建安装包。")
    previous = device.command("shell", "pm", "path", PACKAGE)
    if previous.startswith("package:"):
        path = previous.splitlines()[0].removeprefix("package:").strip()
        with tempfile.TemporaryDirectory() as d:
            snapshot = Path(d) / "installed.apk"
            device.command("pull", path, str(snapshot), timeout=60)
            if hashlib.sha256(snapshot.read_bytes()).digest() == hashlib.sha256(apk.read_bytes()).digest():
                return
    out = device.command("install", "-r", str(apk), timeout=90)
    if "Success" not in out:
        raise ValueError("APK 安装未获成功回执。若签名不兼容，请保留原安装并查看打包指南。")


def pair(device, cfg):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((device.ip, 5555))
        host = probe.getsockname()[0]
    session = secrets.token_hex(16)
    port = int(device.command("forward", "tcp:0", "localabstract:rokid.setup." + session))
    try:
        device.command("shell", "am", "start", "-n", PACKAGE + "/.MainActivity", "-a",
                       PACKAGE + ".PAIR", "--es", "session", session)
        payload = json.dumps({"host": "http://%s:%d" % (host, PORT),
                              "token": cfg["device_token"]}).encode() + b"\n"
        for _ in range(30):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2) as peer:
                    peer.sendall(payload)
                    if peer.makefile("rb").readline(16).strip() == b"OK":
                        return
            except OSError:
                pass
            time.sleep(.25)
        raise ValueError("Bridge 尚未确认写入配对配置，请检查设备后重试。")
    finally:
        device.command("forward", "--remove", "tcp:%d" % port)


def child(role, device):
    argv = [sys.executable] if FROZEN else [sys.executable, str(Path(__file__).resolve())]
    log = open(DATA / (role + ".log"), "ab")
    env = dict(os.environ, ROKID_ADB=device.adb, ROKID_RESOURCES=str(RESOURCES),
               ROKID_DATA=str(DATA), ROKID_ROON=str(DATA / "roon-bridge") if FROZEN else str(SOURCE / "app/roon-bridge"),
               ROKID_WEB=str(RESOURCES / "app/host/web"), PYINSTALLER_RESET_ENVIRONMENT="1")
    try:
        return subprocess.Popen(argv + [role, "--ip", device.ip], stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, env=env, cwd=DATA)
    finally:
        log.close()


def serve(device, no_browser):
    import config
    guard(device, False)
    with socket.socket() as probe:
        probe.bind(("", PORT))
    if not CONFIG.exists():
        config.create_config(CONFIG)
    cfg = json.loads(CONFIG.read_text())
    if cfg.get("device") != device.serial or not cfg.get("device_token"):
        cfg["device_token"] = secrets.token_urlsafe(32)
    cfg.update(device=device.serial, port=PORT, audio_runtime=str(DATA / "runtime"))
    apk = RESOURCES / ("rokid-bridge.apk" if FROZEN else "app/build/rokid-bridge.apk")
    install(device, apk)
    pair(device, cfg)
    config.atomic_json(CONFIG, cfg)
    processes = []
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        host = child("host", device)
        processes.append(host)
        url = "http://127.0.0.1:%d" % PORT
        for _ in range(80):
            if stopping:
                return
            if host.poll() is not None:
                raise ValueError("Host 启动失败，请查看本机 host.log。")
            try:
                req = urllib.request.Request(url + "/api/status", headers={"Authorization": "Bearer " + cfg["token"]})
                with urllib.request.urlopen(req, timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(.15)
        else:
            raise ValueError("Host 健康检查超时。")
        processes.append(child("watcher", device))
        print("Bridge 配置已写入，控制台 " + url + "/console", flush=True)
        if not no_browser:
            webbrowser.open(url + "/console")
        while not stopping:
            if any(p.poll() is not None for p in processes):
                raise ValueError("后台服务意外退出，请查看 host.log 或 watcher.log。")
            time.sleep(.3)
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
        for p in processes:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def main():
    parser = argparse.ArgumentParser(description="若琪 IP → ADB 固化 → 安装并启动控制台")
    parser.add_argument("action", choices=("guard", "run", "host", "watcher"))
    parser.add_argument("--ip", required=True)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    device = Device(args.ip)
    if args.action in ("host", "watcher"):
        guard(device, False)
        sys.argv = [args.action, "--config", str(CONFIG)]
        if args.action == "host":
            import server
            server.main()
        else:
            import asr_watcher
            sys.argv.extend(["--device", device.serial])
            asr_watcher.main()
        return
    with (DATA / "installation.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("该用户已有安装或服务正在运行，请先退出原窗口。")
        if args.action == "guard":
            guard(device, True)
        else:
            serve(device, args.no_browser)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
