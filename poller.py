#!/usr/bin/env python3
"""Background polling service for both devices.

Maintains a persistent BLE connection to each device rather than
reconnecting every cycle (reconnecting takes several seconds - far too
slow for near-real-time updates). Keeps the latest reading for each
device in memory (read via latest_battery()/latest_charger()) and logs
every sample to SQLite for history.

Cadence is best-effort, not a fixed clock tick: the battery's protocol is
fast (single write+notify round-trip) so it can sustain close to 1s
between reads; the charger's status read needs many sequential BLE round
trips (see charger.py's chunked-read protocol) so it realistically lands
more like 1-3s. The frontend should animate/interpolate rather than assume a fixed
1s delivery for the charger specifically.

On any error the connection is dropped and retried with backoff - this
also self-heals if the device's own app is closed/reopened and briefly
holds the connection, or the vehicle moves out of range.
"""
# X | None union syntax below needs this on Python 3.9 (the router's
# version) - see server.py's matching comment for why.
from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient

import battery
import charger
import config
import storage

log = logging.getLogger("poller")

_state = {
    "battery": {"data": None, "updated_at": None, "connected": False, "error": None},
    "charger": {"data": None, "updated_at": None, "connected": False, "error": None},
}

# Set by the web server when the user moves the alt-current slider;
# consumed by the charger poll loop on its next iteration so the write
# happens on the same persistent connection rather than opening a new one.
_pending_alt_current: int | None = None
_pending_alt_current_result: asyncio.Future | None = None


def latest_battery() -> dict:
    return _state["battery"]


def latest_charger() -> dict:
    return _state["charger"]


async def request_set_alt_current(amps: int) -> dict:
    """Called by the web server. Queues the write for the charger poll
    loop (which owns the persistent connection) and waits for the result."""
    global _pending_alt_current, _pending_alt_current_result
    loop = asyncio.get_event_loop()
    _pending_alt_current_result = loop.create_future()
    _pending_alt_current = amps
    return await _pending_alt_current_result


async def _battery_loop():
    backoff = 1.0
    while True:
        try:
            async with BleakClient(config.BATTERY_ADDRESS, timeout=15.0) as client:
                _state["battery"]["connected"] = True
                _state["battery"]["error"] = None
                backoff = 1.0
                name = None
                cell_counter = 0
                while True:
                    info = await battery.read_basic_info(client)
                    cells = None
                    if cell_counter % 5 == 0:
                        cells = await battery.read_cell_voltages(client, info.num_cells)
                    if name is None:
                        name = await battery.read_device_name(client)
                    cell_counter += 1

                    data = battery.basic_info_to_dict(info, cells, name)
                    if cells is None and _state["battery"]["data"]:
                        data["cell_voltages"] = _state["battery"]["data"].get("cell_voltages")
                        data["num_cells"] = info.num_cells

                    _state["battery"]["data"] = data
                    _state["battery"]["updated_at"] = time.time()
                    await storage.insert_battery(data)
                    await asyncio.sleep(1.0)
        except Exception as e:
            log.warning("battery loop error: %s", e)
            _state["battery"]["connected"] = False
            _state["battery"]["error"] = str(e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def _charger_loop():
    global _pending_alt_current, _pending_alt_current_result
    backoff = 1.0
    while True:
        try:
            async with BleakClient(config.CHARGER_ADDRESS, timeout=15.0) as client:
                _state["charger"]["connected"] = True
                _state["charger"]["error"] = None
                backoff = 1.0
                while True:
                    if _pending_alt_current is not None:
                        amps, fut = _pending_alt_current, _pending_alt_current_result
                        _pending_alt_current = None
                        _pending_alt_current_result = None
                        try:
                            result = await charger.set_alt_max_current_from_client(
                                client, config.CHARGER_MAC, amps
                            )
                            if fut and not fut.done():
                                fut.set_result(result)
                        except Exception as e:
                            if fut and not fut.done():
                                fut.set_exception(e)
                            raise

                    status = await charger.get_status_from_client(client, config.CHARGER_MAC)
                    if "_parse_error" in status:
                        log.warning("charger status parse error: %s", status.get("_parse_error"))
                    else:
                        _state["charger"]["data"] = status
                        _state["charger"]["updated_at"] = time.time()
                        await storage.insert_charger(status)
        except Exception as e:
            log.warning("charger loop error: %s", e)
            _state["charger"]["connected"] = False
            _state["charger"]["error"] = str(e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def _prune_loop():
    # Prune once immediately (not just after the first hour) so the size
    # cap is already enforced on startup, not left to grow unbounded for
    # up to an hour first. Same reasoning for the all-time solar
    # accumulator running here too - it needs to catch up before any
    # pruned data would otherwise be lost to the retention window.
    while True:
        try:
            await storage.prune()
        except Exception as e:
            log.warning("prune error: %s", e)
        try:
            await storage.accumulate_alltime_solar_wh()
        except Exception as e:
            log.warning("solar all-time accumulation error: %s", e)
        await asyncio.sleep(3600)


async def run():
    await storage.init()
    await asyncio.gather(_battery_loop(), _charger_loop(), _prune_loop())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
