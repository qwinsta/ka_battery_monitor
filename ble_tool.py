#!/usr/bin/env python3
"""BLE discovery & GATT inspection tool for the KickAss DC-DC charger and
Ultra-X battery - this is the tool that was actually used to reverse
engineer both protocols in the first place (see PROTOCOL.md for the
findings), and doubles as the easiest way to find your own device's BLE
address if you're setting this up fresh.

Usage:
    python3 ble_tool.py scan [--timeout 10]
    python3 ble_tool.py inspect <address> [--timeout 15]
    python3 ble_tool.py capture <address> [--duration 30] [--timeout 15]
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient, BleakScanner

LOG_DIR = Path(__file__).parent / "discovery_logs"

# Advertised-name fragments seen so far for our two devices (from the
# phone's Bluetooth settings list). Update if the naming differs for yours.
TARGET_NAME_HINTS = ["KADCDC", "LT23SX"]


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def matches_target(name: str | None) -> bool:
    if not name:
        return False
    return any(hint.lower() in name.lower() for hint in TARGET_NAME_HINTS)


async def cmd_scan(args):
    print(f"Scanning for BLE advertisements for {args.timeout}s...\n")
    devices = await BleakScanner.discover(timeout=args.timeout, return_adv=True)

    rows = []
    for address, (device, adv) in devices.items():
        name = device.name or adv.local_name or "(no name)"
        rssi = adv.rssi if adv.rssi is not None else -999
        rows.append((matches_target(name), rssi, name, address, adv))

    rows.sort(key=lambda r: (not r[0], -r[1]))

    if not rows:
        print("No BLE devices seen. Try a longer --timeout, or check the "
              "devices are powered on / broadcasting.")
        return

    for hit, rssi, name, address, adv in rows:
        marker = "  *** TARGET ***" if hit else ""
        print(f"{rssi:>5} dB  {address}  {name}{marker}")
        if adv.service_uuids:
            print(f"          service UUIDs: {adv.service_uuids}")
        if adv.manufacturer_data:
            for company_id, data in adv.manufacturer_data.items():
                print(f"          manufacturer_data[{company_id}]: {data.hex()}")
        if adv.service_data:
            for uuid, data in adv.service_data.items():
                print(f"          service_data[{uuid}]: {data.hex()}")

    LOG_DIR.mkdir(exist_ok=True)
    out_file = LOG_DIR / f"scan_{ts()}.json"
    dump = [
        {
            "address": address,
            "name": name,
            "rssi": rssi,
            "target_match": hit,
            "service_uuids": adv.service_uuids,
            "manufacturer_data": {str(k): v.hex() for k, v in adv.manufacturer_data.items()},
            "service_data": {str(k): v.hex() for k, v in adv.service_data.items()},
        }
        for hit, rssi, name, address, adv in rows
    ]
    out_file.write_text(json.dumps(dump, indent=2))
    print(f"\nSaved scan results to {out_file}")

    targets = [r for r in rows if r[0]]
    if targets:
        print("\nNext step - inspect a target device's GATT table, e.g.:")
        for _, _, name, address, _ in targets:
            print(f"  python3 ble_tool.py inspect {address}   # {name}")
    else:
        print("\nNo devices matched the known name hints "
              f"({TARGET_NAME_HINTS}). You can still inspect any address "
              "above manually.")


async def cmd_inspect(args):
    address = args.address
    print(f"Connecting to {address} ...")
    summary = []
    async with BleakClient(address, timeout=args.timeout) as client:
        print(f"Connected: {client.is_connected}\n")

        for service in client.services:
            print(f"[Service] {service.uuid}  ({service.description})")
            for char in service.characteristics:
                props = ",".join(char.properties)
                print(f"    [Char] {char.uuid}  props=({props})  handle={char.handle}")
                value_hex = None
                if "read" in char.properties:
                    try:
                        value = await client.read_gatt_char(char.uuid)
                        value_hex = value.hex()
                        print(f"        read value: {value_hex}  ({bytes(value)!r})")
                    except Exception as e:
                        print(f"        read failed: {e}")
                for desc in char.descriptors:
                    print(f"        [Desc] {desc.uuid}  handle={desc.handle}")
                summary.append({
                    "service_uuid": service.uuid,
                    "char_uuid": char.uuid,
                    "handle": char.handle,
                    "properties": list(char.properties),
                    "read_value_hex": value_hex,
                })

    LOG_DIR.mkdir(exist_ok=True)
    safe_addr = address.replace(":", "-")
    out_file = LOG_DIR / f"inspect_{safe_addr}_{ts()}.json"
    out_file.write_text(json.dumps(summary, indent=2))
    print(f"\nSaved GATT table to {out_file}")

    notify_chars = [s for s in summary if "notify" in s["properties"] or "indicate" in s["properties"]]
    if notify_chars:
        print("\nNotify/indicate characteristics found - next step, capture live data:")
        print(f"  python3 ble_tool.py capture {address} --duration 30")


async def cmd_capture(args):
    address = args.address
    log_lines = []

    def on_notify(sender, data: bytearray):
        t = datetime.now(timezone.utc).isoformat()
        char_uuid = str(getattr(sender, "uuid", sender))
        line = {"time": t, "char": char_uuid, "hex": data.hex(), "len": len(data)}
        log_lines.append(line)
        print(f"{t}  {char_uuid}  ({line['len']}B)  {line['hex']}")

    print(f"Connecting to {address} ...")
    async with BleakClient(address, timeout=args.timeout) as client:
        print(f"Connected: {client.is_connected}")

        notify_uuids = [
            char.uuid
            for service in client.services
            for char in service.characteristics
            if "notify" in char.properties or "indicate" in char.properties
        ]

        if not notify_uuids:
            print("No notify/indicate characteristics found on this device. "
                  "Run 'inspect' first to check the GATT table.")
            return

        print(f"Subscribing to {len(notify_uuids)} characteristic(s): {notify_uuids}")
        for uuid in notify_uuids:
            try:
                await client.start_notify(uuid, on_notify)
            except Exception as e:
                print(f"  failed to subscribe to {uuid}: {e}")

        print(f"Capturing for {args.duration}s (Ctrl+C to stop early)...\n")
        try:
            await asyncio.sleep(args.duration)
        except KeyboardInterrupt:
            pass

        for uuid in notify_uuids:
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass

    LOG_DIR.mkdir(exist_ok=True)
    safe_addr = address.replace(":", "-")
    out_file = LOG_DIR / f"capture_{safe_addr}_{ts()}.json"
    out_file.write_text(json.dumps(log_lines, indent=2))
    print(f"\nCaptured {len(log_lines)} notifications. Saved to {out_file}")


# Generic long-shot "wake" payloads to try against writable characteristics.
# These are NOT vendor-confirmed commands for these devices - just common
# patterns seen on cheap/simple BLE peripherals. If none of these trigger a
# response, the reliable next step is capturing the manufacturer's own
# app's BLE traffic (e.g. with Android's built-in HCI snoop log, or a
# BLE sniffer) while it talks to the device.
WAKE_CANDIDATES = [
    bytes([0x01]),
    bytes([0x00]),
    bytes([0xFF]),
    bytes([0x0A]),
    bytes([0x02]),
    b"1",
    b"0",
    b"A",
    b"AT\r\n",
    b"on\r\n",
]


async def cmd_wake_scan(args):
    address = args.address
    print(f"Connecting to {address} ...")
    received = []

    def on_notify(sender, data: bytearray):
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "char": str(getattr(sender, "uuid", sender)),
            "hex": data.hex(),
        }
        received.append(entry)

    async with BleakClient(address, timeout=args.timeout) as client:
        print(f"Connected: {client.is_connected}\n")

        notify_chars = []
        write_chars = []
        for service in client.services:
            for char in service.characteristics:
                if "notify" in char.properties or "indicate" in char.properties:
                    notify_chars.append(char)
                if "write" in char.properties or "write-without-response" in char.properties:
                    write_chars.append(char)

        if not notify_chars:
            print("No notify/indicate characteristics on this device - nothing to listen for.")
            return
        if not write_chars:
            print("No writable characteristics found - can't send wake commands.")
            return

        for char in notify_chars:
            try:
                await client.start_notify(char.uuid, on_notify)
            except Exception as e:
                print(f"  (couldn't subscribe to {char.uuid}: {e})")

        print(f"Listening on {len(notify_chars)} characteristic(s), probing "
              f"{len(write_chars)} writable characteristic(s) with "
              f"{len(WAKE_CANDIDATES)} candidate payload(s) each.\n"
              "These are generic long-shot guesses, not vendor-confirmed commands.\n")

        found_any = False
        for wchar in write_chars:
            if "write" in wchar.properties:
                use_response = True
            else:
                use_response = False
            for payload in WAKE_CANDIDATES:
                before = len(received)
                label = f"{payload.hex()} ({payload!r})"
                try:
                    await client.write_gatt_char(wchar.uuid, payload, response=use_response)
                except Exception as e:
                    print(f"  write {label} -> {wchar.uuid}: failed ({e})")
                    continue
                await asyncio.sleep(1.2)
                new = received[before:]
                if new:
                    found_any = True
                    print(f"  RESPONSE after writing {label} to {wchar.uuid}:")
                    for entry in new:
                        print(f"      {entry['char']}  {entry['hex']}")
                else:
                    print(f"  write {label} -> {wchar.uuid}: no response")

        for char in notify_chars:
            try:
                await client.stop_notify(char.uuid)
            except Exception:
                pass

    LOG_DIR.mkdir(exist_ok=True)
    safe_addr = address.replace(":", "-")
    out_file = LOG_DIR / f"wakescan_{safe_addr}_{ts()}.json"
    out_file.write_text(json.dumps(received, indent=2))
    if found_any:
        print(f"\nGot response(s)! Logged to {out_file}")
    else:
        print(f"\nNo candidate produced a response. Logged (empty) to {out_file}. "
              "Time to capture the real app's traffic instead.")


# Command bytes confirmed BY HAND to cause real harm on a given write
# characteristic (not just "unknown command" errors). cmd-sweep refuses to
# send these without --force.
KNOWN_HAZARDS = {
    "02f00000-0000-0000-0000-00000000ff01": {
        0x03: "disables the DC-DC charger's BLE radio entirely - requires a "
              "physical power cycle of the unit to recover",
    },
}


async def _connect_with_retry(address, timeout, retries=3, backoff=2.0):
    last_exc = None
    for attempt in range(retries):
        client = BleakClient(address, timeout=timeout)
        try:
            await client.connect()
            return client
        except Exception as e:
            last_exc = e
            try:
                await client.disconnect()
            except Exception:
                pass
            if attempt < retries - 1:
                await asyncio.sleep(backoff)
    raise last_exc


async def cmd_cmd_sweep(args):
    address = args.address
    write_uuid = args.write_char
    all_results = []
    cmd_range = list(range(args.start, args.end + 1))
    batch_size = max(1, args.batch_size)

    hazards = KNOWN_HAZARDS.get(write_uuid.lower(), {})
    hazardous_in_range = {c: reason for c, reason in hazards.items() if c in cmd_range}
    if hazardous_in_range and not args.force:
        print("Refusing to run - this range includes command byte(s) confirmed to cause real harm:")
        for c, reason in hazardous_in_range.items():
            print(f"  0x{c:02x}: {reason}")
        print("Pass --force to send them anyway, or narrow --start/--end to skip them.")
        return

    print(f"Sweeping single-byte commands 0x{args.start:02x}-0x{args.end:02x} "
          f"on {write_uuid}, reconnecting every {batch_size} command(s) "
          "(this device closes the link after a few requests per connection).\n")

    idx = 0
    while idx < len(cmd_range):
        batch = cmd_range[idx: idx + batch_size]
        current_cmd = [None]
        batch_results = []

        def on_notify(sender, data: bytearray):
            entry = {
                "cmd": current_cmd[0],
                "char": str(getattr(sender, "uuid", sender)),
                "hex": data.hex(),
            }
            batch_results.append(entry)
            print(f"  0x{current_cmd[0]:02x} -> {entry['char']}  {entry['hex']}")

        try:
            client = await _connect_with_retry(address, args.timeout)
        except Exception as e:
            print(f"  connect failed for batch {[hex(c) for c in batch]} ({e}), skipping")
            idx += batch_size
            continue

        try:
            notify_chars = [
                char
                for service in client.services
                for char in service.characteristics
                if "notify" in char.properties or "indicate" in char.properties
            ]
            write_char = next(
                (
                    char
                    for service in client.services
                    for char in service.characteristics
                    if char.uuid.lower() == write_uuid.lower()
                ),
                None,
            )
            if write_char is None:
                print(f"Characteristic {write_uuid} not found on this device.")
                return
            use_response = "write" in write_char.properties

            for char in notify_chars:
                try:
                    await client.start_notify(char.uuid, on_notify)
                except Exception:
                    pass

            for cmd in batch:
                current_cmd[0] = cmd
                try:
                    await client.write_gatt_char(write_uuid, bytes([cmd]), response=use_response)
                except Exception as e:
                    print(f"  0x{cmd:02x}: write failed ({e})")
                    break
                await asyncio.sleep(args.delay)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        all_results.extend(batch_results)
        idx += batch_size
        await asyncio.sleep(0.6)

    LOG_DIR.mkdir(exist_ok=True)
    safe_addr = address.replace(":", "-")
    out_file = LOG_DIR / f"cmdsweep_{safe_addr}_{ts()}.json"
    out_file.write_text(json.dumps(all_results, indent=2))
    print(f"\n{len(all_results)} response(s) across the sweep. Saved to {out_file}")


async def cmd_charger_json(args):
    """Write a JSON-RPC command to eee2 and do a single clean bleak read of
    the response, instead of replaying the manufacturer app's own
    chunked-read pattern (which reassembles incompletely here)."""
    address = args.address
    command = args.json_command

    async with BleakClient(address, timeout=args.timeout) as client:
        eee2 = None
        for service in client.services:
            for char in service.characteristics:
                if char.uuid.lower().endswith("eee2-0000-1000-8000-00805f9b34fb"):
                    eee2 = char
        if eee2 is None:
            print("Characteristic eee2 not found on this device.")
            return

        print(f"Writing: {command}")
        await client.write_gatt_char(eee2.uuid, command.encode("utf-8"), response=True)
        await asyncio.sleep(0.5)

        raw = await client.read_gatt_char(eee2.uuid)
        print(f"Raw ({len(raw)} bytes): {raw!r}")
        try:
            text = raw.decode("utf-8")
            obj = json.loads(text)
            print(json.dumps(obj, indent=2))
        except Exception as e:
            print(f"(not clean JSON: {e})")


def main():
    parser = argparse.ArgumentParser(
        description="BLE discovery tool for KickAss DC-DC / Ultra-X battery monitors"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_scan = sub.add_parser("scan", help="Scan for nearby BLE advertisements")
    p_scan.add_argument("--timeout", type=float, default=10.0)

    p_inspect = sub.add_parser("inspect", help="Connect and dump GATT services/characteristics")
    p_inspect.add_argument("address", help="BLE address/UUID from 'scan'")
    p_inspect.add_argument("--timeout", type=float, default=15.0)

    p_capture = sub.add_parser("capture", help="Connect and log raw bytes from notify/indicate characteristics")
    p_capture.add_argument("address", help="BLE address/UUID from 'scan'")
    p_capture.add_argument("--duration", type=float, default=30.0)
    p_capture.add_argument("--timeout", type=float, default=15.0)

    p_wake = sub.add_parser("wake-scan", help="Try generic wake/handshake payloads against writable characteristics")
    p_wake.add_argument("address", help="BLE address/UUID from 'scan'")
    p_wake.add_argument("--timeout", type=float, default=15.0)

    p_sweep = sub.add_parser("cmd-sweep", help="Sweep single-byte command IDs against one write characteristic")
    p_sweep.add_argument("address", help="BLE address/UUID from 'scan'")
    p_sweep.add_argument("write_char", help="UUID of the write characteristic to send command bytes to")
    p_sweep.add_argument("--start", type=lambda x: int(x, 0), default=0x00)
    p_sweep.add_argument("--end", type=lambda x: int(x, 0), default=0x1F)
    p_sweep.add_argument("--delay", type=float, default=1.5)
    p_sweep.add_argument("--batch-size", type=int, default=2,
                          help="Commands per connection before reconnecting (default 2, safely under the observed 3-command limit)")
    p_sweep.add_argument("--timeout", type=float, default=15.0)
    p_sweep.add_argument("--force", action="store_true",
                          help="Send command bytes even if they're in KNOWN_HAZARDS")

    p_json = sub.add_parser("charger-json", help="Write a JSON-RPC command to eee2 and cleanly read the full response")
    p_json.add_argument("address", help="BLE address/UUID from 'scan'")
    p_json.add_argument("json_command", help='JSON command string, e.g. \'{"id":1,"method":"status","params":{"mac":"411E51","action":"GET"}}\'')
    p_json.add_argument("--timeout", type=float, default=15.0)

    args = parser.parse_args()

    if args.subcommand == "scan":
        asyncio.run(cmd_scan(args))
    elif args.subcommand == "inspect":
        asyncio.run(cmd_inspect(args))
    elif args.subcommand == "capture":
        asyncio.run(cmd_capture(args))
    elif args.subcommand == "wake-scan":
        asyncio.run(cmd_wake_scan(args))
    elif args.subcommand == "cmd-sweep":
        asyncio.run(cmd_cmd_sweep(args))
    elif args.subcommand == "charger-json":
        asyncio.run(cmd_charger_json(args))


if __name__ == "__main__":
    main()
