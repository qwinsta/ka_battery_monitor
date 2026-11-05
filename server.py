#!/usr/bin/env python3
"""FastAPI dashboard server. Runs the poller as a background task and
serves the live snapshot / history / alt-current-control API plus the
static frontend.

Run with: uvicorn server:app --host 0.0.0.0 --port 8080
"""
# X | None union syntax needs this on Python 3.9 (the router's version) -
# without it, that syntax is evaluated at runtime and raises TypeError
# there rather than just being a type hint. No effect on newer Python.
# NOTE: this alone isn't enough for FastAPI route signatures or Pydantic
# model fields specifically - both force-resolve string annotations back
# into real type objects at runtime (for request validation/OpenAPI
# schema), which hits the exact same Python 3.9 evaluation problem one
# level removed. Those specific spots use typing.Optional instead - see
# api_history() and AltCurrentResultReport below. Confirmed the hard way
# deploying to a real OpenWrt router running Python 3.9.
from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import poller
import storage

# Absolute, not "static" - a relative path resolves against the process's
# cwd, which depends on how/where this gets launched from (confirmed the
# hard way: procd on the router doesn't default it to the script's own
# directory, so "static" raised RuntimeError: Directory 'static' does not
# exist). __file__ makes this correct regardless of launch method.
STATIC_DIR = Path(__file__).resolve().parent / "static"

RANGE_SECONDS = {
    "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "6h": 6 * 3600,
    "12h": 12 * 3600, "24h": 24 * 3600, "7d": 7 * 86400, "10d": 10 * 86400,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poller.run())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)


def _compute_total_power_usage_w(battery_data: dict | None, charger_data: dict | None) -> float | None:
    """The battery's own reported power and the charger's own reported
    *battery-side* power ("batt" in its status response - see
    PROTOCOL.md - a real sensor reading at the charger's own
    battery-side terminals, not derived from the battery's BMS at all)
    are two independent measurements of the same node. The difference
    approximates total power being drawn by everything else connected
    to that bus, whether it's fed straight from the charger's output or
    via the battery.

    No conversion-efficiency correction needed here (unlike the old
    version, which compared the charger's ALTERNATOR/SOLAR-side input
    power against the battery with a fixed assumed efficiency, per
    Quinn 2026-11-06) - both readings being differenced are already on
    the battery side of the charger's internal DC-DC conversion, so
    there's no cross-side gap to correct for, and this is a real
    measurement rather than a guess.

    Used as a fallback when there's no history yet to average (see
    storage.avg_total_power_usage for the smoothed version used normally)."""
    if not battery_data or not charger_data:
        return None
    result = charger_data.get("result")
    if not result:
        return None
    batt = result.get("batt", {})
    charger_battery_side_w = batt.get("p") or 0
    battery_net_w = battery_data.get("power") or 0
    return round(charger_battery_side_w - battery_net_w, 1)


_SERVER_START_TIME = time.time()
_total_power_1hr_cache: dict = {"value": None, "computed_at": 0.0}
_TOTAL_POWER_1HR_CACHE_TTL_S = 5 * 60


async def _get_total_power_1hr_avg() -> float | None:
    """The 1-hour average is inherently slow-moving - recomputing it on
    every 1-second dashboard poll is unnecessary DB work (per Quinn
    2026-09-30, who suggested a 5-minute refresh is plenty).

    Withheld (returns None, shown as "--") until the process has actually
    been running for a full hour - otherwise, right after a restart
    (which happens on every deploy), this would silently average
    whatever's accumulated so far - a couple of minutes, say - while
    still labeled "1-hour avg", which reads as a real hour-long average
    when it isn't one (per Quinn 2026-09-30)."""
    if time.time() - _SERVER_START_TIME < 3600:
        return None
    now = time.time()
    if now - _total_power_1hr_cache["computed_at"] >= _TOTAL_POWER_1HR_CACHE_TTL_S:
        value = await storage.avg_total_power_usage(60)
        _total_power_1hr_cache["value"] = round(value, 1) if value is not None else None
        _total_power_1hr_cache["computed_at"] = now
    return _total_power_1hr_cache["value"]


