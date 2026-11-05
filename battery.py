#!/usr/bin/env python3
"""Read live data from the KickAss/Ultra-X 230Ah LiFePO4 battery.

Protocol: JBD/Xiaoxiang "Smart BMS" - reverse engineered from a real
packet capture of the manufacturer's own app; see PROTOCOL.md for the
full discovery notes. Read-only: this module only ever sends the
documented read-register request frames.
"""
# X | None union syntax below needs this on Python 3.9 (the router's
# version) - see server.py's matching comment for why.
from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field

from bleak import BleakClient

FF01_NOTIFY = "0000ff01-0000-1000-8000-00805f9b34fb"
FF02_WRITE = "0000ff02-0000-1000-8000-00805f9b34fb"

REG_BASIC_INFO = 0x03
REG_CELL_VOLTAGES = 0x04
REG_DEVICE_NAME = 0x05


def _request_frame(register: int) -> bytes:
    checksum = (0x10000 - register) & 0xFFFF
    return bytes([0xDD, 0xA5, register, 0x00]) + checksum.to_bytes(2, "big") + b"\x77"


@dataclass
class BasicInfo:
    voltage: float          # V
    current: float          # A, positive = charging
    remaining_ah: float
    nominal_ah: float
    cycles: int
    rsoc: int               # %
    num_cells: int
    num_ntc: int
    ntc_temps_c: list       # one reading per NTC sensor, degrees C
    charging: bool
    power: float = field(init=False)

    def __post_init__(self):
        self.power = round(self.voltage * self.current, 1)


def _parse_ntc_temps(data: bytes, num_ntc: int) -> list:
    """NTC temperatures start right after num_ntc (offset 22), 2 bytes
    each, big-endian, in units of 0.1 Kelvin. Verified against a real
    captured frame 2026-09-14 (offsets 23/25 decoded to 21.2C/20.8C - two
    sane, closely-matching sensor readings, not guessed from the generic
    JBD spec alone given we've been burned by an assumption before)."""
    temps = []
    for i in range(num_ntc):
        offset = 23 + i * 2
        if offset + 2 > len(data):
            break
        raw = struct.unpack_from(">H", data, offset)[0]
        temps.append(round(raw / 10 - 273.15, 1))
    return temps


def _parse_basic_info(data: bytes) -> BasicInfo:
    voltage = struct.unpack_from(">H", data, 0)[0] / 100
    current = struct.unpack_from(">h", data, 2)[0] / 100
    remaining = struct.unpack_from(">H", data, 4)[0] / 100
    nominal = struct.unpack_from(">H", data, 6)[0] / 100
    cycles = struct.unpack_from(">H", data, 8)[0]
    rsoc = data[19]
    num_cells = data[21]
    num_ntc = data[22]
    ntc_temps_c = _parse_ntc_temps(data, num_ntc)
    return BasicInfo(
        voltage=voltage, current=current, remaining_ah=remaining,
        nominal_ah=nominal, cycles=cycles, rsoc=rsoc,
        num_cells=num_cells, num_ntc=num_ntc, ntc_temps_c=ntc_temps_c,
        charging=current > 0,
    )


def _parse_cell_voltages(data: bytes, num_cells: int) -> list[float]:
    return [struct.unpack_from(">H", data, i * 2)[0] / 1000 for i in range(num_cells)]


async def _request(client: BleakClient, register: int, timeout: float = 5.0, retries: int = 3) -> bytes:
    """Send a JBD read-register request and wait for the (possibly
    fragmented-across-notifications) response, using the frame's own
    length field rather than scanning for the 0x77 end byte - a data
    byte can coincidentally equal 0x77, so length-based framing is the
    only reliable way to know a frame is complete.

    Retries the write on timeout: a write-without-response sent right
    after connecting can be silently dropped by CoreBluetooth before the
    link's internal "ready to send" state settles - confirmed live
    2026-09-14 (first request after connect timed out with zero bytes
    received). CoreBluetooth won't queue/retry it for us, so we do."""
    received = bytearray()
    done = asyncio.Event()

    def on_notify(_sender, data: bytearray):
        received.extend(data)
        if len(received) >= 4:
            length = received[3]
            if len(received) >= 4 + length + 2 + 1:
                done.set()

    await client.start_notify(FF01_NOTIFY, on_notify)
    try:
        for attempt in range(retries):
            received.clear()
            done.clear()
            await client.write_gatt_char(FF02_WRITE, _request_frame(register), response=False)
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
                break
            except asyncio.TimeoutError:
                if attempt == retries - 1:
                    raise
    finally:
        await client.stop_notify(FF01_NOTIFY)

    if received[0] != 0xDD:
        raise RuntimeError(f"unexpected frame start byte: 0x{received[0]:02x}")
    status, length = received[2], received[3]
    if status != 0:
        raise RuntimeError(f"battery returned error status 0x{status:02x} for register 0x{register:02x}")
    return bytes(received[4:4 + length])


async def read_basic_info(client: BleakClient) -> BasicInfo:
    """Single fast round-trip - safe to call at ~1s cadence on an
    already-open connection (see poller.py)."""
    return _parse_basic_info(await _request(client, REG_BASIC_INFO))


async def read_cell_voltages(client: BleakClient, num_cells: int) -> list[float]:
    return _parse_cell_voltages(await _request(client, REG_CELL_VOLTAGES), num_cells)


async def read_device_name(client: BleakClient) -> str:
    return (await _request(client, REG_DEVICE_NAME)).decode("ascii", errors="replace")


def basic_info_to_dict(info: BasicInfo, cells: list[float] | None = None, name: str | None = None) -> dict:
    d = {
        "voltage": info.voltage,
        "current": info.current,
        "power": info.power,
        "soc_percent": info.rsoc,
        "remaining_ah": info.remaining_ah,
        "nominal_ah": info.nominal_ah,
        "cycles": info.cycles,
        "charging": info.charging,
        "temperature_c": round(sum(info.ntc_temps_c) / len(info.ntc_temps_c), 1) if info.ntc_temps_c else None,
        "ntc_temps_c": info.ntc_temps_c,
    }
    if cells is not None:
        d["cell_voltages"] = cells
        d["num_cells"] = info.num_cells
    if name is not None:
        d["device_name"] = name
    return d


async def read_all(address: str, timeout: float = 15.0) -> dict:
    """One-shot convenience wrapper: connect, read everything, disconnect.
    For continuous polling use read_basic_info() etc. directly on a
    persistent BleakClient instead - reconnecting every cycle is far too
    slow for near-real-time updates (see poller.py)."""
    async with BleakClient(address, timeout=timeout) as client:
        info = await read_basic_info(client)
        cells = await read_cell_voltages(client, info.num_cells)
        name = await read_device_name(client)

    return basic_info_to_dict(info, cells, name)


async def _main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 battery.py <address>")
        return
    import json
    print(json.dumps(await read_all(sys.argv[1]), indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
