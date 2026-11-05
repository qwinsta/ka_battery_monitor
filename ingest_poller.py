#!/usr/bin/env python3
"""Drop-in replacement for poller.py that's fed by HTTP pushes from an
ESP32 running its own BLE client firmware, instead of connecting to the
devices directly via bleak.

Why this exists: many travel/OpenWrt routers have no usable USB
Bluetooth path at all - a stock CSR8510 dongle's clone firmware can fail
Linux's classic-Bluetooth init sequence, and the two realistic
replacement chipsets, Realtek and Broadcom, both need kernel driver
modules this router's firmware doesn't have compiled in at all). An
ESP32 running the same protocol logic (see esp32_firmware/) as its own
BLE client sidesteps all of that - it's a mature, well-supported
platform for exactly this, and is the community-standard answer to "add
BLE to a device that doesn't do it well" (see the Home Assistant
Bluetooth Proxy pattern).

Implements exactly poller.py's public interface (latest_battery(),
latest_charger(), request_set_alt_current(), run()) so server.py works
completely unmodified against it via the same "swap the poller module"
technique already used for emulator_poller.py - see run_server_ingest.py.

Additionally implements the ingest-side functions server.py's new
/api/ingest/* routes call: record_battery(), record_charger(),
get_pending_alt_current(), report_alt_current_result().

Connection state ("connected") is derived from how recently data has
actually arrived, not a sticky flag - if the ESP32 loses WiFi or power,
`connected` correctly flips to False once STALE_AFTER_S elapses, rather
than showing a last-known-good state forever.
"""
# X | None union syntax below needs this on Python 3.9 (the router's
# version) - see server.py's matching comment for why.
from __future__ import annotations

import asyncio
import logging
import time

import storage

log = logging.getLogger("ingest_poller")

STALE_AFTER_S = 10.0

_state = {
    "battery": {"data": None, "updated_at": None, "error": None},
    "charger": {"data": None, "updated_at": None, "error": None},
}

# At most one alt-current command in flight at a time - the dashboard's
# slider is debounced client-side, so this isn't a real limitation.
_pending_alt_current: dict | None = None  # {"id": int, "value": int}
_pending_futures: dict[int, asyncio.Future] = {}
_next_command_id = 1


def _with_connected(entry: dict) -> dict:
    connected = entry["updated_at"] is not None and (time.time() - entry["updated_at"]) < STALE_AFTER_S
    return {**entry, "connected": connected}


def latest_battery() -> dict:
    return _with_connected(_state["battery"])


def latest_charger() -> dict:
    return _with_connected(_state["charger"])


async def record_battery(data: dict):
    _state["battery"]["data"] = data
    _state["battery"]["updated_at"] = time.time()
    _state["battery"]["error"] = None
    await storage.insert_battery(data)


async def record_charger(data: dict):
    _state["charger"]["data"] = data
    _state["charger"]["updated_at"] = time.time()
    _state["charger"]["error"] = None
    await storage.insert_charger(data)


async def request_set_alt_current(amps: int) -> dict:
    """Called by the web server (POST /api/alt-current). Queues the
    command for the ESP32's next poll of GET /api/pending-alt-current and
    waits for it to report a result via POST /api/ingest/alt-current-result.
    server.py wraps this call in its own 20s timeout, same as it does for
    the real bleak-based poller.py - if the ESP32 is offline, this simply
    never resolves and that outer timeout fires."""
    global _pending_alt_current, _next_command_id
    command_id = _next_command_id
    _next_command_id += 1

    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _pending_futures[command_id] = fut
    _pending_alt_current = {"id": command_id, "value": amps}
    try:
        return await fut
    finally:
        _pending_futures.pop(command_id, None)
        if _pending_alt_current and _pending_alt_current["id"] == command_id:
            _pending_alt_current = None


def get_pending_alt_current() -> dict | None:
    """Called by the web server's GET /api/pending-alt-current, which the
    ESP32 polls every second or two. Returns None if nothing's pending."""
    return _pending_alt_current


async def report_alt_current_result(command_id: int, result: dict | None, error: str | None):
    """Called by the web server's POST /api/ingest/alt-current-result once
    the ESP32 has actually executed a command."""
    fut = _pending_futures.get(command_id)
    if fut is None or fut.done():
        return  # already timed out on our side, or a stale/duplicate report
    if error is not None:
        fut.set_exception(RuntimeError(error))
    else:
        fut.set_result(result or {})


# Last battery/charger BLE address the ESP32 has actually confirmed
# using, as opposed to whatever's merely saved in storage.settings (see
# server.py's BATTERY_MAC_KEY/CHARGER_MAC_KEY) - None until the ESP32
# (running firmware new enough to report it - see main.cpp's
# checkDeviceConfig()) has acked at least once. Deliberately a simple
# "last known" value rather than a pending-command-with-future like
# alt-current above: a device address is ongoing configuration state the
# ESP32 re-polls and re-applies on its own schedule (including after a
# reboot), not a one-shot action with an immediate result to wait on, so
# there's nothing for a request to block on here - the Settings page
# polls api_get_device_addresses() instead (per Quinn 2026-10-14).
_device_config_ack: dict = {"battery_mac": None, "charger_mac": None}


