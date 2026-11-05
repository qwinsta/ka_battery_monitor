# Dashboard emulator

Runs the real dashboard server (`server.py`, unmodified) against scripted
test data instead of real BLE hardware, so you can test the app - the
API, storage/history, graphs, the alt-current slider, the frontend -
without being near the vehicle. It does **not** emulate Bluetooth itself;
once the GL-iNet's BLE adapter arrives, testing that connection is a
separate, real step.

## Run it

```bash
cd ka_battery_monitor  # the cloned repo root
source venv/bin/activate
python3 emulator/run_server.py
```

Then open `http://localhost:8080` - same dashboard, fed simulated data.

Options: `--scenario <path>` (default `emulator/scenario.csv`), `--speed <simulated seconds per real second>` (default 20), `--start-soc <percent>` (default 50), `--port` (default 8080).

## How it works

This isn't a lookup table of canned values - `battery_sim.py` runs a real
coulomb-counting simulation of the battery (open-circuit-voltage curve by
SOC, voltage sag under load via an assumed internal resistance, low-
voltage protection cutoff) driven by three inputs you actually control.
Validated against a real measurement (2026-09-14): running a 2000W
induction hob off the battery for ~8 minutes drained about 10% in
reality: this model predicts 9.7% for that exact scenario.

## Editing the scenario

Open `emulator/scenario.csv` in any text editor or spreadsheet app. Each
row holds three inputs for `duration_s` simulated seconds, then moves to
the next row (loops back to the first at the end):

```
load_w,solar_w,alt_current_a,duration_s
2000,0,0,480
```

- `load_w` - total power draw of everything connected (e.g. 2000 for an induction hob via an inverter). Always a positive draw.
- `solar_w` - solar input, watts (realistic range ~1-160 for this rig).
- `alt_current_a` - alternator/DC-DC output current, amps (5-56) - clamped to whatever the ALT max-current *setting* currently is, so the dashboard's Settings slider has a real effect here too.
- `duration_s` - how long to hold these inputs, in *simulated* seconds (so `480` = 8 minutes of simulated time, which plays out in 480/speed real seconds).
- Battery voltage/current/SOC/temperature are never entered directly - they're computed by the simulation. The charger's `batt` reading mirrors the battery automatically.
- Saving a change reloads the script immediately - the battery's current simulated state carries over, only the upcoming inputs change, so you can tweak mid-run without losing where the battery currently is.
- Lines starting with `#` and blank lines are ignored.

The included `scenario.csv` scripts the actual induction-hob test five
times in a row (walking the battery down past the 10% mark so you can see
the low-voltage protection trip and its recovery), then a long recharge.

## Data isolation

The emulator writes to its own `emulator/emulator_history.sqlite3`, never
the real `history.sqlite3` in the project root - scripted test data can
never contaminate real vehicle history, and the two can run at different
times without conflicting (though not literally at once, since both
would want the same port unless you pass `--port`).
