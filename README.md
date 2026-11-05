# KickAss Battery Monitor

A live dashboard for KickAss's BLE-equipped lithium battery and DC-DC/MPPT
charger - built for a 4WD/overlanding dual-battery setup, but nothing
about it is vehicle-specific. Runs continuously on a small router or any
always-on Linux box, polls both devices over Bluetooth Low Energy, and
serves a real-time web dashboard (voltage, current, SOC, charge stage,
solar/alternator input, time-to-full/empty) plus history graphs, from
your phone or any browser on the network.

![status](https://img.shields.io/badge/status-working-brightgreen)

## Why this exists

KickAss's own app works, but it's phone-only, doesn't log history, and
can't be left running unattended. This project talks to the same two
BLE devices directly - reverse engineered from a packet capture of the
manufacturer's app (see [`PROTOCOL.md`](PROTOCOL.md) for the full
findings) - and turns them into a proper always-on dashboard with
history, graphs, and a few conveniences the official app doesn't have
(e.g. a combined "time to full" estimate that correctly picks between
solar and alternator charging, whichever's actually active).

## What it looks like

- **Live dashboard**: voltage, current, power, SOC, charge stage, cell
  voltages, temperature, for both the battery and the charger's
  alternator/solar inputs.
- **History graphs**: battery current/power/SOC, charging sources, and
  total power usage, with a day picker and a few preset ranges (15m
  through 24h) plus drag-to-scroll.
- **Time to full/empty**: computed from the battery's own measured
  current (not a fixed assumption), with a secondary estimate that
  picks whichever source - solar or alternator - is actually charging
  right now and uses its real, live output.
- **Alternator current control**: adjust the charger's alternator input
  current limit from the dashboard.
- **Settings page**: change which battery/charger it's connected to
  (including a "scan for nearby devices" tool that recommends matches
  by name), remote-restart the BLE bridge if a connection gets stuck,
  and a few display preferences.

## Hardware you'll need

| Item | Notes |
|---|---|
| KickAss 230Ah (or similar) LiFePO4 battery with BLE | The one with the built-in Bluetooth BMS - look for Bluetooth/app connectivity in the listing. |
| KickAss DC-DC/MPPT charger with BLE | Charges from alternator and/or solar; also Bluetooth-enabled. |
| ESP32 dev board | Any genuine **ESP32-WROOM-32** DevKit. Avoid **ESP32-S2** specifically (WiFi-only, no Bluetooth); ESP32, ESP32-S3, and ESP32-C3 all have BLE. A ~$6-10 board. |
| A small always-on computer to run the dashboard server | A travel/OpenWrt router (what this was built against - see "Router deployment" below), a Raspberry Pi, an old laptop, or anything else that can stay powered and run Python continuously. |
| USB cable + 5V power source for the ESP32 | It talks to everything over WiFi, so it doesn't need to be near the router/server - just near the battery/charger, within normal BLE range. |

You do **not** need a separate solar charge controller - the KickAss
charger handles both alternator and solar input itself; this project
just reads its output. You also don't strictly need the router: the
dashboard server is just a Python/FastAPI app and will run on anything
that can stay on - a Raspberry Pi is a perfectly good choice if you'd
rather not dedicate a router to it.

## How it fits together

```
KickAss battery  --BLE-->   ESP32 (bridge)   --WiFi/HTTP-->  dashboard server
KickAss charger  --BLE-->                                     (FastAPI + SQLite)
                                                                      |
                                                                 your browser
```

The ESP32 exists because most small routers have no usable native
Bluetooth, and a cheap, well-supported microcontroller with real BLE
hardware sidesteps that entirely (the same pattern Home Assistant's
Bluetooth Proxy uses). It connects to both devices over BLE and relays
everything to the dashboard server over plain WiFi/HTTP.

If the machine running the dashboard server *does* have working
Bluetooth of its own (e.g. a Mac, or a Pi with its onboard BLE), you can
skip the ESP32 entirely and connect directly - see "Direct BLE" below.

## Software setup

```bash
git clone https://github.com/qwinsta/ka_battery_monitor.git
cd ka_battery_monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Try it without any hardware first

```bash
python3 emulator/run_server.py
```

Runs the real dashboard server against scripted test data instead of
real BLE hardware, so you can see the whole app - the API, graphs, the
alt-current slider - before anything is wired up. See
[`emulator/README.md`](emulator/README.md).

### 1. Find your devices' BLE addresses

```bash
python3 ble_tool.py scan
```

Run it near the battery/charger (power them on first). Look for names
containing something like `LT23SX...` (battery) and `KADCDC...`
(charger) - adjust `TARGET_NAME_HINTS` near the top of `ble_tool.py` if
your devices advertise under different names. Note down both addresses.

### 2a. ESP32 bridge (recommended)

This is the path that's actually been run continuously in a vehicle.
See [`esp32_firmware/README.md`](esp32_firmware/README.md) for full
setup - in short: copy `esp32_firmware/include/config.h.example` to
`config.h`, fill in your WiFi details and the addresses from step 1 (or
just leave placeholders and use the dashboard's own device scanner
later), flash over USB with [PlatformIO](https://platformio.org/), then
run the dashboard server with the ESP32-ingest backend:

```bash
python3 run_server_ingest.py --host 0.0.0.0 --port 8080
```

### 2b. Direct BLE

If the machine running the server has its own Bluetooth adapter, skip
the ESP32 entirely: put your addresses from step 1 into `config.py`,
then run

```bash
python3 -c "import uvicorn, server; uvicorn.run(server.app, host='0.0.0.0', port=8080)"
```

(`poller.py` is the direct-BLE backend `server.py` uses by default - no
module swap needed, unlike the ESP32 path above.)

### 3. Open the dashboard

`http://<the server's IP>:8080` (or whatever port you chose) from any
browser on the same network, including your phone.

## Router deployment (optional, for a permanent install)

If you want this running permanently on an OpenWrt-based travel router
(as opposed to a Pi or always-on PC), `deploy/` has a starting point:

- `deploy/ka-battery-monitor.init` - a procd service script. Copy to
  `/etc/init.d/ka-battery-monitor` on the router, `chmod +x` it, then
  `enable` and `start` it. Runs `run_server_ingest.py` (the ESP32-bridge
  backend) on port 5000.
- `deploy/zz-batmon.conf` - an optional nginx snippet that proxies a
  friendly hostname (e.g. `http://batmon.local/`) to port 5000, so you
  don't have to type the port. Adjust `server_name` to whatever hostname
  suits your router. See the file's own comments for an important
  filename-ordering gotcha if your router's nginx config is split across
  multiple files.
- `requirements-router.txt` - dependency versions pinned for a
  musl-libc/aarch64/Python-3.9 router environment (a common combination
  on small OpenWrt devices) where `pip install -r requirements.txt`'s
  "latest" picks can require a from-source build your router has no
  compiler for. Check your own router's Python version and libc before
  assuming these exact pins apply - they were worked out for one
  specific device and firmware version, not universally.

You'll need to get `server.py`, `storage.py`, `ingest_poller.py`,
`run_server_ingest.py`, and `static/` onto the router (`scp` works fine
if it has SSH) and install `requirements-router.txt` there.

## Project layout

```
server.py              FastAPI app - the dashboard's web/API server
storage.py              SQLite-backed history storage
poller.py               Direct-BLE backend (bleak) - polls both devices itself
ingest_poller.py        ESP32-bridge backend - fed by HTTP pushes instead
battery.py / charger.py BLE protocol implementations, used by poller.py
                        and ported to C++ in esp32_firmware/ for the bridge
ble_tool.py             BLE discovery/inspection CLI - also how the
                        protocols in PROTOCOL.md were originally found
run_server_ingest.py    Entry point: server.py + ingest_poller.py (ESP32 path)
config.py               Direct-BLE device addresses + tunables
static/                 Frontend (vanilla HTML/CSS/JS, no build step)
esp32_firmware/         ESP32 BLE bridge firmware (PlatformIO/Arduino)
emulator/               Scripted test-data server for development without hardware
deploy/                 OpenWrt router deployment files
PROTOCOL.md             BLE protocol reverse-engineering notes for both devices
```

## Known limitations / things to watch

- The dashboard assumes a 10% DC-DC conversion loss when estimating
  total power draw (`config.py`'s `CHARGER_EFFICIENCY`) - not measured
  for your specific charger, just the manufacturer's manual figure.
- History retention is governed by a 25MB SQLite size cap
  (`storage.py`'s `MAX_DB_SIZE_BYTES`), not a fixed day count - how far
  back it reaches depends on your actual sample rate.
- OTA firmware updates to the ESP32 have proven unreliable from some
  network paths (e.g. over a VPN hop, or a congested WiFi link) even
  when the device is otherwise reachable - see
  `esp32_firmware/README.md`'s OTA section. USB is the reliable fallback.
- This talks to *specific* KickAss BLE device firmware versions, as
  captured and documented in `PROTOCOL.md`. If KickAss changes their
  firmware's BLE protocol, this may need updating to match.

## Contributing

Issues and PRs welcome, especially from anyone running this against
their own KickAss (or similarly-protocoled) hardware - more real-world
BLE captures in `PROTOCOL.md` only make this more robust.

## License

MIT - see [`LICENSE`](LICENSE). Not affiliated with KickAss; this is an
independent, reverse-engineered client for their public BLE interface.