async def _compute_time_remaining(battery_data: dict | None) -> dict:
    """Time to full (while charging) or time to empty (while
    discharging), using our own confirmed-correct nominal_ah/remaining_ah
    read straight off the BMS - same math the KickAss app's own "Time to
    Full" tries to do (and gets nonsense for), but also
    covering the discharge direction, which the app doesn't show at all.

    Time-to-empty uses remaining_ah (what's actually left), not
    nominal_ah - that's the physically correct basis for "how long has it
    got", as opposed to time-to-full which is naturally based on the gap
    between nominal and remaining.

    Both which mode to show AND the hours estimate use the 2-minute
    average current, not the instantaneous reading - using the
    instantaneous value for just the mode decision (first attempt,
    2026-09-18) still let the readout flicker to "--" whenever the noisy
    live current crossed zero, even while the smoothed average clearly
    hadn't. One signal, used consistently, fixes that.

    Same reasoning as the 1-hour average's startup gate: right after a
    restart, a "2-minute average" computed from only a few seconds of
    real data isn't really a 2-minute average yet - nearly as noisy as
    the instantaneous reading it's meant to replace. Withheld until the
    process has genuinely been up for 2 minutes (per Quinn 2026-09-30)."""
    if not battery_data:
        return {"hours": None, "mode": None}
    if time.time() - _SERVER_START_TIME < 120:
        # Distinct from the generic "nothing to show" (mode: None) below -
        # this is a transient state that resolves on its own shortly, so
        # the frontend shows "Calculating..." here specifically rather
        # than the same "--" it shows when there's genuinely no
        # meaningful charge/discharge happening (per Quinn 2026-09-30).
        return {"hours": None, "mode": "warming_up"}
    avg_current = await storage.avg_battery_current(2)
    if avg_current is None:
        return {"hours": None, "mode": None}
    if avg_current > 0.1:
        needed_ah = (battery_data.get("nominal_ah") or 0) - (battery_data.get("remaining_ah") or 0)
        if needed_ah <= 0:
            return {"hours": 0.0, "mode": "full"}
        return {"hours": round(needed_ah / avg_current, 2), "mode": "to_full"}
    elif avg_current < -0.1:
        remaining_ah = battery_data.get("remaining_ah") or 0
        return {"hours": round(remaining_ah / abs(avg_current), 2), "mode": "to_empty"}
    return {"hours": None, "mode": None}


async def _compute_charge_source_estimate(battery_data: dict | None) -> dict:
    """Which source is actually charging the battery right now -
    alternator or solar - and, for the alternator case specifically, a
    live-rate "time to full" using its actual measured output rather
    than a fixed assumption (per Quinn 2026-11-06: "actual alternator
    output rate as this is variable" - RPM and electrical load both move
    it around, so a fixed set-point figure would be wrong most of the
    time). The two are never simultaneously active on this vehicle (same
    assumption the Settings page's alt-current control already makes),
    so this is a straight either/or, not a blend - alternator takes
    priority in the unlikely event both read as active at once.

    Solar deliberately keeps its own separate, *theoretical* "peak sun-
    hours" estimate (computeSunHoursToFull() in app.js) rather than
    getting a live-current version here too - weather is far more
    volatile than alternator RPM, so a live reading for solar would
    jitter with every passing cloud in a way that isn't actually more
    useful than the existing "roughly, assuming good sun" planning
    figure. This function only decides *which* of the two to surface;
    the frontend already has what it needs for the solar number.

    Same 2-minute smoothing and startup gate as _compute_time_remaining
    above, for the same reason - both avoid flickering off noise/a
    freshly-restarted process's not-yet-meaningful average."""
    if not battery_data or time.time() - _SERVER_START_TIME < 120:
        return {"source": None, "alt_hours": None}
    avg_alt, avg_panel = await asyncio.gather(storage.avg_alt_current(2), storage.avg_panel_current(2))
    alt_active = avg_alt is not None and avg_alt > 0.1
    panel_active = avg_panel is not None and avg_panel > 0.1
    if alt_active:
        needed_ah = (battery_data.get("nominal_ah") or 0) - (battery_data.get("remaining_ah") or 0)
        if needed_ah <= 0:
            return {"source": "alternator", "alt_hours": 0.0}
        # The alternator's own (input-side) current isn't what the
        # battery actually receives - use the charger's own reported
        # *battery-side* current instead (see avg_batt_current - a real
        # sensor reading at the charger's own battery-side terminals,
        # per PROTOCOL.md), not a guessed conversion efficiency applied
        # to the alternator side (per Quinn 2026-11-06, same reasoning
        # as _compute_total_power_usage_w).
        battery_side_current = await storage.avg_batt_current(2)
        if not battery_side_current or battery_side_current <= 0:
            return {"source": "alternator", "alt_hours": None}
        return {"source": "alternator", "alt_hours": round(needed_ah / battery_side_current, 2)}
    if panel_active:
        return {"source": "solar", "alt_hours": None}
    return {"source": None, "alt_hours": None}


