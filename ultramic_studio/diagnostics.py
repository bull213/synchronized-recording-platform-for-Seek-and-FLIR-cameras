"""Read-only device listing and opt-in brief native-rate capture probes."""
import argparse
import json
import platform
import sys
import tempfile
import time
from importlib import metadata
from pathlib import Path

from .audio import create_input_source, list_devices
from . import __version__
from .core import RecorderEngine, RecordingConfig


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true", help="Try a 0.5-second native capture on named Ultramic inputs")
    parser.add_argument("--device", type=int, help="Probe one specific device index")
    args = parser.parse_args()
    report = {"app_version": __version__, "platform": platform.platform(), "python": sys.version,
              "packages": {}, "devices": [], "probes": []}
    for item in ["numpy", "PySide6", "pyqtgraph", "sounddevice", "pyserial", "lameenc"]:
        report["packages"][item] = metadata.version(item)
    try:
        devices = list_devices()
        report["devices"] = devices
        if args.probe or args.device is not None:
            for device in devices:
                chosen = device["index"] == args.device if args.device is not None else any(
                    x in device["name"].lower() for x in ("ultra", "dodotronic", "um200"))
                if not chosen:
                    continue
                entry = {"device": device}
                with tempfile.TemporaryDirectory() as tmp:
                    engine = RecorderEngine(RecordingConfig(output=Path(tmp), auto_mp3=False, device=dict(device)))
                    source = create_input_source(engine, device)
                    try:
                        source.start()
                        time.sleep(.5)
                        state = engine.snapshot()
                        if state["error"] or state["samples"] == 0:
                            raise RuntimeError(state["error"] or "No audio frames received.")
                        entry.update(result="opened", samples_received=state["samples"],
                                     reported_stream=engine.config.device,
                                     note="Opening does not independently prove ultrasonic frequency response or clock accuracy.")
                    except Exception as exc:
                        entry.update(result="failed", error=str(exc))
                    finally:
                        source.stop()
                        engine.close()
                report["probes"].append(entry)
        if not report["probes"] and args.probe:
            report["note"] = "No named Ultramic found. If Windows names it USB Audio Device, run with --device INDEX using an index from the device list."
    except Exception as exc:
        report["error"] = str(exc)
    text = json.dumps(report, indent=2)
    Path("microphone_diagnostics.txt").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
