#!/usr/bin/env python3
"""Run the real dashboard server with the ESP32-ingest backend instead of
direct BLE (poller.py) or the scripted emulator (emulator/run_server.py).

Same "swap the poller module" technique as the emulator - server.py is
completely unmodified. Useful for testing the ESP32 firmware against
your Mac directly (point its server URL at your Mac's IP) before ever
touching the router, and for the eventual real router deployment once
the ESP32 is confirmed working.

Usage:
    python3 run_server_ingest.py [--host 0.0.0.0] [--port 8080]

Then point the ESP32's configured server URL at this machine, and open
http://localhost:8080 (or the LAN IP) for the dashboard.
"""
import argparse
import logging

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="0.0.0.0")
parser.add_argument("--port", type=int, default=8080)
args = parser.parse_args()

import ingest_poller  # noqa: E402
import server  # noqa: E402

server.poller = ingest_poller  # the actual swap - server.py's code is untouched

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    print(f"Ingest-backed dashboard: http://localhost:{args.port}")
    print("Waiting for ESP32 pushes to /api/ingest/battery and /api/ingest/charger")
    # access_log=False: uvicorn logs every single HTTP request at INFO
    # level by default, which given this dashboard's polling volume
    # (phone polling every second, ESP32 pushing continuously) floods the
    # router's small syslog ring buffer within a few minutes - confirmed
    # live 2026-10-01 trying to investigate a system slowdown and finding
    # the log had already rotated past the event, just minutes later.
    # Genuine warnings/errors (log.warning calls elsewhere in this app)
    # aren't part of the access log and still come through fine.
    uvicorn.run(server.app, host=args.host, port=args.port, log_level="info", access_log=False)