@app.get("/api/latest")
async def api_latest():
    b = poller.latest_battery()
    c = poller.latest_charger()
    time_remaining = await _compute_time_remaining(b["data"])
    charge_source = await _compute_charge_source_estimate(b["data"])

    # Live instantaneous reading, plus 2-minute and 1-hour smoothed
    # averages shown alongside it - the instantaneous number is naturally
    # noisy sample-to-sample, the 2-min average is what actually settles
    # near 0 once the charger-loss correction is accounted for, and the
    # 1-hour average smooths out even the slower swings (a cloud passing
    # over the panel, someone running the fridge compressor for a few
    # minutes) to show the genuinely sustained draw (per Quinn 2026-09-30).
    total_power_instant_w = _compute_total_power_usage_w(b["data"], c["data"])
    # Same startup gate as _compute_time_remaining's own 2-minute average,
    # and for the same reason - a "2-minute average" computed from only a
    # few seconds of real data isn't one yet. These are two independent
    # code paths that both average over 2 minutes; both need the gate, or
    # withholding one while the other shows a number just looks like a
    # bug (confirmed live 2026-09-30 - this one was missed the first time).
    if time.time() - _SERVER_START_TIME < 120:
        total_power_2min_avg_w = None
    else:
        total_power_2min_avg_w = await storage.avg_total_power_usage(2)
        if total_power_2min_avg_w is not None:
            total_power_2min_avg_w = round(total_power_2min_avg_w, 1)
    total_power_1hr_avg_w = await _get_total_power_1hr_avg()

    return {
        "battery": b["data"],
        "battery_connected": b["connected"],
        "battery_updated_at": b["updated_at"],
        "battery_error": b["error"],
        "charger": c["data"],
        "charger_connected": c["connected"],
        "charger_updated_at": c["updated_at"],
        "charger_error": c["error"],
        "total_power_usage_w": total_power_instant_w,
        "total_power_usage_2min_avg_w": total_power_2min_avg_w,
        "total_power_usage_1hr_avg_w": total_power_1hr_avg_w,
        "time_remaining_hours": time_remaining["hours"],
        "time_remaining_mode": time_remaining["mode"],
        # Which source is actually charging right now ("alternator" /
        # "solar" / null) and, for the alternator case, a live-rate
        # hours-to-full estimate - see _compute_charge_source_estimate().
        # The solar case's own number stays client-side (app.js's
        # computeSunHoursToFull()); this only tells the frontend which
        # one to show.
        "charge_source": charge_source["source"],
        "charge_source_alt_hours": charge_source["alt_hours"],
        "alt_current_min": config.ALT_CURRENT_MIN,
        "alt_current_max": config.ALT_CURRENT_MAX,
    }


@app.get("/api/history")
async def api_history(range: Optional[str] = None, since: Optional[float] = None,
                       until: Optional[float] = None, points: int = 500):
    """Either pass `range` (a preset key) for the default window, or pass
    explicit `since`/`until` unix timestamps directly - the frontend uses
    the latter while panning left/right through history."""
    now = time.time()
    if since is not None and until is not None:
        pass  # explicit window - panning
    elif range is not None:
        if range not in RANGE_SECONDS:
            raise HTTPException(400, f"range must be one of {list(RANGE_SECONDS)}")
        since, until = now - RANGE_SECONDS[range], now
    else:
        raise HTTPException(400, "must pass either range, or since+until")

    # Run concurrently rather than one after the other - each is a
    # separate SQLite connection under the hood (asyncio.to_thread), and
    # WAL mode allows multiple simultaneous readers, so there's no reason
    # to pay for both queries' latency back-to-back (per Quinn 2026-09-30).
    battery_hist, charger_hist = await asyncio.gather(
        storage.query_battery_history(since, until, points),
        storage.query_charger_history(since, until, points),
    )
    # Bypasses FastAPI's default jsonable_encoder() pass, which
    # recursively type-inspects every individual value before handing off
    # to json.dumps() - fine for small responses, but confirmed live
    # 2026-09-30 to be the actual bottleneck here (not the DB query,
    # which measured ~50ms even for large ranges): jsonable_encoder()'s
    # generic per-value dispatch over a payload with hundreds of small
    # numeric dicts was costing several hundred additional ms on the
    # router's weak CPU. Since every value here is already a plain float/
    # int/None/dict (no Pydantic models, datetimes, etc. that would
    # actually need that encoder), a direct json.dumps() produces
    # identical output for a fraction of the cost.
    payload = json.dumps({"since": since, "until": until, "battery": battery_hist, "charger": charger_hist})
    return Response(content=payload, media_type="application/json")


