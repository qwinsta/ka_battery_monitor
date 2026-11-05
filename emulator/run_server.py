#!/usr/bin/env python3
"""Run the real dashboard server against emulated (scripted) device data
instead of real BLE hardware - lets you test the whole app (API, storage,
history/graphs, the alt-current control, the frontend) without sitting in
the vehicle. The actual Bluetooth transport isn't touched at all here -
once the real adapter arrives, testing that is a separate step.

server.py itself is completely unmodified: this script imports it and
swaps its `poller` module reference for emulator_poller before uvicorn
starts, so every request server.py handles runs its real code, just
reading from the emulator's scripted state instead of live hardware.

Usage:
    python3 emulator/run_server.py [--scenario emulator/scenario.csv] [--speed 20] [--start-soc 50] [--port 8080]

Then open http://localhost:8080 same as the real dashboard.
Edit scenario.csv while this is running to try different values - the
emulator reloads it automatically when it changes (battery state carries
over; only the script of inputs changes).
"""
import argparse
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

parser = argparse.ArgumentParser()
parser.add_argument("--scenario", default=str(Path(__file__).parent / "scenario.csv"))
parser.add_argument("--speed", type=float, default=20.0, help="simulated seconds per real second")
parser.add_argument("--start-soc", type=float, default=50.0, help="starting battery SOC percent")
parser.add_argument("--host", default="0.0.0.0")
parser.add_argument("--port", type=int, default=8080)
args = parser.parse_args()

# Must be set before storage.py is imported anywhere (it reads this at
# module import time) - keeps emulated runs out of the real vehicle's
# history.sqlite3 entirely.
os.environ["KA_DB_PATH"] = str(Path(__file__).parent / "emulator_history.sqlite3")

import emulator_poller  # noqa: E402  (after sys.path/env setup above)
import server  # noqa: E402

server.poller = emulator_poller  # the actual swap - server.py's code is untouched


@asynccontextmanager
async def emulator_lifespan(app):
    task = asyncio.create_task(
        emulator_poller.run(Path(args.scenario), args.speed, args.start_soc)
    )
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


server.app.router.lifespan_context = emulator_lifespan


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    print(f"Emulated dashboard: http://localhost:{args.port}  "
          f"(scenario: {args.scenario}, {args.speed}x speed, starting at {args.start_soc}% SOC)")
    print(f"Emulator DB: {os.environ['KA_DB_PATH']} (separate from real vehicle history)")
    uvicorn.run(server.app, host=args.host, port=args.port, log_level="warning")