async def record_device_config_ack(battery_mac: str | None, charger_mac: str | None):
    """Called by POST /api/ingest/device-config-ack. Only updates
    whichever field was actually reported - the ESP32 may reconnect to
    (and ack) the battery and charger at different times, not
    necessarily together."""
    if battery_mac is not None:
        _device_config_ack["battery_mac"] = battery_mac
    if charger_mac is not None:
        _device_config_ack["charger_mac"] = charger_mac


def get_device_config_ack() -> dict:
    return dict(_device_config_ack)


# One-off BLE scan request/result - same queue+wait pattern as
# request_set_alt_current() above, not the config_ack pattern above it:
# a scan IS a one-shot action with an immediate result to wait on (per
# Quinn 2026-10-14, unlike ongoing device-address config). Deliberately
# disconnects the ESP32's existing battery/charger connections while it
# runs (see main.cpp's checkPendingScan()) - acceptable because this is
# a rare, deliberate "help me find my devices" setup action, not
# something run during normal operation (confirmed with Quinn
# 2026-10-14: "I'm ok with the ESP32 dropping existing connections as
# this is only a once off first time start").
_pending_scan: dict | None = None  # {"id": int}
_scan_futures: dict[int, asyncio.Future] = {}
_next_scan_id = 1


async def request_ble_scan() -> list[dict]:
    """Called by the web server (POST /api/scan-devices). Queues a scan
    for the ESP32's next poll of GET /api/pending-scan and waits for it
    to report results via POST /api/ingest/scan-result. server.py wraps
    this in its own generous timeout (a scan takes several seconds of
    actual radio time, plus however long until the ESP32's next poll) -
    if the ESP32 is offline, this simply never resolves and that outer
    timeout fires, same as request_set_alt_current()."""
    global _pending_scan, _next_scan_id
    scan_id = _next_scan_id
    _next_scan_id += 1

    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _scan_futures[scan_id] = fut
    _pending_scan = {"id": scan_id}
    try:
        return await fut
    finally:
        _scan_futures.pop(scan_id, None)
        if _pending_scan and _pending_scan["id"] == scan_id:
            _pending_scan = None


def get_pending_scan() -> dict | None:
    """Called by the web server's GET /api/pending-scan, polled by the
    ESP32 at the same cadence as /api/pending-alt-current (cheap no-op
    when nothing's pending)."""
    return _pending_scan


async def report_scan_result(scan_id: int, devices: list[dict] | None, error: str | None):
    """Called by the web server's POST /api/ingest/scan-result once the
    ESP32 has actually finished scanning (or failed to)."""
    fut = _scan_futures.get(scan_id)
    if fut is None or fut.done():
        return  # already timed out on our side, or a stale/duplicate report
    if error is not None:
        fut.set_exception(RuntimeError(error))
    else:
        fut.set_result(devices or [])


# One-off ESP32 restart request/ack - same queue+wait pattern as the scan
# above. Added after OTA repeatedly failed to land while the battery BLE
# connection had been down for 16+ hours: OTA pushes the
# whole ~1.2MB firmware image over a UDP handshake, which this project
# has repeatedly found unreliable off the direct LAN; this is a single
# tiny poll+ack over the SAME HTTP channel every other command already
# uses successfully, so it doesn't inherit that problem. A full
# ESP.restart() (not a narrower "just reinit NimBLE" attempt) - simplest
# and matches exactly what a manual power-cycle already does, which is
# the known-good recovery action for this (per Quinn 2026-11-06, in
# response to exactly this situation).
_pending_restart: dict | None = None  # {"id": int}
_restart_futures: dict[int, asyncio.Future] = {}
_next_restart_id = 1


async def request_ble_restart() -> None:
    """Called by the web server (POST /api/restart-ble). Queues a restart
    for the ESP32's next poll of GET /api/pending-restart and waits for
    it to ack via POST /api/ingest/restart-ack - that ack just means "I'm
    about to restart", not "I'm back up", since the device obviously
    can't report anything once it actually reboots. server.py wraps this
    in a short timeout (no scan-length radio work involved here, just
    the ESP32's next poll + a quick ack)."""
    global _pending_restart, _next_restart_id
    restart_id = _next_restart_id
    _next_restart_id += 1

    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _restart_futures[restart_id] = fut
    _pending_restart = {"id": restart_id}
    try:
        await fut
    finally:
        _restart_futures.pop(restart_id, None)
        if _pending_restart and _pending_restart["id"] == restart_id:
            _pending_restart = None


def get_pending_restart() -> dict | None:
    """Called by the web server's GET /api/pending-restart, polled by the
    ESP32 at the same cadence as /api/pending-alt-current."""
    return _pending_restart


async def report_restart_ack(restart_id: int):
    """Called by the web server's POST /api/ingest/restart-ack right
    before the ESP32 calls ESP.restart() - see main.cpp's
    checkPendingRestart()."""
    fut = _restart_futures.get(restart_id)
    if fut is None or fut.done():
        return
    fut.set_result(None)


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
    """No BLE connections to maintain here - just storage init and the
    periodic retention prune. All the real work happens in the
    /api/ingest/* HTTP handlers as pushes arrive."""
    await storage.init()
    await _prune_loop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
