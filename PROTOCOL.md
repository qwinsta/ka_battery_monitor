# Protocol findings

Working notes from the initial BLE discovery phase (`ble_tool.py`) -
how the KickAss DC-DC charger and Ultra-X battery's BLE protocols were
reverse engineered, and what was found. See the main `README.md` for
how this fits into the rest of the project.

## DC-DC / MPPT charger — KADCDC40ABTB-411E51

- Advertised name: `KADCDC40ABTB-411E51`
- macOS CoreBluetooth address (this Mac only, not a real MAC):
  `E9357704-3621-94E9-A374-CADBFAF2381D`

### GATT layout
Three custom services. The one that matters:
- Service `02f00000-0000-0000-0000-00000000fe00`
  - `02f00000-0000-0000-0000-00000000ff01` — write / write-without-response.
    Command channel.
  - `02f00000-0000-0000-0000-00000000ff02` — read + notify. Response channel.
    Default/idle read value is the literal ASCII string `ntf_enable`
    (a firmware placeholder, not real data).
  - `...ff00`, `...ff03` — read-only, empty at rest. Unclear purpose.
- Service `0000fff0-0000-1000-8000-00805f9b34fb` (classic HM-10-style UART
  bridge: `fff3` write / `fff4` notify) — present but never got a response to
  anything tried. Possibly unused by this firmware, or needs its own
  handshake.
- Service `0000eee0-0000-1000-8000-00805f9b34fb` (`eee1`/`eee2`/`eee3`) —
  present but unresponsive. Note: `eee1` advertises `notify` but has **no**
  standard CCCD (0x2902) descriptor, so it can't actually be subscribed to —
  likely a firmware bug/leftover.

### Command/response behaviour (ff01 -> ff02)
Plain CCCD subscription alone produces nothing. Writing a single byte to
`ff01` gets a structured reply on `ff02`, shaped roughly like
`[status][cmd_echo][...payload]`:

| cmd  | response           | notes |
|------|---------------------|-------|
| 0x00 | `0000010011`         | valid, status=00 |
| 0x01 | `0001040000800200`   | valid, status=00 |
| 0x02 | `0002040000000000`   | valid, status=00 |
| 0xFF | `02ff0000`            | status=02 - looks like "unknown command" |
| **0x03** | **DO NOT SEND** | see hazard below |

Payload bytes not yet decoded against real sensor values (charger wasn't
under significant load during testing).

### ⚠️ Hazard — confirmed 2026-09-13
Writing `0x03` to `ff01` **disables the charger's BLE radio outright** —
not just a disconnect, the unit stops advertising entirely and cannot be
reconnected to until the charger's power is physically cycled. Confirmed
twice.

This is now hard-blocked in `ble_tool.py` (`KNOWN_HAZARDS`) — `cmd-sweep`
refuses to send it without `--force`. **Do not pass `--force` for this one.**

Given this device is live power electronics (not just a sensor), further
blind command-byte guessing on it is not worth the risk — an unknown command
could plausibly touch charge parameters, not just BLE state. Next step for
this device should be passive traffic capture of the real app (see below),
not more brute forcing.

Also worth noting: this device appears to hard-close the BLE link after
~3 request/response cycles per connection regardless of which commands were
sent (0x00/0x01/0x02 all succeeded, then the 4th write of any session
failed with a disconnect) — separate from the 0x03 hazard. `cmd-sweep`
handles this by reconnecting every `--batch-size` (default 2) commands.

## Ultra-X 230Ah LiFePO4 battery — LT23SX2726A2026E60109

