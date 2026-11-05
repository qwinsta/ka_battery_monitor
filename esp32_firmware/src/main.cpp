// KickAss Battery Monitor - ESP32 BLE bridge
//
// Connects to the battery and charger directly as a BLE client (same
// protocols as battery.py/charger.py - see ../../PROTOCOL.md) and pushes
// readings to the dashboard server's ingest API (see ../../ingest_poller.py)
// over WiFi. Exists because the target GL.iNet router has no usable
// native USB Bluetooth path - see PROTOCOL.md "GL.iNet router Bluetooth
// investigation".
//
// Confirmed working against real hardware in the vehicle since
// 2026-09-18. Watch the Serial monitor (115200 baud) for connect/read/push
// logging if something's misbehaving.
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <NimBLEDevice.h>
#include <ArduinoJson.h>
#include <ArduinoOTA.h>
#include <Preferences.h>
#include <string.h>

#include "config.h"
#include "ble_battery.h"
#include "ble_charger.h"

static unsigned long g_lastBatteryPoll = 0;
static unsigned long g_lastChargerStatusPoll = 0;
static unsigned long g_lastPendingCheck = 0;
static unsigned long g_lastDeviceConfigCheck = 0;
static int g_batteryCycleCount = 0;
static BatteryReading g_lastCellsAndName;

// Persists a server-assigned battery/charger BLE address across reboots
// (NVS flash, not the compiled-in config.h defaults) - see
// checkDeviceConfig() below and the Settings page's "Device
// connections" section (per Quinn 2026-10-14). Namespace kept short
// ("devcfg") - ESP32 NVS namespace names are capped at 15 chars.
static Preferences g_devicePrefs;

// True once a battery/charger address change has been applied locally
// but not yet successfully reported back via POST
// /api/ingest/device-config-ack - kept pending (and retried every
// checkDeviceConfig() cycle) rather than fire-and-forget, so a single
// dropped HTTP response doesn't leave the Settings page showing
// "unconfirmed" forever for a change that actually did take effect.
static bool g_batteryAckPending = false;
static bool g_chargerAckPending = false;

// Registered once in setup(); ArduinoOTA.begin() itself gets re-called
// from connectWiFi() on every successful (re)connect, since its UDP
// listener doesn't reliably survive a full WiFi drop/reconnect cycle -
// cheap to redo, and silently losing OTA after a reconnect would be a
// confusing thing to have to debug later.
static void setupOTA() {
  ArduinoOTA.setHostname(OTA_HOSTNAME);
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart([]() { Serial.println("OTA: update starting"); });
  ArduinoOTA.onEnd([]() { Serial.println("\nOTA: update complete, rebooting"); });
  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
    Serial.printf("OTA: %u%%\r", (progress * 100) / total);
  });
  ArduinoOTA.onError([](ota_error_t error) { Serial.printf("OTA: error [%u]\n", error); });
}

static void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("WiFi: connecting to '%s'...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWiFi: connected, IP %s\n", WiFi.localIP().toString().c_str());
    ArduinoOTA.begin();
  } else {
    Serial.println("\nWiFi: connect timed out, will retry");
  }
}

static bool httpPostJson(const char *path, const uint8_t *body, size_t len) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  String url = String(SERVER_BASE_URL) + path;
  http.begin(url);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t *)body, len);
  bool ok = code >= 200 && code < 300;
  if (!ok) Serial.printf("HTTP: POST %s failed, code=%d\n", path, code);
  http.end();
  return ok;
}

static bool httpGetJson(const char *path, JsonDocument &doc) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  String url = String(SERVER_BASE_URL) + path;
  http.begin(url);
  http.setTimeout(HTTP_TIMEOUT_MS);
  int code = http.GET();
  bool ok = (code == 200);
  if (ok) {
    DeserializationError err = deserializeJson(doc, http.getStream());
    ok = !err;
    if (err) Serial.printf("HTTP: GET %s bad JSON: %s\n", path, err.c_str());
  } else if (code > 0) {
    Serial.printf("HTTP: GET %s failed, code=%d\n", path, code);
  }
  http.end();
  return ok;
}

