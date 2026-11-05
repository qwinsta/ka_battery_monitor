#include "ble_charger.h"
#include "config.h"
#include <NimBLEDevice.h>
#include <ArduinoJson.h>
#include <string.h>
#include <ctype.h>

static const char *CHARGER_SERVICE_UUID = "0000eee0-0000-1000-8000-00805f9b34fb";
static const char *EEE1_UUID = "0000eee1-0000-1000-8000-00805f9b34fb";
static const char *EEE2_UUID = "0000eee2-0000-1000-8000-00805f9b34fb";

static NimBLEClient *g_client = nullptr;
static NimBLERemoteCharacteristic *g_eee1 = nullptr;
static NimBLERemoteCharacteristic *g_eee2 = nullptr;

// Colon-separated ("AA:BB:CC:DD:EE:FF\0") and plain ("AABBCCDDEEFF\0")
// forms, kept in sync - see deriveMacPlain(). Both start as the
// compiled-in defaults (config.h) and can be overridden at runtime (see
// chargerSetMacAddress() / main.cpp's checkDeviceConfig()). Stored
// uppercase so the colon form compares directly (strcmp) against the
// dashboard server's normalized form (server.py's _normalize_mac()).
static char g_macAddress[18] = CHARGER_MAC_ADDRESS;
static char g_macPlain[13] = CHARGER_MAC_PLAIN;

// Strips ':' separators and uppercases - the runtime equivalent of how
// config.h's comment says CHARGER_MAC_PLAIN was derived from
// CHARGER_MAC_ADDRESS by hand, now done automatically so a
// server-supplied address can't drift out of sync between the two forms.
static void deriveMacPlain(const char *colonMac, char *outPlain, size_t outPlainSize) {
  size_t o = 0;
  for (size_t i = 0; colonMac[i] != '\0' && o < outPlainSize - 1; i++) {
    char c = colonMac[i];
    if (c == ':') continue;
    outPlain[o++] = (char)toupper((unsigned char)c);
  }
  outPlain[o] = '\0';
}

void chargerSetMacAddress(const char *mac) {
  size_t i = 0;
  for (; mac[i] != '\0' && i < sizeof(g_macAddress) - 1; i++) {
    g_macAddress[i] = (char)toupper((unsigned char)mac[i]);
  }
  g_macAddress[i] = '\0';
  deriveMacPlain(g_macAddress, g_macPlain, sizeof(g_macPlain));
}

const char *chargerGetMacAddress() {
  return g_macAddress;
}

bool chargerIsConnected() {
  return g_client != nullptr && g_client->isConnected();
}

void chargerDisconnect() {
  if (g_client) {
    if (g_client->isConnected()) g_client->disconnect();
    NimBLEDevice::deleteClient(g_client);
    g_client = nullptr;
  }
  g_eee1 = nullptr;
  g_eee2 = nullptr;
}

bool chargerConnect() {
  chargerDisconnect();

  g_client = NimBLEDevice::createClient();
  g_client->setConnectTimeout(BLE_CONNECT_TIMEOUT_MS);

  if (!g_client->connect(NimBLEAddress(g_macAddress, BLE_ADDR_PUBLIC))) {
    chargerDisconnect();
    return false;
  }

  NimBLERemoteService *svc = g_client->getService(CHARGER_SERVICE_UUID);
  if (!svc) {
    chargerDisconnect();
    return false;
  }
  g_eee1 = svc->getCharacteristic(EEE1_UUID);
  g_eee2 = svc->getCharacteristic(EEE2_UUID);
  if (!g_eee1 || !g_eee2) {
    chargerDisconnect();
    return false;
  }
  return true;
}

// Mirrors charger.py's _call(): write the JSON-RPC command to eee2 (with
// response - it supports plain "write", not just write-without-response),
// wait, read the 2-byte big-endian length from eee1, then keep re-reading
// eee2 (the same characteristic the command was written to) until that
// many bytes have arrived. Fails closed (returns false) on an incomplete
// read rather than forwarding truncated JSON - same as the Python version.
static bool call(int requestId, const char *method, const char *mac, const char *action,
                  bool hasValue, int value, char *outBuf, size_t outBufSize, size_t &outLen) {
  if (!g_eee1 || !g_eee2) return false;

  JsonDocument cmdDoc;
  cmdDoc["id"] = requestId;
  cmdDoc["method"] = method;
  JsonObject params = cmdDoc["params"].to<JsonObject>();
  params["mac"] = mac;
  params["action"] = action;
  if (hasValue) params["value"] = value;

  char cmdBuf[256];
  size_t cmdLen = serializeJson(cmdDoc, cmdBuf, sizeof(cmdBuf));

  if (!g_eee2->writeValue((uint8_t *)cmdBuf, cmdLen, true)) return false;
  delay(300);

  std::string probe = g_eee1->readValue();
  if (probe.size() < 2) return false;
  uint16_t totalLength = (uint16_t)(((uint8_t)probe[0] << 8) | (uint8_t)probe[1]);
  if (totalLength >= outBufSize) return false;  // wouldn't fit - fail rather than truncate silently

  outLen = 0;
  int reads = 0;
  const int maxReads = 60;
  while (outLen < totalLength && reads < maxReads) {
    std::string chunk = g_eee2->readValue();
    if (chunk.empty()) break;
    size_t toCopy = chunk.size();
    if (outLen + toCopy > outBufSize - 1) toCopy = outBufSize - 1 - outLen;
    memcpy(outBuf + outLen, chunk.data(), toCopy);
    outLen += toCopy;
    reads++;
  }
  outBuf[outLen] = '\0';

  return outLen >= totalLength;
}

bool chargerReadStatusRaw(char *outBuf, size_t outBufSize, size_t &outLen) {
  // status GET uses the MAC's last 3 bytes (6 hex chars), not the full
  // address - confirmed from a real packet capture. altC SET uses the
  // full address instead (see chargerSetAltCurrentRaw below).
  const char *shortMac = g_macPlain + (strlen(g_macPlain) - 6);
  return call(1, "status", shortMac, "GET", false, 0, outBuf, outBufSize, outLen);
}

bool chargerSetAltCurrentRaw(int amps, char *outBuf, size_t outBufSize, size_t &outLen) {
  return call(668, "altC", g_macPlain, "SET", true, amps, outBuf, outBufSize, outLen);
}