@app.get("/api/data-days")
async def api_data_days(day_starts: str):
    """Powers the dashboard's day picker: given comma-separated unix
    timestamps (each a calendar day's start, in the viewer's own local
    timezone - day-boundary math belongs client-side, since this router
    has no way to know what timezone the person looking at it is in),
    returns which of those days have at least one sample."""
    starts = [float(x) for x in day_starts.split(",") if x]
    if not starts:
        raise HTTPException(400, "day_starts must be a non-empty comma-separated list")
    has_data = await storage.days_with_data(starts)
    return {"days": [{"start": s, "has_data": h} for s, h in zip(starts, has_data)]}


@app.get("/api/oldest-sample-ts")
async def api_oldest_sample_ts():
    """Lets the day pickers (main history, and the solar generation
    card's own) know exactly how far back to offer days, instead of
    guessing a fixed candidate window - retention is governed by
    storage.MAX_DB_SIZE_BYTES, not a fixed day count, so how far back
    real data reaches can move over time (per Quinn 2026-11-06)."""
    ts = await storage.oldest_sample_ts()
    return {"ts": ts}


_solar_energy_cache: dict = {}


@app.get("/api/solar-energy")
async def api_solar_energy(since: float, until: Optional[float] = None):
    """Cumulative solar panel Watt-hours in [since, until) - until
    defaults to now. Powers the header's running "today" figure and the
    Solar panel tile's Today/Yesterday/5-day/10-day totals. Both
    boundaries are the caller's own local day boundaries (unix seconds) -
    see api_data_days for why that's computed client-side.

    A window that's already fully in the past (until <= now) can never
    change again, so it's cached indefinitely once computed - the
    Yesterday/5-day/10-day tiles all use exactly this kind of "sealed"
    window and were re-running a full raw-row integration over it every
    5 minutes for every open tab, measured at ~3s of real CPU work for
    the 10-day one on this hardware. That's expensive enough to starve
    the thread pool other requests (like the 1-second /api/latest poll)
    share, which is what made the dashboard feel intermittently slow/
    unresponsive - confirmed live 2026-10-01. "Today so far" (until is
    None, i.e. still climbing) is deliberately never cached here."""
    now = time.time()
    sealed = until is not None and until <= now
    cache_key = (since, until)
    if sealed and cache_key in _solar_energy_cache:
        return {"wh": _solar_energy_cache[cache_key]}
    wh = round(await storage.solar_energy_wh(since, until), 1)
    if sealed:
        _solar_energy_cache[cache_key] = wh
    return {"wh": wh}


@app.get("/api/solar-energy-by-day")
async def api_solar_energy_by_day(day_starts: str):
    """Batched version of /api/solar-energy: one Wh total per given
    local-midnight day start, computed concurrently rather than N
    separate round trips - powers the solar generation card's "Last 30
    days" overview (per the 2026-10-14 incident follow-up: continuous
    multi-day *history* browsing was replaced with picking a single
    calendar day at a time, and this is the equivalent for the solar
    generation chart's own "show me a wider trend" case - one bounded
    request for up to 30 single-day totals, not an open-ended scrollable
    window).

    Reuses _solar_energy_cache (see api_solar_energy above) for any
    requested day that's already sealed and cached - a day that's been
    looked at before (e.g. via the day picker or a previous overview
    load) costs nothing to include here again, and one computed here
    populates the cache for next time either endpoint asks for it."""
    starts = [float(x) for x in day_starts.split(",") if x]
    if not starts:
        raise HTTPException(400, "day_starts must be a non-empty comma-separated list")

    now = time.time()
    wh_by_start: dict[float, float] = {}
    to_compute = []  # (start, until, sealed)
    for s in starts:
        until = s + 86400
        sealed = until <= now
        if sealed and (s, until) in _solar_energy_cache:
            wh_by_start[s] = _solar_energy_cache[(s, until)]
        else:
            to_compute.append((s, until, sealed))

    if to_compute:
        computed = await asyncio.gather(
            *(storage.solar_energy_wh(s, u) for s, u, _ in to_compute)
        )
        for (s, u, sealed), wh in zip(to_compute, computed):
            wh = round(wh, 1)
            if sealed:
                _solar_energy_cache[(s, u)] = wh
            wh_by_start[s] = wh

    return {"days": [{"start": s, "wh": wh_by_start[s]} for s in starts]}