static void pushBatteryReading(const BatteryReading &r) {
  JsonDocument doc;
  doc["voltage"] = r.voltage;
  doc["current"] = r.current;
  doc["power"] = r.power;
  doc["soc_percent"] = r.soc_percent;
  doc["remaining_ah"] = r.remaining_ah;
  doc["nominal_ah"] = r.nominal_ah;
  doc["cycles"] = r.cycles;
  doc["charging"] = r.charging;
  doc["temperature_c"] = r.temperature_c;

  JsonArray ntc = doc["ntc_temps_c"].to<JsonArray>();
  for (int i = 0; i < r.num_ntc; i++) ntc.add(r.ntc_temps_c[i]);

  if (r.has_cells_and_name) {
    JsonArray cells = doc["cell_voltages"].to<JsonArray>();
    for (int i = 0; i < r.num_cells; i++) cells.add(r.cell_voltages[i]);
    doc["num_cells"] = r.num_cells;
    doc["device_name"] = r.device_name;
  }

  char buf[512];
  size_t len = serializeJson(doc, buf, sizeof(buf));
  httpPostJson("/api/ingest/battery", (const uint8_t *)buf, len);
}

static void doBatteryCycle() {
  if (!batteryIsConnected()) {
    Serial.println("Battery: connecting...");
    if (!batteryConnect()) {
      Serial.println("Battery: connect failed");
      return;
    }
    Serial.println("Battery: connected");
  }

  BatteryReading reading;
  if (!batteryReadBasicInfo(reading)) {
    Serial.println("Battery: read failed, dropping connection for a clean retry");
    batteryDisconnect();
    return;
  }

  // Cell voltages + device name change rarely - read them only every 5th
  // cycle to keep the fast path fast (mirrors poller.py's own approach).
  g_batteryCycleCount++;
  if (g_batteryCycleCount % 5 == 1) {
    if (batteryReadCellsAndName(reading)) {
      g_lastCellsAndName = reading;
    }
  } else if (g_lastCellsAndName.has_cells_and_name) {
    reading.has_cells_and_name = true;
    reading.num_cells = g_lastCellsAndName.num_cells;
    memcpy(reading.cell_voltages, g_lastCellsAndName.cell_voltages, sizeof(reading.cell_voltages));
    strncpy(reading.device_name, g_lastCellsAndName.device_name, sizeof(reading.device_name));
  }

  pushBatteryReading(reading);
  Serial.printf("Battery: %.2fV %.2fA %d%% SOC (pushed)\n", reading.voltage, reading.current, reading.soc_percent);
}

// Called once from setup(), before WiFi/BLE are touched - restores
// whatever battery/charger address was last confirmed from the server
// (if any), so a reboot (power cycle, OTA update, crash) doesn't
// silently fall back to the config.h compiled-in default and reconnect
// to the WRONG device for however long it takes checkDeviceConfig() to
// next correct it (per Quinn 2026-10-14).
static void applyPersistedDeviceConfig() {
  g_devicePrefs.begin("devcfg", true);  // read-only
  String batteryMac = g_devicePrefs.getString("battery_mac", "");
  String chargerMac = g_devicePrefs.getString("charger_mac", "");
  g_devicePrefs.end();

  if (batteryMac.length() > 0) {
    batterySetMacAddress(batteryMac.c_str());
    Serial.printf("DeviceConfig: restored battery MAC from flash: %s\n", batteryMac.c_str());
  }
  if (chargerMac.length() > 0) {
    chargerSetMacAddress(chargerMac.c_str());
    Serial.printf("DeviceConfig: restored charger MAC from flash: %s\n", chargerMac.c_str());
  }
}

// Sends (or re-sends) the ack for whichever device address is currently
// pending confirmation - a single small JSON body naming just the one
// field that changed, matching server.py's DeviceAddressesRequest
// (both fields optional, independent).
static void sendDeviceConfigAck(const char *fieldKey, const char *mac) {
  JsonDocument ackDoc;
  ackDoc[fieldKey] = mac;
  char ackBuf[48];
  size_t ackLen = serializeJson(ackDoc, ackBuf, sizeof(ackBuf));
  bool ok = httpPostJson("/api/ingest/device-config-ack", (const uint8_t *)ackBuf, ackLen);
  if (strcmp(fieldKey, "battery_mac") == 0) {
    g_batteryAckPending = !ok;
  } else {
    g_chargerAckPending = !ok;
  }
  if (!ok) Serial.printf("DeviceConfig: %s ack failed, will retry\n", fieldKey);
}

