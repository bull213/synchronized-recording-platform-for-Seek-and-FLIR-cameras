"""Assemble Windows x64 portable files from official runtime + downloaded wheels.

Run from the source folder after downloading requirements.txt for CPython 3.13:
  python -m pip download --only-binary=:all: --platform win_amd64 \
      --implementation cp --python-version 3.13 --abi cp313 \
      --dest WINDOWS_WHEELS -r requirements.txt
  python scripts/build_portable.py --runtime-zip PYTHON_ZIP --wheels WINDOWS_WHEELS

This assembles binaries; it does not execute Windows code on another OS.
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZIP_DEFLATED

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.package_source import source_files

PYTHON_VERSION = "3.13.16"
PYTHON_URL = "https://www.python.org/ftp/python/3.13.16/python-3.13.16-embed-amd64.zip"
PYTHON_SHA256 = "97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def extract(archive, destination, runtime=None):
    with ZipFile(archive) as z:
        for entry in z.infolist():
            parts = PurePosixPath(entry.filename).parts
            if not parts or entry.is_dir():
                continue
            if any(part in ("..", "") or ":" in part or "\\" in part for part in parts) or entry.filename.startswith("/"):
                raise ValueError(f"Unsafe archive path: {entry.filename}")
            base = destination
            if runtime and parts[0].endswith(".data"):
                category, *parts = parts[1:]
                base = {"purelib": destination, "platlib": destination,
                        "data": runtime, "scripts": runtime / "Scripts",
                        "headers": runtime / "Include"}[category]
            target = base.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-zip", type=Path, required=True)
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    approved = source_files(source)
    if args.output and (args.output.exists() or args.output.resolve().is_relative_to(source)):
        raise ValueError("Use a new output ZIP path outside the source folder.")
    runtime = source / "runtime"
    if runtime.exists():
        raise RuntimeError("The runtime folder already exists. Build from a clean source copy.")
    assert digest(args.runtime_zip) == PYTHON_SHA256, "Python runtime checksum mismatch"
    wheels = sorted(args.wheels.glob("*.whl"))
    if not wheels:
        raise RuntimeError("No Windows dependency wheels found")
    manifest = {"target": "Windows 10/11 x64", "python_version": PYTHON_VERSION,
                "python_url": PYTHON_URL, "python_sha256": PYTHON_SHA256, "wheels": []}
    extract(args.runtime_zip, runtime)
    packages = runtime / "Lib" / "site-packages"
    for wheel in wheels:
        if not (wheel.name.endswith("win_amd64.whl") or wheel.name.endswith("none-any.whl")):
            raise ValueError(f"Unexpected wheel platform: {wheel.name}")
        extract(wheel, packages, runtime)
        manifest["wheels"].append({"filename": wheel.name, "sha256": digest(wheel)})
    (runtime / "python313._pth").write_text(
        "python313.zip\n.\nLib\\site-packages\n..\nimport site\n", encoding="ascii")
    for required in [runtime / "python.exe", runtime / "python313.dll",
                     packages / "PySide6" / "QtWidgets.pyd",
                     packages / "PySide6" / "plugins" / "platforms" / "qwindows.dll",
                     packages / "_sounddevice_data" / "portaudio-binaries" / "libportaudio64bit.dll"]:
        if not required.is_file():
            raise RuntimeError(f"Missing portable dependency: {required}")
    (source / "docs" / "PORTABLE_BUILD.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for path in source.glob("*.cmd"):
        path.write_bytes(path.read_text().replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    if args.output:
        generated = [source / "docs" / "PORTABLE_BUILD.json"]
        generated.extend(path for path in runtime.rglob("*") if path.is_file())
        with ZipFile(args.output, "x", ZIP_DEFLATED, compresslevel=6) as z:
            for path in [path for _, path in approved] + sorted(generated):
                relative = path.relative_to(source)
                z.write(path, Path("UltramicStudio") / relative)
        print(json.dumps({"archive": str(args.output.resolve()), "bytes": args.output.stat().st_size,
                          "sha256": digest(args.output)}))
    else:
        print(json.dumps({"runtime": str(runtime), "wheels": len(wheels)}))


if __name__ == "__main__":
    main()
