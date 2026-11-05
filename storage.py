#!/usr/bin/env python3
"""SQLite storage for battery/charger history.

Design notes:
- Raw samples are kept until either a (generous, effectively backstop-
  only) time-based retention window or MAX_DB_SIZE_BYTES (25MB, per
  Quinn 2026-09-18) is reached, whichever comes first - this is meant to
  run on an OpenWrt router with limited NAND flash, so unbounded
  retention isn't appropriate.
  DEFAULT_RETENTION_DAYS used to be the real-world constraint (10 days,
  tighter than the size cap at the sample rates seen so far), but per
  Quinn 2026-10-14: how far back the day picker can usefully go should
  track how much data actually *fits* in 25MB, not an arbitrary fixed
  day count - a lower sample rate (or a bigger cap in future) should let
  history reach further back on its own, not get cut off at exactly 10
  days while there's still plenty of room left. DEFAULT_RETENTION_DAYS
  is now set well past what 25MB could ever hold at any realistic
  sample rate, so MAX_DB_SIZE_BYTES is the constraint that actually
  binds in practice - the days figure is just a sane outer backstop
  (e.g. against a future bug that let the size cap stop enforcing).
- Deleting rows alone doesn't shrink a SQLite file (freed pages are
  reused internally, not released back to the OS) - see
  _enforce_size_cap_sync() for how reclaiming the file's actual on-disk
  size is handled (incremental_vacuum, not a full VACUUM - see its own
  comment for why, after the 2026-10-14 incident).
- This file (history.sqlite3, next to this script) IS the persistence -
  history survives server restarts. The only gap is while the server
  itself isn't running (no BLE connection = nothing to record), which is
  expected, not a bug.
- All writes go through asyncio.to_thread() since sqlite3 is synchronous -
  writes are small and infrequent (~1-2/sec) so this is not a bottleneck,
  and it avoids adding an extra dependency (aiosqlite) for something this
  lightweight.
- History queries downsample via bucket-averaging so a large time range
  doesn't ship hundreds of thousands of points to the browser.
"""
# X | None union syntax below needs this on Python 3.9 (the router's
# version) - see server.py's matching comment for why.
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

# Overridable so the emulator (see emulator/) can point at its own database
# and never mix scripted test data into real vehicle history.
DB_PATH = Path(os.environ.get("KA_DB_PATH", Path(__file__).parent / "history.sqlite3"))
# A backstop, not the real constraint - see the module docstring
# (2026-10-14). At every sample rate seen so far, MAX_DB_SIZE_BYTES binds
# well before 90 days does (today: ~7 days of data already hits 25MB),
# so in practice retention is governed by size, and history reaches back
# as far as it actually fits rather than stopping at a fixed day count.
DEFAULT_RETENTION_DAYS = 90
MAX_DB_SIZE_BYTES = 25 * 1024 * 1024
# Each PRAGMA incremental_vacuum(1) call reclaims one page (~4096 bytes)
# and measured at well under 1ms - 2000 steps is a worst case of a few
# seconds and a few MB per hourly prune cycle, comfortably bounded even
# on this router's constrained hardware. See _enforce_size_cap_sync.
MAX_INCREMENTAL_VACUUM_STEPS = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS battery_samples (
    ts REAL NOT NULL,
    voltage REAL, current REAL, power REAL,
    soc_percent INTEGER, remaining_ah REAL, nominal_ah REAL,
    cycles INTEGER, charging INTEGER
);
CREATE INDEX IF NOT EXISTS idx_battery_ts ON battery_samples(ts);