// Polled at a slow, infrequent cadence (DEVICE_CONFIG_POLL_INTERVAL_MS -
// this almost never changes) - see GET /api/device-config in server.py.
// Lets the Settings page's "Device connections" section repoint this
// bridge at a different battery/charger without reflashing: on a
// mismatch, applies the new address immediately (so the NEXT connect
// attempt uses it), drops any live connection to the old device for a
// clean reconnect, persists it to flash, and reports back via
// /api/ingest/device-config-ack - that ack, not just the server-side
// save, is what the Settings page's "confirmed" indicator is based on
// (per Quinn 2026-10-14: "validation that the ID gets sent to the
// ESP32").
static void checkDeviceConfig() {
  JsonDocument doc;
  if (!httpGetJson("/api/device-config", doc)) return;

  const char *serverBatteryMac = doc["battery_mac"] | "";
  const char *serverChargerMac = doc["charger_mac"] | "";

  if (serverBatteryMac[0] != '\0' && strcmp(serverBatteryMac, batteryGetMacAddress()) != 0) {
    Serial.printf("DeviceConfig: battery MAC changed %s -> %s\n", batteryGetMacAddress(), serverBatteryMac);
    batterySetMacAddress(serverBatteryMac);
    if (batteryIsConnected()) batteryDisconnect();  // clean reconnect with the new address next cycle
    g_devicePrefs.begin("devcfg", false);
    g_devicePrefs.putString("battery_mac", serverBatteryMac);
    g_devicePrefs.end();
    g_batteryAckPending = true;
  }
  if (g_batteryAckPending) sendDeviceConfigAck("battery_mac", batteryGetMacAddress());

  if (serverChargerMac[0] != '\0' && strcmp(serverChargerMac, chargerGetMacAddress()) != 0) {
    Serial.printf("DeviceConfig: charger MAC changed %s -> %s\n", chargerGetMacAddress(), serverChargerMac);
    chargerSetMacAddress(serverChargerMac);
    if (chargerIsConnected()) chargerDisconnect();
    g_devicePrefs.begin("devcfg", false);
    g_devicePrefs.putString("charger_mac", serverChargerMac);
    g_devicePrefs.end();
    g_chargerAckPending = true;
  }
  if (g_chargerAckPending) sendDeviceConfigAck("charger_mac", chargerGetMacAddress());
}

// Classifies a discovered device against the known advertised-name
// hints (mirrors ble_tool.py's matches_target()/TARGET_NAME_HINTS on
// the desktop side - see config.h's SCAN_*_NAME_HINT) - returns
// "battery", "charger", or empty if it matches neither. Case-
// insensitive substring match, same as the Python version.
static String classifyScanResult(const String &name) {
  String upper = name;
  upper.toUpperCase();
  if (upper.indexOf(SCAN_CHARGER_NAME_HINT) >= 0) return "charger";
  if (upper.indexOf(SCAN_BATTERY_NAME_HINT) >= 0) return "battery";
  return "";
}

