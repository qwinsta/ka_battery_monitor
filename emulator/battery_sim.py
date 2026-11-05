#!/usr/bin/env python3
"""Physically-grounded battery simulation: coulomb-counting integration
driven by three high-level inputs (load draw, solar input, alternator
current) rather than scripting every field by hand.

Validated against a real data point (2026-09-14): running a 2000W
induction hob off the battery via an inverter for ~8 minutes drained
about 10% of the pack. This model predicts 9.7% for that exact scenario
(230Ah nominal, 13.2V mid-SOC OCV, 8m-ohm assumed internal resistance) -
close enough to trust the model's shape, not just its capacity number.
"""
from dataclasses import dataclass

NOMINAL_VOLTAGE = 13.2       # used only for energy/OCV shape, not a fixed output
ALT_VOLTAGE = 13.8           # matches real captured alternator voltage
PANEL_VOLTAGE = 20.0         # matches real captured solar panel voltage
INTERNAL_RESISTANCE_OHM = 0.008
LVC_VOLTAGE = 10.0           # low-voltage cutoff - protection trips below this

# Piecewise-linear open-circuit-voltage curve vs SOC%, LiFePO4 4S shape:
# flat through the middle, steep near empty and near full - this is what
# makes "getting low" feel sudden rather than linear, same as a real pack.
_OCV_CURVE = [
    (0, 10.0), (5, 11.6), (10, 12.4), (20, 13.0),
    (50, 13.2), (80, 13.3), (90, 13.4), (100, 13.6),
]


def _ocv_from_soc(soc: float) -> float:
    soc = max(0.0, min(100.0, soc))
    for (s0, v0), (s1, v1) in zip(_OCV_CURVE, _OCV_CURVE[1:]):
        if s0 <= soc <= s1:
            frac = (soc - s0) / (s1 - s0) if s1 != s0 else 0
            return v0 + frac * (v1 - v0)
    return _OCV_CURVE[-1][1]


@dataclass
class BatterySimState:
    voltage: float
    current: float          # positive = charging
    soc_percent: float
    remaining_ah: float
    temperature_c: float
    protection_tripped: bool


class BatterySim:
    """Owns the battery's simulated state and steps it forward in time.
    charger_efficiency (config.CHARGER_EFFICIENCY) is applied to
    alternator+solar input before it reaches the bus, same correction the
    real dashboard applies - a load drawing straight off the bus/inverter
    does NOT go through that conversion, so it isn't scaled."""

    def __init__(self, nominal_ah: float, start_soc: float, charger_efficiency: float):
        self.nominal_ah = nominal_ah
        self.charger_efficiency = charger_efficiency
        self.remaining_ah = nominal_ah * start_soc / 100
        self.temperature_c = 20.0
        self.protection_tripped = False

    def step(self, load_w: float, solar_w: float, alt_current_a: float, dt_hours: float) -> BatterySimState:
        charger_output_w = (alt_current_a * ALT_VOLTAGE + solar_w) * self.charger_efficiency
        net_w = charger_output_w - load_w

        soc = self.remaining_ah / self.nominal_ah * 100
        ocv = _ocv_from_soc(soc)

        # Estimate current from OCV, then refine once against the
        # IR-drop-adjusted terminal voltage (same two-step approach used
        # for the validation check above).
        approx_current = net_w / ocv if ocv > 0 else 0
        terminal_v = ocv + approx_current * INTERNAL_RESISTANCE_OHM
        current = net_w / terminal_v if terminal_v > 0 else 0

        if self.protection_tripped:
            if current >= 0:
                self.protection_tripped = False  # charging resumed - clear the trip
            else:
                current = 0
                terminal_v = ocv
        elif terminal_v <= LVC_VOLTAGE and current < 0:
            # Real BMS behaviour: open the discharge FET rather than let
            # the pack be driven below its low-voltage cutoff.
            self.protection_tripped = True
            current = 0
            terminal_v = ocv

        self.remaining_ah += current * dt_hours
        self.remaining_ah = max(0.0, min(self.nominal_ah, self.remaining_ah))

        # Light thermal touch: warms slightly under heavy current, drifts
        # back toward ambient otherwise - not a rigorous thermal model,
        # just enough to not sit dead-flat on the dashboard.
        target_temp = 20.0 + min(abs(current) * 0.03, 15.0)
        self.temperature_c += (target_temp - self.temperature_c) * min(dt_hours * 20, 1.0)

        soc = self.remaining_ah / self.nominal_ah * 100
        return BatterySimState(
            voltage=round(terminal_v, 2),
            current=round(current, 2),
            soc_percent=round(soc, 1),
            remaining_ah=round(self.remaining_ah, 1),
            temperature_c=round(self.temperature_c, 1),
            protection_tripped=self.protection_tripped,
        )
