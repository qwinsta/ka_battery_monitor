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

# Assumed DC-DC conversion loss (charger manual: not 100% efficient
# converting alternator/solar input to battery-side output). Used to
# correct the "total power usage" calculation so charger loss isn't
# misattributed to phantom device draw. Not empirically measured/
# calibrated for your specific charger - revisit if real testing
# suggests a different figure.
CHARGER_EFFICIENCY = 0.9