@app.get("/api/solar-energy-alltime")
async def api_solar_energy_alltime():
    """A running total that never resets and outlives the raw-sample
    retention window (size-bound, not a fixed day count - see
    storage.py's module docstring) - see
    storage.accumulate_alltime_solar_wh() for how it's kept up to date
    incrementally rather than recomputed from (pruned) history (per
    Quinn 2026-09-30)."""
    wh = await storage.get_alltime_solar_wh()
    return {"wh": round(wh, 1)}


SOLAR_PANEL_MAX_W_KEY = "solar_panel_max_w"
SOLAR_PANEL_MAX_W_DEFAULT = 200


@app.get("/api/settings/solar-panel-max-w")
async def api_get_solar_panel_max_w():
    value = await storage.get_setting(SOLAR_PANEL_MAX_W_KEY, str(SOLAR_PANEL_MAX_W_DEFAULT))
    return {"max_w": int(value)}


class SolarPanelMaxWRequest(BaseModel):
    max_w: int = Field(gt=0, le=100000)


@app.post("/api/settings/solar-panel-max-w")
async def api_set_solar_panel_max_w(req: SolarPanelMaxWRequest):
    """Persisted server-side (not localStorage) so every device viewing
    the dashboard sees the same panel capacity, not just whichever
    browser it was last changed from. Drives the solar generation
    chart's fixed 0-to-capacity y-axis (per Quinn 2026-09-30 - kept
    configurable rather than hardcoded to the current 200W panel, since
    it may be upgraded later)."""
    await storage.set_setting(SOLAR_PANEL_MAX_W_KEY, str(req.max_w))
    return {"ok": True, "max_w": req.max_w}


# --- Device (BLE) addresses ---
# Which real battery/charger this software connects to - previously only
# changeable by editing esp32_firmware/include/config.h and reflashing.
# Moving it here (same generic settings store as solar_panel_max_w above)
# means: (1) the current address is visible from the Settings page
# instead of having to go dig through firmware source, and (2) someone
# running this project against their OWN battery/charger - a different
# vehicle, or a replacement unit - can point it at their devices without
# touching firmware at all. The ESP32 bridge picks up a change via
# api_device_config() below (per Quinn 2026-10-14 - see
# esp32_firmware/src/main.cpp's checkDeviceConfig()). Defaults match
# today's real, compiled-in addresses so an existing install keeps
# working unchanged until someone actually edits this.
BATTERY_MAC_KEY = "battery_mac_address"
CHARGER_MAC_KEY = "charger_mac_address"
BATTERY_MAC_DEFAULT = "1C:C8:C1:4E:58:CE"
CHARGER_MAC_DEFAULT = "E0:53:19:41:1E:51"

MAC_ADDRESS_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def _normalize_mac(raw: str) -> str:
    mac = raw.strip().upper()
    if not MAC_ADDRESS_RE.match(mac):
        raise HTTPException(400, f"'{raw}' is not a BLE MAC address (expected AA:BB:CC:DD:EE:FF)")
    return mac


def _device_config_ack() -> dict:
    # Only ingest_poller.py (the ESP32 bridge) implements this - the
    # direct-bleak poller.py has no separate device to confirm back from
    # (it IS the thing connecting), so this degrades gracefully to "can't
    # confirm" there rather than erroring the whole settings page out.
    getter = getattr(poller, "get_device_config_ack", None)
    return getter() if getter else {"battery_mac": None, "charger_mac": None}