CREATE TABLE IF NOT EXISTS charger_samples (
    ts REAL NOT NULL,
    alt_v REAL, alt_c REAL, alt_p REAL,
    panel_v REAL, panel_c REAL, panel_p REAL,
    batt_v REAL, batt_c REAL, batt_p REAL,
    alt_input_c REAL, battery_temp REAL, charge_state INTEGER
);
CREATE INDEX IF NOT EXISTS idx_charger_ts ON charger_samples(ts);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    # VACUUM (see _enforce_size_cap_sync) needs an exclusive lock; without
    # this, it can raise "database is locked" if it lands mid-insert from
    # the battery/charger poll loops instead of just waiting them out.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_sync():
    # Schema creation belongs here, once, rather than in _connect() (run
    # on every single connection this module ever opens) - re-running
    # four CREATE-IF-NOT-EXISTS statements on every query is small but
    # real, avoidable overhead when it happens dozens of times a second
    # across live polling, history queries, and inserts (confirmed live
    # 2026-09-30 while chasing down slow large-range history queries).
    #
    # NOT using _connect() for this first step specifically: auto_vacuum
    # must be set before any tables exist AND before journal_mode=WAL to
    # take effect at all - confirmed by direct test that setting it
    # after journal_mode=WAL (which is what _connect() always does,
    # correctly, for every other use) gets silently ignored, leaving the
    # database in the default NONE mode with no error. Harmless no-op on
    # a database that already has tables (an existing database stays in
    # whatever mode it was already migrated to - see
    # _enforce_size_cap_sync's comment on how this router's live
    # database was converted); only matters for a genuinely fresh file.
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


def _insert_battery_sync(ts: float, sample: dict):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO battery_samples (ts, voltage, current, power, soc_percent, "
            "remaining_ah, nominal_ah, cycles, charging) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, sample.get("voltage"), sample.get("current"), sample.get("power"),
             sample.get("soc_percent"), sample.get("remaining_ah"), sample.get("nominal_ah"),
             sample.get("cycles"), int(bool(sample.get("charging")))),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_charger_sync(ts: float, status: dict):
    result = status.get("result", {})
    alt = result.get("alternator", {})
    panel = result.get("panel", {})
    batt = result.get("batt", {})
    battery_info = result.get("batteryInfo", {})
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO charger_samples (ts, alt_v, alt_c, alt_p, panel_v, panel_c, panel_p, "
            "batt_v, batt_c, batt_p, alt_input_c, battery_temp, charge_state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, alt.get("v"), alt.get("c"), alt.get("p"),
             panel.get("v"), panel.get("c"), panel.get("p"),
             batt.get("v"), batt.get("c"), batt.get("p"),
             result.get("altInputC"), battery_info.get("temperature"),
             battery_info.get("chargeState")),
        )
        conn.commit()
    finally:
        conn.close()