- Advertised name: `LT23SX2726A2026E60109` (truncated to
  `LT23SX2726A2026E60` in iOS's Bluetooth settings list)
- Real MAC (confirmed via manufacturer_data in the advert, decodes to the
  same value captured earlier from the app): `1C:C8:C1:4E:58:CE`
- macOS CoreBluetooth address (this Mac only):
  `F1C7A354-94DC-2D0D-A5B7-2A64ED2B885F`

### GATT layout
Single custom vendor service `0000ff00-0000-1000-8000-00805f9b34fb`:
- `0000ff01` — read + notify, 176-byte buffer, all-zero at rest. Main data
  channel (presumed).
- `0000ff02` — read + write-without-response, 128-byte buffer, all-zero at
  rest. Presumed command/request channel.
- Plus a standard Device Information service (0x180A) and one unrelated
  custom characteristic (`00010203-...-2b12`), neither useful.

### SOLVED — this is a standard JBD / Xiaoxiang "Smart BMS" protocol
Confirmed 2026-09-14 from a real PacketLogger capture of the KickAss battery
app (`iPhone Packet capture 14-09-26.pklg`, captured via the iOS Bluetooth
diagnostic profile + PacketLogger, no extra hardware). Value handle for
`ff01` is 0x11, for `ff02` is 0x15 (differs from the declaration handles
16/20 that `bleak`'s `inspect` reports).

This is why single-byte brute forcing (`wake-scan`) never got a response -
it needs a full framed request, not a bare byte.

**Frame format** (both directions), all multi-byte data fields big-endian:
- Request: `DD A5 <register> <len> [data...] <checksum:2> 77`
- Response: `DD <register> <status> <len> <data...> <checksum:2> 77`
- Checksum = two's complement of `sum(len byte + data bytes)`, as uint16.
  e.g. register 0x03 request has len=0, so checksum = `0x10000 - 0x03 =
  0xFFFD` → bytes `FF FD`.
- Responses to a single request can arrive split across multiple ATT
  Handle Value Notifications on `ff01` - concatenate the raw notification
  values (stripping only the 3-byte ATT notification header, not anything
  from the JBD frame itself) before parsing.

**Confirmed registers:**
- `0x03` "basic info" - request `DD A5 03 00 FF FD 77`. Response data
  layout (offsets within the `data` field, all big-endian):
  | offset | field | unit |
  |--------|-------|------|
  | 0-1 | total voltage | x10mV |
  | 2-3 | current (signed) | x10mA |
  | 4-5 | remaining capacity | x10mAh |
  | 6-7 | nominal/rated capacity | x10mAh |
  | 8-9 | cycle count | 1 |
  | 10-11 | production date | packed y/m/d |
  | 12-13 | balance status low | bitmask |
  | 14-15 | balance status high | bitmask |
  | 16-17 | protection status | bitmask |
  | 18 | software version | 1 |
  | 19 | RSOC | % |
  | 20 | FET status | bitmask |
  | 21 | number of cells (strings) | 1 |
  | 22 | number of NTC sensors | 1 |
  | 23+ | NTC temperatures, 2 bytes each | x0.1K |

  Sample decoded values across the capture (charger was actively pushing
  current into it at the time): 13.39-13.57V, 0-36.3A, remaining
  223-224Ah, **nominal capacity a rock-steady 230.00Ah across every sample
  in the session**, RSOC 97-98%, 4 cells, 2 NTC sensors.
- `0x04` "cell voltages" - request `DD A5 04 00 FF FC 77`. Data = 2 bytes
  per cell, x1mV. Saw ~3.35V/cell, well balanced.
- `0x05` "device name" - request `DD A5 05 00 FF FB 77`. Data = ASCII
  string, decoded to `DP04S019-L4S-250` (the underlying module's model
  string, not the KickAss branding).
- `0x00` - request `DD A5 00 00 FF FF 77`(ish, seen as
  `dda50000000077`)/response `DD 00 00 02 00 05 FF F9 77`. Purpose not
  confirmed - small 2-byte payload, low priority.

### Re: the "rated capacity shows 10mAh" scare (2026-09-14)
Suspected at first to be fallout from writing `0x0A` to `ff02` during the
2026-09-11 `wake-scan` session (`0x0A` is decimal 10, an eerie match).
**Checked directly against this capture and it does not hold up**:
register `0x03`'s nominal-capacity field reads 230.00Ah consistently
across every sample in the whole session (see table above) - the device's
own BMS chip is reporting the correct value.

Full write audit (2026-09-14): the *only* file that ever wrote to the
battery, ever, is `discovery_logs/wakescan_F1C7A354..._20260911_064242.json`
- one `wake-scan` run at 06:42:42 UTC on 2026-09-11. `cmd-sweep` was never
pointed at the battery (only ever run against the charger). So the
complete list of every byte ever sent to the battery is the 10
`WAKE_CANDIDATES` bytes (`0x01, 0x00, 0xFF, 0x0A, 0x02, '1', '0', 'A',
"AT\r\n", "on\r\n"`), all to `ff02`, all write-without-response, all in
that one session. Given the confirmed frame format needs `DD A5 <reg> ...`
with a valid checksum, none of those 10 bytes are valid JBD frames - the
BMS almost certainly ignored all of them outright.

**Update 2026-09-14 (later same day):** user deleted and reinstalled the
KickAss battery app from scratch, reconnected fresh - still shows 10mAh.
This rules out simple local app cache as the explanation (a fresh install
has no prior local state to be stale). Two remaining theories, neither
confirmed:
1. It's a genuine device-side value, sourced from somewhere in the JBD
   register set *other than* the `0x03` basic-info nominal-capacity field
   (which we've directly confirmed reads 230.00Ah) - maybe a separate
   "design capacity" EEPROM parameter that basic-info doesn't reflect.
2. It's sourced from a KickAss-side lookup (e.g. by serial number/model,
   possibly even a remote/cloud lookup) unrelated to any BLE register at
   all - in which case no BLE write could ever change what's displayed.

### RESOLVED 2026-09-14: not a device issue, no write needed
Got the missing capture (`iPhone Packet capture 14-09-26 battery.pklg`,
battery app only, post-reinstall, opened straight to the screen showing
"Rated Capacity: 10mAH" and the broken "Time to Full: -26H:28m"). Findings:

- Decoded basic-info nominal-capacity again from this session: **230.00Ah**
  - same as every other capture, still correct.
- **Every single command the app sent was a read** (`0xA5` registers
  `0x00`, `0x03`, `0x04`, `0x05` only) - zero `0x5A` write-register
  commands anywhere in the session. The real app has never once tried to
  write a capacity value to this device.

Conclusion: theory 1 above (a separate writable EEPROM field) is ruled
out - there's no write of any kind for the app to have used, correct or
not. This has to be theory 2: the "10mAh"/broken time-to-full is a bug in
KickAss's own app (misreading its own correct 230.00Ah read, or a
lookup-table default for an unrecognized serial/model) - not a device
fault, not something we caused, and **not something any BLE write could
fix**, since the display isn't sourced from a writable register at all.
Treat as closed - it's a bug for KickAss to fix in their app, out of
scope for this project.

## DC-DC / MPPT charger — real protocol found (2026-09-14)
A second capture (`iPhone Packet capture 14-09-26 dc-dc.pklg`), taken with
only the charger app open, force-quit + reconnected, engine cycled on/off
(alternator + solar both active), and the ALT max-current slider swept
40A→23A→40A→55A→5A→40A, revealed the **real** control/telemetry interface.
It is completely different from the raw `02f00000.../ff01+ff02` byte
interface `wake-scan`/`cmd-sweep` were poking at - the real app never
touches that interface at all. That explains why `0x03` there behaved so
unpredictably: it's likely an unused/legacy/debug path, not the real
control surface. **Recommendation: abandon `ff01`/`ff02` entirely.**

### Real interface: JSON-RPC over the `eee0` service
- `0000eee2` (handle varies by connection, resolve by UUID not raw number)
  - write target for JSON-RPC command objects, e.g.
    `{"id":1,"method":"status","params":{"mac":"411E51","action":"GET"}}`
- `0000eee1` - read once after writing a command: returns the pending
  response's length as a plain 2-byte big-endian integer (not
  `[marker, length]` - see "SOLVED" note below), not JSON content itself
