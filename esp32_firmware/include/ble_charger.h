// KickAss DC-DC charger: plain JSON-RPC over BLE. Port of charger.py.
// Deliberately forwards raw reassembled JSON text rather than parsing
// and rebuilding it - the dashboard server already accepts an arbitrary
// dict body and tolerates missing/odd fields (see server.py's
// _compute_total_power_usage_w/_compute_time_remaining, both built
// around .get() with defaults), so there's no need to duplicate the
// JSON shape here and risk a transcription bug.
//
// HAZARD - DO NOT write raw bytes to ff01/ff02 on this device: writing
// 0x03 to those characteristics disables the charger's BLE radio
// outright, recoverable only by a physical power cycle. This module
// never touches ff01/ff02 at all - it only uses eee1/eee2.
#pragma once
#include <Arduino.h>

bool chargerConnect();
void chargerDisconnect();
bool chargerIsConnected();

// Which BLE address chargerConnect() (and the JSON-RPC commands' own
// "mac" param) target - defaults to CHARGER_MAC_ADDRESS (config.h) and
// can be overridden at runtime (see main.cpp's checkDeviceConfig()).
// Takes effect on the NEXT chargerConnect() call - call
// chargerDisconnect() first to switch over immediately (per Quinn
// 2026-10-14).
void chargerSetMacAddress(const char *mac);
const char *chargerGetMacAddress();

// Fetches the full status JSON-RPC response as raw, null-terminated text
// into outBuf (capacity outBufSize, including the null terminator).
// Returns false if the read didn't complete - the chunked read protocol
// is a 2-byte length probe on eee1, then repeated reads of eee2 until
// that many bytes arrive.
bool chargerReadStatusRaw(char *outBuf, size_t outBufSize, size_t &outLen);

// Sends the altC SET command (whole amps) and fetches its JSON-RPC
// response, same raw-text contract as above.
bool chargerSetAltCurrentRaw(int amps, char *outBuf, size_t outBufSize, size_t &outLen);
