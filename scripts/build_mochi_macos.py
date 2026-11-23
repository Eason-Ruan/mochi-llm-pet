#!/usr/bin/env python3
"""Build a checkout-local, signed macOS launcher with a stable app identity."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import sysconfig
import tempfile

from deskpet.c_ui.native_notifications import MAC_BUNDLE_ID


ROOT = Path(__file__).resolve().parents[1]


def compile_launcher(binary: Path, *, checkout: Path = ROOT, module: str = "deskpet") -> None:
    libdir = Path(sysconfig.get_config_var("LIBDIR"))
    include = sysconfig.get_config_var("INCLUDEPY")
    library = libdir / sysconfig.get_config_var("LDLIBRARY")
    framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
    if framework:
        library = Path(sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX")) / (
            f"{framework}.framework/Versions/{sysconfig.get_python_version()}/{framework}"
        )
    if not library.is_file():
        raise SystemExit(f"The selected Python's shared library was not found at {library}.")

    subprocess.run(
        [
            "xcrun", "clang", "-Wall", "-Wextra", "-Werror", "-I", include,
            f"-DMOCHI_CHECKOUT={json.dumps(str(checkout), ensure_ascii=False)}",
            f"-DMOCHI_PYTHON={json.dumps(sys.executable, ensure_ascii=False)}",
            f"-DMOCHI_MAIN_MODULE={json.dumps(module)}",
            str(ROOT / "scripts" / "mochi_macos_launcher.c"), str(library),
            "-Xlinker", "-rpath", "-Xlinker", str(library.parent), "-o", str(binary),
        ],
        check=True,
    )


def build_bundle(bundle: Path, *, identity: str, checkout: Path = ROOT, module: str = "deskpet") -> None:
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "Mochi",
        "CFBundleExecutable": "Mochi",
        "CFBundleIdentifier": identity,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Mochi",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
    }
    with (bundle / "Contents" / "Info.plist").open("wb") as file:
        plistlib.dump(info, file)
    compile_launcher(macos / "Mochi", checkout=checkout, module=module)
    subprocess.run(["codesign", "--force", "--sign", "-", str(bundle)], check=True)
    subprocess.run(["codesign", "--verify", "--strict", str(bundle)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".mochi-native" / "Mochi.app")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This launcher is built on macOS.")
    if sys.prefix == sys.base_prefix:
        parser.error("Build with the environment created by uv sync.")
    destination = args.output.expanduser().absolute()
    if destination.suffix != ".app":
        parser.error("The output path must end in .app.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        parser.error(f"{destination} already exists. Remove the previous bundle before rebuilding.")

    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        bundle = Path(temporary) / destination.name
        build_bundle(bundle, identity=MAC_BUNDLE_ID)
        bundle.rename(destination)
    print(destination)


if __name__ == "__main__":
    main()