def _prune_sync(retention_days: float, max_bytes: int):
    cutoff = time.time() - retention_days * 86400
    conn = _connect()
    try:
        conn.execute("DELETE FROM battery_samples WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM charger_samples WHERE ts < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()
    _enforce_size_cap_sync(max_bytes)


def _db_size_bytes(conn: sqlite3.Connection) -> int:
    # WAL mode buffers recent writes in a separate -wal file; TRUNCATE
    # folds that back into the main file (and empties the -wal file) so
    # the on-disk size we check next is the real total, not just whatever
    # happened to be checkpointed already.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return DB_PATH.stat().st_size if DB_PATH.exists() else 0


def _enforce_size_cap_sync(max_bytes: int):
    """Trims oldest rows (beyond whatever the time-based prune already
    removed) until the file is back under max_bytes. Only engages when
    the time-based window's data genuinely doesn't fit - the common case
    is this does nothing and returns immediately after one size check."""
    conn = _connect()
    try:
        size = _db_size_bytes(conn)
        if size <= max_bytes:
            return
        total_rows = conn.execute(
            "SELECT (SELECT COUNT(*) FROM battery_samples) + (SELECT COUNT(*) FROM charger_samples)"
        ).fetchone()[0]
        if not total_rows:
            return

        excess = size - max_bytes
        bytes_per_row = size / total_rows
        # bytes_per_row is an average, not exact - overshoot a bit so one
        # pass is normally enough. Any leftover error self-corrects on the
        # next hourly prune anyway.
        rows_to_delete = int(excess / bytes_per_row * 1.15) + 1

        cutoff_row = conn.execute(
            "SELECT ts FROM ("
            "  SELECT ts FROM battery_samples"
            "  UNION ALL"
            "  SELECT ts FROM charger_samples"
            ") ORDER BY ts LIMIT 1 OFFSET ?",
            (min(rows_to_delete, total_rows) - 1,),
        ).fetchone()

        if cutoff_row is None:
            # Estimate says even everything we have isn't enough - drop it all.
            conn.execute("DELETE FROM battery_samples")
            conn.execute("DELETE FROM charger_samples")
        else:
            cutoff_ts = cutoff_row[0]
            # Keep both tables trimmed to the same point in time - a chart
            # with battery data but no matching charger data (or vice
            # versa) past some point would look like a sensor dropout
            # rather than the deliberate cutoff it actually is.
            conn.execute("DELETE FROM battery_samples WHERE ts < ?", (cutoff_ts,))
            conn.execute("DELETE FROM charger_samples WHERE ts < ?", (cutoff_ts,))
        conn.commit()
        # NOT a full VACUUM. A full VACUUM rewrites the ENTIRE database
        # file in one atomic operation, regardless of how much was
        # actually freed - on this router's constrained hardware (492MB
        # RAM total, shared with Tailscale/nginx/the WiFi stack/etc.),
        # that was confirmed live 2026-10-14 to cause "database is
        # locked" errors cascading into a full OOM kill and crash loop,
        # repeating on every hourly prune cycle for however long the DB
        # sat above the cap (likely several days, given the 7-day actual
        # retention span found when debugging this - the 10-day/25MB
        # config intends the time limit to bind first, not the size cap).
        # incremental_vacuum reclaims pages in bounded amounts instead -
        # requires auto_vacuum=INCREMENTAL (set for new databases in
        # _init_sync; this router's existing database was migrated to it
        # manually, offline, while debugging this).
        #
        # PRAGMA incremental_vacuum(N) does NOT reclaim up to N pages in
        # one call the way its own name and SQLite's docs suggest -
        # measured directly (freelist_count before/after) that a single
        # call, regardless of N (tried 1, 2000, and no argument at all),
        # only ever reclaims one page. Calling it repeatedly in a small
        # loop instead does work correctly and is fast - 1002 individual
        # calls to reclaim 1002 pages (~4MB) measured at 85ms. The loop
        # below is capped so one hourly prune cycle can never do more
        # than a bounded, cheap amount of work regardless of how much is
        # left to reclaim; checking freelist_count lets it stop early
        # once there's genuinely nothing left to do.
        for _ in range(MAX_INCREMENTAL_VACUUM_STEPS):
            if conn.execute("PRAGMA freelist_count").fetchone()[0] == 0:
                break
            conn.execute("PRAGMA incremental_vacuum(1)")
        # In WAL mode, incremental_vacuum (like VACUUM) writes its result
        # into the -wal file, not straight to the main file - the main
        # file only actually shrinks once this checkpoint folds it back
        # in. Confirmed the hard way with the old VACUUM call: without
        # this, the file stayed at its pre-vacuum size until something
        # else happened to checkpoint it.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _query_sync(table: str, columns: list[str], since: float, until: float, max_points: int) -> list[dict]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE ts >= ? AND ts <= ?", (since, until)
        ).fetchone()[0]

        cols_sql = ", ".join(columns)
        if n <= max_points:
            rows = conn.execute(
                f"SELECT ts, {cols_sql} FROM {table} WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (since, until),
            ).fetchall()
            return [dict(r) for r in rows]

        # Bucket-average downsampling, done IN SQL via GROUP BY/AVG rather
        # than fetching every raw row and looping in Python - confirmed
        # live 2026-09-30 that the old Python-side version was the real
        # bottleneck behind "30m/1h+ ranges take a long time to load"
        # (SQLite's own index scan was already confirmed fine via EXPLAIN
        # QUERY PLAN). At 6+ hours of accumulated data this meant pulling
        # tens of thousands of raw rows into Python objects just to
        # average them down to 300 points - SQLite's C-level aggregation
        # does the same work directly against the B-tree, and only ~300
        # aggregated rows ever cross into Python.
        #
        # Buckets are anchored to ABSOLUTE epoch time (floor(ts /
        # bucket_width)), not to this query's own (moving) `since` or to
        # row position - bucket_width is constant for a given range
        # preset (e.g. always 6s per bucket for "30m"), so a historical
        # timestamp always lands in the same bucket regardless of what
        # window was requested this time - without this, values from a
        # few minutes ago could shift to a different bucket (and a
        # different displayed value) on the next poll, just because the
        # window moved (confirmed live 2026-09-18).
        bucket_width = (until - since) / max_points
        if bucket_width <= 0:
            rows = conn.execute(
                f"SELECT ts, {cols_sql} FROM {table} WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (since, until),
            ).fetchall()
            return [dict(r) for r in rows]

        avg_cols_sql = ", ".join(f"AVG({c}) AS {c}" for c in columns)
        bucket_expr = f"CAST(ts / {bucket_width!r} AS INTEGER)"
        rows = conn.execute(
            f"SELECT {bucket_expr} AS bucket, {avg_cols_sql} "
            f"FROM {table} WHERE ts >= ? AND ts <= ? "
            f"GROUP BY bucket ORDER BY bucket",
            (since, until),
        ).fetchall()
        return [{**dict(r), "ts": (r["bucket"] + 0.5) * bucket_width} for r in rows]
    finally:
        conn.close()


