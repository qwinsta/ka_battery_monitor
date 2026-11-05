"use strict";

/* ---------------- view mode (mobile / desktop) ---------------- */

const appEl = document.getElementById("app");

function setView(mode) {
  appEl.dataset.view = mode;
  localStorage.setItem("ka_view", mode);
  document.querySelectorAll(".view-toggle button").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === mode);
  });
}

(function initView() {
  const stored = localStorage.getItem("ka_view");
  const mode = stored || (window.innerWidth < 720 ? "mobile" : "desktop");
  setView(mode);
})();

document.querySelectorAll(".view-toggle button").forEach((b) => {
  b.addEventListener("click", () => setView(b.dataset.view));
});

/* ---------------- page navigation (Dashboard / Settings) ---------------- */

document.querySelectorAll(".page-tabs button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".page-tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".page").forEach((p) => {
      p.hidden = p.id !== `page-${b.dataset.page}`;
    });
  });
});

/* ---------------- helpers ---------------- */

function fmt(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  return Number(n).toFixed(digits);
}

function socColor(pct) {
  if (pct === null || pct === undefined) return "var(--text-muted)";
  if (pct >= 50) return "var(--status-good)";
  if (pct >= 20) return "var(--status-warning)";
  return "var(--status-critical)";
}

function setDot(id, ok) {
  document.getElementById(id).classList.toggle("ok", !!ok);
}

function fmtHours(hours) {
  if (hours === null || hours === undefined) return "--";
  if (hours <= 0) return "Full";
  const totalMinutes = Math.round(hours * 60);
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// Rough, deliberately simple illustrative estimate - NOT a real forecast
// (no actual weather/sun data involved). "Sun-hours" is the standard
// solar-industry "peak sun hours" concept: how many hours the panel
// would need to run at its own rated peak output to make up the
// battery's current Wh deficit. Tied to the Settings page's "Solar panel
// max capacity" (solarPanelMaxW, same value the Solar Generation chart's
// y-axis uses) rather than a hardcoded figure, so it stays correct
// whenever that setting changes (per Quinn 2026-10-01) - a fixed day-
// total assumption (first tried 2026-09-30: 800Wh spread across a full
// 24h day) turned out to algebraically cancel out of this calculation
// entirely once expressed in terms of peak watts, so there was no reason
// to keep a separate assumption around at all.
//
// SUNNY_DAY_DERATE: even on a genuinely sunny day a panel rarely sustains
// its exact rated peak continuously - sun angle, slight haze, and panel
// heating all shave a bit off in real-world conditions. 85% is a common
// real-world solar-sizing rule of thumb for this, applied here so the
// estimate reads as a realistic sunny day rather than an unreachable
// best case (per Quinn 2026-10-01 - still no weather lookup, just a
// fixed, simple correction).
const SUNNY_DAY_DERATE = 0.85;

function computeSunHoursToFull(battery) {
  if (!battery) return null;
  const neededAh = (battery.nominal_ah || 0) - (battery.remaining_ah || 0);
  if (neededAh <= 0) return 0;
  const neededWh = neededAh * NOMINAL_BATTERY_VOLTAGE_V;
  return neededWh / (solarPanelMaxW * SUNNY_DAY_DERATE);
}

// Alternator and solar are never both active on this vehicle (per Quinn
// 2026-11-06), so this is an either/or, not two stats shown together -
// which block is visible is driven entirely by data.charge_source from
// /api/latest (see server.py's _compute_charge_source_estimate()). The
// alternator figure is a live-rate estimate from the server (reacts to
// actual output, not a fixed assumption); the solar figure stays the
// existing client-side theoretical "peak sun-hours" calc, unchanged.
function updateChargeSourceEstimate(data) {
  const altEl = document.getElementById("charge-source-alt");
  const solarEl = document.getElementById("charge-source-solar");
  const noneEl = document.getElementById("charge-source-none");
  if (!altEl || !solarEl || !noneEl) return;

  altEl.hidden = data.charge_source !== "alternator";
  solarEl.hidden = data.charge_source !== "solar";
  noneEl.hidden = data.charge_source !== null && data.charge_source !== undefined;

  if (data.charge_source === "alternator") {
    const altCurrent = data.charger && data.charger.result && data.charger.result.alternator
      ? data.charger.result.alternator.c
      : null;
    setText("alt-current-live", fmt(altCurrent, 1));
    setText("alt-hours-to-full", fmtHours(data.charge_source_alt_hours));
  } else if (data.charge_source === "solar") {
    setText("sun-hours-to-full", fmtHours(computeSunHoursToFull(data.battery)));
  }
}

// charger.result.batteryInfo.chargeState mapping, confirmed by watching
// this against the manufacturer's own app's display during a real
// charge cycle.
const CHARGE_STAGE_LABELS = { 0: "Standby", 1: "Bulk", 2: "Absorption", 3: "Float" };
function chargeStageLabel(raw) {
  if (raw === null || raw === undefined) return "--";
  return CHARGE_STAGE_LABELS[raw] || `Unknown (${raw})`;
}

/* ---------------- SOC dial ---------------- */

const socDialFill = document.getElementById("soc-dial-fill");
const socPctEl = document.getElementById("soc-pct");
// Must match the circle's r="52" in index.html - kept as a separate JS
// constant rather than read from the DOM since it's simple, fixed, and
// only ever changed by hand alongside the markup anyway.
const SOC_DIAL_CIRCUMFERENCE = 2 * Math.PI * 52;
socDialFill.style.strokeDasharray = String(SOC_DIAL_CIRCUMFERENCE);

function updateSocBar(pct) {
  if (pct === null || pct === undefined) {
    socPctEl.textContent = "--";
    socDialFill.style.strokeDashoffset = String(SOC_DIAL_CIRCUMFERENCE);
    socDialFill.style.stroke = "var(--gridline)";
    return;
  }
  const clamped = Math.max(0, Math.min(100, pct));
  socDialFill.style.strokeDashoffset = String(SOC_DIAL_CIRCUMFERENCE * (1 - clamped / 100));
  socDialFill.style.stroke = socColor(pct);
  socPctEl.textContent = fmt(pct, 0);
}

/* ---------------- power flow diagram ---------------- */

const FLOW_ACTIVE_THRESHOLD_W = 1;

function setFlow(pathId, valId, watts) {
  const path = document.getElementById(pathId);
  const val = document.getElementById(valId);
  const active = watts !== null && watts !== undefined && Math.abs(watts) > FLOW_ACTIVE_THRESHOLD_W;
  if (path) path.classList.toggle("active", active);
  if (val) val.textContent = watts === null || watts === undefined ? "--" : Math.round(Math.abs(watts)) + "W";
}

function updateFlowDiagram(battery, chargerResult) {
  const alt = (chargerResult && chargerResult.alternator) || {};
  const panel = (chargerResult && chargerResult.panel) || {};
  setFlow("flow-alt", "flow-val-alt", chargerResult ? alt.p : null);
  setFlow("flow-solar", "flow-val-solar", chargerResult ? panel.p : null);
  setFlow("flow-batt", "flow-val-batt", battery ? battery.power : null);
  const battPath = document.getElementById("flow-batt");
  if (battPath && battery) battPath.classList.toggle("reverse", !!battery.charging);
}

function updateTotalPower(totalPowerW, totalPowerAvgW, totalPower1hrAvgW) {
  // Server computes all three as the charger's own battery-side power
  // reading minus the battery's own reading - no conversion-efficiency
  // guess involved (see server.py's _compute_total_power_usage_w). The
  // live instantaneous reading is the headline number; the 2-minute
  // average is shown smaller alongside it since it's naturally noisy
  // sample-to-sample, and the 1-hour average underneath that smooths
  // out even the slower swings to show the genuinely sustained draw
  // (per Quinn 2026-09-30).
  setText("total-power-value", fmt(totalPowerW, 0));
  setText("total-power-avg-value", fmt(totalPowerAvgW, 0));
  setText("total-power-1hr-avg-value", fmt(totalPower1hrAvgW, 0));
}

/* ---------------- live polling ---------------- */

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

async function pollLatest() {
  let data;
  try {
    const res = await fetch("/api/latest");
    data = await res.json();
  } catch (e) {
    setDot("dot-battery", false);
    setDot("dot-charger", false);
    return;
  }

  setDot("dot-battery", data.battery_connected);
  setDot("dot-charger", data.charger_connected);

  const b = data.battery;
  if (b) {
    updateSocBar(b.soc_percent);
    setText("hero-voltage", fmt(b.voltage, 2));
    setText("hero-current", fmt(b.current, 1));
    setText("hero-power", fmt(b.power, 0));
    setText("tile-batt-voltage", fmt(b.voltage, 2) + " V");
    setText("tile-batt-current", fmt(b.current, 1) + " A");
    setText("tile-batt-power", fmt(b.power, 0) + " W");
    setText("tile-batt-remaining", fmt(b.remaining_ah, 1) + " Ah");
    setText("tile-batt-nominal", fmt(b.nominal_ah, 0) + " Ah");
    setText("tile-batt-cycles", b.cycles ?? "--");
    setText("tile-batt-state", b.charging ? "Charging" : "Discharging");
    setText("tile-batt-temp", fmt(b.temperature_c, 1) + " °C");
    if (b.cell_voltages && b.cell_voltages.length) {
      setText("tile-batt-cells", b.cell_voltages.map((v) => v.toFixed(2)).join(" / ") + " V");
    }
  }
  updateChargeSourceEstimate(data);

  if (data.time_remaining_mode === "warming_up") {
    // Distinct from the plain "--" fmtHours(null) would show - this is
    // a transient just-started state that resolves on its own shortly,
    // not "there's genuinely nothing to report" (per Quinn 2026-09-30).
    setText("hero-time-remaining", "Calculating…");
    setText("hero-time-remaining-label", "Time remaining");
  } else {
    setText("hero-time-remaining", fmtHours(data.time_remaining_hours));
    setText(
      "hero-time-remaining-label",
      data.time_remaining_mode === "to_empty" ? "Time to empty" : "Time to full"
    );
  }

  const c = data.charger;
  const result = c && c.result;
  if (result) {
    const alt = result.alternator || {};
    const panel = result.panel || {};
    setText("tile-alt-voltage", fmt(alt.v, 1) + " V");
    setText("tile-alt-current", fmt(alt.c, 1) + " A");
    setText("tile-alt-power", fmt(alt.p, 0) + " W");
    setText("tile-solar-voltage", fmt(panel.v, 1) + " V");
    setText("tile-solar-current", fmt(panel.c, 1) + " A");
    setText("tile-solar-power", fmt(panel.p, 0) + " W");
    setText("tile-temp", fmt(result.batteryInfo && result.batteryInfo.temperature, 0) + " °C");
    setText("tile-charge-stage", chargeStageLabel(result.batteryInfo && result.batteryInfo.chargeState));

    if (!sliderDirty) {
      const altInputC = result.altInputC;
      if (altInputC !== undefined && altInputC !== null
          && (!sliderInitialized || Number(slider.value) !== Number(altInputC))) {
        slider.value = altInputC;
        sliderValueLabel.textContent = altInputC + " A";
        sliderInitialized = true;
      }
    }
  }

  updateFlowDiagram(b, result);
  updateTotalPower(data.total_power_usage_w, data.total_power_usage_2min_avg_w, data.total_power_usage_1hr_avg_w);
}

setInterval(pollLatest, 1000);
pollLatest();

/* ---------------- alt-current slider (auto-save, debounced) ---------------- */

const slider = document.getElementById("alt-current-slider");
const sliderValueLabel = document.getElementById("alt-current-value");
const saveState = document.getElementById("alt-current-save-state");
let sliderDirty = false;
// Tracks whether the label has ever been set from a real reading yet -
// without this, if the charger's actual value happens to equal the
// slider's hardcoded HTML default (40A - which it commonly does, that's
// the charger's own rated max), the "value changed" check below never
// fires and the "--" placeholder never gets replaced (confirmed live
// 2026-09-30).
let sliderInitialized = false;
let saveTimer = null;

slider.addEventListener("input", () => {
  sliderDirty = true;
  sliderValueLabel.textContent = slider.value + " A";
  saveState.textContent = "typing…";
  saveState.className = "save-state";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveAltCurrent, 500);
});