// Runs a one-off BLE scan for the Settings page's device-discovery tool
// (see server.py's /api/scan-devices and /api/pending-scan). Disconnects
// any existing battery/charger connections first and blocks for the
// whole scan - acceptable since this only ever runs in response to a
// deliberate, rare "find my devices" action (confirmed with Quinn
// 2026-10-14: "I'm ok with the ESP32 dropping existing connections as
// this is only a once off first time start"), not during normal
// operation. Normal polling (battery/charger/OTA/etc.) is simply
// stalled for SCAN_DURATION_S rather than run concurrently - doBatteryCycle()/
// doChargerStatusCycle() reconnect on their own on the next loop() tick
// once this returns, same as after any other dropped connection.
//
// NOTE: this is the one piece of this change I couldn't verify against
// the exact installed NimBLE-Arduino version from here (no compiler
// available) - the synchronous getResults()/getCount()/getDevice()
// pattern below matches NimBLE-Arduino's long-standing public scanning
// API, but double check this compiles (`pio run -e esp32dev`) before
// OTA-pushing it, same as you'd want to for any firmware change.
static void runBleScan(int scanId) {
  Serial.println("Scan: starting (disconnecting existing devices)...");
  if (batteryIsConnected()) batteryDisconnect();
  if (chargerIsConnected()) chargerDisconnect();

  NimBLEScan *pScan = NimBLEDevice::getScan();
  pScan->setActiveScan(true);  // request scan response too - gives a better chance of getting the full advertised name
  NimBLEScanResults results = pScan->getResults(SCAN_DURATION_S * 1000, false);

  int count = results.getCount();
  Serial.printf("Scan: found %d advertisement(s)\n", count);

  JsonDocument reportDoc;
  reportDoc["id"] = scanId;
  JsonArray devices = reportDoc["devices"].to<JsonArray>();

  int added = 0;
  for (int i = 0; i < count && added < SCAN_MAX_DEVICES; i++) {
    const NimBLEAdvertisedDevice *dev = results.getDevice(i);
    if (!dev->haveName()) continue;  // an address with no advertised name isn't useful to recommend from a list
    String name = dev->getName().c_str();
    String addr = dev->getAddress().toString().c_str();
    String type = classifyScanResult(name);

    JsonObject d = devices.add<JsonObject>();
    d["name"] = name;
    d["address"] = addr;
    d["rssi"] = dev->getRSSI();
    if (type.length() > 0) d["likely_type"] = type;
    added++;
  }

  pScan->clearResults();

  char reportBuf[2048];
  size_t reportLen = serializeJson(reportDoc, reportBuf, sizeof(reportBuf));
  if (!httpPostJson("/api/ingest/scan-result", (const uint8_t *)reportBuf, reportLen)) {
    // Not retried - unlike device-config's ack, a dropped scan report
    // just means asking the Settings page to scan again, which is cheap
    // and the person is already sitting there waiting for a result.
    Serial.println("Scan: failed to report results");
  }
  Serial.println("Scan: done, resuming normal operation");
}

static void checkPendingScan() {
  JsonDocument doc;
  if (!httpGetJson("/api/pending-scan", doc)) return;
  if (!(doc["pending"] | false)) return;
  int scanId = doc["id"] | 0;
  runBleScan(scanId);
}

// Settings page "Restart BLE" recovery button (see server.py's
// api_restart_ble()) - acks first so the waiting dashboard request
// unblocks with "the ESP32 got the message", then does a full
// ESP.restart(), same as a manual power-cycle. Deliberately simple: no
// attempt to gracefully close the WiFi/BLE connections first, since
// restart() tears everything down anyway and the whole point is a known-
// clean slate, not a careful shutdown (per Quinn 2026-11-06, added after
// OTA repeatedly failed to deliver this as a side effect of pushing a
// full firmware image).
static void checkPendingRestart() {
  JsonDocument doc;
  if (!httpGetJson("/api/pending-restart", doc)) return;
  if (!(doc["pending"] | false)) return;
  int restartId = doc["id"] | 0;

  JsonDocument ackDoc;
  ackDoc["id"] = restartId;
  char ackBuf[32];
  size_t ackLen = serializeJson(ackDoc, ackBuf, sizeof(ackBuf));
  httpPostJson("/api/ingest/restart-ack", (const uint8_t *)ackBuf, ackLen);

  Serial.println("Restart: requested from Settings page, restarting now");
  delay(200);  // give the ack POST's own TCP connection a moment to actually flush before the reboot tears it down
  ESP.restart();
}

