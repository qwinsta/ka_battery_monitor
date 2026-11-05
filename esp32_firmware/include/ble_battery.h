// JBD/Xiaoxiang "Smart BMS" protocol.
// Port of battery.py. Read-only: only ever sends the documented
// read-register request frames (0xDD 0xA5 <reg> 0x00 <checksum> 0x77).
#pragma once
#include <Arduino.h>

struct BatteryReading {
  bool valid = false;
  float voltage = 0;
  float current = 0;          // positive = charging
  float power = 0;
  int soc_percent = 0;
  float remaining_ah = 0;
  float nominal_ah = 0;
  int cycles = 0;
  bool charging = false;
  float temperature_c = 0;
  int num_ntc = 0;
  float ntc_temps_c[4] = {0};
  int num_cells = 0;
  float cell_voltages[8] = {0};
  char device_name[32] = {0};
  bool has_cells_and_name = false;
};

// Connects (or reconnects) to the battery. Call this after any read
// failure - matches poller.py's "drop the connection and retry" pattern
// rather than trying to recover a half-broken session.
bool batteryConnect();
void batteryDisconnect();
bool batteryIsConnected();

// Which BLE address batteryConnect() targets - defaults to
// BATTERY_MAC_ADDRESS (config.h) and can be overridden at runtime (see
// main.cpp's checkDeviceConfig(), which fetches a possibly-different
// address from the dashboard server's Settings page). Takes effect on
// the NEXT batteryConnect() call, not retroactively on an already-open
// one - call batteryDisconnect() first if a live connection should
// switch over immediately (per Quinn 2026-10-14).
void batterySetMacAddress(const char *mac);
const char *batteryGetMacAddress();

// Fast round-trip (register 0x03) - safe to call at ~1s cadence on an
// already-open connection.
bool batteryReadBasicInfo(BatteryReading &out);

// Slower (registers 0x04 + 0x05) - call less often (e.g. every 5th poll).
bool batteryReadCellsAndName(BatteryReading &out);