async def init():
    await asyncio.to_thread(_init_sync)


async def insert_battery(sample: dict, ts: float | None = None):
    await asyncio.to_thread(_insert_battery_sync, ts or time.time(), sample)


async def insert_charger(status: dict, ts: float | None = None):
    await asyncio.to_thread(_insert_charger_sync, ts or time.time(), status)


async def prune(retention_days: float = DEFAULT_RETENTION_DAYS, max_bytes: int = MAX_DB_SIZE_BYTES):
    await asyncio.to_thread(_prune_sync, retention_days, max_bytes)


BATTERY_COLUMNS = ["voltage", "current", "power", "soc_percent", "remaining_ah", "charging"]
CHARGER_COLUMNS = ["alt_v", "alt_c", "alt_p", "panel_v", "panel_c", "panel_p",
                   "batt_v", "batt_c", "batt_p", "alt_input_c", "battery_temp"]


async def query_battery_history(since: float, until: float, max_points: int = 500) -> list[dict]:
    return await asyncio.to_thread(_query_sync, "battery_samples", BATTERY_COLUMNS, since, until, max_points)


async def query_charger_history(since: float, until: float, max_points: int = 500) -> list[dict]:
    return await asyncio.to_thread(_query_sync, "charger_samples", CHARGER_COLUMNS, since, until, max_points)


def _avg_charging_power_sync(minutes: float) -> float | None:
    since = time.time() - minutes * 60
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT AVG(power) FROM battery_samples WHERE ts >= ? AND charging = 1",
            (since,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] is not None else None


async def avg_charging_power(minutes: float = 5) -> float | None:
    """Average battery charging power over the trailing window - only
    counts samples where the battery was actually charging, so a recent
    stretch of discharging doesn't drag the "charging power" figure down."""
    return await asyncio.to_thread(_avg_charging_power_sync, minutes)


def _avg_battery_current_sync(minutes: float) -> float | None:
    since = time.time() - minutes * 60
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT AVG(current) FROM battery_samples WHERE ts >= ?", (since,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] is not None else None


async def avg_battery_current(minutes: float = 2) -> float | None:
    """Average battery current over the trailing window - unfiltered by
    charging state, unlike avg_charging_power. Used for time-to-full/
    time-to-empty (see server.py's _compute_time_remaining) for BOTH
    deciding which of those two modes to show and the hours estimate
    itself - both need to react to the same smoothed signal, or the
    readout can flicker to "--" whenever the noisy instantaneous current
    happens to cross zero even while the 2-minute average clearly hasn't
    (confirmed live 2026-09-18: total power usage's own 2-min average
    showed a steady ~4W while time-remaining blanked out because the
    *mode* decision was still gated on the instantaneous reading)."""
    return await asyncio.to_thread(_avg_battery_current_sync, minutes)