@app.get("/api/settings/device-addresses")
async def api_get_device_addresses():
    """battery_mac_confirmed/charger_mac_confirmed reflect whether the
    ESP32 bridge has actually reported back using this exact address
    (see api_ingest_device_config_ack below) - not just that it's saved
    here. Per Quinn 2026-10-14: "there needs to be validation that ID
    gets sent to the ESP32", since a saved-but-unconfirmed address could
    just as easily mean a typo'd MAC the device can never actually
    connect to. Firmware predating this feature never sends that ack, so
    this correctly (and honestly) reads as unconfirmed until it's
    updated - see esp32_firmware/src/main.cpp's checkDeviceConfig()."""
    battery_mac = await storage.get_setting(BATTERY_MAC_KEY, BATTERY_MAC_DEFAULT)
    charger_mac = await storage.get_setting(CHARGER_MAC_KEY, CHARGER_MAC_DEFAULT)
    ack = _device_config_ack()
    return {
        "battery_mac": battery_mac,
        "charger_mac": charger_mac,
        "battery_mac_confirmed": ack.get("battery_mac") == battery_mac,
        "charger_mac_confirmed": ack.get("charger_mac") == charger_mac,
        # Lets the Settings page offer a "go back to a previous device"
        # dropdown - see storage.record_device_mac_history() (per Quinn
        # 2026-10-14).
        "battery_mac_history": await storage.get_device_mac_history("battery"),
        "charger_mac_history": await storage.get_device_mac_history("charger"),
    }


class DeviceAddressesRequest(BaseModel):
    # Optional, not required together - the Settings page's Battery and
    # Charger fields each have their own independent Apply button
    # (2026-10-14), so a request only ever carries the one field actually
    # being changed; the other stays whatever it already was.
    battery_mac: Optional[str] = None
    charger_mac: Optional[str] = None
    # Only meaningfully set when applying a value picked from a scan
    # result (which has an advertised name) rather than hand-typed - used
    # purely to make that address's history entry recognizable later
    # ("KADCDC-1234" beats a bare MAC), never validated or otherwise
    # acted on.
    battery_name: Optional[str] = None
    charger_name: Optional[str] = None


@app.post("/api/settings/device-addresses")
async def api_set_device_addresses(req: DeviceAddressesRequest):
    if req.battery_mac is None and req.charger_mac is None:
        raise HTTPException(400, "must provide battery_mac and/or charger_mac")
    if req.battery_mac is not None:
        battery_mac = _normalize_mac(req.battery_mac)
        await storage.set_setting(BATTERY_MAC_KEY, battery_mac)
        await storage.record_device_mac_history("battery", battery_mac, req.battery_name)
    else:
        battery_mac = await storage.get_setting(BATTERY_MAC_KEY, BATTERY_MAC_DEFAULT)
    if req.charger_mac is not None:
        charger_mac = _normalize_mac(req.charger_mac)
        await storage.set_setting(CHARGER_MAC_KEY, charger_mac)
        await storage.record_device_mac_history("charger", charger_mac, req.charger_name)
    else:
        charger_mac = await storage.get_setting(CHARGER_MAC_KEY, CHARGER_MAC_DEFAULT)
    return {"ok": True, "battery_mac": battery_mac, "charger_mac": charger_mac}


class AltCurrentRequest(BaseModel):
    value: int = Field(ge=config.ALT_CURRENT_MIN, le=config.ALT_CURRENT_MAX)


@app.post("/api/alt-current")
async def api_set_alt_current(req: AltCurrentRequest):
    try:
        result = await asyncio.wait_for(poller.request_set_alt_current(req.value), timeout=20.0)
    except asyncio.TimeoutError:
        raise HTTPException(504, "charger did not respond in time")
    except Exception as e:
        raise HTTPException(502, str(e))
    return result


@app.post("/api/scan-devices")
async def api_scan_devices():
    """Triggers a one-off BLE scan on the ESP32 bridge for the Settings
    page's device-discovery tool - lets someone setting this up for the
    first time (their own battery/charger, a different vehicle, a
    replacement unit) find the right MAC addresses without needing
    ble_tool.py and a second BLE-capable machine. Disconnects the
    ESP32's existing battery/charger connections for the scan's duration
    (see main.cpp's checkPendingScan()) - acceptable since this is a
    rare, deliberate action, not something run during normal operation
    (confirmed with Quinn 2026-10-14). A generous timeout: the ESP32
    needs several seconds of actual scan time plus however long until
    its next poll of /api/pending-scan, well past alt-current's 20s."""
    request_scan = getattr(poller, "request_ble_scan", None)
    if request_scan is None:
        raise HTTPException(501, "active poller module doesn't support BLE scanning")
    try:
        devices = await asyncio.wait_for(request_scan(), timeout=45.0)
    except asyncio.TimeoutError:
        raise HTTPException(504, "ESP32 did not respond with scan results in time")
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"devices": devices}


