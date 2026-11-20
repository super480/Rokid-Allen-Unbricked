#!/usr/bin/env python3
"""Build the standalone idle clock APK. Mirrors app/build_android.py.

Occupies the type-4 idle slot (package == action == rokid:package), so the
APK carries assets/RokidManifest.xml and a bundled face font. No Gradle.
"""
from pathlib import Path
import os
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT.parent.parent / "tools"
JDK = Path(os.environ["JAVA_HOME"])
SDK = Path(os.environ["ANDROID_HOME"])
BT = Path(os.environ.get("ANDROID_BUILD_TOOLS", SDK / "build-tools" / "35.0.0"))
PLATFORM = Path(os.environ.get("ANDROID_JAR", SDK / "platforms" / "android-19" / "android.jar"))
OUT = ROOT / "build"

for required in (JDK / "bin" / "javac", BT / "aapt", PLATFORM):
    if not required.exists():
        raise SystemExit("missing toolchain piece: %s" % required)

OUT.mkdir(parents=True, exist_ok=True)
env = dict(os.environ, JAVA_HOME=str(JDK))
env["PATH"] = str(JDK / "bin") + os.pathsep + env.get("PATH", "")


def run(*args):
    subprocess.run([str(x) for x in args], check=True, env=env)


work = Path(tempfile.mkdtemp(prefix="rokid-clock-"))
classes = work / "classes"
dex = work / "dex"
classes.mkdir()
dex.mkdir()

run(BT / "aapt", "package", "-f", "-M", ROOT / "android/AndroidManifest.xml",
    "-I", PLATFORM, "-F", OUT / "unsigned.apk")

sources = sorted((ROOT / "android/src").rglob("*.java"))
run(JDK / "bin/javac", "-encoding", "UTF-8", "-source", "8", "-target", "8",
    "-bootclasspath", PLATFORM, "-d", classes, *sources)

run(JDK / "bin/java", "-cp", BT / "lib/d8.jar", "com.android.tools.r8.D8",
    "--min-api", "19", "--lib", PLATFORM, "--output", dex,
    *sorted(classes.rglob("*.class")))

with zipfile.ZipFile(OUT / "unsigned.apk", "a", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(dex.glob("*.dex")):
        archive.write(path, path.name)
    for path in sorted((ROOT / "android/assets").rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            rel = path.relative_to(ROOT / "android/assets")
            archive.write(path, "assets/" + str(rel), compress_type=zipfile.ZIP_DEFLATED)

run(BT / "zipalign", "-f", "4", OUT / "unsigned.apk", OUT / "aligned.apk")

key = ROOT.parent / "build" / "debug.keystore"
if not key.exists():
    key.parent.mkdir(parents=True, exist_ok=True)
    run(JDK / "bin/keytool", "-genkeypair", "-keystore", key,
        "-storepass", "android", "-keypass", "android",
        "-alias", "androiddebugkey", "-dname", "CN=Rokid Clock Development",
        "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000")

run(BT / "apksigner", "sign", "--ks", key,
    "--ks-pass", "pass:android", "--key-pass", "pass:android",
    "--min-sdk-version", "19",
    "--v1-signing-enabled", "true", "--v2-signing-enabled", "false",
    "--v3-signing-enabled", "false",
    "--out", OUT / "rokid-clock-debug.apk", OUT / "aligned.apk")
run(BT / "apksigner", "verify", "--min-sdk-version", "19", OUT / "rokid-clock-debug.apk")

print("APK:", OUT / "rokid-clock-debug.apk")