async function saveAltCurrent() {
  const value = Number(slider.value);
  saveState.textContent = "saving…";
  saveState.className = "save-state saving";
  try {
    const res = await fetch("/api/alt-current", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    if (!res.ok) throw new Error(await res.text());
    saveState.textContent = "saved";
    saveState.className = "save-state saved";
    setTimeout(() => {
      saveState.textContent = "";
      sliderDirty = false;
    }, 1500);
  } catch (e) {
    saveState.textContent = "failed to save - will retry";
    saveState.className = "save-state error";
  }
}

/* ---------------- solar panel max capacity (settings) ---------------- */

const solarPanelMaxWInput = document.getElementById("solar-panel-max-w-input");
const solarPanelMaxWSaveBtn = document.getElementById("solar-panel-max-w-save");
const solarPanelMaxWSaveState = document.getElementById("solar-panel-max-w-save-state");
let solarPanelMaxW = 200; // overwritten by the real persisted value below before it matters

// Shared by the initial load and the save handler below - keeps the
// input field, the sun-hours-to-full display, and the Solar Generation
// chart's y-axis all in sync with wherever this setting actually is,
// rather than needing to remember to update all three in two places.
function applySolarPanelMaxW(value) {
  solarPanelMaxW = value;
  if (solarPanelMaxWInput) solarPanelMaxWInput.value = value;
  setText("sun-hours-panel-w", value);
  if (charts.solarGen) {
    charts.solarGen.yMinFixed = 0;
    charts.solarGen.yMaxFixed = value;
    charts.solarGen.draw();
  }
}

async function loadSolarPanelMaxW() {
  try {
    const res = await fetch("/api/settings/solar-panel-max-w");
    const data = await res.json();
    applySolarPanelMaxW(data.max_w);
  } catch (e) {
    // Leave the input/chart showing whatever they already had.
  }
}

if (solarPanelMaxWSaveBtn) {
  solarPanelMaxWSaveBtn.addEventListener("click", async () => {
    const value = Number(solarPanelMaxWInput.value);
    if (!value || value <= 0) return;
    solarPanelMaxWSaveState.textContent = "saving…";
    solarPanelMaxWSaveState.className = "save-state saving";
    try {
      const res = await fetch("/api/settings/solar-panel-max-w", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_w: value }),
      });
      if (!res.ok) throw new Error(await res.text());
      applySolarPanelMaxW(value);
      solarPanelMaxWSaveState.textContent = "saved";
      solarPanelMaxWSaveState.className = "save-state saved";
      setTimeout(() => (solarPanelMaxWSaveState.textContent = ""), 1500);
    } catch (e) {
      solarPanelMaxWSaveState.textContent = "failed to save";
      solarPanelMaxWSaveState.className = "save-state error";
    }
  });
}

/* ---------------- device (BLE) addresses (settings) ---------------- */
// Which real battery/charger the ESP32 bridge connects to - lets this
// be pointed at different hardware (or someone else's vehicle running
// this same project) without reflashing firmware. See server.py's
// api_get_device_addresses()/api_set_device_addresses() and
// esp32_firmware/src/main.cpp's checkDeviceConfig() (per Quinn
// 2026-10-14).

const batteryMacInput = document.getElementById("battery-mac-input");
const chargerMacInput = document.getElementById("charger-mac-input");
const batteryMacHistoryEl = document.getElementById("battery-mac-history");
const chargerMacHistoryEl = document.getElementById("charger-mac-history");

const MAC_ADDRESS_RE = /^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$/;

// Fills a MAC input from a known device (a history entry or a scan
// result) and remembers its advertised name (if any) on the input
// itself, so Apply can pass it through to record_device_mac_history() -
// a hand-typed address has no name to attach, so plain typing clears it
// again (see the "input" listener in setupDeviceAddressApply below).
function fillMacInput(inputEl, mac, name) {
  inputEl.value = mac;
  inputEl.classList.remove("invalid");
  if (name) inputEl.dataset.deviceName = name;
  else delete inputEl.dataset.deviceName;
}

function renderMacHistory(selectEl, history, inputEl) {
  if (!selectEl) return;
  selectEl.innerHTML = '<option value="">Previous devices&hellip;</option>';
  for (const entry of history || []) {
    const opt = document.createElement("option");
    opt.value = entry.mac;
    opt.dataset.name = entry.name || "";
    const when = entry.last_used_at ? new Date(entry.last_used_at * 1000).toLocaleDateString([], { day: "numeric", month: "short" }) : "";
    opt.textContent = entry.name ? `${entry.name} (${entry.mac})` : entry.mac;
    if (when) opt.textContent += ` - last used ${when}`;
    selectEl.appendChild(opt);
  }
  selectEl.onchange = () => {
    if (!selectEl.value) return;
    const opt = selectEl.selectedOptions[0];
    fillMacInput(inputEl, selectEl.value, opt.dataset.name || null);
    selectEl.value = "";  // one-shot picker, not a persistent selector - matches the solar-gen "More..." dropdown
  };
}

async function loadDeviceAddresses() {
  if (!batteryMacInput || !chargerMacInput) return;
  try {
    const res = await fetch("/api/settings/device-addresses");
    const data = await res.json();
    batteryMacInput.value = data.battery_mac;
    chargerMacInput.value = data.charger_mac;
    renderMacHistory(batteryMacHistoryEl, data.battery_mac_history, batteryMacInput);
    renderMacHistory(chargerMacHistoryEl, data.charger_mac_history, chargerMacInput);
  } catch (e) {
    // Leave the inputs blank rather than guessing - the placeholder
    // text still shows the expected format.
  }
}

loadDeviceAddresses();