- the JSON response itself is read back from **`eee2` - the same
  characteristic you wrote the command to** - by issuing repeated plain
  ATT Read Requests, ~20-30 bytes per read, advancing a server-side cursor
  (corrected 2026-09-14 - earlier notes wrongly attributed these reads to
  `eee1`, which corrupted reassembly by mixing in that 2-byte probe)
- `0000eee3` - notify characteristic that **spontaneously pushes** the same
  kind of JSON status blob periodically, chunked across multiple Handle
  Value Notifications, without needing to send a GET first

**Confirmed working SET command** (`method: "altC"` = alternator max
current limit, in whole amps, plain decimal in the JSON, no
encoding/scaling):
```json
{"id":668,"method":"altC","params":{"mac":"E05319411E51","action":"SET","value":40}}
```
Verified against the real slider sweep - captured values `23`, `40`, `55`,
`5`, `40` matched the on-screen slider moves exactly. `mac` is the
charger's real BD address as a plain hex string (`E05319411E51`, no
colons) - confirmed this also appears truncated as `411E51` in the
`status` GET command's `mac` param, matching the last 3 bytes advertised
in the device name `KADCDC40ABTB-411E51`.

### SOLVED 2026-09-14: full status schema confirmed live
The reassembly issue was two separate bugs, both now fixed in `charger.py`:
1. The `eee1` probe is a **2-byte big-endian length**, not `[marker, byte]`
   as first assumed - reading only the second byte truncated a ~590-byte
   response to under 256 bytes (this, not data loss, is why every earlier
   capture-based reassembly attempt cut off at the same spot - the real
   app apparently has the same bug and just displays whatever partial
   data it gets).
