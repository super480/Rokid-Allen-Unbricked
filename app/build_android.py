#!/usr/bin/env python3
"""Small reproducible APK build; no Gradle and no proprietary JAR dependency.

The toolchain lives under ../../tools/ as symlinks, so the build works the
same on a fresh checkout once doctor.py reports the SDK and JDK in place.
"""
from pathlib import Path
import os
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT.parent / "tools"
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


# Intermediate outputs go into a fresh directory every run. Deleting the
# previous build/classes and build/dex with rmtree trips bulk-delete guards
# on some machines, and stale .class files would otherwise leak into the
# dex when a source file is removed. A new directory avoids both.
work = Path(tempfile.mkdtemp(prefix="rokid-build-"))
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
    for name in (ROOT / "android/assets.list").read_text().splitlines():
        archive.write(ROOT / "android/assets" / name, "assets/" + name,
                      compress_type=zipfile.ZIP_STORED if name.endswith(".wav") else zipfile.ZIP_DEFLATED)

run(BT / "zipalign", "-f", "4", OUT / "unsigned.apk", OUT / "aligned.apk")

# Stable local key for repeat local builds; release maintainers provide their own.
key = os.environ.get("ROKID_KEYSTORE")
if key:
    signing = ["--ks", key, "--ks-pass", "env:ROKID_STORE_PASSWORD",
               "--key-pass", "env:ROKID_KEY_PASSWORD", "--ks-key-alias", os.environ["ROKID_KEY_ALIAS"]]
else:
    key = OUT / "debug.keystore"
    if not key.exists():
        run(JDK / "bin/keytool", "-genkeypair", "-keystore", key,
            "-storepass", "android", "-keypass", "android", "-alias", "local",
            "-dname", "CN=Local Development", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000")
    signing = ["--ks", key, "--ks-pass", "pass:android", "--key-pass", "pass:android"]
apk = OUT / "rokid-bridge.apk"
run(BT / "apksigner", "sign", *signing, "--min-sdk-version", "19",
    "--v1-signing-enabled", "true", "--out", apk, OUT / "aligned.apk")
run(BT / "apksigner", "verify", "--min-sdk-version", "19", apk)
print("APK:", apk)
