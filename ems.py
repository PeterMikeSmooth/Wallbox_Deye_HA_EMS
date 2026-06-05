"""EMS — Energy Management System for Deye battery + Wallbox Pulsar Max.

State machine with 7 states:
  IDLE, FULL_SPEED, EV_NO_SOLAR, BATTERY_PRIORITY,
  SOLAR_ONLY, SOLAR_BOOSTED, STORAGE_BOOSTED, STORAGE_ONLY.

Modes (input_select.ems_mode):
  SOLAR_ONLY, SOLAR_BOOSTED, FULL_SPEED, STORAGE_BOOSTED, STORAGE_ONLY.

See README.md for full specification.
"""

import enum
import logging
import os
import time
import sys
from datetime import datetime

import config
from ha_api import HomeAssistantAPI


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    log = logging.getLogger("ems")
    log.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # File handler
    os.makedirs(os.path.dirname(config.LOG_FILE) or ".", exist_ok=True)
    fh = logging.FileHandler(config.LOG_FILE)
    fh.setFormatter(formatter)
    log.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    log.addHandler(ch)

    return log


log = _setup_logging()


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class State(enum.Enum):
    IDLE = "IDLE"                        # Internal: no EV charging detected
    FULL_SPEED = "FULL_SPEED"            # Mode: wallbox 32A, battery covers house
    EV_NO_SOLAR = "EV_NO_SOLAR"          # Internal: no solar, wallbox 6A, discharge limited to house
    BATTERY_PRIORITY = "BATTERY_PRIORITY"  # Internal: solar present but SOC < priority
    SOLAR_ONLY = "SOLAR_ONLY"            # Mode: solar surplus → wallbox, grid ≈ 0
    SOLAR_BOOSTED = "SOLAR_BOOSTED"      # Mode: wallbox boosted, grid pays 50-60% of EV
    STORAGE_BOOSTED = "STORAGE_BOOSTED"  # Mode: battery discharge + grid pays 50-60%
    STORAGE_ONLY = "STORAGE_ONLY"        # Mode: battery + solar → wallbox, grid = 0


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def is_off_peak() -> bool:
    """Return True if current time is in an off-peak window."""
    now = datetime.now()
    minutes = now.hour * 60 + now.minute
    for start, end in config.OFF_PEAK_WINDOWS:
        sh, sm = start
        eh, em = end
        s = sh * 60 + sm
        e = eh * 60 + em
        if s <= e:
            if s <= minutes < e:
                return True
        else:  # crosses midnight
            if minutes >= s or minutes < e:
                return True
    return False


# ---------------------------------------------------------------------------
# EMS controller
# ---------------------------------------------------------------------------

