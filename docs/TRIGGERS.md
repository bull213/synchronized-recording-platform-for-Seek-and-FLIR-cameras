# Trigger connections

## TTL via Arduino Uno R3 / classic Nano

The Ultramic remains connected to the PC by its own USB cable. A second USB cable
connects the Arduino. The Arduino translates digital edges into USB serial
commands; it does not carry microphone audio.

Use the supplied `firmware/UltramicTTL/UltramicTTL.ino` sketch in the Arduino IDE.
Select the correct board and COM port, then upload it. Close the IDE's serial
monitor before connecting the COM port from Ultramic Studio.

Default is **gate mode**, with `GATE_MODE = true`:

| Experiment controller | Uno R3 / classic Nano |
| --- | --- |
| 0–5 V TTL gate output (BNC center, if applicable) | D2, through an optional 1 kΩ series resistor |
| Signal ground (BNC shield, if applicable) | GND |
| 10 kΩ pull-down resistor | Between D2 and GND |
| USB | Recording computer |

A rising edge starts a recording; a falling edge stops it. High means “record.”
**A brief pulse in gate mode makes a brief recording; it is not a toggle.**

For **separate start/stop pulses**, change `GATE_MODE` to `false` and re-upload:

| Signal | Input | Effect |
| --- | --- | --- |
| START pulse | D2, with 10 kΩ pull-down to GND | Rising edge starts |
| STOP pulse | D3, with 10 kΩ pull-down to GND | Rising edge stops |
| Common signal ground | GND | Shared reference |

These instructions are for a **5 V Uno R3 or classic ATmega328P Nano**. Use only
0–5 V logic. Do not attach ±5 V, 12/24 V, or RS-232 outputs. A 3.3 V microcontroller
board is a different electrical interface and may require level conversion.
Leave the TTL low while setting up and connecting. No electrical hardware was
assembled or tested during software development.

In the app:

1. Connect and monitor the microphone (or demo for testing).
2. Select the Arduino COM port and click **Connect TTL bridge**.
3. Wait for “TTL bridge ready.” Opening the port can reset the Arduino.
4. Check **Arm start / stop triggers**. Gate mode synchronizes to the current
   input level, so arming while the input is already high starts recording.
5. Send a gate or start/stop pulses and check the recording badge and saved files.

Edges are interrupt-captured and queued on the board with `micros()` timestamps.
Queue overflow reports an error. A periodic heartbeat lets the app detect a
stalled bridge; losing it while armed stops capture. This does not detect every
possible failure upstream of the bridge, such as a disconnected TTL cable held
low by its pull-down. Verify the full path with a real pulse before each setup is
used for experiments.

The USB transport and microphone clock are independent. This is a recording
control interface, not sample-accurate audio/TTL acquisition.

## Serial protocol for another digital-input device

115200 baud, 8 data bits, no parity, 1 stop bit, newline-terminated UTF-8. The app
accepts `START\n` / `STOP\n` / `MARK label\n` and these JSON forms:

```json
{"command":"START","device_us":123456}
{"command":"MARK","label":"intruder_entry","device_us":123556}
{"command":"STOP","device_us":234567}
```

`device_us` is optional and is logged without claiming clock alignment. Generic
devices do not need a heartbeat. A device that sends READY or HEARTBEAT opts into
the four-second heartbeat timeout. The included firmware sends both.
The app sends `SYNC\n` when arming a connected bridge; the included gate-mode
firmware responds with the current START/STOP level.

## Software trigger

Click **Enable software trigger** and **Arm start / stop triggers**. Connect to
`127.0.0.1:8765` from the same computer. There is no remote/LAN listener.

Each connection carries one UTF-8 line, maximum 1024 bytes, terminated by `\n`.
It receives one JSON response and closes. Acknowledgment means the recording
worker has processed the command; it is not a hardware timing acknowledgment.

| Command | Result |
| --- | --- |
| `START` | Opens a new recording folder and writes available pre-trigger history |
| `STOP` | Finalizes the active recording; repeated STOP is harmless |
| `MARK label` | Writes a labelled event during a recording |
| `STATUS` | Returns connection/armed/recording state and sample counts |

Structured example:

```python
from examples.software_trigger import send_command

send_command("START", label="mouse_01_trial_02")
# Run the trial in your existing experiment-control code.
send_command("MARK", label="intruder_entry")
send_command("STOP")
```

Repeated START while already recording returns the current recording and logs
that the extra start was ignored. It does not overwrite or split the recording.
If a client times out, check STATUS before retrying: a timed-out request might
already have been processed. Only the GUI can arm triggers.

TTL and software commands share a single recording state. If both are enabled,
START from either opens the file and STOP from either closes it. Disarming the
app or disabling either trigger connection stops the recording and disarms both.

For MATLAB, add `examples` to the MATLAB path and call:

```matlab
ultramic_command("START", "mouse_01_trial_02");
ultramic_command("STOP");
```

The Python client was exercised against the server protocol. The MATLAB example
and Arduino sketch are supplied as integration source and were not run on their
target hardware/runtime here.