static void checkPendingAltCurrent() {
  JsonDocument doc;
  if (!httpGetJson("/api/pending-alt-current", doc)) return;
  if (!(doc["pending"] | false)) return;

  int id = doc["id"] | 0;
  int value = doc["value"] | 0;
  Serial.printf("Charger: pending altC command id=%d value=%dA\n", id, value);

  if (!chargerIsConnected()) {
    Serial.println("Charger: connecting for pending command...");
    if (!chargerConnect()) {
      Serial.println("Charger: connect failed, leaving command pending for next check");
      return;  // don't report failure yet - try again on the next poll
    }
  }

  char resultBuf[1024];
  size_t resultLen = 0;
  bool ok = chargerSetAltCurrentRaw(value, resultBuf, sizeof(resultBuf), resultLen);
  if (!ok) {
    Serial.println("Charger: altC command failed, dropping connection");
    chargerDisconnect();
  }

  JsonDocument reportDoc;
  reportDoc["id"] = id;
  if (ok) {
    JsonDocument respDoc;
    DeserializationError err = deserializeJson(respDoc, resultBuf, resultLen);
    if (!err) {
      reportDoc["result"] = respDoc.as<JsonObject>();
    } else {
      // The charger's own JSON serializer has a known bug (see
      // PROTOCOL.md / charger.py's matching _raw_text fallback) - the
      // BLE write+read itself already succeeded above (`ok` is true, so
      // the charger DID accept the new current setting), only its own
      // confirmation response's JSON is malformed. Report this as a
      // success with the raw text attached, not an error - failing the
      // whole command here made a setting that genuinely took effect
      // look like it hadn't (confirmed live 2026-09-30, reported as
      // "unable to change setting, will retry").
      JsonObject result = reportDoc["result"].to<JsonObject>();
      result["_raw_text"] = resultBuf;
    }
  } else {
    reportDoc["error"] = "failed to reach the charger to set alternator current";
  }

  char reportBuf[1300];
  size_t reportLen = serializeJson(reportDoc, reportBuf, sizeof(reportBuf));
  httpPostJson("/api/ingest/alt-current-result", (const uint8_t *)reportBuf, reportLen);
}

static void doChargerStatusCycle() {
  if (!chargerIsConnected()) {
    Serial.println("Charger: connecting...");
    if (!chargerConnect()) {
      Serial.println("Charger: connect failed");
      return;
    }
    Serial.println("Charger: connected");
  }

  char statusBuf[1024];
  size_t statusLen = 0;
  if (!chargerReadStatusRaw(statusBuf, sizeof(statusBuf), statusLen)) {
    Serial.println("Charger: status read failed, dropping connection for a clean retry");
    chargerDisconnect();
    return;
  }

  httpPostJson("/api/ingest/charger", (const uint8_t *)statusBuf, statusLen);
  Serial.println("Charger: status pushed");
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\nKickAss Battery Monitor - ESP32 BLE bridge starting");

  // Before anything BLE-related, so a persisted (server-assigned)
  // address is already in place by the time the very first
  // batteryConnect()/chargerConnect() happens.
  applyPersistedDeviceConfig();

  setupOTA();
  connectWiFi();
  NimBLEDevice::init("");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
    delay(1000);
    return;
  }
  ArduinoOTA.handle();

  unsigned long now = millis();

  if (now - g_lastBatteryPoll >= BATTERY_POLL_INTERVAL_MS) {
    g_lastBatteryPoll = now;
    doBatteryCycle();
  }

  if (now - g_lastPendingCheck >= PENDING_COMMAND_POLL_INTERVAL_MS) {
    g_lastPendingCheck = now;
    // First - if a restart is pending, ESP.restart() cuts this tick (and
    // everything else) off immediately, so there's no reason to spend
    // time on the others first.
    checkPendingRestart();
    checkPendingAltCurrent();
    checkPendingScan();  // blocks for SCAN_DURATION_S if a scan is actually pending - see its own comment
  }

  if (now - g_lastChargerStatusPoll >= CHARGER_POLL_INTERVAL_MS) {
    g_lastChargerStatusPoll = now;
    doChargerStatusCycle();
  }

  if (now - g_lastDeviceConfigCheck >= DEVICE_CONFIG_POLL_INTERVAL_MS) {
    g_lastDeviceConfigCheck = now;
    checkDeviceConfig();
  }

  delay(50);
}
