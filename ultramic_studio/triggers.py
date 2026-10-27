"""Serial TTL bridge and optional localhost TCP command interface."""
from __future__ import annotations

import json
import socketserver
import threading
import time


def parse_command(line: str) -> dict:
    if len(line) > 1024:
        raise ValueError("Command exceeds 1024 characters.")
    line = line.strip()
    if not line:
        raise ValueError("Empty command.")
    if line.startswith("{"):
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
    else:
        fields = line.split(maxsplit=1)
        data = {"command": fields[0]}
        if len(fields) > 1:
            data["label"] = fields[1]
    command = data.get("command", "")
    if not isinstance(command, str):
        raise ValueError("command must be a string.")
    command = command.upper()
    if command not in ("START", "STOP", "MARK", "STATUS", "READY", "HEARTBEAT", "ERROR"):
        raise ValueError("Supported commands: START, STOP, MARK, STATUS.")
    result = {"command": command}
    if "label" in data:
        if not isinstance(data["label"], str) or len(data["label"]) > 128:
            raise ValueError("label must be a string of at most 128 characters.")
        result["label"] = data["label"]
    if "device_us" in data:
        if type(data["device_us"]) is not int or not 0 <= data["device_us"] < 2**32:
            raise ValueError("device_us must be an unsigned 32-bit integer.")
        result["device_us"] = data["device_us"]
    for field in ("mode", "message"):
        if field in data:
            result[field] = str(data[field])[:256]
    return result


def dispatch(get_engine, data: dict, source: str) -> dict:
    engine = get_engine()
    if engine is None:
        return {"ok": False, "error": "Connect the microphone and arm triggers in the app first."}
    command = data["command"]
    if command not in ("START", "STOP", "MARK", "STATUS"):
        return {"ok": False, "error": "Not an acquisition command."}
    return engine.request(command, source=source,
                          **{k: v for k, v in data.items() if k != "command"})


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class SoftwareTrigger:
    """One newline-delimited command and JSON response per TCP connection."""
    def __init__(self, get_engine, port: int = 8765):
        owner = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(6)
                try:
                    raw = self.rfile.readline(1026)
                    if not raw.endswith(b"\n") or len(raw) > 1025:
                        raise ValueError("Send one UTF-8 command ending in a newline (max 1024 bytes).")
                    result = dispatch(owner.get_engine, parse_command(raw.decode("utf-8")), "tcp")
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
                try:
                    self.wfile.write((json.dumps(result) + "\n").encode("utf-8"))
                except OSError:
                    pass
        self.get_engine = get_engine
        self.server = _Server(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class SerialTrigger:
    def __init__(self, port: str, get_engine, notify, baud: int = 115200):
        import serial
        self.get_engine, self.notify = get_engine, notify
        self.serial = serial.Serial(port, baudrate=baud, timeout=.05, write_timeout=.2)
        self.stop_event = threading.Event()
        self.seen_heartbeat = False
        self.last_heartbeat = time.monotonic()
        self.thread = threading.Thread(target=self._run, name="TTL serial input", daemon=True)
        self.thread.start()

    def sync_gate(self) -> None:
        self.serial.write(b"SYNC\n")

    def _run(self):
        buffer = bytearray()
        try:
            while not self.stop_event.is_set():
                chunk = self.serial.read(max(1, min(1024, self.serial.in_waiting)))
                if chunk:
                    buffer.extend(chunk)
                    while b"\n" in buffer:
                        raw, _, rest = buffer.partition(b"\n")
                        buffer = bytearray(rest)
                        if not raw.strip():
                            continue
                        data = parse_command(raw.decode("utf-8"))
                        command = data["command"]
                        if command in ("READY", "HEARTBEAT"):
                            engine = self.get_engine()
                            if command == "READY" and self.seen_heartbeat and engine and engine.snapshot()["armed"]:
                                raise RuntimeError("TTL bridge rebooted while triggers were armed.")
                            self.seen_heartbeat = True
                            self.last_heartbeat = time.monotonic()
                            if command == "READY":
                                self.notify(f"TTL bridge ready ({data.get('mode', 'serial')}).")
                                if engine and engine.snapshot()["armed"]:
                                    self.sync_gate()
                            continue
                        if command == "ERROR":
                            raise RuntimeError(data.get("message", "TTL bridge error."))
                        result = dispatch(self.get_engine, data, "serial")
                        self.notify(f"TTL {command}: " + ("accepted" if result["ok"] else result["error"]))
                    if len(buffer) > 1024:
                        raise ValueError("Serial command exceeds 1024 bytes without a newline.")
                if self.seen_heartbeat and time.monotonic() - self.last_heartbeat > 4:
                    raise RuntimeError("TTL bridge heartbeat was lost.")
        except Exception as exc:
            if not self.stop_event.is_set():
                message = f"TTL connection failed: {exc}"
                self.notify(message)
                engine = self.get_engine()
                if engine and engine.snapshot()["armed"]:
                    engine.fault(message)
        finally:
            self.serial.close()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
        self.serial.close()