// Each device (battery/charger) applies independently and confirms
// independently - set up once per device rather than duplicating this
// whole flow twice inline (2026-10-14: "apply button on each of those"
// + "are you sure" + "validation that the ID gets sent to the ESP32").
function setupDeviceAddressApply({ deviceLabel, fieldKey, nameFieldKey, inputEl, applyBtnId, stateId }) {
  const applyBtn = document.getElementById(applyBtnId);
  const stateEl = document.getElementById(stateId);
  if (!applyBtn || !stateEl || !inputEl) return;

  // A name only means anything when it was attached via fillMacInput()
  // (a history pick or a scan result) - typing directly invalidates it,
  // since there's no reason to believe whatever name was last attached
  // still refers to the address now being typed.
  inputEl.addEventListener("input", () => delete inputEl.dataset.deviceName);

  applyBtn.addEventListener("click", async () => {
    const mac = inputEl.value.trim().toUpperCase();
    const valid = MAC_ADDRESS_RE.test(mac);
    inputEl.classList.toggle("invalid", !valid);
    if (!valid) {
      stateEl.textContent = "format must be AA:BB:CC:DD:EE:FF";
      stateEl.className = "save-state error";
      return;
    }

    // This changes which physical device the ESP32 talks to - a typo or
    // the wrong device's address means losing live readings until it's
    // fixed, so confirm before actually sending it (per Quinn
    // 2026-10-14), same as any other hard-to-silently-notice live change
    // in this app.
    const confirmed = window.confirm(
      `Change the ${deviceLabel} BLE address to ${mac}?\n\n` +
      `The ESP32 will disconnect from the current ${deviceLabel.toLowerCase()} and try to connect to this ` +
      `address instead - double check it's correct.`
    );
    if (!confirmed) return;

    stateEl.textContent = "saving…";
    stateEl.className = "save-state saving";
    try {
      const body = { [fieldKey]: mac };
      if (inputEl.dataset.deviceName) body[nameFieldKey] = inputEl.dataset.deviceName;
      const res = await fetch("/api/settings/device-addresses", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(await res.text());
      inputEl.value = mac;
      await loadDeviceAddresses();  // refresh the history dropdown with this newly-applied entry
    } catch (e) {
      stateEl.textContent = "failed to save";
      stateEl.className = "save-state error";
      return;
    }

    await waitForEsp32Confirmation({ fieldKey, expectedMac: mac, deviceLabel, stateEl });
  });
}

// Polls back api_get_device_addresses() (not just trusting the save
// succeeded) until the ESP32 itself has reported using this exact
// address - "validation that the ID gets sent to the ESP32" (2026-10-14),
// not just that it reached the database. Gives up after a while with an
// honest "can't confirm" rather than spinning forever if the ESP32 is
// offline or running older firmware that never sends this ack at all.
const ESP32_CONFIRM_POLL_MS = 3000;
const ESP32_CONFIRM_TIMEOUT_MS = 90 * 1000;
// Must match esp32_firmware/include/config.h's DEVICE_CONFIG_POLL_INTERVAL_MS -
// shown in the waiting message so "why hasn't this confirmed yet" has an
// honest answer instead of looking stuck (2026-10-14).
const DEVICE_CONFIG_POLL_INTERVAL_S = 30;

async function waitForEsp32Confirmation({ fieldKey, expectedMac, deviceLabel, stateEl }) {
  const confirmedKey = fieldKey === "battery_mac" ? "battery_mac_confirmed" : "charger_mac_confirmed";
  const startedAt = Date.now();
  const deadline = startedAt + ESP32_CONFIRM_TIMEOUT_MS;
  // The text below must actually change every tick, not just get set
  // once and sit there for up to 90s - a status line that looks
  // identical the whole time it's "working" is indistinguishable from
  // frozen (2026-10-14: "it says waiting... but that never changes").
  const setWaitingText = () => {
    const elapsedS = Math.round((Date.now() - startedAt) / 1000);
    stateEl.textContent = `saved - waiting for ESP32 to confirm… (${elapsedS}s, the ESP32 only checks for changes every ${DEVICE_CONFIG_POLL_INTERVAL_S}s)`;
    stateEl.className = "save-state saving";
  };
  setWaitingText();

  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, ESP32_CONFIRM_POLL_MS));
    try {
      const res = await fetch("/api/settings/device-addresses");
      const data = await res.json();
      // Someone may have changed it again (here or another tab) while
      // this was waiting - stop confirming a value that's no longer
      // even the current target, but say so rather than leaving this
      // tick's "waiting" text to sit there forever unexplained.
      if (data[fieldKey] !== expectedMac) {
        stateEl.textContent = `stopped waiting - a different ${deviceLabel.toLowerCase()} address was saved since`;
        stateEl.className = "save-state error";
        return;
      }
      if (data[confirmedKey]) {
        stateEl.textContent = `✓ confirmed - ESP32 is using this ${deviceLabel.toLowerCase()} address`;
        stateEl.className = "save-state saved";
        setTimeout(() => (stateEl.textContent = ""), 5000);
        return;
      }
      setWaitingText();
    } catch (e) {
      // Transient fetch failure - keep polling until the deadline, but
      // still tick the elapsed-time text so this doesn't look frozen.
      setWaitingText();
    }
  }
  stateEl.textContent = "saved, but not yet confirmed by the ESP32 (offline, or firmware doesn't report back yet)";
  stateEl.className = "save-state error";
}

setupDeviceAddressApply({
  deviceLabel: "Battery",
  fieldKey: "battery_mac",
  nameFieldKey: "battery_name",
  inputEl: batteryMacInput,
  applyBtnId: "battery-mac-apply",
  stateId: "battery-mac-save-state",
});
setupDeviceAddressApply({
  deviceLabel: "Charger",
  fieldKey: "charger_mac",
  nameFieldKey: "charger_name",
  inputEl: chargerMacInput,
  applyBtnId: "charger-mac-apply",
  stateId: "charger-mac-save-state",
});

/* ---------------- scan for devices (settings) ---------------- */
// One-off BLE scan to help find a battery/charger's address for initial
// setup - see server.py's /api/scan-devices and main.cpp's
// checkPendingScan() (per Quinn 2026-10-14). Disconnects the ESP32's
// existing connections for the scan's duration - a deliberate trade-off
// the user explicitly accepted for this rare, one-time setup action, not
// something hidden from them, hence the button's own explanatory note.

const scanDevicesBtn = document.getElementById("scan-devices-btn");
const scanDevicesState = document.getElementById("scan-devices-state");
const scanResultsEl = document.getElementById("scan-results");

function renderScanResults(devices) {
  if (!scanResultsEl) return;
  scanResultsEl.innerHTML = "";
  if (!devices.length) {
    scanResultsEl.innerHTML = '<p class="control-note">No named BLE devices found nearby - try moving closer, or scanning again.</p>';
    return;
  }
  // Recommended matches first, then by signal strength (closer/stronger first).
  const sorted = [...devices].sort((a, b) => (a.likely_type ? 0 : 1) - (b.likely_type ? 0 : 1) || b.rssi - a.rssi);
  for (const dev of sorted) {
    const row = document.createElement("div");
    row.className = "scan-result-row" + (dev.likely_type ? " recommended" : "");
    row.innerHTML = `
      <span class="scan-result-name">${dev.name}</span>
      <span class="scan-result-address">${dev.address}</span>
      <span class="scan-result-rssi">${dev.rssi} dBm</span>
      ${dev.likely_type ? `<span class="scan-result-badge">Likely ${dev.likely_type}</span>` : ""}
    `;
    const useButtons = document.createElement("span");
    useButtons.className = "use-buttons";
    const useBattery = document.createElement("button");
    useBattery.textContent = "Use as Battery";
    useBattery.addEventListener("click", () => fillMacInput(batteryMacInput, dev.address, dev.name));
    const useCharger = document.createElement("button");
    useCharger.textContent = "Use as Charger";
    useCharger.addEventListener("click", () => fillMacInput(chargerMacInput, dev.address, dev.name));
    useButtons.appendChild(useBattery);
    useButtons.appendChild(useCharger);
    row.appendChild(useButtons);
    scanResultsEl.appendChild(row);
  }
}