@app.post("/api/restart-ble")
async def api_restart_ble():
    """Remotely restarts the ESP32 bridge - the Settings page's recovery
    button for when battery/charger data goes missing or stale and won't
    come back on its own (per Quinn 2026-11-06, after a battery BLE
    connection sat dead for 16+ hours with no way to fix it short of
    physically unplugging the device). A full restart, not a narrower
    "just the BLE stack" attempt - matches exactly what a manual power-
    cycle already does, the known-good fix for this.

    Deliberately NOT done via OTA (pushing the firmware image again just
    to force a reboot) - OTA transfers the whole ~1.2MB image over a UDP
    handshake this project has repeatedly found unreliable off the
    direct LAN (3/3 in-session attempts failed on 2026-11-06, including
    from directly on the same WiFi). This is a single tiny poll+ack over
    the same HTTP channel every other command (alt-current, scan,
    device-config) already uses successfully, so it doesn't inherit that
    problem - see main.cpp's checkPendingRestart().

    Short timeout: there's no scan-length radio work here, just the
    ESP32's next poll of /api/pending-restart (same cadence as alt-
    current) plus a quick ack - if it doesn't land within that, the
    ESP32 almost certainly isn't reachable at all right now, in which
    case a longer timeout wouldn't help either."""
    request_restart = getattr(poller, "request_ble_restart", None)
    if request_restart is None:
        raise HTTPException(501, "active poller module doesn't support remote restart")
    try:
        await asyncio.wait_for(request_restart(), timeout=10.0)
    except asyncio.TimeoutError:
        raise HTTPException(504, "ESP32 did not acknowledge the restart in time - it may be offline")
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


# ---------------------------------------------------------------------
# Ingest API - for the ESP32 BLE bridge (see ingest_poller.py and
# esp32_firmware/). Only meaningful when the active `poller` module has
# been swapped to ingest_poller (see run_server_ingest.py) - these routes
# just delegate to whatever module `poller` currently points at, exactly
# like every other route in this file already does.
# ---------------------------------------------------------------------

@app.post("/api/ingest/battery")
async def api_ingest_battery(data: dict):
    await poller.record_battery(data)
    return {"ok": True}


@app.post("/api/ingest/charger")
async def api_ingest_charger(data: dict):
    await poller.record_charger(data)
    return {"ok": True}


@app.get("/api/device-config")
async def api_device_config():
    """Polled by the ESP32 bridge at a slow, infrequent cadence (this
    almost never changes) so a battery/charger MAC address edited in the
    Settings page's Device Connections section takes effect without
    reflashing firmware - see esp32_firmware/src/main.cpp's
    checkDeviceConfig() and api_get_device_addresses() above, which this
    deliberately mirrors (kept as its own route rather than reused
    directly, same as /api/pending-alt-current below being separate from
    any browser-facing equivalent - see this section's own header
    comment) (per Quinn 2026-10-14)."""
    battery_mac = await storage.get_setting(BATTERY_MAC_KEY, BATTERY_MAC_DEFAULT)
    charger_mac = await storage.get_setting(CHARGER_MAC_KEY, CHARGER_MAC_DEFAULT)
    return {"battery_mac": battery_mac, "charger_mac": charger_mac}


@app.post("/api/ingest/device-config-ack")
async def api_ingest_device_config_ack(req: DeviceAddressesRequest):
    """Called by the ESP32 bridge once it has actually applied a
    battery/charger BLE address it read from GET /api/device-config (see
    main.cpp's checkDeviceConfig()) - this, not the save itself, is what
    api_get_device_addresses()'s battery_mac_confirmed/
    charger_mac_confirmed fields are based on. Accepts either field
    independently, same as the settings POST above, since the ESP32
    reports whichever one it actually just reconnected with, not
    necessarily both at once."""
    ack = getattr(poller, "record_device_config_ack", None)
    if ack is None:
        raise HTTPException(501, "active poller module doesn't support device config acks")
    await ack(req.battery_mac, req.charger_mac)
    return {"ok": True}


@app.get("/api/pending-alt-current")
async def api_pending_alt_current():
    """Polled by the ESP32 every second or two. Deliberately outbound-only
    from the ESP32's side (poll, don't listen) - no inbound exposure on
    the device to worry about."""
    pending = poller.get_pending_alt_current()
    if pending is None:
        return {"pending": False}
    return {"pending": True, "id": pending["id"], "value": pending["value"]}


class AltCurrentResultReport(BaseModel):
    id: int
    result: Optional[dict] = None
    error: Optional[str] = None


