#!/usr/bin/env python3
"""Fake replacement for poller.py - drives a physically-grounded battery
simulation (battery_sim.py) from a small script of high-level inputs,
instead of talking to real BLE hardware.

Implements the exact same public interface real poller.py exposes
(run(), latest_battery(), latest_charger(), request_set_alt_current()), so
server.py runs completely unmodified against it - swap is done in
run_server.py by replacing the `poller` module reference before uvicorn
starts. What you're testing against the emulator is the real dashboard/
API/storage code and the battery physics; the actual Bluetooth transport
still needs the real adapter and hardware once that arrives.

CSV format (see scenario.csv for a worked, validated example):
    load_w, solar_w, alt_current_a, duration_s
- load_w: total power draw of everything connected (e.g. 2000 for an
  induction hob via an inverter). Always a positive draw.
- solar_w: solar panel input, watts (realistic range ~1-160 for this rig).
- alt_current_a: alternator/DC-DC output current, amps (5-56, matching
  the real charger's own range) - clamped to whatever the ALT max-current
  *setting* currently is (the Settings slider works against the emulator
  too, same as the real charger).
- duration_s: how long (simulated seconds) to hold these inputs before
  moving to the next row.
- Blank lines and lines starting with # are ignored.
- Reaching the last row loops back to the first.
- The file is re-read if it changes on disk - the running battery state
  is NOT reset, only which row/inputs are active, so you can tweak a
  script mid-run and see the effect continue from wherever the battery
  currently is.

Simulated time runs faster than real time via --speed (simulated seconds
per real second, default 20) - an 8-minute (480s) scripted row plays out
in 24 real seconds at the default speed.
"""
import asyncio
import csv
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
import storage
from battery_sim import BatterySim, ALT_VOLTAGE, PANEL_VOLTAGE

log = logging.getLogger("emulator")

DEFAULT_SCENARIO_PATH = Path(__file__).parent / "scenario.csv"
DEFAULT_SPEED = 20.0       # simulated seconds per real second
TICK_REAL_S = 1.0          # how often we step/publish state, in real seconds
DEFAULT_START_SOC = 50.0
NOMINAL_AH = 230.0

_state = {
    "battery": {"data": None, "updated_at": None, "connected": True, "error": None},
    "charger": {"data": None, "updated_at": None, "connected": True, "error": None},
}
_alt_input_current = config.ALT_CURRENT_DEFAULT


def latest_battery() -> dict:
    return _state["battery"]


def latest_charger() -> dict:
    return _state["charger"]


async def request_set_alt_current(amps: int) -> dict:
    """Mimics charger.py's set_alt_max_current() response shape. Actually
    clamps the scripted alternator current in the simulation too, so the
    Settings slider has a real, visible effect against the emulator."""
    global _alt_input_current
    _alt_input_current = amps
    await asyncio.sleep(0.1)
    return {"id": 668, "result": {"altInputC": amps}}


def _read_rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        lines = [line for line in f if line.strip() and not line.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    rows = [{k: float(v) for k, v in row.items()} for row in reader]
    if not rows:
        raise ValueError(f"no data rows found in {path}")
    for r in rows:
        for col in ("load_w", "solar_w", "alt_current_a", "duration_s"):
            if col not in r:
                raise ValueError(f"scenario CSV missing required column: {col}")
    return rows


def _battery_dict_from_sim(sim_state) -> dict:
    return {
        "voltage": sim_state.voltage,
        "current": sim_state.current,
        "power": round(sim_state.voltage * sim_state.current, 1),
        "soc_percent": sim_state.soc_percent,
        "remaining_ah": sim_state.remaining_ah,
        "nominal_ah": NOMINAL_AH,
        "cycles": 3,
        "charging": sim_state.current > 0,
        "temperature_c": sim_state.temperature_c,
        "ntc_temps_c": [sim_state.temperature_c],
        "cell_voltages": [round(sim_state.voltage / 4, 3)] * 4,
        "num_cells": 4,
        "device_name": "EMULATED-BATTERY",
        "protection_tripped": sim_state.protection_tripped,
    }


def _charger_dict(battery: dict, solar_w: float, alt_current_a: float) -> dict:
    panel_i = solar_w / PANEL_VOLTAGE if solar_w else 0.0
    return {
        "id": 1, "statusMsg": "Get data success", "mac": config.CHARGER_MAC, "code": 200,
        "result": {
            "deviceInfo": {"name": "EMULATED-CHARGER", "type": "KADCDC40ABTB"},
            "batteryInfo": {"type": 4, "vGrade": 1, "chargeState": 1 if battery["charging"] else 0,
                             "temperature": round(25 + alt_current_a * 0.3, 1)},
            "deviceWarnId": 0,
            "altInputC": _alt_input_current,
            "alternator": {"v": ALT_VOLTAGE if alt_current_a else 0.0, "c": alt_current_a,
                            "p": round(ALT_VOLTAGE * alt_current_a, 1) if alt_current_a else 0.0},
            "panel": {"v": PANEL_VOLTAGE if solar_w else 0.0, "c": round(panel_i, 2), "p": round(solar_w, 1)},
            "batt": {"v": battery["voltage"], "c": battery["current"], "p": battery["power"]},
        },
    }


async def run(scenario_path: Path = DEFAULT_SCENARIO_PATH, speed: float = DEFAULT_SPEED,
              start_soc: float = DEFAULT_START_SOC):
    await storage.init()
    sim = BatterySim(NOMINAL_AH, start_soc, config.CHARGER_EFFICIENCY)
    log.info("emulator running from %s, speed=%sx, start_soc=%s%%, DB=%s",
             scenario_path, speed, start_soc, storage.DB_PATH)

    rows = _read_rows(scenario_path)
    last_mtime = scenario_path.stat().st_mtime
    row_idx = 0
    row_elapsed_s = 0.0

    while True:
        try:
            mtime = scenario_path.stat().st_mtime
            if mtime != last_mtime:
                log.info("scenario file changed, reloading script (battery state carries over)")
                rows = _read_rows(scenario_path)
                last_mtime = mtime
                row_idx, row_elapsed_s = 0, 0.0
        except FileNotFoundError:
            log.warning("scenario file missing, keeping last-loaded script")

        row = rows[row_idx % len(rows)]
        load_w = row["load_w"]
        solar_w = max(0.0, min(160.0, row["solar_w"]))
        alt_current_a = max(0.0, min(float(_alt_input_current), row["alt_current_a"]))

        sim_state = sim.step(load_w, solar_w, alt_current_a, dt_hours=speed / 3600)

        battery = _battery_dict_from_sim(sim_state)
        charger = _charger_dict(battery, solar_w, alt_current_a)

        now = time.time()
        _state["battery"]["data"] = battery
        _state["battery"]["updated_at"] = now
        _state["charger"]["data"] = charger
        _state["charger"]["updated_at"] = now

        await storage.insert_battery(battery, ts=now)
        await storage.insert_charger(charger, ts=now)

        row_elapsed_s += speed
        if row_elapsed_s >= row["duration_s"]:
            row_idx += 1
            row_elapsed_s = 0.0

        await asyncio.sleep(TICK_REAL_S)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
