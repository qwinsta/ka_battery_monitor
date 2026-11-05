"""Device addresses for the direct-BLE path (poller.py, when running this
on a machine with its own Bluetooth adapter rather than the ESP32
bridge - see run_server_ingest.py vs. plain server.py+poller.py).

These are placeholders - replace them with your own battery/charger's
addresses before running poller.py. Find them with:

    python3 ble_tool.py scan

On macOS, bleak reports devices as CoreBluetooth UUIDs (per-Mac handles,
not real MAC addresses); on Linux these are real BLE MAC addresses
instead - bleak accepts either depending on platform.

If you're running the ESP32 bridge (ingest_poller.py / run_server_ingest.py,
the recommended path - see the README), these addresses don't matter at
all: the ESP32 has its own addresses in esp32_firmware/include/config.h,
and once that's flashed you can change them live from the dashboard's
Settings page (including scanning for nearby devices there) without
touching this file or reflashing anything.
"""

BATTERY_ADDRESS = "REPLACE-WITH-YOUR-BATTERY-ADDRESS"

CHARGER_ADDRESS = "REPLACE-WITH-YOUR-CHARGER-ADDRESS"
CHARGER_MAC = "REPLACEMAC"  # real BD address (no colons), used inside JSON-RPC commands - see charger.py

ALT_CURRENT_MIN = 5
ALT_CURRENT_MAX = 56
ALT_CURRENT_DEFAULT = 40

# Note: there used to be a CHARGER_EFFICIENCY constant here, an assumed
# 10% DC-DC conversion loss used to correct "total power usage" so the
# charger's own internal loss wasn't misattributed to phantom device
# draw. Removed (per Quinn 2026-11-06) because it was a guess the
# calculation no longer needs: the charger's own status response
# already reports a separate, independently-measured "batt" v/c/p group
# (see PROTOCOL.md) at its own battery-side terminals, not derived from
# the alternator/solar input side or the battery's BMS at all. Comparing
# that directly against the battery's own reading needs no efficiency
# correction, since both are already on the battery side - see
# server.py's _compute_total_power_usage_w and
# storage.avg_total_power_usage.
