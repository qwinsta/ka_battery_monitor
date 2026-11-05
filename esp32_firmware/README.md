# ESP32 BLE bridge

Connects to the battery and charger directly over BLE (same protocols as
`battery.py`/`charger.py` - see `../PROTOCOL.md` for how those protocols
were reverse engineered in the first place) and pushes readings to the
dashboard server's ingest API (`../ingest_poller.py`) over WiFi.

**Why this exists:** many travel/OpenWrt routers have no usable native
USB Bluetooth path of their own (clone Bluetooth dongles are a common
source of trouble, and plenty of routers' kernels don't have the
Realtek/Broadcom driver modules compiled in as a fallback either). An
ESP32 running its own BLE client firmware sidesteps all of that - it's
the community-standard pattern for "add reliable BLE to something that
doesn't do it well" (see Home Assistant's Bluetooth Proxy).

Confirmed working against real KickAss DC-DC charger + Ultra-X battery
hardware - both BLE connections, all pushes, the alt-current control
loop, device-address updates, and the BLE device scanner all verified
live. Watch the Serial monitor at 115200 baud if something's misbehaving
- every connect/read/push step logs a line there.

## Hardware

Any genuine ESP32-WROOM-32 DevKit board. These are all real Espressif
silicon regardless of board brand - just avoid **ESP32-S2** specifically
(WiFi-only, no Bluetooth at all); plain ESP32, ESP32-S3, and ESP32-C3 all
have BLE.

Power: any 5V USB source. It talks to everything over WiFi, so it
doesn't need to be physically near your router - it can sit right next
to the battery/charger instead.

## Setup

1. Install [PlatformIO](https://platformio.org/) (the VS Code extension
   is the easiest way in).
2. Copy `include/config.h.example` to `include/config.h` and fill in:
   - `WIFI_SSID` / `WIFI_PASSWORD` - your own WiFi network.
   - `SERVER_BASE_URL` - where the dashboard server is running. For
     initial bring-up, point this at your own computer
     (`python3 run_server_ingest.py` from the project root) rather than
     a router - much faster iteration.
   - `OTA_PASSWORD` - pick your own (see "Wireless updates" below).
   - `BATTERY_MAC_ADDRESS` / `CHARGER_MAC_ADDRESS` - leave the
     placeholders for now; find your real ones with
     `python3 ble_tool.py scan` from the project root (run it a few
     times near the devices if they don't show up immediately), or just
     flash with the placeholders and use the dashboard Settings page's
     "Scan for devices" button once it's up and running - either way,
     you don't need to reflash to change these later, see below.
3. Build and upload over USB: `pio run -e esp32dev -t upload` (or the
   PlatformIO extension's Upload button), then open the Serial monitor to
   watch it connect.
4. `config.h` is gitignored on purpose (it has your WiFi password in
   it) - don't commit your filled-in copy anywhere public.

## Changing which battery/charger it connects to, without reflashing

Once it's running, the dashboard's Settings page can point this bridge
at a different battery/charger - including scanning for nearby devices
and recommending matches by name - entirely from the web UI. The ESP32
polls for address changes, applies them, reconnects, and persists the
new address to flash so it survives a reboot. `config.h`'s
`BATTERY_MAC_ADDRESS`/`CHARGER_MAC_ADDRESS` only matter for a completely
fresh device that's never received a setting from the dashboard yet.

## Wireless updates (OTA)

Once this OTA-enabled build is on the device (needs one USB flash first -
`min_spiffs.csv`'s dual app partitions are what make OTA possible at all,
see `platformio.ini`'s comment), future firmware changes can be pushed
over WiFi instead of needing the USB cable again:

```
pio run -e esp32dev_ota -t upload
```

This targets the device's IP (`platformio.ini`'s `upload_port` - check
your router's DHCP leases, or set a static/reserved IP for the ESP32 so
this doesn't change) using the password in `OTA_PASSWORD` (`config.h`) -
keep `platformio.ini`'s `--auth` flag in sync if that's ever changed.
The password only needs to stop an accidental push from a random device
on the network, not resist a determined attacker - your WiFi password
and LAN isolation are the real boundary.

**Caveat found the hard way:** OTA's UDP-based handshake has proven
unreliable from some network paths (e.g. over a VPN/Tailscale hop, or on
a congested WiFi link) even when the device is otherwise reachable -
plain HTTP (ping, the dashboard itself) working fine is not a reliable
predictor that OTA specifically will succeed. If OTA keeps failing with
"no response"/"no answer to authentication" from a network path that
should work, try it from a machine on the exact same LAN segment as the
router, or fall back to USB.

## What it does

- Connects to the battery, reads basic info every ~1s (voltage, current,
  power, SOC, capacity, temperature) plus cell voltages/device name every
  5th cycle, and POSTs the decoded JSON to `/api/ingest/battery`.
- Connects to the charger, reads full status every ~2s and forwards the
  **raw JSON text** to `/api/ingest/charger` unmodified - no need to
  parse and rebuild it here; the server already tolerates an arbitrary
  dict body.
- Polls `/api/pending-alt-current` every ~1.5s; if the dashboard's
  alternator-current slider has a pending change, executes it on the
  charger and reports the result back via
  `/api/ingest/alt-current-result`.
- Polls `/api/pending-scan` on the same cadence; if the Settings page's
  "Scan for devices" button has been pressed, disconnects from the
  battery/charger, runs an 8-second BLE scan, and reports named nearby
  devices back - letting someone find their own battery/charger's
  address without a second BLE-capable computer.
- Polls `/api/device-config` every 30s for a battery/charger address
  change from the Settings page (see above) and `/api/pending-restart`
  on the fast cadence for the Settings page's "Restart BLE" recovery
  button (a full `ESP.restart()` - the same thing a physical power cycle
  does, triggerable from the dashboard instead).
- All of this is deliberately outbound-only from the ESP32's side (it
  polls, it never listens) - no inbound exposure to worry about.
- Both BLE connections are held open persistently and only reconnected on
  a read failure - reconnecting every cycle is far too slow for
  near-real-time updates (same reasoning as `poller.py`).

## Hazard - do not touch

Per `../PROTOCOL.md`: writing `0x03` to the charger's `ff01`/`ff02`
characteristics disables its BLE radio outright, recoverable only by a
physical power cycle. This firmware never touches those characteristics
at all (it only uses `eee1`/`eee2`), but if you're extending it, don't
go near `ff01`/`ff02`
on the charger.

## Debugging

Watch the Serial monitor first - every connect attempt, read, and push
logs a line. Likely first issues to check, in order:
1. WiFi connects but BLE connect fails - double check the MAC addresses
   in `config.h` (or whatever's currently set via the Settings page)
   match what `python3 ble_tool.py scan` reports, or use the dashboard's
   own "Scan for devices" button.
2. BLE connects but reads fail - check `BLE_CONNECT_TIMEOUT_MS` and the
   per-request timeouts in `config.h` are generous enough; these
   devices' own protocols are already known-slow in places (the charger
   in particular does a multi-step chunked read - see `../PROTOCOL.md`).
3. Everything connects/reads but pushes fail - check `SERVER_BASE_URL` is
   reachable from wherever the ESP32 actually is on the network (not just
   from your own computer), and that the dashboard server is actually
   running with the ingest backend (`run_server_ingest.py`, not plain
   `server.py` with the real bleak-based `poller.py`).
