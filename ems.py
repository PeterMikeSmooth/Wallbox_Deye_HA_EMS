"""EMS — Energy Management System for Deye battery + Wallbox Pulsar Max.

State machine with 4 states: IDLE, EV_NO_SOLAR, EV_BATTERY_PRIORITY, EV_SURPLUS.
See README.md for full specification.
"""

import enum
import logging
import os
import time
import sys

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
    IDLE = "IDLE"
    FULL_SPEED = "FULL_SPEED"
    EV_NO_SOLAR = "EV_NO_SOLAR"
    BATTERY_PRIORITY = "BATTERY_PRIORITY"
    SOLAR_SURPLUS = "SOLAR_SURPLUS"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


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

    # -- entry actions --------------------------------------------------------

    def _enter_idle(self) -> None:
        log.info("→ IDLE")
        self.state = State.IDLE
        self._ema_discharge = None
        self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A)
        # max_charging managed by batt_charge_limit enforcement in tick()

    def _enter_full_speed(self) -> None:
        log.info("→ FULL_SPEED")
        self.state = State.FULL_SPEED
        self._ema_discharge = None
        self._set_wallbox(config.WALLBOX_MAX_CURRENT_A)
        self._last_slow_tick = time.monotonic()
        # max_discharging managed in tick() based on solar availability

    def _enter_ev_no_solar(self) -> None:
        log.info("→ EV_NO_SOLAR")
        self.state = State.EV_NO_SOLAR
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)
        # discharge driven by fast loop in tick()

    def _enter_battery_priority(self) -> None:
        log.info("→ BATTERY_PRIORITY")
        self.state = State.BATTERY_PRIORITY
        self._ema_discharge = None
        self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A)
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)
        # max_charging managed by batt_charge_limit enforcement in tick()

    def _enter_solar_surplus(self) -> None:
        log.info("→ SOLAR_SURPLUS")
        self.state = State.SOLAR_SURPLUS
        self._ema_discharge = None
        self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A)
        self._last_slow_tick = 0.0  # force immediate first wallbox adjustment
        # max_charging = SURPLUS_MAX_CHARGING_A, managed in tick()

    # -- write-with-dedup helpers ---------------------------------------------

    def _set_max_discharging(self, amps: int) -> None:
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

    def _compute_wallbox_surplus(self, s: dict) -> int:
        """Surplus steering algorithm (EV_SURPLUS only).

        Incremental: adjusts from current setpoint based on observed
        grid + battery error.  Works regardless of wallbox charging mode
        (minimal, normal, etc.) since we never assume P = I × V for the
        wallbox — we just observe the actual power balance.
        """
        # Positive excess = surplus being wasted (export or unwanted battery charge)
        excess = -(s["grid_power"] + s["battery_power"])
        voltage = max(s["grid_voltage"], 1.0)
        delta = round(excess / voltage)
        current = self._last_written_wallbox or config.WALLBOX_MIN_CURRENT_A
        target = current + delta
        return int(clamp(target, config.WALLBOX_MIN_CURRENT_A, config.WALLBOX_MAX_CURRENT_A))

    # -- state evaluation -----------------------------------------------------

    def _determine_target_state(self, s: dict) -> State:
        """Determine the target state based on current sensor readings."""
        ev_charging = s["ev_power"] > config.EV_CHARGING_DETECT_W

        if not ev_charging:
            return State.IDLE

        mode = s.get("ems_mode", "SOLAR_ONLY").upper()
        if mode == "FULL_SPEED":
            return State.FULL_SPEED

        # SOLAR_ONLY mode
        solar_available = s["solar_power"] > config.SOLAR_AVAILABLE_W
        if not solar_available:
            return State.EV_NO_SOLAR

        soc = s["battery_soc"]
        prio = s["batt_charge_prio"]

        # Hysteresis: stay in SOLAR_SURPLUS unless SOC drops significantly
        if self.state == State.SOLAR_SURPLUS:
            if soc < (prio - config.SOC_HYSTERESIS_PCT):
                return State.BATTERY_PRIORITY
            return State.SOLAR_SURPLUS

        if soc >= prio:
            return State.SOLAR_SURPLUS

        return State.BATTERY_PRIORITY

    def _transition(self, target: State) -> None:
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
            self._enter_battery_priority()
        elif target == State.SOLAR_SURPLUS:
            self._enter_solar_surplus()

        # Update HA with current state
        try:
            self.ha.set_ems_state(self.state.value)
        except Exception:
            log.warning("Failed to update ems_state in HA", exc_info=True)

    # -- per-tick logic -------------------------------------------------------

    def tick(self, s: dict) -> None:
        """Called every fast-loop iteration with fresh sensor data *s*."""

        # 1. Evaluate state machine
        target = self._determine_target_state(s)
        self._transition(target)

        # 2. Per-state continuous work
        if self.state == State.EV_NO_SOLAR:
            amps = self._compute_discharge_limit(s)
            self._set_max_discharging(amps)

        elif self.state == State.FULL_SPEED:
            if s["solar_power"] > config.SOLAR_AVAILABLE_W:
                self._set_max_discharging(0)
                self._ema_discharge = None
            else:
                amps = self._compute_discharge_limit(s)
                self._set_max_discharging(amps)
            # Re-send wallbox 32A periodically (cloud may override)
            now = time.monotonic()
            if now - self._last_slow_tick >= config.SLOW_LOOP_INTERVAL_S:
                self._last_slow_tick = now
                self._set_wallbox(config.WALLBOX_MAX_CURRENT_A)

        elif self.state == State.SOLAR_SURPLUS:
            now = time.monotonic()
            if now - self._last_slow_tick >= config.SLOW_LOOP_INTERVAL_S:
                self._last_slow_tick = now
                amps = self._compute_wallbox_surplus(s)
                self._set_wallbox(amps)
                log.info(
                    "SURPLUS steering: grid=%.0fW  batt=%.0fW  ev=%.0fW → wallbox=%dA",
                    s["grid_power"], s["battery_power"], s["ev_power"], amps,
                )

        # 3. Enforce batt_charge_limit across all states
        if self.state == State.SOLAR_SURPLUS:
            self._set_max_charging(config.SURPLUS_MAX_CHARGING_A)
        elif s["battery_soc"] >= s["batt_charge_limit"]:
            self._set_max_charging(0)
        else:
            self._set_max_charging(config.DEFAULT_MAX_CHARGING_CURRENT_A)


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
