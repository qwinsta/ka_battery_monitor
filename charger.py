#!/usr/bin/env python3
"""Control/read the KickAss 12V 40A DC-DC MPPT charger.

Protocol: plain JSON-RPC over BLE characteristic eee2 (0000eee2-...).
Reverse engineered from a packet capture of the manufacturer's own app -
see PROTOCOL.md for the full discovery notes; confirmed working live
2026-09-14. The device's firmware always returns
short, fixed-size chunks per read regardless of total length - read a
2-byte big-endian length from eee1, then keep re-reading eee2 (the same
characteristic the command was written to) until that many bytes have
arrived.

HAZARD - DO NOT write raw bytes to ff01/ff02 on this device: writing
0x03 to those characteristics disables the charger's BLE radio outright,
recoverable only by a physical power cycle. This module never touches
ff01/ff02 at all - it only uses eee1/eee2 - but if you're extending it,
stay away from ff01/ff02.
"""
# X | None union syntax below needs this on Python 3.9 (the router's
# version) - see server.py's matching comment for why.
from __future__ import annotations

import asyncio
import json

from bleak import BleakClient

EEE1 = "0000eee1-0000-1000-8000-00805f9b34fb"
EEE2 = "0000eee2-0000-1000-8000-00805f9b34fb"


async def _find_char(client: BleakClient, uuid: str):
    for service in client.services:
        for char in service.characteristics:
            if char.uuid.lower() == uuid:
                return char
    raise RuntimeError(f"characteristic {uuid} not found on this device")


async def _call(client: BleakClient, mac: str, method: str, action: str,
                 value: int | None = None, request_id: int = 1,
                 max_reads: int = 60) -> dict:
    eee1 = await _find_char(client, EEE1)
    eee2 = await _find_char(client, EEE2)
    params = {"mac": mac, "action": action}
    if value is not None:
        params["value"] = value
    command = json.dumps({"id": request_id, "method": method, "params": params})

    await client.write_gatt_char(eee2.uuid, command.encode("utf-8"), response=True)
    await asyncio.sleep(0.3)

    probe = await client.read_gatt_char(eee1.uuid)
    if len(probe) < 2:
        raise RuntimeError(f"unexpected eee1 probe: {probe!r}")
    # Big-endian 2-byte length, not [marker, length] as first assumed -
    # confirmed live 2026-09-14 when reading only probe[1] truncated a
    # ~590-byte response to under 256 bytes, cutting off mid-value.
    total_length = (probe[0] << 8) | probe[1]

    content = bytearray()
    reads = 0
    while len(content) < total_length and reads < max_reads:
        chunk = await client.read_gatt_char(eee2.uuid)
        if not chunk:
            break
        content += chunk
        reads += 1
    if len(content) < total_length:
        raise RuntimeError(
            f"only got {len(content)}/{total_length} bytes after {reads} reads: {bytes(content)!r}"
        )

    text = bytes(content[:total_length]).decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # The device's own JSON serializer has a known bug around a
        # "statusMs..." field (confirmed independently from packet
        # captures - not a bug in our reassembly). Surface the raw text
        # instead of crashing, so the data is still usable.
        return {"_raw_text": text, "_parse_error": str(e)}


async def set_alt_max_current_from_client(client: BleakClient, full_mac: str, amps: int) -> dict:
    """Set the alternator max-current limit, in whole amps, on an
    already-open connection - use this for continuous polling (see
    poller.py). Confirmed working - verified against a real slider sweep
    (23/40/55/5/40A).

    full_mac: the charger's real BD address as plain hex, no colons, e.g.
    "E05319411E51" - the captured `altC` command used this full form,
    unlike `status` GET which used just the last 3 bytes.
    """
    return await _call(client, full_mac, "altC", "SET", value=amps, request_id=668)


async def get_status_from_client(client: BleakClient, full_mac: str) -> dict:
    """Read full status on an already-open connection - use this for
    continuous polling (see poller.py). Confirmed working live
    2026-09-14. Returns: deviceInfo (name/type/firmware versions),
    batteryInfo (chemistry type, voltage grade, charge state,
    temperature), altInputC (the alternator max-current setting), and
    per-source alternator/panel/batt dicts each with v/c/p (voltage/
    current/power).
    """
    short_mac = full_mac[-6:]
    return await _call(client, short_mac, "status", "GET")


async def set_alt_max_current(address: str, full_mac: str, amps: int, timeout: float = 15.0) -> dict:
    """One-shot convenience wrapper: connect, set, disconnect. For
    continuous polling use set_alt_max_current_from_client() on a
    persistent connection instead."""
    async with BleakClient(address, timeout=timeout) as client:
        return await set_alt_max_current_from_client(client, full_mac, amps)


async def get_status(address: str, full_mac: str, timeout: float = 15.0) -> dict:
    """One-shot convenience wrapper: connect, read, disconnect. For
    continuous polling use get_status_from_client() on a persistent
    connection instead."""
    async with BleakClient(address, timeout=timeout) as client:
        return await get_status_from_client(client, full_mac)


async def _main():
    import sys
    if len(sys.argv) < 4:
        print("Usage: python3 charger.py <address> <mac> status")
        print("       python3 charger.py <address> <mac> set-alt-current <amps>")
        return
    address, mac, action = sys.argv[1], sys.argv[2], sys.argv[3]
    if action == "status":
        print(json.dumps(await get_status(address, mac), indent=2))
    elif action == "set-alt-current":
        amps = int(sys.argv[4])
        print(json.dumps(await set_alt_max_current(address, mac, amps), indent=2))
    else:
        print(f"Unknown action: {action}")


if __name__ == "__main__":
    asyncio.run(_main())