class EMS:
    def __init__(self, ha: HomeAssistantAPI):
        self.ha = ha
        self.state = State.IDLE
        self._ema_discharge = None          # smoothed discharge current (A)
        self._last_written_discharge = None  # last integer written to HA
        self._last_written_wallbox = None
        self._last_written_charging = None
        self._last_slow_tick = 0.0          # timestamp of last slow-loop action
        self._storage_low_soc = False       # STORAGE_TO_EV: SOC below floor
        self._car_connected = False         # wallbox: car plugged in
        self._last_written_grid_ratio = None
        self._battery_voltage = 52.0        # last known battery voltage
        # Wallbox override detection
        self._wallbox_override_since = None  # timestamp when override first detected
        self._wallbox_override_retries = 0   # number of toggle retries attempted
        # Dusk/sunrise tracking for overnight range calculation
        self._solar_was_available = False     # was solar > threshold last tick
        self._soc_at_dusk = None              # SOC % when solar dropped below threshold
        self._sunrise_computed = False        # already computed today's sunrise values
        # Force safe wallbox default on startup
        self.ha.set_wallbox_current(config.WALLBOX_MIN_CURRENT_A)
        self._last_written_wallbox = config.WALLBOX_MIN_CURRENT_A
        log.info("SET wallbox_current = %d A (startup)", config.WALLBOX_MIN_CURRENT_A)

    # -- entry actions --------------------------------------------------------

    def _enter_idle(self) -> None:
        log.info("→ IDLE")
        self.state = State.IDLE
        self._ema_discharge = None
        self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A)
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)

    def _enter_full_speed(self) -> None:
        log.info("→ FULL_SPEED")
        self.state = State.FULL_SPEED
        self._ema_discharge = None
        self._set_wallbox(config.WALLBOX_MAX_CURRENT_A)
        self._last_slow_tick = time.monotonic()

    def _enter_ev_no_solar(self) -> None:
        log.info("→ EV_NO_SOLAR")
        self.state = State.EV_NO_SOLAR
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)

    def _enter_battery_priority(self, mode: str = "SOLAR_ONLY") -> None:
        log.info("→ BATTERY_PRIORITY")
        self.state = State.BATTERY_PRIORITY
        self._ema_discharge = None
        # Boosted modes: never discharge battery even in BATTERY_PRIORITY
        if mode == "SOLAR_BOOSTED":
            self._set_max_discharging(0)
        else:
            self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A)
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)

    def _enter_solar_only(self) -> None:
        log.info("→ SOLAR_ONLY")
        self.state = State.SOLAR_ONLY
        self._ema_discharge = None
        self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A)
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)
        self._last_slow_tick = 0.0  # force immediate first wallbox adjustment

    def _enter_solar_boosted(self) -> None:
        log.info("→ SOLAR_BOOSTED")
        self.state = State.SOLAR_BOOSTED
        self._ema_discharge = None
        self._set_max_discharging(0)
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)
        self._last_slow_tick = 0.0  # force immediate first wallbox adjustment


    def _enter_storage_boosted(self) -> None:
        log.info("→ STORAGE_BOOSTED")
        self.state = State.STORAGE_BOOSTED
        self._ema_discharge = None
        self._storage_low_soc = False
        self._set_wallbox(config.WALLBOX_MAX_CURRENT_A)
        self._last_slow_tick = time.monotonic()

    def _enter_storage_only(self) -> None:
        log.info("→ STORAGE_ONLY")
        self.state = State.STORAGE_ONLY
        self._ema_discharge = None
        self._storage_low_soc = False
        self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A)
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)
        self._last_slow_tick = 0.0  # force immediate first wallbox adjustment

    # -- write-with-dedup helpers ---------------------------------------------

    def _set_max_discharging(self, amps: int) -> None:
        # Global cap: only apply MAX_DISCHARGE_POWER_W when EV is NOT charging
        if self.state == State.IDLE:
            max_from_power = int(config.MAX_DISCHARGE_POWER_W / max(self._battery_voltage, 1.0))
            amps = min(amps, max_from_power)
        if self._last_written_discharge != amps:
            self.ha.set_max_discharging_current(amps)
            log.info("SET max_discharging_current = %d A", amps)
            self._last_written_discharge = amps

    def _set_max_charging(self, amps: int) -> None:
        if self._last_written_charging != amps:
            self.ha.set_max_charging_current(amps)
            log.info("SET max_charging_current = %d A", amps)
            self._last_written_charging = amps

    def _set_wallbox(self, amps: int) -> None:
        # Always write — the wallbox cloud integration may override our value
        self.ha.set_wallbox_current(amps)
        if self._last_written_wallbox != amps:
            log.info("SET wallbox_current = %d A", amps)
            self._last_written_wallbox = amps

    def _set_grid_ratio(self, pct: int) -> None:
        if self._last_written_grid_ratio != pct:
            try:
                self.ha.set_input_number("input_number.grid_ratio_value", pct)
            except Exception:
                log.warning("Failed to update grid_ratio_value", exc_info=True)
            self._last_written_grid_ratio = pct

    # -- algorithms -----------------------------------------------------------

    def _compute_discharge_limit(self, s: dict) -> int:
        """Discharge limitation algorithm (EV_NO_SOLAR only).

        Returns the integer max discharge current in A.
        """
        house_load = (
            s["solar_power"] + s["battery_power"] + s["grid_power"] - s["ev_power"]
        )
        raw = max(house_load, 0) / max(s["battery_voltage"], 1.0) + config.DISCHARGE_MARGIN_A

        # EMA smoothing
        if self._ema_discharge is None:
            self._ema_discharge = raw
        else:
            self._ema_discharge = (
                config.EMA_ALPHA * raw + (1 - config.EMA_ALPHA) * self._ema_discharge
            )

        return int(clamp(round(self._ema_discharge), 0, 100))

    def _compute_wallbox_surplus(self, s: dict, grid_target: float = 0) -> int:
        """Surplus steering algorithm.

        Incremental: adjusts from current setpoint based on observed
        grid + battery error.  Works regardless of wallbox charging mode
        (minimal, normal, etc.) since we never assume P = I × V.

        grid_target: desired grid import in watts.
          - SOLAR_ONLY: 0 (grid ≈ 0)
          - SOLAR_BOOSTED: ev_power * ratio (grid imports a share of EV)
        """
        excess = -(s["grid_power"] + s["battery_power"]) + grid_target
        voltage = max(s["grid_voltage"], 1.0)
        delta = round(excess / voltage)
        current = self._last_written_wallbox or config.WALLBOX_MIN_CURRENT_A
        target = current + delta
        return int(clamp(target, config.WALLBOX_MIN_CURRENT_A, config.WALLBOX_MAX_CURRENT_A))

    def _compute_storage_discharge(self, s: dict, grid_ratio: float) -> int:
        """Discharge algorithm for STORAGE_BOOSTED / STORAGE_ONLY.

        Targets grid = grid_ratio × ev_power.
        At equilibrium: discharge ≈ (1 - grid_ratio) × ev + house - solar.
        """
        target_power = (
            s["battery_power"] + s["grid_power"]
            - grid_ratio * s["ev_power"]
        )
        raw = max(target_power, 0) / max(s["battery_voltage"], 1.0) + config.DISCHARGE_MARGIN_A

        # EMA smoothing
        if self._ema_discharge is None:
            self._ema_discharge = raw
        else:
            self._ema_discharge = (
                config.EMA_ALPHA * raw + (1 - config.EMA_ALPHA) * self._ema_discharge
            )

        return int(clamp(round(self._ema_discharge), 0, 100))

    # -- wallbox override protection ------------------------------------------

    _OVERRIDE_THRESHOLD_W = 1500  # ev_power must exceed expected by this much
    _OVERRIDE_CONFIRM_S = 30      # seconds before triggering a retry

    def _check_wallbox_override(self, s: dict) -> None:
        """Detect when wallbox ignores our current setpoint and toggle retry."""
        if self._last_written_wallbox is None:
            return

        expected_w = self._last_written_wallbox * max(s["grid_voltage"], 1.0)
        actual_w = s["ev_power"]
        overshoot = actual_w - expected_w

        if overshoot > self._OVERRIDE_THRESHOLD_W:
            now = time.monotonic()
            if self._wallbox_override_since is None:
                self._wallbox_override_since = now
                log.warning(
                    "WALLBOX OVERRIDE DETECTED: setpoint=%dA (%.0fW expected) "
                    "but ev_power=%.0fW (overshoot=+%.0fW). Monitoring...",
                    self._last_written_wallbox, expected_w, actual_w, overshoot,
                )
            elif now - self._wallbox_override_since >= self._OVERRIDE_CONFIRM_S:
                # Confirmed override — toggle to force cloud update
                self._wallbox_override_retries += 1
                target = self._last_written_wallbox
                log.warning(
                    "WALLBOX OVERRIDE RETRY #%d: toggling %dA → %dA → %dA "
                    "(ev_power=%.0fW, expected=%.0fW)",
                    self._wallbox_override_retries,
                    target, target + 1, target,
                    actual_w, expected_w,
                )
                # Send target+1, then target to force a state change
                self.ha.set_wallbox_current(target + 1)
                time.sleep(2)
                self.ha.set_wallbox_current(target)
                # Reset timer to wait another confirmation period
                self._wallbox_override_since = time.monotonic()
        else:
            # No override — reset detection only if EV is actually at expected level
            # Don't reset during IDLE (EV power may temporarily drop during handshake)
            if self._wallbox_override_since is not None and self.state != State.IDLE:
                if self._wallbox_override_retries > 0:
                    log.info(
                        "WALLBOX OVERRIDE RESOLVED after %d retries "
                        "(ev_power=%.0fW, expected=%.0fW)",
                        self._wallbox_override_retries, actual_w, expected_w,
                    )
                self._wallbox_override_since = None
                self._wallbox_override_retries = 0

    # -- overnight range tracking ---------------------------------------------

    _MIN_SOC_LFP = 20  # Minimum safe SOC for LFP battery (%)
    _SAFETY_MARGIN = 10  # Extra margin above overnight need (%)

    def _track_overnight_range(self, s: dict) -> None:
        """Track SOC at dusk, compute overnight need at sunrise."""
        solar_available = s["solar_power"] > config.SOLAR_AVAILABLE_W
        soc = s["battery_soc"]

        # Dusk: solar just dropped below threshold → record SOC
        if self._solar_was_available and not solar_available:
            self._soc_at_dusk = soc
            self._sunrise_computed = False
            log.info("DUSK: solar lost, recording SOC at dusk = %.0f%%", soc)

        # Sunrise: solar just appeared → compute overnight range
        if not self._solar_was_available and solar_available and not self._sunrise_computed:
            if self._soc_at_dusk is not None:
                range_needed = self._soc_at_dusk - soc
                range_needed = max(range_needed, 0)
                target = self._MIN_SOC_LFP + range_needed + self._SAFETY_MARGIN
                target = min(target, 100)
                log.info(
                    "SUNRISE: SOC dusk=%.0f%% now=%.0f%% → range_needed=%.0f%% "
                    "→ setting batt_charge_prio=%.0f%% discharge_limit=%.0f%%",
                    self._soc_at_dusk, soc, range_needed, target, target,
                )
                try:
                    self.ha.set_input_number("input_number.range_needed_over_night", range_needed)
                    self.ha.set_input_number("input_number.batt_charge_prio", target)
                    self.ha.set_input_number("input_number.discharge_limit", target)
                except Exception:
                    log.warning("Failed to set overnight range helpers", exc_info=True)
            self._sunrise_computed = True

        self._solar_was_available = solar_available

    # -- state evaluation -----------------------------------------------------

    def _determine_target_state(self, s: dict) -> State:
        """Determine the target state based on current sensor readings."""
        ev_charging = s["ev_power"] > config.EV_CHARGING_DETECT_W

        if not ev_charging:
            return State.IDLE

        mode = s.get("ems_mode", "SOLAR_ONLY").upper()

        if mode == "FULL_SPEED":
            return State.FULL_SPEED

        if mode == "STORAGE_BOOSTED":
            if s["battery_soc"] < s["discharge_limit"]:
                return State.SOLAR_ONLY if s["solar_power"] > config.SOLAR_AVAILABLE_W else State.EV_NO_SOLAR
            return State.STORAGE_BOOSTED

        if mode == "STORAGE_ONLY":
            if s["battery_soc"] < s["discharge_limit"]:
                return State.SOLAR_ONLY if s["solar_power"] > config.SOLAR_AVAILABLE_W else State.EV_NO_SOLAR
            return State.STORAGE_ONLY

        # SOLAR_ONLY / SOLAR_BOOSTED share routing
        solar_available = s["solar_power"] > config.SOLAR_AVAILABLE_W
        if not solar_available:
            return State.EV_NO_SOLAR

        soc = s["battery_soc"]
        prio = s["batt_charge_prio"]

        # Pick the right surplus state based on mode
        if mode == "SOLAR_BOOSTED":
            surplus_state = State.SOLAR_BOOSTED
        else:
            surplus_state = State.SOLAR_ONLY

        # Hysteresis: stay in surplus unless SOC drops significantly
        if self.state in (State.SOLAR_ONLY, State.SOLAR_BOOSTED):
            if soc < (prio - config.SOC_HYSTERESIS_PCT):
                return State.BATTERY_PRIORITY
            return surplus_state

        if soc >= prio:
            return surplus_state

        return State.BATTERY_PRIORITY

    def _transition(self, target: State, s: dict) -> None:
        """Perform the transition from current state to target state."""
        if target == self.state:
            return

        # Enter target state
        if target == State.IDLE:
            self._enter_idle()
        elif target == State.FULL_SPEED:
            self._enter_full_speed()
        elif target == State.EV_NO_SOLAR:
            self._enter_ev_no_solar()
        elif target == State.BATTERY_PRIORITY:
            self._enter_battery_priority(s.get("ems_mode", "SOLAR_ONLY").upper())
        elif target == State.SOLAR_ONLY:
            self._enter_solar_only()
        elif target == State.SOLAR_BOOSTED:
            self._enter_solar_boosted()
        elif target == State.STORAGE_BOOSTED:
            self._enter_storage_boosted()
        elif target == State.STORAGE_ONLY:
            self._enter_storage_only()

        # Update HA with current state
        try:
            self.ha.set_ems_state(self.state.value)
        except Exception:
            log.warning("Failed to update ems_state in HA", exc_info=True)

    # -- per-tick logic -------------------------------------------------------

    def tick(self, s: dict) -> None:
        """Called every fast-loop iteration with fresh sensor data *s*."""

        # Update battery voltage for global discharge cap
        self._battery_voltage = s.get("battery_voltage", self._battery_voltage)

        # 0. Detect car plug-in → reset mode to default
        car_connected = "connected" in s.get("wallbox_status", "").lower()
        if car_connected and not self._car_connected:
            log.info("Car plugged in — resetting ems_mode to %s", config.DEFAULT_EMS_MODE)
            try:
                self.ha.set_ems_mode(config.DEFAULT_EMS_MODE)
                s["ems_mode"] = config.DEFAULT_EMS_MODE
            except Exception:
                log.warning("Failed to reset ems_mode", exc_info=True)
        self._car_connected = car_connected

        # 1. Evaluate state machine
        target = self._determine_target_state(s)
        self._transition(target, s)

        # 2. Per-state continuous work
        if self.state == State.EV_NO_SOLAR:
            amps = self._compute_discharge_limit(s)
            self._set_max_discharging(amps)

        elif self.state == State.FULL_SPEED:
            if s["battery_soc"] >= s["discharge_limit"]:
                # Above discharge_limit: battery at max (3.6kW cap) for EV + house
                self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A)
            else:
                # Below discharge_limit: battery covers house only
                amps = self._compute_discharge_limit(s)
                self._set_max_discharging(amps)
            # Re-send wallbox 32A periodically (cloud may override)
            now = time.monotonic()
            if now - self._last_slow_tick >= config.SLOW_LOOP_INTERVAL_S:
                self._last_slow_tick = now
                self._set_wallbox(config.WALLBOX_MAX_CURRENT_A)

        elif self.state == State.SOLAR_ONLY:
            now = time.monotonic()
            if now - self._last_slow_tick >= config.SLOW_LOOP_INTERVAL_S:
                self._last_slow_tick = now
                amps = self._compute_wallbox_surplus(s, grid_target=0)
                self._set_wallbox(amps)
                log.info(
                    "SOLAR_ONLY steering: grid=%.0fW  batt=%.0fW  ev=%.0fW → wallbox=%dA",
                    s["grid_power"], s["battery_power"], s["ev_power"], amps,
                )

        elif self.state == State.SOLAR_BOOSTED:
            now = time.monotonic()
            if now - self._last_slow_tick >= config.SLOW_LOOP_INTERVAL_S:
                self._last_slow_tick = now
                off_peak = is_off_peak()
                grid_ratio = config.BOOSTED_GRID_RATIO_OFF_PEAK if off_peak else config.BOOSTED_GRID_RATIO_PEAK
                amps = self._compute_wallbox_surplus(
                    s, grid_target=s["ev_power"] * grid_ratio
                )
                self._set_wallbox(amps)
                log.info(
                    "BOOSTED steering: grid=%.0fW  batt=%.0fW  ev=%.0fW "
                    "grid_ratio=%.0f%% %s → wallbox=%dA",
                    s["grid_power"], s["battery_power"], s["ev_power"],
                    grid_ratio * 100, "OFF-PEAK" if off_peak else "PEAK", amps,
                )

        elif self.state == State.STORAGE_BOOSTED:
            soc = s["battery_soc"]
            # SOC floor with hysteresis (40% stop, 42% resume)
            if self._storage_low_soc:
                if soc >= config.STORAGE_TO_EV_SOC_FLOOR + 2:
                    self._storage_low_soc = False
                    log.info("STORAGE_BOOSTED: SOC recovered above floor, resuming")
                    self._set_wallbox(config.WALLBOX_MAX_CURRENT_A)
            elif soc < config.STORAGE_TO_EV_SOC_FLOOR:
                self._storage_low_soc = True
                log.info("STORAGE_BOOSTED: SOC below %.0f%%, stopping",
                         config.STORAGE_TO_EV_SOC_FLOOR)
                self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)
                self._set_max_discharging(0)
                self._ema_discharge = None

            if self._storage_low_soc:
                self._set_max_discharging(0)
            else:
                off_peak = is_off_peak()
                grid_ratio = config.BOOSTED_GRID_RATIO_OFF_PEAK if off_peak else config.BOOSTED_GRID_RATIO_PEAK
                amps = self._compute_storage_discharge(s, grid_ratio)
                self._set_max_discharging(amps)
                # Re-send wallbox 32A periodically (cloud may override)
                now = time.monotonic()
                if now - self._last_slow_tick >= config.SLOW_LOOP_INTERVAL_S:
                    self._last_slow_tick = now
                    self._set_wallbox(config.WALLBOX_MAX_CURRENT_A)

        elif self.state == State.STORAGE_ONLY:
            soc = s["battery_soc"]
            # SOC floor with hysteresis (40% stop, 42% resume)
            if self._storage_low_soc:
                if soc >= config.STORAGE_TO_EV_SOC_FLOOR + 2:
                    self._storage_low_soc = False
                    log.info("STORAGE_ONLY: SOC recovered, resuming")
                    self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A)
                    self._ema_discharge = None
            elif soc < config.STORAGE_TO_EV_SOC_FLOOR:
                self._storage_low_soc = True
                log.info("STORAGE_ONLY: SOC below %.0f%%, house only",
                         config.STORAGE_TO_EV_SOC_FLOOR)
                self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)
                self._ema_discharge = None

            if self._storage_low_soc:
                # Battery covers house only
                amps = self._compute_discharge_limit(s)
                self._set_max_discharging(amps)
            else:
                # Direct calculation: wallbox = (max_discharge + solar - house) / voltage
                # The Deye inverter keeps grid≈0 on its own, so the incremental
                # algorithm cannot work (it always sees grid≈0 regardless of wallbox).
                self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A)
                now = time.monotonic()
                if now - self._last_slow_tick >= config.SLOW_LOOP_INTERVAL_S:
                    self._last_slow_tick = now
                    house_load = (
                        s["solar_power"] + s["battery_power"]
                        + s["grid_power"] - s["ev_power"]
                    )
                    available = (
                        config.MAX_DISCHARGE_POWER_W
                        + s["solar_power"]
                        - max(house_load, 0)
                    )
                    amps = int(available / max(s["grid_voltage"], 1.0))
                    amps = int(clamp(amps, config.WALLBOX_MIN_CURRENT_A, config.WALLBOX_MAX_CURRENT_A))
                    self._set_wallbox(amps)
                    log.info(
                        "STORAGE_ONLY steering: grid=%.0fW batt=%.0fW ev=%.0fW "
                        "solar=%.0fW house=%.0fW avail=%.0fW → wallbox=%dA",
                        s["grid_power"], s["battery_power"], s["ev_power"],
                        s["solar_power"], house_load, available, amps,
                    )

        # 3. Update grid ratio indicator
        if s["ev_power"] > config.EV_CHARGING_DETECT_W:
            ratio_pct = int(clamp(round(s["grid_power"] / s["ev_power"] * 100), 0, 100))
        else:
            ratio_pct = 0
        self._set_grid_ratio(ratio_pct)

        # 4. Enforce batt_charge_limit across all states
        if self.state in (State.SOLAR_ONLY, State.SOLAR_BOOSTED):
            self._set_max_charging(config.SURPLUS_MAX_CHARGING_A)
        elif s["battery_soc"] >= s["batt_charge_limit"]:
            self._set_max_charging(0)
        else:
            self._set_max_charging(config.DEFAULT_MAX_CHARGING_CURRENT_A)

        # 5. Wallbox override detection: wallbox ignoring our setpoint
        self._check_wallbox_override(s)

        # 6. Dusk/sunrise tracking for overnight range
        self._track_overnight_range(s)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("EMS starting — connecting to %s", config.HA_URL)

    ha = HomeAssistantAPI(config.HA_URL, config.HA_TOKEN)
    ems = EMS(ha)

    while True:
        try:
            sensors = ha.read_all_sensors()
            log.debug(
                "sensors: ev=%.0fW solar=%.0fW soc=%.0f%% batt_pwr=%.0fW "
                "grid=%.0fW batt_v=%.1fV grid_v=%.1fV limit=%.0f%% prio=%.0f%% mode=%s | state=%s",
                sensors["ev_power"], sensors["solar_power"],
                sensors["battery_soc"], sensors["battery_power"],
                sensors["grid_power"], sensors["battery_voltage"],
                sensors["grid_voltage"], sensors["batt_charge_limit"],
                sensors["batt_charge_prio"], sensors["ems_mode"],
                ems.state.value,
            )
            ems.tick(sensors)

        except KeyboardInterrupt:
            log.info("Shutting down (KeyboardInterrupt)")
            sys.exit(0)
        except Exception:
            log.exception("Error in main loop — will retry next tick")

        time.sleep(config.FAST_LOOP_INTERVAL_S)


if __name__ == "__main__":
    main()