@app.post("/api/ingest/alt-current-result")
async def api_ingest_alt_current_result(req: AltCurrentResultReport):
    await poller.report_alt_current_result(req.id, req.result, req.error)
    return {"ok": True}


@app.get("/api/pending-scan")
async def api_pending_scan():
    """Polled by the ESP32 at the same cadence as /api/pending-alt-current -
    see main.cpp's checkPendingScan(). Cheap no-op when nothing's
    pending, which is nearly always (a scan is a rare, deliberate setup
    action - per Quinn 2026-10-14)."""
    getter = getattr(poller, "get_pending_scan", None)
    pending = getter() if getter else None
    if pending is None:
        return {"pending": False}
    return {"pending": True, "id": pending["id"]}


class ScannedDevice(BaseModel):
    name: str
    address: str
    rssi: int
    # "battery" / "charger" / null - set by the ESP32's own name-hint
    # matching (see config.h's SCAN_*_NAME_HINTS, mirroring ble_tool.py's
    # TARGET_NAME_HINTS), not re-derived here - the firmware is the one
    # that actually saw the advertisement.
    likely_type: Optional[str] = None


class ScanResultReport(BaseModel):
    id: int
    devices: Optional[list[ScannedDevice]] = None
    error: Optional[str] = None


@app.post("/api/ingest/scan-result")
async def api_ingest_scan_result(req: ScanResultReport):
    report = getattr(poller, "report_scan_result", None)
    if report is None:
        raise HTTPException(501, "active poller module doesn't support BLE scanning")
    devices = [d.model_dump() for d in req.devices] if req.devices is not None else None
    await report(req.id, devices, req.error)
    return {"ok": True}


@app.get("/api/pending-restart")
async def api_pending_restart():
    """Polled by the ESP32 at the same cadence as /api/pending-alt-current -
    see main.cpp's checkPendingRestart() and api_restart_ble() above."""
    getter = getattr(poller, "get_pending_restart", None)
    pending = getter() if getter else None
    if pending is None:
        return {"pending": False}
    return {"pending": True, "id": pending["id"]}


class RestartAckReport(BaseModel):
    id: int


@app.post("/api/ingest/restart-ack")
async def api_ingest_restart_ack(req: RestartAckReport):
    """Called by the ESP32 right before it calls ESP.restart() - this ack
    just unblocks the waiting POST /api/restart-ble request on the
    Settings page; it can't mean "I'm back up" since the device is about
    to go dark for a few seconds."""
    ack = getattr(poller, "report_restart_ack", None)
    if ack is None:
        raise HTTPException(501, "active poller module doesn't support remote restart")
    await ack(req.id)
    return {"ok": True}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Browsers (mobile Safari especially) cache app.js/style.css aggressively
# enough that a deploy alone doesn't guarantee a viewer actually gets the
# new version - confirmed repeatedly the hard way this session (stale
# CHARGE_STAGE_LABELS, a stale chart layout mismatched against newer
# HTML). Rather than relying on everyone remembering to hard-refresh
# after every update, index.html's own asset references get a cache-
# busting ?v=<mtime> query string, sourced from the files' own
# modification times - browsers treat a changed URL as a new resource
# and fetch it fresh, while still caching normally between deploys since
# the URL only changes when the file actually does.
_index_cache: dict = {"html": None, "mtime_key": None}


def _render_index() -> str:
    app_js_mtime = int((STATIC_DIR / "app.js").stat().st_mtime)
    style_css_mtime = int((STATIC_DIR / "style.css").stat().st_mtime)
    # index.html's own mtime has to be part of the cache key too, not
    # just the two assets it links to - missed this the first time
    # (2026-09-30), so an index.html-only edit (no app.js/style.css
    # change alongside it) kept serving the stale cached version
    # indefinitely. Confirmed live 2026-10-01 adding a footer link.
    index_html_mtime = int((STATIC_DIR / "index.html").stat().st_mtime)
    mtime_key = (app_js_mtime, style_css_mtime, index_html_mtime)
    if _index_cache["mtime_key"] != mtime_key:
        html = (STATIC_DIR / "index.html").read_text()
        html = html.replace('/static/app.js"', f'/static/app.js?v={app_js_mtime}"')
        html = html.replace('/static/style.css"', f'/static/style.css?v={style_css_mtime}"')
        _index_cache["html"] = html
        _index_cache["mtime_key"] = mtime_key
    return _index_cache["html"]


@app.get("/")
async def index():
    return HTMLResponse(_render_index())