if (scanDevicesBtn) {
  scanDevicesBtn.addEventListener("click", async () => {
    scanDevicesBtn.disabled = true;
    scanResultsEl.innerHTML = "";
    scanDevicesState.textContent = "scanning… (battery/charger readings will pause for a few seconds)";
    scanDevicesState.className = "save-state saving";
    try {
      const res = await fetch("/api/scan-devices", { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      renderScanResults(data.devices || []);
      scanDevicesState.textContent = `found ${data.devices.length} device(s)`;
      scanDevicesState.className = "save-state saved";
    } catch (e) {
      scanDevicesState.textContent = "scan failed - is the ESP32 online?";
      scanDevicesState.className = "save-state error";
    } finally {
      scanDevicesBtn.disabled = false;
    }
  });
}

/* ---------------- restart BLE (settings) ---------------- */
// Recovery action for when battery/charger data goes missing and won't
// come back on its own - see server.py's /api/restart-ble (per Quinn
// 2026-11-06, after a battery BLE connection sat dead for 16+ hours with
// no remote way to fix it). A full ESP32 restart, same as a manual
// power-cycle - briefly drops both live connections, so it's worth a
// confirm rather than a silent one-click, same reasoning as the device-
// address Apply buttons.

const restartBleBtn = document.getElementById("restart-ble-btn");
const restartBleState = document.getElementById("restart-ble-state");

if (restartBleBtn) {
  restartBleBtn.addEventListener("click", async () => {
    const confirmed = window.confirm(
      "Restart the ESP32's Bluetooth receiver?\n\n" +
      "It will briefly disconnect from the battery and charger and reconnect fresh - takes about 15-20 seconds."
    );
    if (!confirmed) return;

    restartBleBtn.disabled = true;
    restartBleState.textContent = "sending restart command…";
    restartBleState.className = "save-state saving";
    try {
      const res = await fetch("/api/restart-ble", { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      restartBleState.textContent = "restarting - give it 15-20 seconds, then check the battery/charger dots above";
      restartBleState.className = "save-state saved";
      setTimeout(() => (restartBleState.textContent = ""), 20000);
    } catch (e) {
      restartBleState.textContent = "no response - the ESP32 may already be offline";
      restartBleState.className = "save-state error";
    } finally {
      restartBleBtn.disabled = false;
    }
  });
}

/* ---------------- history charts ---------------- */

// 15m is the shortest preset (5m was removed - not a useful enough
// window to justify a tab) and 24h is the longest - anything wider than
// a day is now a single whole calendar day via the day picker below,
// not an open-ended scrollable multi-day window (see the day-picker
// markup comment in index.html for why: a live multi-day range here
// used to mean a continuous, unbounded query re-run every second).
const RANGE_SECONDS = {
  "15m": 900, "30m": 1800, "1h": 3600, "6h": 6 * 3600,
  "12h": 12 * 3600, "24h": 24 * 3600,
};

// Shared time window across all charts (unix seconds) - dragging any
// chart pans all of them together so metrics stay comparable at a glance.
let windowSince = Date.now() / 1000 - RANGE_SECONDS["15m"];
let windowUntil = Date.now() / 1000;
let currentRangeKey = "15m";
// True while showing a live, auto-advancing preset window; false once the
// user drags away from it - a manually-panned view should stay put rather
// than snapping back to "now" on the next periodic refresh.
let liveMode = true;

// On a mouse, hover and drag-to-pan are naturally different gestures -
// the pointer only "clicks and holds" when you actually mean to drag.
// On a touchscreen they're the same gesture (touch and move), so a
// touch defaulted to always panning meant there was no way to just
// glance at a value via the tooltip without the chart immediately
// scrolling out from under your finger (2026-11-06: "if I just want to
// see the hover over rate on my phone it starts to scroll"). Touch
// defaults to tap-to-inspect instead; the "Touch: tap to inspect" toggle
// button below switches it to drag-to-pan when that's actually wanted.
// Mouse/pen dragging is completely unaffected either way - this only
// changes what a touch pointer does. Read directly by LineChart's own
// pointerdown handler (declared well before any chart is constructed).
let touchPanEnabled = localStorage.getItem("ka_touch_pan") === "1";

const liveBadge = document.getElementById("live-badge");
const scrollToLiveBtn = document.getElementById("scroll-to-live");

function updateLiveUI() {
  liveBadge.classList.toggle("on", liveMode);
  scrollToLiveBtn.hidden = liveMode;
}

// touchPanEnabled itself is declared up near the LineChart class (every
// chart's pointerdown handler reads it directly) - this just wires up
// the button that flips it, persisted per-device since it's a UI
// preference, not shared app state (same localStorage pattern as the
// desktop/mobile view toggle above).
const touchPanToggleBtn = document.getElementById("touch-pan-toggle");

function updateTouchPanToggleUI() {
  if (!touchPanToggleBtn) return;
  touchPanToggleBtn.textContent = touchPanEnabled ? "Touch: drag to scroll" : "Touch: tap to inspect";
  touchPanToggleBtn.classList.toggle("on", touchPanEnabled);
}

if (touchPanToggleBtn) {
  updateTouchPanToggleUI();
  touchPanToggleBtn.addEventListener("click", () => {
    touchPanEnabled = !touchPanEnabled;
    localStorage.setItem("ka_touch_pan", touchPanEnabled ? "1" : "0");
    updateTouchPanToggleUI();
  });
}

function selectRange(rangeKey) {
  currentRangeKey = rangeKey;
  const durationSeconds = RANGE_SECONDS[rangeKey];
  if (liveMode) {
    // The normal case: already following live data, just changing how
    // wide a window to show - stays live, anchored to now.
    const now = Date.now() / 1000;
    windowUntil = now;
    windowSince = now - durationSeconds;
  } else {
    // Browsing history (panned, or a picked day from the day picker) -
    // narrow/widen the CURRENT view to this duration around wherever we
    // already are, rather than jumping back to "now" and abandoning
    // whatever historical period was being looked at. This is what
    // makes "pick a day, then use 5m/30m/1h to zoom into it" work.
    const center = (windowSince + windowUntil) / 2;
    windowSince = center - durationSeconds / 2;
    windowUntil = center + durationSeconds / 2;
  }
  document.querySelectorAll(".range-tabs button[data-range]").forEach((x) => x.classList.toggle("active", x.dataset.range === rangeKey));
  clearDayPickerActive();
  updateLiveUI();
  loadHistory();
}

function refreshHistoryTick() {
  // Only live mode auto-advances - a panned/custom view is frozen on
  // purpose, so there's nothing to refresh (and re-fetching the same
  // window every second was racing against drag-triggered fetches:
  // whichever response happened to land last would win, which looked
  // like panning "snapping back" instead of actually scrolling).
  if (!liveMode) return;
  // Also pause while hovering any chart, even in live mode - otherwise
  // the window keeps sliding forward under the cursor every second and
  // a value you're trying to read never holds still long enough to see.
  if (Object.values(charts).some((c) => c._hovering)) return;
  const now = Date.now() / 1000;
  windowUntil = now;
  windowSince = now - RANGE_SECONDS[currentRangeKey];
  loadHistory();
}

document.querySelectorAll(".range-tabs button[data-range]").forEach((b) => {
  b.addEventListener("click", () => selectRange(b.dataset.range));
});

scrollToLiveBtn.addEventListener("click", () => {
  // selectRange() only jumps back to "now" when liveMode is already
  // true (otherwise it narrows the current historical view in place -
  // see selectRange) - this button's whole purpose is the opposite of
  // that, so set it first.
  liveMode = true;
  selectRange(currentRangeKey);
});

/* ---------------- custom date/time range picker ---------------- */

const customRangeToggle = document.getElementById("custom-range-toggle");
const customRangePanel = document.getElementById("custom-range-panel");
const customRangeFrom = document.getElementById("custom-range-from");
const customRangeTo = document.getElementById("custom-range-to");

customRangeToggle.addEventListener("click", () => {
  customRangePanel.hidden = !customRangePanel.hidden;
  if (!customRangePanel.hidden) {
    const toDate = new Date(windowUntil * 1000);
    const fromDate = new Date(windowSince * 1000);
    customRangeTo.value = toLocalDatetimeInputValue(toDate);
    customRangeFrom.value = toLocalDatetimeInputValue(fromDate);
  }
});

function toLocalDatetimeInputValue(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

document.getElementById("custom-range-apply").addEventListener("click", () => {
  const from = customRangeFrom.value ? new Date(customRangeFrom.value).getTime() / 1000 : null;
  const to = customRangeTo.value ? new Date(customRangeTo.value).getTime() / 1000 : null;
  if (!from || !to || from >= to) return;
  liveMode = false;
  windowSince = from;
  windowUntil = to;
  document.querySelectorAll(".range-tabs button[data-range]").forEach((x) => x.classList.remove("active"));
  clearDayPickerActive();
  updateLiveUI();
  customRangePanel.hidden = true;
  loadHistory();
});

// Time-of-day alone ("14:31") is only meaningful for an intraday span -
// stretched across a multi-day window (a custom multi-day range, or the
// solar generation card's "Last 30 days" overview) every tick/tooltip
// would show indistinguishable times with no way to tell which day is
// which. 36h (not 24h) as the cutoff gives a single picked day, which is
// exactly 24h wide, a little headroom rather than sitting right on the
// boundary.
const CHART_MULTI_DAY_SPAN_SECONDS = 36 * 3600;

function formatChartTimestamp(unixSeconds, spanSeconds, { withTime = false } = {}) {
  const d = new Date(unixSeconds * 1000);
  if (spanSeconds > CHART_MULTI_DAY_SPAN_SECONDS) {
    const datePart = d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
    return withTime ? `${datePart} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : datePart;
  }
  return d.toLocaleTimeString([], withTime ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" });
}

class LineChart {
  constructor(canvas, { unit = "", yMin = null, yMax = null, zeroLine = false, onPan = null, yLabelFormatter = null } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.unit = unit;
    this.yMinFixed = yMin;
    this.yMaxFixed = yMax;
    // Set directly (not a constructor option) by charts whose x-range
    // needs to be a fixed window rather than whatever the data happens
    // to span - e.g. the solar generation chart always shows the full
    // day, not just the hours it has data for so far.
    this.xMinFixed = null;
    this.xMaxFixed = null;
    this.zeroLine = zeroLine;
    this.onPan = onPan;
    // Optional (value) => string override for the y-axis tick labels -
    // e.g. the solar generation chart shows "4 kW" instead of a flat
    // "4000" for a chart whose underlying values are plain watts.
    this.yLabelFormatter = yLabelFormatter;
    this.series = []; // [{label, color, points: [{x,y}], axis: 'left'|'right', unit, fill}]
    this._hovering = false;
    this.tooltip = canvas.parentElement.querySelector(".tooltip");
    this._bindHover();
    if (onPan) this._bindDrag();
    window.addEventListener("resize", () => this.draw());
  }

  setSeries(series) {
    this.series = series;
    this.draw();
  }

  _bindDrag() {
    this._dragging = false;
    let lastX = 0;

    const onMove = (clientX) => {
      if (!this._dragging || !this._plot || !this._plot.bounds) return;
      const deltaPx = clientX - lastX;
      lastX = clientX;
      if (!deltaPx) return;
      const spanSeconds = this._plot.bounds.xMax - this._plot.bounds.xMin;
      const deltaSeconds = (deltaPx / this._plot.plotW) * spanSeconds;
      this.onPan(deltaSeconds);
    };
    const onUp = (e) => {
      this._dragging = false;
      try {
        this.canvas.releasePointerCapture(e.pointerId);
      } catch (_) {}
    };

    this.canvas.addEventListener("pointerdown", (e) => {
      // Touch defaults to tap-to-inspect (see touchPanEnabled above) -
      // letting this fall through to the hover binding's own touch
      // handlers instead of starting a drag. Mouse/pen are unaffected.
      if (e.pointerType === "touch" && !touchPanEnabled) return;
      this._dragging = true;
      lastX = e.clientX;
      this.tooltip.style.visibility = "hidden";
      this.canvas.setPointerCapture(e.pointerId);
    });
    this.canvas.addEventListener("pointermove", (e) => onMove(e.clientX));
    this.canvas.addEventListener("pointerup", onUp);
    this.canvas.addEventListener("pointercancel", onUp);
    // Belt-and-braces alongside the self-healing check in _bindHover:
    // catch the up-event at the window level too, in case it never
    // reaches the canvas at all (observed on Safari - pointer capture
    // doesn't always redirect pointerup back to the capturing element).
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }

  _bindHover() {
    const move = (clientX, mouseButtons) => {
      // Self-healing: if the mouse is moving with no button actually
      // held down, dragging cannot legitimately still be in progress -
      // force-clear a stuck _dragging flag right here rather than
      // trusting a pointerup/pointercancel to have fired. Safari in
      // particular has had cases where the up-event doesn't land on the
      // element pointer capture was set on, leaving _dragging stuck
      // true forever and silently killing hover from then on (2026-11-06:
      // "the hover over ... doesn't really work anymore" on Safari).
      if (mouseButtons === 0 && this._dragging) this._dragging = false;
      if (this._dragging) return;
      this._hovering = true;
      const rect = this.canvas.getBoundingClientRect();
      const x = clientX - rect.left;
      this._showTooltip(x, rect);
    };
    const leave = () => {
      this._hovering = false;
      this.tooltip.style.visibility = "hidden";
    };
    this.canvas.addEventListener("mousemove", (e) => move(e.clientX, e.buttons));
    this.canvas.addEventListener("mouseleave", leave);
    this.canvas.addEventListener(
      "touchstart",
      (e) => {
        move(e.touches[0].clientX);
      },
      { passive: true }
    );
    this.canvas.addEventListener(
      "touchmove",
      (e) => {
        move(e.touches[0].clientX);
      },
      { passive: true }
    );
    this.canvas.addEventListener("touchend", leave);
    this.canvas.addEventListener("touchcancel", leave);
  }

  _yBounds(seriesList, yMinFixed, yMaxFixed, zeroLine) {
    const bothFixed = yMinFixed !== null && yMinFixed !== undefined && yMaxFixed !== null && yMaxFixed !== undefined;
    let ys = seriesList.flatMap((s) => s.points).map((p) => p.y).filter((y) => y !== null && y !== undefined);
    if (zeroLine) ys.push(0);
    // A fully fixed range (e.g. the solar chart's 0-to-panel-capacity)
    // should still draw its axis frame even with zero data points yet
    // today - bailing out here would show nothing at all instead of an
    // empty day waiting to fill in.
    if (!ys.length && !bothFixed) return null;
    let yMin = yMinFixed !== null && yMinFixed !== undefined ? yMinFixed : Math.min(...ys);
    let yMax = yMaxFixed !== null && yMaxFixed !== undefined ? yMaxFixed : Math.max(...ys);
    if (yMin === yMax) {
      yMin -= 1;
      yMax += 1;
    }
    // Don't pad an explicitly fixed range (e.g. SOC's clean 0-100) - only
    // pad when we derived the bounds from the data ourselves.
    const pad = bothFixed ? 0 : (yMax - yMin) * 0.1;
    return { yMin: yMin - pad, yMax: yMax + pad };
  }

  _bounds() {
    const allPts = this.series.flatMap((s) => s.points);
    const xBothFixed = this.xMinFixed !== null && this.xMinFixed !== undefined
      && this.xMaxFixed !== null && this.xMaxFixed !== undefined;
    // Same reasoning as _yBounds: a fixed x-range (the solar chart's
    // full day, so it starts before sunrise and ends after sunset
    // instead of stretching to whatever little data exists so far -
    // per Quinn 2026-09-30) should still draw its frame with no data.
    if (!allPts.length && !xBothFixed) return null;
    const xs = allPts.map((p) => p.x);
    const xMin = xBothFixed ? this.xMinFixed : Math.min(...xs);
    const xMax = xBothFixed ? this.xMaxFixed : Math.max(...xs);

    const leftSeries = this.series.filter((s) => s.axis !== "right" && s.axis !== "soc");
    const rightSeries = this.series.filter((s) => s.axis === "right");
    const left = this._yBounds(leftSeries, this.yMinFixed, this.yMaxFixed, this.zeroLine);
    const right = rightSeries.length ? this._yBounds(rightSeries, null, null, false) : null;
    if (!left && !right) return null;

    return { xMin, xMax, left: left || right, right };
  }

  draw() {
    const ctx = this.ctx;
    const dpr = window.devicePixelRatio || 1;
    const cssW = this.canvas.clientWidth || 300;
    const cssH = this.canvas.clientHeight || 160;
    this.canvas.width = cssW * dpr;
    this.canvas.height = cssH * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const bounds = this._bounds();
    const style = getComputedStyle(document.documentElement);
    const gridColor = style.getPropertyValue("--gridline").trim();
    const baseColor = style.getPropertyValue("--baseline").trim();
    const mutedColor = style.getPropertyValue("--text-muted").trim();

    const hasRight = !!bounds && !!bounds.right;
    const padLeft = 42;
    const padRight = hasRight ? 46 : 8;
    const padTop = 10;
    const padBottom = 20;
    const plotW = cssW - padLeft - padRight;
    const plotH = cssH - padTop - padBottom;
    this._plot = { padLeft, padTop, plotW, plotH, bounds };

    if (!bounds || plotW <= 0 || plotH <= 0) return;

    const xToPx = (x) => padLeft + ((x - bounds.xMin) / (bounds.xMax - bounds.xMin || 1)) * plotW;
    const yToPxLeft = (y) => padTop + plotH - ((y - bounds.left.yMin) / (bounds.left.yMax - bounds.left.yMin || 1)) * plotH;
    const yToPxRight = bounds.right
      ? (y) => padTop + plotH - ((y - bounds.right.yMin) / (bounds.right.yMax - bounds.right.yMin || 1)) * plotH
      : null;
    // Fixed 0-100 scale compressed into a thin band at the top of the
    // plot, rather than sharing the full height the way left/right do -
    // a state-of-charge trend just needs to be glanceable alongside
    // current/power, not a full-height series of its own competing for
    // the same visual space (per Quinn 2026-09-30).
    const SOC_BAND_HEIGHT_FRACTION = 0.25;
    const yToPxSoc = (pct) => padTop + (1 - pct / 100) * (plotH * SOC_BAND_HEIGHT_FRACTION);
    this._xToPx = xToPx;
    this._yToPxFor = (axis) => (axis === "right" ? yToPxRight : axis === "soc" ? yToPxSoc : yToPxLeft);

    // gridlines + left-axis y labels (right axis, if any, shares these same
    // rows - see the pxToLeftFrac inversion below - one shared gridline set
    // keeps the chart readable instead of a cluttered double grid)
    ctx.strokeStyle = gridColor;
    ctx.fillStyle = mutedColor;
    ctx.font = "10px system-ui, sans-serif";
    ctx.lineWidth = 1;
    const yTicks = 4;
    for (let i = 0; i <= yTicks; i++) {
      const yLeft = bounds.left.yMin + ((bounds.left.yMax - bounds.left.yMin) * i) / yTicks;
      const py = yToPxLeft(yLeft);
      ctx.beginPath();
      ctx.moveTo(padLeft, py);
      ctx.lineTo(padLeft + plotW, py);
      ctx.stroke();
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      const yLabel = this.yLabelFormatter ? this.yLabelFormatter(yLeft) : yLeft.toFixed(this.unit === "%" ? 0 : 1);
      ctx.fillText(yLabel, padLeft - 6, py);

      if (bounds.right) {
        // Same pixel row, mapped through the right axis's own scale -
        // its ticks won't be "round numbers" but they line up exactly
        // with the left axis's gridlines, which is what matters here.
        const frac = (py - padTop) / plotH;
        const yRight = bounds.right.yMax - frac * (bounds.right.yMax - bounds.right.yMin);
        ctx.textAlign = "left";
        ctx.fillText(yRight.toFixed(0), padLeft + plotW + 6, py);
      }
    }

    if (this.zeroLine && bounds.left.yMin < 0 && bounds.left.yMax > 0) {
      ctx.strokeStyle = baseColor;
      ctx.lineWidth = 1;
      const py = yToPxLeft(0);
      ctx.beginPath();
      ctx.moveTo(padLeft, py);
      ctx.lineTo(padLeft + plotW, py);
      ctx.stroke();
    }

    // x labels (start / mid / end) - the start and end labels are anchored
    // to their own edge, not centered on it, otherwise half the text
    // overflows past the canvas edge (confirmed live 2026-09-14 - the
    // rightmost time label was getting cut off).
    ctx.textBaseline = "top";
    [
      { f: 0, align: "left" },
      { f: 0.5, align: "center" },
      { f: 1, align: "right" },
    ].forEach(({ f, align }) => {
      const x = bounds.xMin + (bounds.xMax - bounds.xMin) * f;
      const label = formatChartTimestamp(x, bounds.xMax - bounds.xMin);
      ctx.textAlign = align;
      ctx.fillText(label, xToPx(x), padTop + plotH + 4);
    });

    // series lines
    for (const s of this.series) {
      const pts = s.points.filter((p) => p.y !== null && p.y !== undefined);
      if (pts.length < 2) continue;
      const yToPx = s.axis === "right" ? yToPxRight : s.axis === "soc" ? yToPxSoc : yToPxLeft;
      // Purely cosmetic vertical nudge (see the "Current"/"Power" series
      // definitions in loadHistory) - never affects the actual value, so
      // gridlines and the tooltip stay 100% accurate; it only stops two
      // near-identical-shaped lines from silently sitting on top of
      // each other on screen.
      const offsetPx = s.renderOffsetPx || 0;

      // A real gap in the data (e.g. the router was off) should show as
      // an actual break in the line, not a straight segment silently
      // connecting across it - that would misrepresent missing data as
      // a smooth transition, which is actively misleading for
      // real-valued readings like voltage/SOC (a break reads as
      // "unknown"; a flat line - or worse, a flat zero - reads as
      // "this is what happened", which it isn't). Threshold is derived
      // from this series' own typical point spacing rather than a fixed
      // number, since that spacing varies a lot by zoom level (seconds
      // apart at 5m, tens of seconds apart at 10d).
      const deltas = [];
      for (let i = 1; i < pts.length; i++) deltas.push(pts[i].x - pts[i - 1].x);
      deltas.sort((a, b) => a - b);
      const typicalDelta = deltas[Math.floor(deltas.length / 2)] || 1;
      const gapThreshold = Math.max(typicalDelta * 4, 1);

      const segments = [[pts[0]]];
      for (let i = 1; i < pts.length; i++) {
        if (pts[i].x - pts[i - 1].x > gapThreshold) segments.push([]);
        segments[segments.length - 1].push(pts[i]);
      }

      // Filled area under the curve (e.g. the solar generation chart) -
      // a gradient fading to transparent toward the baseline, drawn per
      // gap-broken segment so a real data gap isn't visually bridged by
      // a fill either, same reasoning as the line itself.
      if (s.fill && s.axis !== "soc") {
        const axisBounds = s.axis === "right" ? bounds.right : bounds.left;
        const baselinePy = yToPx(Math.max(axisBounds.yMin, 0));
        const gradient = ctx.createLinearGradient(0, padTop, 0, padTop + plotH);
        // Denser/more opaque than a typical fade - closer to the solid,
        // chunky look of the Tesla-app reference (per Quinn 2026-09-30)
        // than a subtle wash.
        gradient.addColorStop(0, s.color + "cc");
        gradient.addColorStop(1, s.color + "20");
        ctx.fillStyle = gradient;
        for (const seg of segments) {
          if (seg.length < 2) continue;
          ctx.beginPath();
          seg.forEach((p, i) => {
            const px = xToPx(p.x);
            const py = yToPx(p.y) + offsetPx;
            if (i === 0) ctx.moveTo(px, py);
            else ctx.lineTo(px, py);
          });
          ctx.lineTo(xToPx(seg[seg.length - 1].x), baselinePy);
          ctx.lineTo(xToPx(seg[0].x), baselinePy);
          ctx.closePath();
          ctx.fill();
        }
      }

      ctx.strokeStyle = s.color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      // Lets two series share a color (e.g. Alternator's current and
      // power both being orange) while staying visually distinguishable -
      // solid vs dashed, rather than needing a whole extra color per
      // metric on top of the existing per-source color scheme.
      ctx.setLineDash(s.dashed ? [5, 4] : []);
      for (const seg of segments) {
        if (seg.length < 2) continue;
        ctx.beginPath();
        seg.forEach((p, i) => {
          const px = xToPx(p.x);
          const py = yToPx(p.y) + offsetPx;
          if (i === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        });
        ctx.stroke();
      }
      ctx.setLineDash([]);
    }
  }

  _showTooltip(pxX, rect) {
    if (!this._plot || !this._plot.bounds) return;
    const { bounds } = this._plot;
    const frac = (pxX - this._plot.padLeft) / this._plot.plotW;
    if (frac < 0 || frac > 1) {
      this.tooltip.style.visibility = "hidden";
      return;
    }
    const targetX = bounds.xMin + frac * (bounds.xMax - bounds.xMin);

    let lines = [];
    let nearestPx = null;
    for (const s of this.series) {
      if (!s.points.length) continue;
      let closest = s.points[0];
      let bestDiff = Infinity;
      for (const p of s.points) {
        const diff = Math.abs(p.x - targetX);
        if (diff < bestDiff) {
          bestDiff = diff;
          closest = p;
        }
      }
      if (closest.y !== null && closest.y !== undefined) {
        const unit = s.unit !== undefined ? s.unit : this.unit;
        const decimals = s.decimals !== undefined ? s.decimals : 2;
        lines.push(`<span style="color:${s.color}">●</span> ${s.label}: <b>${closest.y.toFixed(decimals)}${unit}</b>`);
        nearestPx = this._xToPx(closest.x);
      }
    }
    if (!lines.length) {
      this.tooltip.style.visibility = "hidden";
      return;
    }
    const time = formatChartTimestamp(targetX, bounds.xMax - bounds.xMin, { withTime: true });
    this.tooltip.innerHTML = `<div>${time}</div>${lines.join("<br>")}`;
    this.tooltip.style.left = (nearestPx ?? pxX) + "px";
    this.tooltip.style.top = "0px";
    this.tooltip.style.visibility = "visible";
  }
}

let panFetchTimer = null;

function panWindow(deltaSeconds) {
  liveMode = false;
  clearDayPickerActive();
  updateLiveUI();
  const now = Date.now() / 1000;
  windowSince -= deltaSeconds;
  windowUntil -= deltaSeconds;
  if (windowUntil > now) {
    const overshoot = windowUntil - now;
    windowUntil -= overshoot;
    windowSince -= overshoot;
  }
  clearTimeout(panFetchTimer);
  panFetchTimer = setTimeout(loadHistory, 60);
}

/* ---------------- day picker (browse a specific past day) ---------------- */

const dayPickerSelectEl = document.getElementById("day-picker-select");
let dayPickerSelectedStart = null;

function clearDayPickerActive() {
  dayPickerSelectedStart = null;
  if (dayPickerSelectEl) dayPickerSelectEl.value = "";
}

// Local midnight for "N days before today", in the viewer's own
// timezone - a day boundary only means something relative to wherever
// the person looking at the dashboard actually is, not the router's
// system clock (see server.py's api_data_days).
function localDayStart(daysAgo) {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - daysAgo);
  return d.getTime() / 1000;
}

function dayLabel(daysAgo, start) {
  if (daysAgo === 0) return "Today";
  if (daysAgo === 1) return "Yesterday";
  return new Date(start * 1000).toLocaleDateString([], { weekday: "short", day: "numeric" });
}

// Shared by the main history day picker and the solar generation card's
// own day picker - both need "every day that actually has data, oldest
// to newest", derived from the real oldest sample on hand rather than a
// fixed candidate window (per Quinn 2026-11-06: "show all previous days
// that have data" - retention is governed by storage.MAX_DB_SIZE_BYTES,
// not a fixed day count, so how far back it's safe to probe can move
// over time). Falls back to a conservative 10-day guess if the lookup
// itself fails, rather than showing nothing.
async function fetchDaysWithData() {
  let daysBack = 10;
  try {
    const res = await fetch("/api/oldest-sample-ts");
    const data = await res.json();
    if (data.ts !== null && data.ts !== undefined) {
      const oldestDayStart = new Date(data.ts * 1000);
      oldestDayStart.setHours(0, 0, 0, 0);
      daysBack = Math.max(1, Math.round((localDayStart(0) - oldestDayStart.getTime() / 1000) / 86400) + 1);
    }
  } catch (e) {
    // Fall back to the default above.
  }

  const starts = Array.from({ length: daysBack }, (_, i) => localDayStart(i));
  try {
    const res = await fetch(`/api/data-days?day_starts=${starts.join(",")}`);
    const data = await res.json();
    return starts.map((start, i) => ({ start, i, hasData: data.days[i].has_data }));
  } catch (e) {
    // Today (i === 0) still selectable even if this fails - everything
    // else shows as unavailable rather than guessing.
    return starts.map((start, i) => ({ start, i, hasData: i === 0 }));
  }
}

async function renderDayPicker() {
  // Defensive: a stale cached index.html served alongside a fresher
  // app.js (or vice versa) would leave this element missing - happened
  // once before this session with a different feature, fixed by a hard
  // refresh. Failing loudly here would kill the rest of the page's JS
  // (this function's own errors are caught, but nothing downstream
  // depends on it - better to just skip the day picker than let a
  // mismatch cascade into anything else breaking).
  if (!dayPickerSelectEl) return;

  const days = await fetchDaysWithData();

  // A dropdown rather than a row of buttons (per the 2026-10-14 incident
  // follow-up) - same single-bounded-day browsing, just without filling
  // up the screen with a button per day. Only days that actually have
  // data are listed (stops exactly where real retention stops) - except
  // Today (index 0), which is kept even before its first sample of the
  // day has landed, as the obvious "back to today" option.
  const placeholder = '<option value="">Pick a day&hellip;</option>';
  const options = days
    .filter(({ i, hasData }) => i === 0 || hasData)
    .map(({ start, i }) => {
      const selected = dayPickerSelectedStart === start ? "selected" : "";
      return `<option value="${start}" ${selected}>${dayLabel(i, start)}</option>`;
    });
  dayPickerSelectEl.innerHTML = placeholder + options.join("");
  if (dayPickerSelectedStart === null) dayPickerSelectEl.value = "";
}

if (dayPickerSelectEl) {
  dayPickerSelectEl.addEventListener("change", () => {
    if (dayPickerSelectEl.value) selectDay(parseFloat(dayPickerSelectEl.value));
  });
}

function selectDay(dayStart) {
  liveMode = false;
  dayPickerSelectedStart = dayStart;
  windowSince = dayStart;
  windowUntil = dayStart + 86400;
  // None of the relative presets ("last 5m", "last 1h"...) describe a
  // specific past day, so they shouldn't look selected - but the range
  // buttons still work from here to zoom into part of this day (see
  // selectRange's non-live branch).
  document.querySelectorAll(".range-tabs button[data-range]").forEach((x) => x.classList.remove("active"));
  updateLiveUI();
  renderDayPicker();
  loadHistory();
  loadSolarGenChart(dayStart);
}

renderDayPicker();
setInterval(renderDayPicker, 5 * 60 * 1000);

/* ---------------- solar energy totals (header + solar panel tile) ---------------- */

const solarTodayValueEl = document.getElementById("solar-today-value");
const tileSolarTodayEl = document.getElementById("tile-solar-today");
const tileSolarYesterdayEl = document.getElementById("tile-solar-yesterday");
const tileSolar5dayEl = document.getElementById("tile-solar-5day");
const tileSolar10dayEl = document.getElementById("tile-solar-10day");
const tileSolarAlltimeEl = document.getElementById("tile-solar-alltime");

// 4S LiFePO4 nominal (4 x 3.2V) - the conventional nameplate voltage
// battery capacity is normally quoted against, not this battery's actual
// live voltage (which swings 12-14.4V through the day) - using the
// fluctuating live voltage would make the Ah figure jitter for no good
// reason, when the whole point is a stable, comparable "how many amp-
// hours does this represent" figure (per Quinn 2026-09-30).
const NOMINAL_BATTERY_VOLTAGE_V = 12.8;

function fmtWh(wh) {
  if (wh === null || wh === undefined) return "--";
  const ahSuffix = ` (${Math.round(wh / NOMINAL_BATTERY_VOLTAGE_V)} Ah)`;
  if (wh >= 1000) return (wh / 1000).toFixed(2) + " kWh" + ahSuffix;
  return Math.round(wh) + " Wh" + ahSuffix;
}

async function fetchSolarWh(since, until) {
  try {
    const url = until === null || until === undefined
      ? `/api/solar-energy?since=${since}`
      : `/api/solar-energy?since=${since}&until=${until}`;
    const res = await fetch(url);
    const data = await res.json();
    return data.wh;
  } catch (e) {
    return null;
  }
}

async function pollSolarToday() {
  // Feeds both the header's always-visible figure and the Solar panel
  // tile's "Today" row - same number, one fetch, kept in sync rather
  // than risking the two drifting a poll apart from each other.
  const midnight = localDayStart(0);
  const wh = await fetchSolarWh(midnight, null);
  if (wh === null) return; // leave both showing whatever they last showed
  const label = fmtWh(wh);
  if (solarTodayValueEl) solarTodayValueEl.textContent = label;
  if (tileSolarTodayEl) tileSolarTodayEl.textContent = label;
}

pollSolarToday();
// A running daily total doesn't need per-second precision - 30s keeps
// it feeling live without adding unnecessary load for something this
// slow-moving.
setInterval(pollSolarToday, 30 * 1000);

async function fetchSolarWhAlltime() {
  try {
    const res = await fetch("/api/solar-energy-alltime");
    const data = await res.json();
    return data.wh;
  } catch (e) {
    return null;
  }
}

async function refreshSolarHistoryStats() {
  // Yesterday/5-day/10-day are "last N *completed* days" - they only
  // ever change right at midnight rollover, so a 5-minute refresh
  // (piggybacking on the day picker's own cadence) is already more
  // often than they can actually change. The all-time total only
  // updates hourly server-side anyway (see
  // storage.accumulate_alltime_solar_wh) so this cadence is still
  // plenty fresh for it too.
  const todayStart = localDayStart(0);
  const [yesterday, fiveDay, tenDay, alltime] = await Promise.all([
    fetchSolarWh(localDayStart(1), todayStart),
    fetchSolarWh(localDayStart(5), todayStart),
    fetchSolarWh(localDayStart(10), todayStart),
    fetchSolarWhAlltime(),
  ]);
  if (tileSolarYesterdayEl) tileSolarYesterdayEl.textContent = fmtWh(yesterday);
  if (tileSolar5dayEl) tileSolar5dayEl.textContent = fmtWh(fiveDay);
  if (tileSolar10dayEl) tileSolar10dayEl.textContent = fmtWh(tenDay);
  if (tileSolarAlltimeEl) tileSolarAlltimeEl.textContent = fmtWh(alltime);
}

refreshSolarHistoryStats();
setInterval(refreshSolarHistoryStats, 5 * 60 * 1000);

function fmtSolarAxisLabel(w) {
  // Panel is 200W today but this must keep working if it's ever
  // upgraded (per Quinn 2026-09-30) - a single day's instantaneous
  // power realistically never reaches 1000W from a panel this size, but
  // the formatter itself doesn't assume that, only reacts to whatever
  // the actual y-axis range (driven by the settings max-capacity value)
  // turns out to be.
  return w >= 1000 ? (w / 1000).toFixed(1) : Math.round(w).toString();
}

const charts = {
  battCurrent: new LineChart(document.getElementById("chart-batt-current"), { unit: "A", zeroLine: true, onPan: panWindow }),
  sourceCurrent: new LineChart(document.getElementById("chart-source-current"), { unit: "A", onPan: panWindow }),
  loads: new LineChart(document.getElementById("chart-loads"), { unit: "W", onPan: panWindow }),
  // No onPan - this one always shows a full fixed day (today, or
  // whichever day is picked via the day picker - see selectDay), not
  // the shared free-scrolling window the other charts share.
  solarGen: new LineChart(document.getElementById("chart-solar-gen"), { unit: "W", yLabelFormatter: fmtSolarAxisLabel }),
};
charts.solarGen.yMinFixed = 0;
charts.solarGen.yMaxFixed = solarPanelMaxW; // placeholder default - loadSolarPanelMaxW() overwrites once fetched

let solarGenDayStart = null; // set on first loadSolarGenChart() call
let solarGenIsToday = true;

function solarGenDateLabel(dayStart) {
  const daysAgo = Math.round((localDayStart(0) - dayStart) / 86400);
  if (daysAgo === 0) return "Today";
  if (daysAgo === 1) return "Yesterday";
  return new Date(dayStart * 1000).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}

// "day": showing one day's intraday power curve (the normal case).
// "overview": showing a Last-N-days daily-totals trend instead - see
// loadSolarGenOverview().
let solarGenMode = "day";
// Which overview is currently showing (10 or 30), or null - drives the
// "10 day overview"/"30 day overview" buttons' highlighted state (per
// Quinn 2026-11-06: "can you highlight 10 day overview or 30 day
// overview if I click those?").
let solarGenOverviewDays = null;

function updateSolarGenOverviewButtonsUI() {
  const btn10 = document.getElementById("solar-gen-10d-btn");
  const btn30 = document.getElementById("solar-gen-30d-btn");
  if (btn10) btn10.classList.toggle("active", solarGenMode === "overview" && solarGenOverviewDays === 10);
  if (btn30) btn30.classList.toggle("active", solarGenMode === "overview" && solarGenOverviewDays === 30);
}

// Every day that actually has data (same mechanism, and same
// fetchDaysWithData() helper, as the main history day picker above) -
// replaces the old fixed "Today + 4 days back" button row, which
// couldn't reach further back than that no matter how much real history
// existed (per Quinn 2026-11-06).
const solarGenDaySelectEl = document.getElementById("solar-gen-day-select");

async function renderSolarGenDaySelect() {
  if (!solarGenDaySelectEl) return;
  const days = await fetchDaysWithData();
  const placeholder = '<option value="">Previous days&hellip;</option>';
  const options = days
    .filter(({ i, hasData }) => i === 0 || hasData)
    .map(({ start, i }) => {
      const selected = solarGenMode === "day" && solarGenDayStart === start ? "selected" : "";
      return `<option value="${start}" ${selected}>${solarGenDateLabel(start)}</option>`;
    });
  solarGenDaySelectEl.innerHTML = placeholder + options.join("");
  if (!(solarGenMode === "day" && solarGenDayStart !== null)) solarGenDaySelectEl.value = "";
}

if (solarGenDaySelectEl) {
  solarGenDaySelectEl.addEventListener("change", () => {
    if (solarGenDaySelectEl.value) loadSolarGenChart(parseFloat(solarGenDaySelectEl.value));
  });
}

// "10 day overview" / "30 day overview" - plain buttons rather than
// tucked into a "More..." dropdown (2026-11-06: now that the day picker
// above is itself a dropdown, nesting another choice inside a second
// dropdown just added a click for no benefit - these are exactly two
// options, which a dropdown doesn't earn its keep over).
const solarGen10dBtn = document.getElementById("solar-gen-10d-btn");
const solarGen30dBtn = document.getElementById("solar-gen-30d-btn");
if (solarGen10dBtn) solarGen10dBtn.addEventListener("click", () => loadSolarGenOverview(10));
if (solarGen30dBtn) solarGen30dBtn.addEventListener("click", () => loadSolarGenOverview(30));

function fmtSolarOverviewAxisLabel(wh) {
  return wh >= 1000 ? (wh / 1000).toFixed(1) : Math.round(wh).toString();
}

async function loadSolarGenOverview(daysBack) {
  solarGenMode = "overview";
  solarGenOverviewDays = daysBack;
  solarGenDayStart = null;
  solarGenIsToday = false; // belt-and-braces alongside the solarGenMode check in the refresh interval below
  renderSolarGenDaySelect(); // clears the day picker's selection
  updateSolarGenOverviewButtonsUI();

  const dateLabelEl = document.getElementById("solar-gen-date");
  if (dateLabelEl) dateLabelEl.textContent = `Last ${daysBack} days`;

  // Oldest to newest, today included (as a partial/in-progress bar) -
  // one bounded batched request rather than daysBack separate ones (see
  // server.py's api_solar_energy_by_day).
  const starts = Array.from({ length: daysBack }, (_, i) => localDayStart(daysBack - 1 - i));

  charts.solarGen.xMinFixed = starts[0];
  charts.solarGen.xMaxFixed = starts[starts.length - 1] + 86400;
  charts.solarGen.yLabelFormatter = fmtSolarOverviewAxisLabel;

  try {
    const res = await fetch(`/api/solar-energy-by-day?day_starts=${starts.join(",")}`);
    const data = await res.json();
    const days = data.days || [];
    // Plotted at each day's midpoint, one point per day - a simple
    // trend line/area rather than true bars (reuses the existing chart
    // machinery - panning/tooltip/resize - instead of a second
    // from-scratch bar renderer for what both close readings of this
    // request boil down to: "a nice, easy, single graph").
    const points = days.map((d) => ({ x: d.start + 43200, y: d.wh }));
    const totalWh = days.reduce((sum, d) => sum + (d.wh || 0), 0);
    charts.solarGen.setSeries([{ label: "Daily solar generation", color: getVar("--series-solar"), points, fill: true }]);
    setText("solar-gen-total-value", fmtWh(totalWh));

    const unitEl = document.getElementById("solar-gen-unit");
    if (unitEl) unitEl.textContent = "Wh/day";
  } catch (e) {
    // Leave the chart/total showing whatever they last showed.
  }
}

// Fixed daylight window rather than midnight-to-midnight - there's
// nothing to see overnight, and stretching the chart to include hours
// that never have sun just wastes width. 4am-8pm gives a bit of leeway
// either side of actual sunrise/sunset without needing real astronomical
// calculation - Australia (where this vehicle lives) never sees sun
// outside that range (per Quinn 2026-09-30).
const SOLAR_CHART_START_HOUR = 4;
const SOLAR_CHART_END_HOUR = 20;

async function loadSolarGenChart(dayStart) {
  solarGenMode = "day"; // back out of the 10/30-day overview, if that was showing
  solarGenOverviewDays = null;
  charts.solarGen.yLabelFormatter = fmtSolarAxisLabel;
  solarGenDayStart = dayStart;
  solarGenIsToday = dayStart === localDayStart(0);
  renderSolarGenDaySelect(); // keeps the dropdown's selection in sync however this got called - itself, the main day picker, or the periodic auto-refresh
  updateSolarGenOverviewButtonsUI();
  const windowStart = dayStart + SOLAR_CHART_START_HOUR * 3600;
  const windowEnd = dayStart + SOLAR_CHART_END_HOUR * 3600;

  // Fixed range rather than derived from whatever data actually exists -
  // early in the day that data might only span the last hour, which
  // used to stretch to fill the whole chart width instead of showing a
  // small blip with the rest of the window still to come.
  charts.solarGen.xMinFixed = windowStart;
  charts.solarGen.xMaxFixed = windowEnd;

  const dateLabelEl = document.getElementById("solar-gen-date");
  if (dateLabelEl) dateLabelEl.textContent = solarGenDateLabel(dayStart);

  try {
    const [histData, wh] = await Promise.all([
      fetch(`/api/history?since=${windowStart}&until=${windowEnd}&points=300`).then((r) => r.json()),
      fetchSolarWh(windowStart, windowEnd),
    ]);
    const chargerRows = histData.charger || [];
    const points = chargerRows.map((r) => ({ x: r.ts, y: r.panel_p }));
    charts.solarGen.setSeries([{ label: "Solar", color: getVar("--series-solar"), points, fill: true }]);
    setText("solar-gen-total-value", fmtWh(wh));

    // Instantaneous power from a panel this size realistically never
    // crosses into kW territory (per Quinn 2026-09-30), but this reacts
    // to the actual data/setting rather than assuming that, so it keeps
    // working correctly if a much bigger panel is ever fitted.
    const unitEl = document.getElementById("solar-gen-unit");
    if (unitEl) unitEl.textContent = charts.solarGen.yMaxFixed >= 1000 ? "kW" : "W";
  } catch (e) {
    // Leave the chart/total showing whatever they last showed.
  }
}

loadSolarPanelMaxW();
loadSolarGenChart(localDayStart(0));
setInterval(() => {
  // Only re-fetch while looking at today's per-day chart - a past day's
  // chart/total are done changing (same reasoning as the day picker's
  // own data), and the 30-day overview has its own refresh story (it's
  // a one-shot "show me the trend" request, not a live view - picking
  // it again gets the latest numbers).
  if (solarGenMode === "day" && solarGenIsToday) loadSolarGenChart(localDayStart(0));
}, 60 * 1000);

let historyRequestSeq = 0;

// The three shared history charts always frame exactly the requested
// window (windowSince..windowUntil), not whatever subset of it actually
// has data - otherwise a 24h view with only the last hour of real
// samples (e.g. right after a restart) silently shrinks to a 1-hour-wide
// chart instead of showing 23 hours of honest blank space, and a picked
// day with a gap in the middle would stretch to hide the gap instead of
// showing it. Set here, in the one function every window-change flow
// (range tabs, day picker, custom range, panning) funnels through
// before fetching, rather than at each of those call sites separately
// (confirmed live 2026-10-14: this was exactly the "24h only goes back
// to 2pm" and "day picker doesn't show midnight to midnight" report).
function syncChartWindowToRequest() {
  for (const c of [charts.battCurrent, charts.sourceCurrent, charts.loads]) {
    c.xMinFixed = windowSince;
    c.xMaxFixed = windowUntil;
  }
}

// Which date(s) the current (non-live) window actually covers - derived
// straight from windowSince/windowUntil rather than from "which day
// picker option is selected", so it stays correct no matter how you got
// here: pick a day then zoom with 6h/12h (which clears the day picker's
// own selection - see selectRange), drag a chart across a day boundary,
// or use Custom... directly. Addresses "I don't actually know which day
// I'm scrolling around" once a picked day gets zoomed into (2026-10-14).
function updateViewingDateLabel() {
  const el = document.getElementById("viewing-date-label");
  if (!el) return;
  if (liveMode) {
    el.hidden = true;
    return;
  }
  const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  // windowUntil is an exclusive-ish boundary (a picked day runs
  // dayStart..dayStart+86400) - stepping back a second before labeling
  // it stops a window that ends exactly at next midnight from reading
  // as if it spills into the following day.
  const startLabel = fmtDate(windowSince);
  const endLabel = fmtDate(windowUntil - 1);
  el.textContent = startLabel === endLabel ? startLabel : `${startLabel} – ${endLabel}`;
  el.hidden = false;
}

async function loadHistory() {
  syncChartWindowToRequest();
  updateViewingDateLabel();
  const seq = ++historyRequestSeq;
  let data;
  try {
    const res = await fetch(`/api/history?since=${windowSince}&until=${windowUntil}&points=300`);
    data = await res.json();
  } catch (e) {
    return;
  }
  // A newer request has since been issued (e.g. more dragging happened
  // while this one was in flight) - applying this older response now
  // would visually snap the chart back to a stale window.
  if (seq !== historyRequestSeq) return;

  const battery = data.battery || [];
  const chargerRows = data.charger || [];

  charts.battCurrent.setSeries([
    // Current and power are nearly proportional (power = voltage x
    // current, and voltage barely moves) - once each axis independently
    // auto-scales to fill the same plot height, the two lines end up
    // tracing almost exactly the same path and sit right on top of each
    // other. renderOffsetPx nudges them a few pixels apart purely for
    // visual separation - it never touches the actual value->pixel
    // mapping tooltips/gridlines use (per Quinn 2026-09-30).
    { label: "Current", color: getVar("--series-battery"), axis: "left", unit: "A", renderOffsetPx: -3,
      points: battery.map((r) => ({ x: r.ts, y: r.current })) },
    { label: "Power", color: getVar("--series-loads"), axis: "right", unit: "W", renderOffsetPx: 3,
      points: battery.map((r) => ({ x: r.ts, y: r.power })) },
    // Folded in from the old standalone SOC chart (per Quinn 2026-09-30)
    // - axis:"soc" draws it compressed into a thin band rather than
    // competing for the full chart height, since a state-of-charge
    // trend just needs to be glanceable, not a full-height graph.
    { label: "State of charge", color: getVar("--series-alt"), axis: "soc", unit: "%", decimals: 0,
      points: battery.map((r) => ({ x: r.ts, y: r.soc_percent })) },
  ]);

  charts.sourceCurrent.setSeries([
    // Current (A, left axis, solid) and power (W, right axis, dashed) -
    // each source keeps its own color for both metrics (dashed vs solid
    // is what tells them apart) rather than needing two more colors on
    // top of the existing alternator/solar scheme. Dashing alone wasn't
    // enough to tell them apart though - same root cause as the battery
    // chart (power is roughly proportional to current, so once each axis
    // auto-scales to fill the same height the two traces still sit
    // right on top of each other) - so renderOffsetPx nudges them apart
    // too, same fix, same reasoning (per Quinn 2026-09-30).
    { label: "Alternator (A)", color: getVar("--series-alt"), axis: "left", unit: "A", renderOffsetPx: -3,
      points: chargerRows.map((r) => ({ x: r.ts, y: r.alt_c })) },
    { label: "Solar (A)", color: getVar("--series-solar"), axis: "left", unit: "A", renderOffsetPx: -3,
      points: chargerRows.map((r) => ({ x: r.ts, y: r.panel_c })) },
    { label: "Alternator (W)", color: getVar("--series-alt"), axis: "right", unit: "W", dashed: true, renderOffsetPx: 3,
      points: chargerRows.map((r) => ({ x: r.ts, y: r.alt_p })) },
    { label: "Solar (W)", color: getVar("--series-solar"), axis: "right", unit: "W", dashed: true, renderOffsetPx: 3,
      points: chargerRows.map((r) => ({ x: r.ts, y: r.panel_p })) },
  ]);

  // Total power usage: joined per charger sample timestamp against the
  // nearest battery sample. The charger's own reported *battery-side*
  // power ("batt_p" - a real sensor reading at its own battery-side
  // terminals, per PROTOCOL.md, not derived from the battery's BMS)
  // minus the battery's own reported power - no conversion-efficiency
  // guess needed, since both readings are already on the battery side
  // of the charger's internal DC-DC conversion (per Quinn 2026-11-06 -
  // previously this scaled the alternator/solar INPUT-side power by a
  // fixed assumed efficiency instead, which was a guess rather than a
  // measurement).
  const loadsPts = chargerRows.map((r) => {
    const nearestBatt = nearestByTs(battery, r.ts);
    if (!nearestBatt) return { x: r.ts, y: null };
    const chargerBattSideW = r.batt_p || 0;
    const battNetW = nearestBatt.power || 0;
    return { x: r.ts, y: chargerBattSideW - battNetW };
  });
  charts.loads.setSeries([{ label: "Total power usage", color: getVar("--series-loads"), points: loadsPts }]);
}

function nearestByTs(rows, ts) {
  if (!rows.length) return null;
  let best = rows[0];
  let bestDiff = Math.abs(rows[0].ts - ts);
  for (const r of rows) {
    const diff = Math.abs(r.ts - ts);
    if (diff < bestDiff) {
      bestDiff = diff;
      best = r;
    }
  }
  return best;
}

function getVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

updateLiveUI();
refreshHistoryTick();
// 2s rather than 1s - the graphs re-fetching/re-bucketing on every tick
// is more work than the plain live-tile numbers (pollLatest, still at
// 1s), and halving this frequency measurably reduces router load for a
// barely-perceptible difference in how "live" the charts feel (per
// Quinn 2026-10-01).
setInterval(refreshHistoryTick, 2000);
