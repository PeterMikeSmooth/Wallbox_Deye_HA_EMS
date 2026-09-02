"""EMS — Energy Management System for Deye battery + Wallbox Pulsar Max.

State machine with states:
  IDLE, FULL_SPEED, EV_NO_SOLAR, BATTERY_PRIORITY,
  SOLAR_ONLY, SOLAR_BOOSTED, STORAGE_BOOSTED, STORAGE_ONLY, MANUAL.

Modes (input_select.ems_mode):
  SOLAR_ONLY, SOLAR_BOOSTED, FULL_SPEED, STORAGE_BOOSTED, STORAGE_ONLY, MANUAL.

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
# Discharge setpoint write policy
#
# Deliberately module constants and not config.py entries: the Pi's config.py
# is gitignored and never updated by `git pull`, so a constant added here but
# missing there would raise AttributeError inside the main loop's catch-all —
# silently, tick after tick.
# ---------------------------------------------------------------------------

DISCHARGE_WRITE_DEADBAND_A = 2       # ignore corrections smaller than this
DISCHARGE_WRITE_MIN_INTERVAL_S = 10  # min delay between small corrections
DISCHARGE_LARGE_STEP_A = 5           # a step this big is written immediately
DISCHARGE_RECONCILE_GRACE_S = 240    # let the inverter reflect a write before judging
DISCHARGE_RECONCILE_SAMPLES = 2      # consecutive divergent reads before re-writing


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
    MANUAL = "MANUAL"                    # Mode: user sets wallbox current; EMS only manages battery


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
        self._last_discharge_write_ts = 0.0  # monotonic ts of that write
        self._discharge_divergence_n = 0     # consecutive divergent read-backs
        self._discharge_alerted = False      # HA notification already raised
        self._last_written_wallbox = None
        self._last_written_charging = None
        self._last_slow_tick = 0.0          # timestamp of last slow-loop action
        self._storage_low_soc = False       # STORAGE_TO_EV: SOC below floor
        self._car_connected = None          # wallbox: car plugged in (None = unknown yet)
        self._last_wallbox_status = None    # last logged wallbox status string
        self._last_written_grid_ratio = None
        self._battery_voltage = 52.0        # last known battery voltage
        # Wallbox override detection
        self._wallbox_override_since = None  # timestamp when override first detected
        self._wallbox_override_retries = 0   # number of toggle retries attempted
        # Overnight range tracking — two-phase state machine (see _track_overnight_range)
        self._overnight_phase = "WAIT_FOR_NIGHT"  # WAIT_FOR_NIGHT | WAIT_FOR_DAYLIGHT
        self._soc_overnight_start = None          # SOC captured at dusk (last sub-threshold crossing)
        self._solar_was_available = True          # previous tick solar state (for edge detection)
        # Force safe wallbox default on startup
        self.ha.set_wallbox_current(config.WALLBOX_MIN_CURRENT_A)
        self._last_written_wallbox = config.WALLBOX_MIN_CURRENT_A
        log.info("SET wallbox_current = %d A (startup)", config.WALLBOX_MIN_CURRENT_A)

    # -- entry actions --------------------------------------------------------

    def _enter_idle(self) -> None:
        log.info("→ IDLE")
        self.state = State.IDLE
        self._ema_discharge = None
        self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A, force=True)
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)

    def _enter_full_speed(self) -> None:
        log.info("→ FULL_SPEED")
        self.state = State.FULL_SPEED
        self._ema_discharge = None
        self._set_wallbox(config.WALLBOX_MAX_CURRENT_A)

    def _enter_ev_no_solar(self) -> None:
        log.info("→ EV_NO_SOLAR")
        self.state = State.EV_NO_SOLAR
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)

    def _enter_battery_priority(self) -> None:
        log.info("→ BATTERY_PRIORITY")
        self.state = State.BATTERY_PRIORITY
        self._ema_discharge = None
        # Discharge policy (0A for SOLAR_BOOSTED, free otherwise) is applied
        # every tick in the main loop, not just here — see tick().
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)

    def _enter_solar_only(self) -> None:
        log.info("→ SOLAR_ONLY")
        self.state = State.SOLAR_ONLY
        self._ema_discharge = None
        self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A, force=True)
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)
        self._last_slow_tick = 0.0  # force immediate first wallbox adjustment

    def _enter_solar_boosted(self) -> None:
        log.info("→ SOLAR_BOOSTED")
        self.state = State.SOLAR_BOOSTED
        self._ema_discharge = None
        self._set_max_discharging(0, force=True)
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
        self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A, force=True)
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)
        self._last_slow_tick = 0.0  # force immediate first wallbox adjustment

    def _enter_manual(self) -> None:
        # User controls wallbox current from the app; EMS starts it at 6A then
        # hands off (never touches wallbox current again while in MANUAL).
        log.info("→ MANUAL")
        self.state = State.MANUAL
        self._ema_discharge = None
        self._storage_low_soc = False
        self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A, force=True)
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)

    # -- write-with-dedup helpers ---------------------------------------------

    def _set_max_discharging(self, amps: int, force: bool = False) -> None:
        """Write the Deye max discharge current, throttled.

        *force* bypasses the deadband and rate limit: use it for policy values
        (state entry, 0 A, free discharge) which must land immediately.  The
        throttle exists because hammering this Modbus register roughly once per
        second is what got a write silently dropped on 2026-08-27, leaving the
        battery clamped at 5 A all night.
        """
        # Global cap: apply MAX_DISCHARGE_POWER_W only when EV is charging
        # (long sustained discharge → inverter heating). Transient house loads are fine.
        if self.state != State.IDLE:
            max_from_power = int(config.MAX_DISCHARGE_POWER_W / max(self._battery_voltage, 1.0))
            amps = min(amps, max_from_power)

        if self._last_written_discharge == amps:
            return

        if not force and self._last_written_discharge is not None:
            delta = abs(amps - self._last_written_discharge)
            if delta < DISCHARGE_LARGE_STEP_A:
                if delta < DISCHARGE_WRITE_DEADBAND_A:
                    return
                if time.monotonic() - self._last_discharge_write_ts < DISCHARGE_WRITE_MIN_INTERVAL_S:
                    return

        self._write_max_discharging(amps)

    def _write_max_discharging(self, amps: int) -> None:
        """Unconditional write + bookkeeping (also used by reconciliation)."""
        self.ha.set_max_discharging_current(amps)
        log.info("SET max_discharging_current = %d A", amps)
        self._last_written_discharge = amps
        self._last_discharge_write_ts = time.monotonic()
        self._discharge_divergence_n = 0

    def _reconcile_discharge(self, s: dict) -> None:
        """Re-assert the discharge setpoint if the inverter dropped it.

        HA acknowledges our write optimistically; the Deye integration only
        reveals the truth at its next poll of the register.  Without this the
        EMS stays convinced its setpoint is applied and never retries.
        """
        actual = s.get("max_discharging_actual")
        want = self._last_written_discharge
        if actual is None or want is None:
            return
        # Give the inverter/integration time to reflect the last write.
        if time.monotonic() - self._last_discharge_write_ts < DISCHARGE_RECONCILE_GRACE_S:
            return

        if int(round(actual)) == want:
            self._discharge_divergence_n = 0
            self._discharge_alerted = False
            return

        self._discharge_divergence_n += 1
        if self._discharge_divergence_n < DISCHARGE_RECONCILE_SAMPLES:
            return

        log.warning(
            "Discharge setpoint diverged: inverter=%.0f A, expected %d A "
            "(state=%s) — re-writing", actual, want, self.state.value,
        )
        if not self._discharge_alerted:
            self._discharge_alerted = True
            try:
                self.ha.notify(
                    "EMS: consigne de décharge non appliquée",
                    f"L'onduleur est à {actual:.0f} A alors que l'EMS demande "
                    f"{want} A (état {self.state.value}). Réécriture automatique.",
                    "ems_discharge_divergence",
                )
            except Exception:
                log.warning("Failed to raise HA notification", exc_info=True)

        self._write_max_discharging(want)

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
    _PREDAWN_HOUR = 1  # local hour: lock dusk SOC and start watching for daylight

    def _track_overnight_range(self, s: dict) -> None:
        """Compute overnight battery drain with a two-phase state machine.

        WAIT_FOR_NIGHT:
            Capture the SOC only on the falling edge (solar crossing below the
            threshold) — i.e. at dusk, not continuously through the night, so the
            value reflects the SOC at sunset rather than the depleted pre-dawn SOC.
            Dusk oscillation re-captures values near sunset (harmless); daytime
            cloud dips get overwritten by later edges, so the last edge before
            01:00 is the real sunset. At 01:00, switch to WAIT_FOR_DAYLIGHT.

        WAIT_FOR_DAYLIGHT:
            The first time solar exceeds the threshold (real sunrise), compute
            range_needed = soc_dusk - soc_now, update the HA helpers, and switch
            back to WAIT_FOR_NIGHT.

        Robust against solar dropping out and returning for >30 min: daylight is
        only ever evaluated after 01:00, and the first crossing ends the phase,
        so dawn oscillation cannot reset range_needed_over_night.
        """
        solar_available = s["solar_power"] > config.SOLAR_AVAILABLE_W
        soc = s["battery_soc"]
        hour = datetime.now().hour

        if self._overnight_phase == "WAIT_FOR_NIGHT":
            # Falling edge: solar just dropped below threshold → record dusk SOC.
            if self._solar_was_available and not solar_available:
                self._soc_overnight_start = soc
                log.info("DUSK: solar dropped below threshold, SOC at dusk = %.0f%%", soc)
            # At 01:00, lock in the dusk SOC and wait for sunrise.
            if hour == self._PREDAWN_HOUR and self._soc_overnight_start is not None:
                self._overnight_phase = "WAIT_FOR_DAYLIGHT"
                log.info(
                    "OVERNIGHT: pre-dawn reached — SOC at dusk = %.0f%%, "
                    "waiting for daylight", self._soc_overnight_start,
                )

        elif self._overnight_phase == "WAIT_FOR_DAYLIGHT":
            if solar_available:
                range_needed = max(self._soc_overnight_start - soc, 0)
                target = min(self._MIN_SOC_LFP + range_needed + self._SAFETY_MARGIN, 100)
                log.info(
                    "DAYLIGHT: SOC dusk=%.0f%% now=%.0f%% → range_needed=%.0f%% "
                    "→ setting batt_charge_prio=%.0f%% discharge_limit=%.0f%%",
                    self._soc_overnight_start, soc, range_needed, target, target,
                )
                try:
                    self.ha.set_input_number("input_number.range_needed_over_night", range_needed)
                    self.ha.set_input_number("input_number.batt_charge_prio", target)
                    self.ha.set_input_number("input_number.discharge_limit", target)
                except Exception:
                    log.warning("Failed to set overnight range helpers", exc_info=True)
                self._overnight_phase = "WAIT_FOR_NIGHT"

        self._solar_was_available = solar_available

    # -- wallbox status -------------------------------------------------------

    @staticmethod
    def _car_plugged(status: str) -> bool | None:
        """Is a car plugged in?  ``None`` when the status is not conclusive.

        Keyword sets derived from the statuses this wallbox actually reports
        (13 days of history): Charging, Locked/Locked-car-connected, Waiting,
        Waiting for car demand, Ready, Disconnected, unavailable.  "Waiting" is
        always a plugged context — it appears mid-session between two
        "Charging" samples, which is exactly what used to fake a plug-in.

        Plugged is checked first so "Locked, car connected" wins over the bare
        "Locked".  A bare "Locked" is deliberately inconclusive: history shows
        it mid-session ("Waiting for car demand" -> "Locked" -> "Locked, car
        connected"), so it describes the charger's lock, not the plug.

        Anything unrecognised returns None: the caller then keeps its previous
        value rather than guessing, because a wrong True wipes the user's mode
        mid-charge.
        """
        st = status.strip().lower()
        if any(k in st for k in ("charging", "car connected", "connected:",
                                 "waiting", "discharging", "paused", "queue")):
            return True
        if any(k in st for k in ("disconnected", "ready", "no car")):
            return False
        return None

    # -- state evaluation -----------------------------------------------------

    def _determine_target_state(self, s: dict) -> State:
        """Determine the target state based on current sensor readings."""
        ev_charging = s["ev_power"] > config.EV_CHARGING_DETECT_W

        if not ev_charging:
            return State.IDLE

        mode = s.get("ems_mode", "SOLAR_ONLY").upper()

        if mode == "FULL_SPEED":
            return State.FULL_SPEED

        if mode == "MANUAL":
            return State.MANUAL

        if mode == "STORAGE_BOOSTED":
            if s["battery_soc"] <= s["discharge_limit"]:
                self.ha.set_ems_mode("SOLAR_ONLY")
                s["ems_mode"] = "SOLAR_ONLY"
                log.info("SOC <= discharge_limit — forcing ems_mode to SOLAR_ONLY")
                return State.SOLAR_ONLY if s["solar_power"] > config.SOLAR_AVAILABLE_W else State.EV_NO_SOLAR
            return State.STORAGE_BOOSTED

        if mode == "STORAGE_ONLY":
            if s["battery_soc"] <= s["discharge_limit"]:
                self.ha.set_ems_mode("SOLAR_ONLY")
                s["ems_mode"] = "SOLAR_ONLY"
                log.info("SOC <= discharge_limit — forcing ems_mode to SOLAR_ONLY")
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

        # Leaving MANUAL: reset wallbox to 6A so it never stays stuck on the
        # user's manual setpoint (the target state's entry may override this).
        if self.state == State.MANUAL and target != State.MANUAL:
            self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)

        # Enter target state
        if target == State.IDLE:
            self._enter_idle()
        elif target == State.FULL_SPEED:
            self._enter_full_speed()
        elif target == State.EV_NO_SOLAR:
            self._enter_ev_no_solar()
        elif target == State.BATTERY_PRIORITY:
            self._enter_battery_priority()
        elif target == State.SOLAR_ONLY:
            self._enter_solar_only()
        elif target == State.SOLAR_BOOSTED:
            self._enter_solar_boosted()
        elif target == State.STORAGE_BOOSTED:
            self._enter_storage_boosted()
        elif target == State.STORAGE_ONLY:
            self._enter_storage_only()
        elif target == State.MANUAL:
            self._enter_manual()

    # -- per-tick logic -------------------------------------------------------

    def tick(self, s: dict) -> None:
        """Called every fast-loop iteration with fresh sensor data *s*."""

        # Update battery voltage for global discharge cap
        self._battery_voltage = s.get("battery_voltage", self._battery_voltage)

        # 0. Detect car plug-in → reset mode to default
        status = s.get("wallbox_status", "")
        if status != self._last_wallbox_status:
            log.info("Wallbox status: %r → %r", self._last_wallbox_status, status)
            self._last_wallbox_status = status

        plugged = self._car_plugged(status)
        if plugged is not None:
            # Rising edge only from a *known* unplugged state.  The wallbox
            # status flips between "Charging" and "Connected: waiting for car
            # demand" during a session (and can go unavailable); those must not
            # look like a fresh plug-in and wipe the user's mode mid-charge.
            # The ev_power guard is the belt-and-braces version of the same
            # rule: whatever the status says, a car that is drawing power is
            # not a car that was just plugged in.
            charging = s["ev_power"] > config.EV_CHARGING_DETECT_W
            if plugged and self._car_connected is False and not charging:
                log.info("Car plugged in — resetting ems_mode to %s", config.DEFAULT_EMS_MODE)
                try:
                    self.ha.set_ems_mode(config.DEFAULT_EMS_MODE)
                    s["ems_mode"] = config.DEFAULT_EMS_MODE
                except Exception:
                    log.warning("Failed to reset ems_mode", exc_info=True)
            self._car_connected = plugged

        # 1. Evaluate state machine
        target = self._determine_target_state(s)
        self._transition(target, s)

        # 2. Per-state continuous work
        if self.state == State.EV_NO_SOLAR:
            amps = self._compute_discharge_limit(s)
            self._set_max_discharging(amps)

        elif self.state == State.BATTERY_PRIORITY:
            # Re-evaluate every tick (not just on entry) so a mode change made
            # while already parked in BATTERY_PRIORITY (e.g. SOLAR_BOOSTED ->
            # SOLAR_ONLY without a state transition) takes effect immediately.
            if s.get("ems_mode", "SOLAR_ONLY").upper() == "SOLAR_BOOSTED":
                self._set_max_discharging(0, force=True)
            else:
                self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A, force=True)

        elif self.state == State.FULL_SPEED:
            if s["battery_soc"] > s["discharge_limit"]:
                # Above discharge_limit: battery at max (4.6kW cap) for EV + house
                self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A, force=True)
            elif s["solar_power"] > config.SOLAR_AVAILABLE_W:
                # At or below discharge_limit but solar available: don't discharge —
                # the solar production covers the house instead of the battery.
                self._set_max_discharging(0, force=True)
                self._ema_discharge = None
            else:
                # At or below discharge_limit and no solar: battery covers house only
                amps = self._compute_discharge_limit(s)
                self._set_max_discharging(amps)
            # Wallbox is set to 32A on entry; user may adjust manually and it will stick.

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
            soc_floor = s["discharge_limit"]
            # SOC floor with hysteresis (floor stop, floor+2 resume)
            if self._storage_low_soc:
                if soc >= soc_floor + 2:
                    self._storage_low_soc = False
                    log.info("STORAGE_BOOSTED: SOC recovered above floor, resuming")
            elif soc < soc_floor:
                self._storage_low_soc = True
                log.info("STORAGE_BOOSTED: SOC below %.0f%%, stopping",
                         soc_floor)
                self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)
                self._set_max_discharging(0, force=True)
                self._ema_discharge = None

            if self._storage_low_soc:
                self._set_max_discharging(0, force=True)
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
            soc_floor = s["discharge_limit"]
            # SOC floor with hysteresis (floor stop, floor+2 resume)
            if self._storage_low_soc:
                if soc >= soc_floor + 2:
                    self._storage_low_soc = False
                    log.info("STORAGE_ONLY: SOC recovered, resuming")
                    self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A, force=True)
                    self._ema_discharge = None
            elif soc < soc_floor:
                self._storage_low_soc = True
                log.info("STORAGE_ONLY: SOC below %.0f%%, house only",
                         soc_floor)
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
                self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A, force=True)
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

        elif self.state == State.MANUAL:
            # Wallbox current is user-controlled — never touched here.
            # Battery: discharge normally to support the EV while SOC is above
            # discharge_limit; below it, hands off the Deye discharge current too
            # so the user can set it manually via the Deye control in HA.
            soc = s["battery_soc"]
            soc_floor = s["discharge_limit"]
            # SOC floor with hysteresis (floor stop, floor+2 resume)
            if self._storage_low_soc:
                if soc >= soc_floor + 2:
                    self._storage_low_soc = False
                    log.info("MANUAL: SOC recovered above floor, resuming battery support")
                    self._ema_discharge = None
            elif soc < soc_floor:
                self._storage_low_soc = True
                log.info("MANUAL: SOC below %.0f%%, discharge under manual Deye control",
                         soc_floor)
                self._ema_discharge = None
                # Forget last written value so a future resume always re-writes
                self._last_written_discharge = None

            if not self._storage_low_soc:
                self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A, force=True)

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

        # 5. Wallbox override detection: wallbox ignoring our setpoint.
        # Skip in MANUAL — the user deliberately sets the current from the app,
        # so a higher ev_power is expected, not a cloud override to fight.
        if self.state != State.MANUAL:
            self._check_wallbox_override(s)

        # 6. Dusk/sunrise tracking for overnight range
        self._track_overnight_range(s)

        # 7. Verify the inverter actually kept our discharge setpoint
        self._reconcile_discharge(s)


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
                "grid=%.0fW batt_v=%.1fV grid_v=%.1fV limit=%.0f%% prio=%.0f%% mode=%s "
                "wallbox=%r | state=%s",
                sensors["ev_power"], sensors["solar_power"],
                sensors["battery_soc"], sensors["battery_power"],
                sensors["grid_power"], sensors["battery_voltage"],
                sensors["grid_voltage"], sensors["batt_charge_limit"],
                sensors["batt_charge_prio"], sensors["ems_mode"],
                sensors["wallbox_status"], ems.state.value,
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