2. `eee2` must be re-read repeatedly (~20-30 bytes/read) until the full
   length is reached - a single `bleak` read only ever returns one chunk.

**Confirmed full `status` GET response shape** (real values from a live
read, engine running):
```json
{
  "id": 1, "statusMsg": "Get data success", "mac": "E05319411E51", "code": 200,
  "result": {
    "deviceInfo": {
      "name": "KADCDC40ABTB-411E51", "type": "KADCDC40ABTB",
      "pcuFwVersion": 149, "pcuHdVersion": 10,
      "bootloaderVersion": 114, "bleFwVersion": 31
    },
    "batteryInfo": { "type": 4, "vGrade": 1, "chargeState": 1, "temperature": 49 },
    "deviceWarnId": 0,
    "altInputC": 40,
    "alternator": { "v": 12.1, "c": 0,   "p": 0 },
    "panel":      { "v": 23.5, "c": 2.1, "p": 49.35 },
    "batt":       { "v": 13.4, "c": 3.4, "p": 45.56 }
  }
}
```
`altInputC` is the alternator max-current setting (matches whatever was
last sent via `altC` SET). `batteryInfo.type`/`vGrade` likely correspond
to the LiFePO4/Bulk mode toggle in the app; `chargeState` is presumably
an enum (bulk/absorption/float/etc - not yet decoded, only seen `1`).
`temperature` (49 here) is plausibly the charger's own internal
temperature, not ambient - not cross-checked against the app yet.

This completes Phase 1 for the charger - `charger.py get_status()` and
`set_alt_max_current()` are both confirmed working against real hardware.

**Next step for this device:** decode the full JSON status shape (a
capture with ONLY periodic `eee3` notifications, i.e. never issuing a
`status` GET at all, would remove the interleaving and should reassemble
cleanly), and use the same JSON-RPC approach to find the read/status
equivalents rather than anything on `ff01`/`ff02`.

## Tooling
`ble_tool.py` subcommands (see `--help` on each):
- `scan` — list nearby BLE adverts, flag the two targets by name
- `inspect <address>` — dump GATT services/characteristics, read anything
  readable
- `capture <address>` — subscribe to all notify/indicate characteristics,
  log raw bytes
- `wake-scan <address>` — try a handful of generic single-byte "wake"
  payloads against every writable characteristic
- `cmd-sweep <address> <write_char>` — sweep a range of single-byte command
  IDs against one write characteristic, reconnecting every `--batch-size`
  commands; refuses known-hazardous bytes without `--force`
