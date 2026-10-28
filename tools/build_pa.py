"""Build the bundled K2 PA helper and run focused host-native checks."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "native-pa"
ZIG_VERSION = "0.14.1"
ZIG_SHA256 = "554f5378228923ffd558eac35e21af020c73789d87afeabf4bfd16f2e6feed2c"
ZIG_SIZE = 82229343
ZIG_URL = "https://ziglang.org/download/0.14.1/zig-x86_64-windows-0.14.1.zip"


def compiler(download):
    folder = CACHE / ("zig-x86_64-windows-" + ZIG_VERSION)
    executable = folder / "zig.exe"
    if executable.exists():
        return executable
    if not download:
        raise RuntimeError("Portable Zig is missing; explicitly use --download to fetch it.")
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / ("zig-" + ZIG_VERSION + ".zip")
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != ZIG_SHA256:
        with urllib.request.urlopen(ZIG_URL, timeout=60) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
    if archive.stat().st_size != ZIG_SIZE or hashlib.sha256(archive.read_bytes()).hexdigest() != ZIG_SHA256:
        raise RuntimeError("Portable compiler download failed checksum validation.")
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            if not (CACHE / item.filename).resolve().is_relative_to(CACHE.resolve()):
                raise RuntimeError("Compiler archive escapes the project cache.")
        package.extractall(CACHE)
    return executable


def build(download=False):
    zig = compiler(download)
    source = ROOT / "native" / "pa"
    assets = ROOT / "app" / "assets" / "pa"
    checks = ROOT / "build" / "native-pa"
    assets.mkdir(parents=True, exist_ok=True)
    checks.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["ZIG_GLOBAL_CACHE_DIR"] = str(CACHE / "zig-cache")
    environment["ZIG_LOCAL_CACHE_DIR"] = str(CACHE / "zig-local")
    common = [str(zig), "cc", "-O2", "-shared", "-I", str(source / "include"), "-ffp-contract=off"]
    native = checks / "pa_test.dll"
    subprocess.run(common + ["-target", "x86_64-windows-gnu", str(source / "test_pa.c"), "-o", str(native)],
                   cwd=ROOT, env=environment, check=True)
    library = ctypes.CDLL(str(native))
    library.uncurser_pa_native_test.restype = ctypes.c_int
    result = library.uncurser_pa_native_test()
    if result:
        raise RuntimeError("Native PA timing/ownership check failed at case " + str(result))
    output = assets / "uncurser_pa.so"
    subprocess.run(common + ["-target", "arm-linux-gnueabihf.2.17", "-mcpu=cortex_a7", "-fPIC",
                             "-fvisibility=hidden", "-ffunction-sections", "-fdata-sections", "-s",
                             "-Wl,-z,defs", "-Wl,--build-id=none", "-Wl,--gc-sections",
                             "-Wl,--version-script=" + str(source / "exports.map"),
                             str(source / "kin_extruder.c"), "-o", str(output)],
                   cwd=ROOT, env=environment, check=True)
    data = output.read_bytes()
    if (data[:6] != b"\x7fELF\x01\x01" or struct.unpack_from("<H", data, 18)[0] != 40
            or struct.unpack_from("<I", data, 36)[0] != 0x5000400):
        raise RuntimeError("PA helper is not the expected ARM32 hard-float ELF interface.")
    manifest = {"api": 1, "target": "arm-linux-gnueabihf.2.17", "cpu": "cortex_a7",
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "source_sha256": hashlib.sha256((source / "kin_extruder.c").read_bytes()).hexdigest()}
    (assets / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (checks / "checks.json").write_text(json.dumps({"host_checks": "passed", **manifest}, indent=2) + "\n", encoding="utf-8")
    print("PA timing, fixed-value, travel, enable/disable, allocation and bounded-history checks passed.")
    print("ARM helper: " + str(output.relative_to(ROOT)) + " (" + str(len(data)) + " bytes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Fetch the pinned portable compiler into this project")
    build(parser.parse_args().download)
