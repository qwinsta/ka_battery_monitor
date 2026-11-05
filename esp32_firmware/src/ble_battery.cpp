#include "ble_battery.h"
#include "config.h"
#include <NimBLEDevice.h>
#include <string.h>
#include <ctype.h>

static const char *BATTERY_SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb";
static const char *FF01_NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb";
static const char *FF02_WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb";

static NimBLEClient *g_client = nullptr;
static NimBLERemoteCharacteristic *g_writeChar = nullptr;
static NimBLERemoteCharacteristic *g_notifyChar = nullptr;

// "AA:BB:CC:DD:EE:FF\0" - 17 chars + terminator. Starts as the compiled-
// in default (config.h) and can be overridden at runtime - see
// batterySetMacAddress(). Stored uppercase so it compares directly
// (strcmp) against the dashboard server's own normalized form (see
// server.py's _normalize_mac()) without a separate case-fold step at
// every comparison site.
static char g_macAddress[18] = BATTERY_MAC_ADDRESS;

void batterySetMacAddress(const char *mac) {
  size_t i = 0;
  for (; mac[i] != '\0' && i < sizeof(g_macAddress) - 1; i++) {
    g_macAddress[i] = (char)toupper((unsigned char)mac[i]);
  }
  g_macAddress[i] = '\0';
}

const char *batteryGetMacAddress() {
  return g_macAddress;
}

// Response frames are small (basic info is ~48 bytes total) but may
// arrive split across several notifications - 256B is generous headroom.
static uint8_t g_notifyBuf[256];
static volatile size_t g_notifyLen = 0;
static volatile bool g_frameComplete = false;

static void notifyCallback(NimBLERemoteCharacteristic *chr, uint8_t *data, size_t length, bool isNotify) {
  if (g_notifyLen + length > sizeof(g_notifyBuf)) {
    g_notifyLen = 0;  // corrupt/overflowing frame - drop and let the caller time out and retry
    return;
  }
  memcpy(g_notifyBuf + g_notifyLen, data, length);
  g_notifyLen += length;
  // Length-based framing, not scanning for the 0x77 end byte - a data
  // byte can coincidentally equal 0x77 (see battery.py's own comment on
  // this exact point).
  if (g_notifyLen >= 4) {
    uint8_t frameLen = g_notifyBuf[3];
    if (g_notifyLen >= (size_t)(4 + frameLen + 2 + 1)) {
      g_frameComplete = true;
    }
  }
}

static inline uint16_t be16(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }
static inline int16_t sbe16(const uint8_t *p) { return (int16_t)((p[0] << 8) | p[1]); }

bool batteryIsConnected() {
  return g_client != nullptr && g_client->isConnected();
}

void batteryDisconnect() {
  if (g_client) {
    if (g_client->isConnected()) g_client->disconnect();
    NimBLEDevice::deleteClient(g_client);
    g_client = nullptr;
  }
  g_writeChar = nullptr;
  g_notifyChar = nullptr;
}

bool batteryConnect() {
  batteryDisconnect();

  g_client = NimBLEDevice::createClient();
  g_client->setConnectTimeout(BLE_CONNECT_TIMEOUT_MS);

  if (!g_client->connect(NimBLEAddress(g_macAddress, BLE_ADDR_PUBLIC))) {
    batteryDisconnect();
    return false;
  }

  NimBLERemoteService *svc = g_client->getService(BATTERY_SERVICE_UUID);
  if (!svc) {
    batteryDisconnect();
    return false;
  }
  g_notifyChar = svc->getCharacteristic(FF01_NOTIFY_UUID);
  g_writeChar = svc->getCharacteristic(FF02_WRITE_UUID);
  if (!g_notifyChar || !g_writeChar) {
    batteryDisconnect();
    return false;
  }

  if (!g_notifyChar->canNotify() || !g_notifyChar->subscribe(true, notifyCallback)) {
    batteryDisconnect();
    return false;
  }

  return true;
}

// Sends a single JBD register-read request and waits for the (possibly
// fragmented) response. Retries the write on timeout, same as
// battery.py's _request() - a write-without-response sent right after
// connecting can be silently dropped before the link fully settles.
static bool sendRequest(uint8_t reg, uint8_t *outData, size_t &outLen, size_t maxOutLen,
                         uint32_t timeoutMs = 5000, int retries = 3) {
  if (!g_writeChar || !g_notifyChar) return false;

  uint16_t checksum = (uint16_t)(0x10000 - reg);
  uint8_t frame[7] = {0xDD, 0xA5, reg, 0x00, (uint8_t)(checksum >> 8), (uint8_t)(checksum & 0xFF), 0x77};

  bool gotFrame = false;
  for (int attempt = 0; attempt < retries && !gotFrame; attempt++) {
    g_notifyLen = 0;
    g_frameComplete = false;

    if (!g_writeChar->writeValue(frame, sizeof(frame), false)) {
      delay(200);
      continue;
    }

    uint32_t start = millis();
    while (!g_frameComplete && (millis() - start) < timeoutMs) {
      delay(20);
    }
    gotFrame = g_frameComplete;
  }
  if (!gotFrame) return false;

  if (g_notifyBuf[0] != 0xDD) return false;
  uint8_t status = g_notifyBuf[2];
  uint8_t length = g_notifyBuf[3];
  if (status != 0) return false;  // battery reported an error status for this register

  outLen = length < maxOutLen ? length : maxOutLen;
  memcpy(outData, g_notifyBuf + 4, outLen);
  return true;
}

bool batteryReadBasicInfo(BatteryReading &out) {
  uint8_t data[64];
  size_t len = 0;
  if (!sendRequest(0x03, data, len, sizeof(data))) return false;
  if (len < 23) return false;  // need at least through num_ntc at offset 22

  out.voltage = be16(data + 0) / 100.0f;
  out.current = sbe16(data + 2) / 100.0f;
  out.remaining_ah = be16(data + 4) / 100.0f;
  out.nominal_ah = be16(data + 6) / 100.0f;
  out.cycles = be16(data + 8);
  out.soc_percent = data[19];
  out.num_cells = data[21];
  out.num_ntc = data[22];
  out.charging = out.current > 0;
  out.power = out.voltage * out.current;

  int ntc = out.num_ntc > 4 ? 4 : out.num_ntc;
  float tempSum = 0;
  int gotTemps = 0;
  for (int i = 0; i < ntc; i++) {
    size_t offset = 23 + i * 2;
    if (offset + 2 > len) break;
    uint16_t raw = be16(data + offset);
    out.ntc_temps_c[i] = raw / 10.0f - 273.15f;
    tempSum += out.ntc_temps_c[i];
    gotTemps++;
  }
  out.temperature_c = gotTemps > 0 ? tempSum / gotTemps : 0;
  out.valid = true;
  return true;
}

bool batteryReadCellsAndName(BatteryReading &out) {
  uint8_t data[64];
  size_t len = 0;
  bool ok = true;

  if (sendRequest(0x04, data, len, sizeof(data))) {
    int cells = out.num_cells > 8 ? 8 : out.num_cells;
    for (int i = 0; i < cells && (size_t)(i * 2 + 2) <= len; i++) {
      out.cell_voltages[i] = be16(data + i * 2) / 1000.0f;
    }
  } else {
    ok = false;
  }

  if (sendRequest(0x05, data, len, sizeof(data))) {
    size_t n = len < sizeof(out.device_name) - 1 ? len : sizeof(out.device_name) - 1;
    memcpy(out.device_name, data, n);
    out.device_name[n] = 0;
  } else {
    ok = false;
  }

  out.has_cells_and_name = ok;
  return ok;
}
