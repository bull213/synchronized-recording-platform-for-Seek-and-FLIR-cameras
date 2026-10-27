"""Run after enabling software triggering and arming triggers in the app.

Usage: python examples/software_trigger.py START --label mouse_01_trial_02
       python examples/software_trigger.py MARK --label intruder_entry
       python examples/software_trigger.py STOP
       python examples/software_trigger.py STATUS
"""
import argparse
import json
import socket


def send_command(command, label=None, port=8765):
    payload = {"command": command}
    if label is not None:
        payload["label"] = label
    with socket.create_connection(("127.0.0.1", port), timeout=6) as connection:
        connection.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        with connection.makefile("rb") as f:
            reply = json.loads(f.readline(16384))
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "Trigger failed"))
    return reply


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["START", "STOP", "MARK", "STATUS"])
    parser.add_argument("--label")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    print(json.dumps(send_command(args.command, args.label, args.port), indent=2))