def _avg_charger_current_sync(column: str, minutes: float) -> float | None:
    since = time.time() - minutes * 60
    conn = _connect()
    try:
        # column is always one of the two hardcoded literals passed by
        # avg_alt_current/avg_panel_current below, never user input - an
        # f-string here is building a column name, not interpolating a
        # value into the query, so this doesn't carry the usual injection
        # risk an f-string in SQL normally would.
        row = conn.execute(
            f"SELECT AVG({column}) FROM charger_samples WHERE ts >= ?", (since,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] is not None else None


async def avg_alt_current(minutes: float = 2) -> float | None:
    """Average alternator output current over the trailing window - used
    to work out which source (alternator vs solar) is actually charging
    the battery right now, and the alternator-output-based "time to
    full" estimate, since the two are never simultaneously active on
    this vehicle (per Quinn 2026-11-06 - replaces always assuming solar,
    which was wrong any time the alternator was the one actually
    charging, e.g. while driving)."""
    return await asyncio.to_thread(_avg_charger_current_sync, "alt_c", minutes)


async def avg_panel_current(minutes: float = 2) -> float | None:
    """Same as avg_alt_current but for the solar panel - see there."""
    return await asyncio.to_thread(_avg_charger_current_sync, "panel_c", minutes)


def _avg_total_power_usage_sync(minutes: float, efficiency: float) -> float | None:
    """Total power usage, smoothed over a trailing window rather than a
    single noisy instantaneous reading. Total usage = battery current +
    charger output, with the charger's side scaled down by `efficiency`
    first (2026-09-14) since its reported source power isn't 100% of
    what actually reaches the battery.

    Battery and charger samples arrive from independent async polling
    loops at slightly different times, so each charger sample is paired
    with its nearest-by-timestamp battery sample (same approach the
    frontend's history chart uses) before averaging."""
    since = time.time() - minutes * 60
    conn = _connect()
    try:
        battery_rows = conn.execute(
            "SELECT ts, power FROM battery_samples WHERE ts >= ? ORDER BY ts", (since,)
        ).fetchall()
        charger_rows = conn.execute(
            "SELECT ts, alt_p, panel_p FROM charger_samples WHERE ts >= ? ORDER BY ts", (since,)
        ).fetchall()
    finally:
        conn.close()

    if not battery_rows or not charger_rows:
        return None

    # Two-pointer merge instead of a linear scan through all of
    # battery_rows for every charger row - both lists are already
    # ts-sorted (from the SQL ORDER BY), so each charger sample's nearest
    # battery sample can be found in O(n+m) total instead of O(n*m).
    # Didn't matter at the 2-minute window this was first written for
    # (~120x60 comparisons, trivial), but the 1-hour average (added
    # 2026-09-30) has ~30x more rows in both lists - that's ~900x more
    # comparisons, not 30x, and measured at ~320ms on a fast desktop
    # Mac for a realistic hour of data. That's already too slow for a
    # value recomputed on every 1-second dashboard poll, and the
    # router's ARM chip is significantly slower still for an interpreted
    # Python loop like this - confirmed worth fixing before ever
    # deploying the 1-hour average, not after someone noticed lag.
    values = []
    bi = 0
    n = len(battery_rows)
    for c_ts, alt_p, panel_p in charger_rows:
        while bi + 1 < n and battery_rows[bi + 1][0] <= c_ts:
            bi += 1
        candidates = [battery_rows[bi]]
        if bi + 1 < n:
            candidates.append(battery_rows[bi + 1])
        nearest = min(candidates, key=lambda b: abs(b[0] - c_ts))
        battery_power = nearest[1] or 0
        source_w = (alt_p or 0) + (panel_p or 0)
        values.append(source_w * efficiency - battery_power)
    return sum(values) / len(values) if values else None


async def avg_total_power_usage(minutes: float = 2, efficiency: float = 0.9) -> float | None:
    return await asyncio.to_thread(_avg_total_power_usage_sync, minutes, efficiency)


def _has_data_sync(since: float, until: float) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT EXISTS("
            "  SELECT 1 FROM battery_samples WHERE ts >= ? AND ts < ?"
            "  UNION SELECT 1 FROM charger_samples WHERE ts >= ? AND ts < ?"
            ")",
            (since, until, since, until),
        ).fetchone()
    finally:
        conn.close()
    return bool(row[0])


def _solar_energy_wh_sync(since: float, until: float) -> float:
    # Trapezoidal integration of solar panel power over time - Wh = area
    # under the power(t) curve. Skips any interval wider than
    # MAX_GAP_HOURS rather than integrating across it: the charger poll
    # cadence is normally ~2s, so a much bigger gap
    # means the router/ESP32 was actually off for a while, and assuming
    # constant/interpolated power for the whole outage would fabricate
    # energy that was never actually measured.
    #
    # Done entirely in SQL via a LAG() window function rather than
    # fetchall()-ing every raw row and looping in Python - confirmed live
    # 2026-10-14 that for the 10-day window (the widest this is ever
    # called with, from the "Last 10 days" solar tile) this was pulling
    # ~194k rows into a Python list of tuples, measured at ~24MB of
    # Python-side heap and ~360ms of pure-Python loop time on commodity
    # hardware - worse still on this router's weak ARM CPU and ~490MB
    # total RAM. The existing _solar_energy_cache (see server.py) only
    # saves *repeat* calls for the same sealed window; it does nothing
    # for the first call after a process restart, and during the
    # 2026-10-14 incident the process was restarting every few minutes
    # (OOM crash loop), so this ran at full cost on almost every tile
    # refresh - directly contributing to the memory pressure that caused
    # the crash loop in the first place. The SQL version produces
    # identical results (verified against the old Python loop on a
    # synthetic 194k-row table) in ~165ms with ~0MB Python-side memory,
    # since SQLite's C-level window-function evaluation never has to
    # materialize the rows as Python objects at all.
    max_gap_seconds = (5 / 60) * 3600.0
    conn = _connect()
    try:
        row = conn.execute(
            """
            WITH s AS (
                SELECT ts, panel_p,
                       LAG(ts) OVER (ORDER BY ts) AS prev_ts,
                       LAG(panel_p) OVER (ORDER BY ts) AS prev_p
                FROM charger_samples
                WHERE ts >= ? AND ts < ?
            )
            SELECT COALESCE(SUM(
                CASE WHEN prev_ts IS NOT NULL AND (ts - prev_ts) <= ?
                     THEN (COALESCE(prev_p, 0.0) + COALESCE(panel_p, 0.0)) / 2.0
                          * (ts - prev_ts) / 3600.0
                     ELSE 0.0
                END
            ), 0.0)
            FROM s
            """,
            (since, until, max_gap_seconds),
        ).fetchone()
    finally:
        conn.close()
    return row[0]


async def solar_energy_wh(since: float, until: float | None = None) -> float:
    """Cumulative solar panel energy (Watt-hours) in [since, until) -
    until defaults to now. Both are computed by the caller in its own
    local timezone (see app.js's header counter and daily-totals rows) -
    same reasoning as days_with_data: a "day" only means something
    relative to the viewer's own timezone, which this router has no way
    to know (per Quinn 2026-09-30)."""
    return await asyncio.to_thread(_solar_energy_wh_sync, since, until if until is not None else time.time())


async def days_with_data(day_starts: list[float]) -> list[bool]:
    """For each given day-start unix timestamp, whether there's at least
    one battery or charger sample in [day_start, day_start+86400). Day
    boundaries are computed by the CALLER (the frontend, in the viewer's
    own local timezone - see app.js's day picker) rather than here, since
    this router has no reliable way to know what timezone the person
    looking at the dashboard is actually in."""
    results = []
    for start in day_starts:
        results.append(await asyncio.to_thread(_has_data_sync, start, start + 86400))
    return results


def _get_setting_sync(key: str, default: str) -> str:
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else default


def _set_setting_sync(key: str, value: str):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


async def get_setting(key: str, default: str) -> str:
    """Small persisted key/value store for user preferences that need to
    be shared across every device viewing the dashboard (not a per-
    browser localStorage thing) - first use: solar panel max capacity,
    see server.py's /api/settings/solar-panel-max-w (per Quinn 2026-09-30)."""
    return await asyncio.to_thread(_get_setting_sync, key, default)


async def set_setting(key: str, value: str):
    await asyncio.to_thread(_set_setting_sync, key, value)


# Previously-applied battery/charger BLE addresses, most-recently-used
# first - lets the Settings page offer a "go back to a previous device"
# dropdown instead of only ever showing whichever one is currently
# configured (per Quinn 2026-10-14). Stored as a JSON-encoded list under
# the existing generic settings key/value store (same mechanism as
# solar_panel_max_w above) rather than a dedicated table - this is small,
# per-role, rarely-written data, not a candidate for its own schema.
DEVICE_MAC_HISTORY_MAX_ENTRIES = 8


def _device_mac_history_key(role: str) -> str:
    return f"{role}_mac_history"


async def get_device_mac_history(role: str) -> list[dict]:
    """role is "battery" or "charger". Returns [] if nothing's been
    recorded yet (e.g. on a fresh install, or before this feature
    existed) - not an error case."""
    raw = await get_setting(_device_mac_history_key(role), "[]")
    try:
        history = json.loads(raw)
        return history if isinstance(history, list) else []
    except ValueError:
        return []  # corrupt/unexpected stored value - degrade to empty rather than crash the settings page


async def record_device_mac_history(role: str, mac: str, name: str | None = None):
    """Called whenever a battery/charger address is actually applied
    (see server.py's api_set_device_addresses) - moves this address to
    the front of its history, deduplicated (re-applying an address
    already in history refreshes its position/name/timestamp rather than
    creating a second entry for it), capped at
    DEVICE_MAC_HISTORY_MAX_ENTRIES oldest-dropped-first."""
    history = await get_device_mac_history(role)
    history = [h for h in history if h.get("mac") != mac]
    history.insert(0, {"mac": mac, "name": name, "last_used_at": time.time()})
    history = history[:DEVICE_MAC_HISTORY_MAX_ENTRIES]
    await set_setting(_device_mac_history_key(role), json.dumps(history))


SOLAR_ALLTIME_WH_KEY = "solar_alltime_wh"
SOLAR_ALLTIME_CHECKPOINT_KEY = "solar_alltime_checkpoint_ts"


def _oldest_charger_ts_sync() -> float | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT MIN(ts) FROM charger_samples").fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] is not None else None


async def accumulate_alltime_solar_wh():
    """Adds newly-elapsed solar energy to a running total that never
    resets and outlives the raw-sample retention window (effectively
    governed by MAX_DB_SIZE_BYTES, not DEFAULT_RETENTION_DAYS - see the
    module docstring) - raw samples get pruned once that's reached, so
    this has to be accumulated incrementally into a persisted counter
    BEFORE that happens, not recomputed from history at query time.
    Called hourly alongside the existing prune (per Quinn 2026-09-30, who
    suggested "end of each day or whenever's best" - hourly keeps the
    figure reasonably current rather than looking stale for up to a day,
    and comfortably outpaces however far back raw samples actually
    reach, regardless of how that figure moves over time)."""
    checkpoint_str = await get_setting(SOLAR_ALLTIME_CHECKPOINT_KEY, "")
    now = time.time()
    if checkpoint_str:
        since = float(checkpoint_str)
    else:
        # First run ever - backfill from the oldest sample still on hand
        # rather than starting from zero and silently discarding whatever
        # history already exists at the point this feature was added.
        oldest = await asyncio.to_thread(_oldest_charger_ts_sync)
        since = oldest if oldest is not None else now

    added_wh = await solar_energy_wh(since, now)
    total_str = await get_setting(SOLAR_ALLTIME_WH_KEY, "0")
    new_total = float(total_str) + added_wh
    await set_setting(SOLAR_ALLTIME_WH_KEY, str(new_total))
    await set_setting(SOLAR_ALLTIME_CHECKPOINT_KEY, str(now))


async def get_alltime_solar_wh() -> float:
    value = await get_setting(SOLAR_ALLTIME_WH_KEY, "0")
    return float(value)
