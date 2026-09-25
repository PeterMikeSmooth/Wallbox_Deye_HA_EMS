"""EMS — Energy Management System for Deye battery + Wallbox Pulsar Max.

State machine with states:
  IDLE, FULL_SPEED, EV_NO_SOLAR, BATTERY_PRIORITY,
  SOLAR_ONLY, SOLAR_BOOSTED, STORAGE_BOOSTED, STORAGE_ONLY, MANUAL.

Modes (input_select.ems_mode):
  SOLAR_ONLY, SOLAR_BOOSTED, FULL_SPEED, STORAGE_BOOSTED, STORAGE_ONLY, MANUAL.

See README.md for full specification.
"""

import enum
import json
import logging
import os
import time
import sys
import threading
from datetime import date, datetime

import config
from ha_api import HomeAssistantAPI
from wallbox_current import WallboxCurrent


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
# SOLAR_ONLY Tesla pause (see README "Tesla solar pause")
#
# Module constants for the same reason as the discharge ones above: the Pi's
# config.py is gitignored and never refreshed by `git pull`.
# ---------------------------------------------------------------------------

TESLA_PAUSE_CONFIRM_S = 300     # low sun must hold this long before cutting
TESLA_CUT_BELOW_W = 1000        # sun available to the car under this -> cut
TESLA_RESUME_ABOVE_W = 1200     # ...and back above this -> restart (hysteresis band)
TESLA_SETTLE_AFTER_CUT_S = 120  # let the meters settle before judging a restart
TESLA_PAUSE_LOG_INTERVAL_S = 300  # heartbeat log while the charge is paused
TESLA_STATE_FILE = os.path.join(
    os.path.dirname(config.LOG_FILE) or ".", "ems_state.json"
)


# ---------------------------------------------------------------------------
# Deferred batt_charge_prio (see README "Overnight Range Tracking")
#
# Octopus refunds EV kWh at the current tariff, so solar sent to the car before
# the midday off-peak window is worth more than solar stored in the battery.
# At sunrise batt_charge_prio is only lowered to MORNING_BATT_CHARGE_PRIO; the
# computed target is applied when the midday off-peak window opens.
# Module constants, not config.py: same reason as the Tesla pause above.
# ---------------------------------------------------------------------------

MORNING_BATT_CHARGE_PRIO = 25       # % — batt_charge_prio from sunrise to release
BATT_PRIO_RELEASE_AT = (12, 24)     # local (hour, minute): start of midday off-peak


# ---------------------------------------------------------------------------
# Which car is on the cable (written to input_text.ev_connected)
#
# Identification runs on the DISCONNECTED -> CONNECTED edge, off the 1 Hz loop:
# kia_uvo.force_update is synchronous and took 29 s when measured on
# 2026-09-20, so it must never block a tick.
# ---------------------------------------------------------------------------

EV_TESLA = "Tesla connected"
EV_IONIQ = "Ioniq connected"
EV_OTHER = "other connected"
EV_NONE = "disconnected"
EV_UNKNOWN = "unknown"           # the Ioniq could not be reached — not "not the Ioniq"

IONIQ_REFRESH_TIMEOUT_S = 60     # wait for the payload after force_update returns
IONIQ_REFRESH_POLL_S = 3         # how often to look while waiting
IONIQ_MAX_DATA_AGE_S = 120       # a payload older than this is not trusted
IONIQ_FORCE_ATTEMPTS = 3         # DuplicateRequestError is transient — retry
IONIQ_RETRY_DELAY_S = 30         # let the in-flight request finish first
IONIQ_LATE_WATCH_S = 4200        # 70 min: long enough to catch the hourly
IONIQ_LATE_POLL_S = 60           # HA automation that refreshes the car anyway


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


def ha_text(s: dict, key: str) -> str | None:
    """Normalized text reading, or ``None`` when the entity says nothing.

    HA reports a missing/offline entity as the strings ``unknown`` /
    ``unavailable``; callers must treat those exactly like a failed read, not
    like a real value (see the Tesla entities, which come from a cloud
    integration).
    """
    value = s.get(key)
    if value is None:
        return None
    value = value.strip().lower()
    return None if value in ("", "unknown", "unavailable", "none") else value


def sun_to_ev(s: dict) -> float:
    """Solar power actually available for the car, in W.

    ``ev_power - battery_power - grid_power`` is algebraically the same as
    ``solar_power - house_load``, and — unlike a raw surplus — it reads the same
    whether the car is charging or stopped.  That is what makes a hysteresis
    band possible at all: cutting a 1.2 kW charge instantly turns its
    consumption into 1.2 kW of *surplus*, so a threshold expressed on the
    surplus is crossed by the cut itself (observed in production on 2026-09-11
    at 17:40: charge cut, then restarted 20 s later).
    """
    return s["ev_power"] - s["battery_power"] - s["grid_power"]


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
        self.wallbox = WallboxCurrent(ha)   # BLE gateway first, cloud fallback
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
        self._ev_id_generation = 0          # bumped on every plug/unplug edge
        self._last_wallbox_status = None    # last logged wallbox status string (cloud)
        self._last_gw_status = None         # last logged gateway charger_status
        self._plug_seen = None              # plug change read once, awaiting confirmation
        self._last_written_grid_ratio = None
        self._battery_voltage = 52.0        # last known battery voltage
        # Wallbox override detection
        self._wallbox_override_since = None  # timestamp when override first detected
        self._wallbox_override_retries = 0   # number of toggle retries attempted
        # Overnight range tracking — two-phase state machine (see _track_overnight_range)
        self._overnight_phase = "WAIT_FOR_NIGHT"  # WAIT_FOR_NIGHT | WAIT_FOR_DAYLIGHT
        self._soc_overnight_start = None          # SOC captured at dusk (last sub-threshold crossing)
        self._solar_was_available = True          # previous tick solar state (for edge detection)
        # SOLAR_ONLY Tesla pause — the "paused" flag is persisted so a restart
        # of ems.service never leaves the car switched off for the whole day.
        self._tesla_low_sun_since = None    # monotonic ts of the first low-sun tick
        self._tesla_cut_ts = 0.0            # monotonic ts of the last cut (settling)
        self._tesla_pause_log_ts = 0.0      # last heartbeat log while paused
        persisted = self._load_state_file()
        self._tesla_paused = bool(persisted.get("tesla_paused", False))
        self._tesla_paused_at = persisted.get("tesla_paused_at")
        if self._tesla_paused:
            log.info(
                "TESLA PAUSE: restored from %s — charge was cut by the EMS at %s",
                TESLA_STATE_FILE, self._tesla_paused_at,
            )
        # Deferred batt_charge_prio — persisted so a restart before the release
        # time does not leave the prio at the morning value for the whole day.
        self._pending_batt_prio = persisted.get("pending_batt_prio")
        self._pending_batt_prio_date = persisted.get("pending_batt_prio_date")
        if self._pending_batt_prio is not None:
            log.info(
                "BATT PRIO: restored from %s — target %.0f%% pending since %s",
                TESLA_STATE_FILE, self._pending_batt_prio,
                self._pending_batt_prio_date,
            )
        # Force safe wallbox default on startup
        path = self.wallbox.set_current(config.WALLBOX_MIN_CURRENT_A)
        self._last_written_wallbox = config.WALLBOX_MIN_CURRENT_A
        log.info("SET wallbox_current = %d A (startup, %s)", config.WALLBOX_MIN_CURRENT_A, path)

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
        path = self.wallbox.set_current(amps)
        if self._last_written_wallbox != amps:
            log.info("SET wallbox_current = %d A (%s)", amps, path)
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
                self.wallbox.set_current(target + 1)
                time.sleep(2)
                self.wallbox.set_current(target)
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
                morning = max(MORNING_BATT_CHARGE_PRIO, self._MIN_SOC_LFP)
                defer = target > morning and not self._batt_prio_release_reached()
                log.info(
                    "DAYLIGHT: SOC dusk=%.0f%% now=%.0f%% → range_needed=%.0f%% "
                    "→ setting batt_charge_prio=%.0f%%%s discharge_limit=%.0f%%",
                    self._soc_overnight_start, soc, range_needed,
                    morning if defer else target,
                    " (target %.0f%% at %02d:%02d)" % ((target,) + BATT_PRIO_RELEASE_AT)
                    if defer else "",
                    target,
                )
                try:
                    self.ha.set_input_number("input_number.range_needed_over_night", range_needed)
                    self.ha.set_input_number("input_number.discharge_limit", target)
                    self.ha.set_input_number("input_number.batt_charge_prio",
                                             morning if defer else target)
                    if defer:
                        self._pending_batt_prio = target
                        self._pending_batt_prio_date = date.today().isoformat()
                        self._save_state_file()
                except Exception:
                    log.warning("Failed to set overnight range helpers", exc_info=True)
                self._overnight_phase = "WAIT_FOR_NIGHT"

        self._solar_was_available = solar_available

    @staticmethod
    def _batt_prio_release_reached() -> bool:
        now = datetime.now()
        return (now.hour, now.minute) >= BATT_PRIO_RELEASE_AT

    def _release_batt_prio(self, s: dict) -> None:
        """Apply the batt_charge_prio target deferred at sunrise.

        Due at BATT_PRIO_RELEASE_AT, or at once if it dates from an earlier day
        (service down across the release time).  A prio no longer at the
        morning value was changed by the user in the meantime: left alone.
        """
        if self._pending_batt_prio is None:
            return
        stale = self._pending_batt_prio_date != date.today().isoformat()
        if not (stale or self._batt_prio_release_reached()):
            return
        morning = max(MORNING_BATT_CHARGE_PRIO, self._MIN_SOC_LFP)
        target = self._pending_batt_prio
        if abs(s["batt_charge_prio"] - morning) >= 0.5:
            log.info(
                "BATT PRIO: release — prio is %.0f%% (changed by hand), "
                "target %.0f%% dropped", s["batt_charge_prio"], target,
            )
        else:
            try:
                self.ha.set_input_number("input_number.batt_charge_prio", target)
            except Exception:
                log.warning("BATT PRIO: failed to set target %.0f%% — will retry",
                            target, exc_info=True)
                return
            log.info("BATT PRIO: release — batt_charge_prio %.0f%% → %.0f%%",
                     morning, target)
        self._pending_batt_prio = None
        self._pending_batt_prio_date = None
        self._save_state_file()

    # -- SOLAR_ONLY Tesla pause -----------------------------------------------

    def _load_state_file(self) -> dict:
        """Read the small persisted-state file (best effort)."""
        try:
            with open(TESLA_STATE_FILE) as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {}
        except Exception:
            log.warning("Could not read %s — starting clean", TESLA_STATE_FILE,
                        exc_info=True)
            return {}

    def _save_state_file(self) -> None:
        try:
            with open(TESLA_STATE_FILE, "w") as fh:
                json.dump({"tesla_paused": self._tesla_paused,
                           "tesla_paused_at": self._tesla_paused_at,
                           "pending_batt_prio": self._pending_batt_prio,
                           "pending_batt_prio_date": self._pending_batt_prio_date},
                          fh)
        except Exception:
            log.warning("Could not persist %s", TESLA_STATE_FILE, exc_info=True)

    def _clear_low_sun_timer(self) -> None:
        if self._tesla_low_sun_since is not None:
            log.info("TESLA PAUSE: sun back after %.0f s — cut cancelled",
                     time.monotonic() - self._tesla_low_sun_since)
            self._tesla_low_sun_since = None

    def _read_tesla(self) -> dict:
        """Normalized Tesla reading — only ever called when about to act.

        These are Home Assistant *cache* reads: the state machine answers from
        memory, nothing reaches Tessie and the car is never woken (verified
        2026-09-11: 200 reads in 2 s moved no ``last_updated``).  The Tesla
        still has a heavy vampire drain, so the loop stays deliberately silent
        around it: on a quiet tick the EMS does not even ask HA about the car.
        """
        raw = self.ha.read_tesla_state()
        return {
            "location": ha_text(raw, "tesla_location"),
            "cable": ha_text(raw, "tesla_charge_cable"),
            "charging": ha_text(raw, "tesla_charging"),
            "switch": ha_text(raw, "tesla_charge_switch"),
        }

    def _tesla_solar_pause(self, s: dict) -> None:
        """SOLAR_ONLY only: stop the Tesla when the sun no longer covers it.

        Both decisions read the same quantity, :func:`sun_to_ev` — the solar
        power actually available for the car — with a hysteresis band:
        cut under TESLA_CUT_BELOW_W, restart above TESLA_RESUME_ABOVE_W.

        Cut — the wallbox is pinned at its 6 A floor *and* the sun gives the car
        less than TESLA_CUT_BELOW_W, held continuously for TESLA_PAUSE_CONFIRM_S
        (so a passing cloud never cuts the charge).  Only then do we look at the
        Tesla, to check the car on the cable really is it: at home, plugged, and
        reporting charging/starting.  The wallbox alone cannot tell which car is
        charging.

        Resume — no confirmation delay, only TESLA_SETTLE_AFTER_CUT_S of settling
        after a cut (the Shelly and the Deye do not sample at the same instant,
        and the readings right after a 1.2 kW load drop are not trustworthy).

        The pause is only ever *our* pause: a charge the user stopped by hand is
        never restarted, and the flag survives a restart of ems.service through
        TESLA_STATE_FILE.  Any other EMS mode releases it.

        Tesla reads are on demand only (see :meth:`_read_tesla`): none at all on
        a quiet tick, one every TESLA_PAUSE_CONFIRM_S at worst while a countdown
        or a pause is running.
        """
        mode = (s.get("ems_mode") or "").upper()

        if self._tesla_paused:
            self._tesla_low_sun_since = None
            self._tesla_paused_tick(s, mode)
            return

        # Not paused.  Every gate below is read from the sensor batch we already
        # have — the Tesla is not consulted unless the countdown actually runs out.
        if mode != "SOLAR_ONLY" or self.state == State.IDLE:
            self._clear_low_sun_timer()
            return

        wallbox_floored = (
            self._last_written_wallbox is not None
            and self._last_written_wallbox <= config.WALLBOX_MIN_CURRENT_A
        )
        available = sun_to_ev(s)

        if not (wallbox_floored and available < TESLA_CUT_BELOW_W):
            self._clear_low_sun_timer()
            return

        now = time.monotonic()
        if self._tesla_low_sun_since is None:
            self._tesla_low_sun_since = now
            log.info(
                "TESLA PAUSE: wallbox floored at %d A and only %.0f W of sun for "
                "the car (< %d W; batt=%.0fW grid=%.0fW ev=%.0fW solar=%.0fW) — "
                "checking the car in %d s if it holds",
                self._last_written_wallbox, available, TESLA_CUT_BELOW_W,
                s["battery_power"], s["grid_power"], s["ev_power"],
                s["solar_power"], TESLA_PAUSE_CONFIRM_S,
            )
            return

        if now - self._tesla_low_sun_since < TESLA_PAUSE_CONFIRM_S:
            return  # countdown running: no Tesla read, no write, nothing

        # Countdown elapsed — the one moment we ask who is on the cable.
        tesla = self._read_tesla()
        is_tesla = (tesla["location"] == "home" and tesla["cable"] == "on"
                    and tesla["charging"] in ("charging", "starting"))
        if not is_tesla:
            # Another car (or Tessie down): leave the charge alone and restart
            # the countdown, so the next look is TESLA_PAUSE_CONFIRM_S away.
            log.info(
                "TESLA PAUSE: not the Tesla charging (location=%s cable=%s "
                "charging=%s) — leaving the charge alone, next check in %d s",
                tesla["location"], tesla["cable"], tesla["charging"],
                TESLA_PAUSE_CONFIRM_S,
            )
            self._tesla_low_sun_since = now
            return

        self._pause_tesla(s, available)

    def _tesla_paused_tick(self, s: dict, mode: str) -> None:
        """Per-tick work while *our* pause is active."""
        if mode != "SOLAR_ONLY":
            tesla = self._read_tesla()
            self._resume_tesla(f"EMS mode is now {mode}",
                               plugged=tesla["cable"] == "on")
            return

        # Sun and SOC come from the sensor batch: while the sun is too weak the
        # EMS does not talk to HA about the Tesla at all.
        available = sun_to_ev(s)
        soc_ok = s["battery_soc"] >= s["batt_charge_prio"]
        now = time.monotonic()
        settled = now - self._tesla_cut_ts >= TESLA_SETTLE_AFTER_CUT_S

        if settled and soc_ok and available > TESLA_RESUME_ABOVE_W:
            tesla = self._read_tesla()
            if tesla["location"] is None or tesla["cable"] is None:
                # Tessie unreachable: hold the pause rather than reading its
                # silence as "car gone" and restarting under clouds.
                self._tesla_pause_log_ts = now
                log.warning("TESLA RESUME: Tesla state unreadable — holding the pause")
                return
            if tesla["location"] != "home" or tesla["cable"] != "on":
                self._resume_tesla(
                    f"car no longer plugged in at home "
                    f"(location={tesla['location']}, cable={tesla['cable']})",
                    plugged=tesla["cable"] == "on",
                )
                return
            self._resume_tesla(
                f"{available:.0f} W of sun for the car > {TESLA_RESUME_ABOVE_W} W "
                f"and SOC {s['battery_soc']:.0f}% >= prio {s['batt_charge_prio']:.0f}%",
                plugged=True,
            )
            return

        # Not enough sun yet.  Every TESLA_PAUSE_LOG_INTERVAL_S, say where we
        # stand and take one look at the car — it may have been unplugged or
        # driven away, or the user may have re-enabled the charge by hand.
        if now - self._tesla_pause_log_ts >= TESLA_PAUSE_LOG_INTERVAL_S:
            self._tesla_pause_log_ts = now
            log.info(
                "TESLA PAUSE: still paused — sun for the car=%.0fW (need >%dW) "
                "SOC=%.0f%% prio=%.0f%% solar=%.0fW",
                available, TESLA_RESUME_ABOVE_W, s["battery_soc"],
                s["batt_charge_prio"], s["solar_power"],
            )
            self._check_pause_still_relevant()

    def _check_pause_still_relevant(self) -> None:
        """Five-minutely sanity check while paused (one HA cache read)."""
        tesla = self._read_tesla()

        if tesla["switch"] == "on":
            # Somebody re-enabled the charge behind our back: the pause is no
            # longer ours to hold.  The normal cut logic may arm again.
            log.info("TESLA PAUSE: switch.martine_charge is back on — "
                     "dropping the EMS pause")
            self._tesla_paused = False
            self._tesla_paused_at = None
            self._save_state_file()
            return

        if tesla["location"] is None or tesla["cable"] is None:
            log.warning("TESLA PAUSE: Tesla state unreadable — holding the pause")
            return

        if tesla["location"] != "home" or tesla["cable"] != "on":
            self._resume_tesla(
                f"car no longer plugged in at home "
                f"(location={tesla['location']}, cable={tesla['cable']})",
                plugged=tesla["cable"] == "on",
            )

    def _pause_tesla(self, s: dict, available: float) -> None:
        log.warning(
            "TESLA PAUSE: cutting the charge after %.0f s at wallbox %d A — "
            "sun for the car=%.0fW (< %dW; batt=%.0fW grid=%.0fW) solar=%.0fW "
            "ev=%.0fW SOC=%.0f%%",
            time.monotonic() - self._tesla_low_sun_since,
            self._last_written_wallbox, available, TESLA_CUT_BELOW_W,
            s["battery_power"], s["grid_power"], s["solar_power"],
            s["ev_power"], s["battery_soc"],
        )
        try:
            self.ha.set_tesla_charge(False)
        except Exception:
            # Keep the timer armed: the next tick retries immediately.
            log.warning("TESLA PAUSE: stop request failed — retrying next tick",
                        exc_info=True)
            return

        self._tesla_paused = True
        self._tesla_paused_at = datetime.now().isoformat(timespec="seconds")
        self._tesla_low_sun_since = None
        self._tesla_cut_ts = time.monotonic()
        self._tesla_pause_log_ts = self._tesla_cut_ts
        self._save_state_file()
        log.info(
            "TESLA PAUSE: charge stopped (switch.martine_charge = off) — "
            "waiting for more than %d W of sun for the car (meters settle for "
            "%d s first)", TESLA_RESUME_ABOVE_W, TESLA_SETTLE_AFTER_CUT_S,
        )

    def _resume_tesla(self, reason: str, plugged: bool) -> None:
        log.info("TESLA RESUME: restarting the charge — %s", reason)
        try:
            self.ha.set_tesla_charge(True)
        except Exception:
            log.warning("TESLA RESUME: start request failed", exc_info=True)
            if plugged:
                # Still on the cable: keep the pause and retry on the next tick.
                return
            # Unplugged car: Tessie cannot start a charge, so there is nothing
            # left to resume.  Drop the pause but warn — switch.martine_charge
            # may stay off until the user turns it back on.
            try:
                self.ha.notify(
                    "EMS: charge Tesla restée coupée",
                    "L'EMS avait coupé la charge (soleil insuffisant en "
                    "SOLAR_ONLY) et n'a pas pu la relancer : la voiture n'est "
                    "plus branchée. Vérifie switch.martine_charge avant le "
                    "prochain branchement.",
                    "ems_tesla_charge_off",
                )
            except Exception:
                log.warning("Failed to raise HA notification", exc_info=True)

        self._tesla_paused = False
        self._tesla_paused_at = None
        self._save_state_file()

    # -- wallbox status -------------------------------------------------------

    # -- which car is on the cable -------------------------------------------

    def _identify_plugged_car(self) -> None:
        """Kick off the identification in the background.

        The 1 Hz loop has to keep steering the battery while we wake a car up,
        and ``kia_uvo.force_update`` blocks for ~30 s, so the sequence runs in
        a daemon thread.  It only reads HA and writes one helper; it never
        touches the EMS state machine.
        """
        threading.Thread(target=self._identify_plugged_car_blocking,
                         args=(self._ev_id_generation,),
                         name="ev-identify", daemon=True).start()

    def _identify_plugged_car_blocking(self, generation: int) -> None:
        """Thread body: identify the car, then publish the answer.

        *generation* pins this run to the plug-in that started it, so a thread
        still waiting on a slow Hyundai API cannot overwrite the verdict of a
        later session.
        """
        # Its own HA client: requests.Session is not thread-safe, and the main
        # loop keeps using self.ha throughout.
        ha = HomeAssistantAPI(config.HA_URL, config.HA_TOKEN)
        try:
            label = self._detect_plugged_car(ha, generation)
        except Exception:
            log.warning("EV identification failed", exc_info=True)
            label = EV_UNKNOWN
        self._write_ev_connected(label, ha, generation)
        if label == EV_UNKNOWN:
            self._late_ioniq_watch(ha, generation)

    def _detect_plugged_car(self, ha: HomeAssistantAPI, generation: int) -> str:
        """Which car is on the cable?  Tesla first, then Ioniq, else "other"."""
        # 1. Tesla — free: HA answers from its cache and the car is never woken.
        #
        # The cable sensor, not the charging state: over 9 days the Tesla was
        # plugged in at home for 70.1 h, and for 21.0 h of those (30 %)
        # sensor.martine_charging read "stopped" — which is what the EMS's own
        # solar pause produces.  Keying on the charge state would have missed
        # the car one time in three.  The cable never claimed "plugged at home"
        # while another car was on the wallbox: the only disagreements were
        # 0.2 h total, in four 2-4 min bursts where the cable was simply
        # quicker than the wallbox's 5-minute-polled sensor.
        tesla = ha.read_tesla_state()
        cable = ha_text(tesla, "tesla_charge_cable")
        location = ha_text(tesla, "tesla_location")
        if cable == "on" and location == "home":
            return EV_TESLA
        log.info("EV id: not the Tesla (cable=%r location=%r)", cable, location)

        # 2. Ioniq — the Bluelink cache runs ~2 h behind, so ask the car itself.
        ioniq = self._refresh_ioniq(ha, generation)
        if ioniq is None:
            # The car never answered.  That is "we do not know", not "some
            # other car" — saying EV_OTHER here would invent a third vehicle.
            return EV_UNKNOWN
        return self._verdict_from_ioniq(ioniq)

    @staticmethod
    def _verdict_from_ioniq(ioniq: dict) -> str:
        loc = ha_text(ioniq, "ioniq_location")
        plug = ha_text(ioniq, "ioniq_plug")
        if loc == "home" and plug == "on":
            return EV_IONIQ
        log.info("EV id: not the Ioniq (location=%r plug=%r)", loc, plug)
        return EV_OTHER

    def _refresh_ioniq(self, ha: HomeAssistantAPI, generation: int) -> dict | None:
        """Get a fresh Bluelink payload, retrying a busy car.

        ``force_update`` failing does not mean the car is unreachable: the
        Hyundai API answers "Duplicate request" when a refresh is already in
        flight, so we wait for that one to land before trying again.
        """
        for attempt in range(1, IONIQ_FORCE_ATTEMPTS + 1):
            if generation != self._ev_id_generation:
                log.info("EV id: superseded by a newer session, giving up")
                return None
            before = ha_text(ha.read_ioniq_state(), "ioniq_data_ts")
            ok = ha.force_update_ioniq()
            log.info("EV id: Ioniq force_update attempt %d/%d -> %s",
                     attempt, IONIQ_FORCE_ATTEMPTS, "sent" if ok else "refused")
            fresh = self._wait_for_ioniq_refresh(ha, before)
            if fresh is not None:
                return fresh
            if attempt < IONIQ_FORCE_ATTEMPTS:
                time.sleep(IONIQ_RETRY_DELAY_S)
        return None

    def _wait_for_ioniq_refresh(self, ha: HomeAssistantAPI,
                                before_ts: str | None) -> dict | None:
        """Poll until the Bluelink payload is newer than *before_ts*.

        ``force_update`` returns once the vehicle has answered, but the
        entities land a few seconds later, so the timestamp is what we watch.
        Returns None when the car stayed silent or answered with stale data.
        """
        deadline = time.monotonic() + IONIQ_REFRESH_TIMEOUT_S
        while time.monotonic() < deadline:
            ioniq = ha.read_ioniq_state()
            ts = ha_text(ioniq, "ioniq_data_ts")
            if ts and ts != before_ts:
                age = self._payload_age_s(ts)
                if age is None or age > IONIQ_MAX_DATA_AGE_S:
                    log.warning("Ioniq refreshed but payload age is %s s — not trusted", age)
                    return None
                log.info("Ioniq refreshed, payload %.0f s old", age)
                return ioniq
            time.sleep(IONIQ_REFRESH_POLL_S)
        log.warning("Ioniq did not refresh within %d s", IONIQ_REFRESH_TIMEOUT_S)
        return None

    def _late_ioniq_watch(self, ha: HomeAssistantAPI, generation: int) -> None:
        """Keep listening after giving up, and upgrade "unknown" if it clears.

        An HA automation force-updates the Ioniq every hour while it draws
        power, so a car we could not reach at plug-in time usually reports in
        later — on 2026-09-20 the answer landed 33 min after the failed
        attempt.  Waiting costs nothing here: the thread is already detached,
        and it stops as soon as the cable state changes.
        """
        log.info("EV id: watching for a late Ioniq refresh (up to %d min)",
                 IONIQ_LATE_WATCH_S // 60)
        before = ha_text(ha.read_ioniq_state(), "ioniq_data_ts")
        deadline = time.monotonic() + IONIQ_LATE_WATCH_S
        while time.monotonic() < deadline:
            time.sleep(IONIQ_LATE_POLL_S)
            if generation != self._ev_id_generation:
                log.info("EV id: late watch cancelled, cable state changed")
                return
            ioniq = ha.read_ioniq_state()
            ts = ha_text(ioniq, "ioniq_data_ts")
            if not ts or ts == before:
                continue
            age = self._payload_age_s(ts)
            if age is None or age > IONIQ_LATE_WATCH_S:
                continue
            label = self._verdict_from_ioniq(ioniq)
            log.info("EV id: late Ioniq answer (payload %.0f s old) -> %s", age, label)
            self._write_ev_connected(label, ha, generation)
            return
        log.info("EV id: late watch expired, leaving %s", EV_UNKNOWN)

    @staticmethod
    def _payload_age_s(ts: str) -> float | None:
        """Age in seconds of an ISO timestamp, or None if it cannot be parsed.

        ``ha_text`` lower-cases what it returns, hence the ``upper()``.
        """
        try:
            when = datetime.fromisoformat(ts.upper().replace("Z", "+00:00"))
        except ValueError:
            return None
        return (datetime.now(when.tzinfo) - when).total_seconds()

    def _write_ev_connected(self, label: str, ha: HomeAssistantAPI | None = None,
                            generation: int | None = None) -> None:
        """Publish which car is on the cable to the HA helper."""
        if generation is not None and generation != self._ev_id_generation:
            log.info("EV id: dropping stale verdict %r", label)
            return
        log.info("EV connected: %s", label)
        try:
            (ha or self.ha).set_ev_connected(label)
        except Exception:
            log.warning("Failed to write ev_connected=%s", label, exc_info=True)

    def _reset_mode_on_unplug(self, s: dict, status: str) -> None:
        """Car unplugged: hand the next session back the default mode.

        Fired on the falling edge of :meth:`_car_plugged`, i.e. when the BLE
        gateway's ``car_connected`` drops to off.  Doing this at unplug rather
        than at plug-in means the user can plug in, pick FULL_SPEED and never
        see it wiped under them; it also makes the detection latency free,
        since the mode is only consumed by the *next* session.

        ``ev_power`` can only veto, never confirm: power flowing proves a car
        is there, but a car sitting idle draws the same ~6 W as an empty cable,
        so silence proves nothing.  Over 9 days this veto blocked 0 of the 17
        real unplug edges, so it costs nothing to keep.
        """
        if s["ev_power"] > config.EV_CHARGING_DETECT_W:
            log.info(
                "Car unplugged per gateway (%r) but EV still draws %.0f W — "
                "mode left untouched", status, s["ev_power"],
            )
            return
        log.info("Car unplugged (gateway: %r) — resetting ems_mode to %s",
                 status, config.DEFAULT_EMS_MODE)
        self._write_ev_connected(EV_NONE)
        try:
            self.ha.set_ems_mode(config.DEFAULT_EMS_MODE)
            s["ems_mode"] = config.DEFAULT_EMS_MODE
        except Exception:
            log.warning("Failed to reset ems_mode", exc_info=True)

    @staticmethod
    def _car_plugged(s: dict) -> bool | None:
        """Is a car plugged in?  ``None`` when the answer is not conclusive.

        Read from the BLE gateway (``car_connected``), not from the cloud
        ``status_description`` any more.  The cloud status used to be
        classified here (Locked/Ready = no car, learned from 9 days of history,
        see MEMO §6bis), but it polls every 90 s and can freeze: on 2026-09-25
        at 16:41 it went to "Ready" when the Ioniq was unplugged and stayed
        there while the Tesla, plugged one minute later, charged at 1.2 kW —
        so no plug-in edge, and ev_connected stuck on "disconnected".

        Since pairing (2026-09-24 18:17 → 09-25 14:42) the gateway matched all
        6 plug-ins and 5 unplugs, 1 to 80 s ahead of the cloud, with no flap
        during a charge.

        ``off`` is only trusted once the gateway has heard from the charger:
        the firmware reports ``car_connected = false`` by default, before any
        data.  Seen right after a gateway reboot (09-24 18:15): "off" for two
        minutes with ``charger_status = unknown`` while the Ioniq was charging.
        Unknown/unavailable therefore leave the remembered state untouched —
        they cannot fabricate an edge, exactly like "Disconnected" and
        "unavailable" on the old cloud status.
        """
        if s.get("gw_charger_status") in (None, "unknown", "unavailable"):
            return None
        return {"on": True, "off": False}.get(s.get("gw_car_connected"))

    # -- state evaluation -----------------------------------------------------

    def _force_solar_only_on_low_soc(self, s: dict) -> State:
        """SOC floor reached in a storage mode: fall back to SOLAR_ONLY.

        This overrides a choice the user is actively making, so it targets
        SOLAR_ONLY literally rather than ``DEFAULT_EMS_MODE``: it is a battery
        protection, not a return to the default — unlike
        :meth:`_reset_mode_on_unplug`, which ends a session.
        """
        self.ha.set_ems_mode("SOLAR_ONLY")
        s["ems_mode"] = "SOLAR_ONLY"
        log.info("SOC <= discharge_limit — forcing ems_mode to SOLAR_ONLY")
        if s["solar_power"] > config.SOLAR_AVAILABLE_W:
            return State.SOLAR_ONLY
        return State.EV_NO_SOLAR

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

        if mode in ("STORAGE_BOOSTED", "STORAGE_ONLY"):
            if s["battery_soc"] <= s["discharge_limit"]:
                return self._force_solar_only_on_low_soc(s)
            return State.STORAGE_BOOSTED if mode == "STORAGE_BOOSTED" else State.STORAGE_ONLY

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

        # 0. Detect the end of a session → reset mode to default
        # Both statuses are logged: the cloud one no longer drives anything,
        # but it is what the 9-day history behind the old rule was built on.
        status = s.get("wallbox_status", "")
        if status != self._last_wallbox_status:
            log.info("Wallbox status: %r → %r", self._last_wallbox_status, status)
            self._last_wallbox_status = status
        gw_status = s.get("gw_charger_status")
        if gw_status != self._last_gw_status:
            log.info("Gateway status: %r → %r (car_connected=%s)",
                     self._last_gw_status, gw_status, s.get("gw_car_connected"))
            self._last_gw_status = gw_status
        status = gw_status

        plugged = self._car_plugged(s)
        # A change must be read on two ticks in a row.  HA publishes
        # car_connected and charger_status one after the other, and our two
        # GETs can land in between: a stale "off" paired with a fresh known
        # status would be a false unplug.
        if plugged is not None and plugged != self._car_connected:
            if self._plug_seen != plugged:
                self._plug_seen = plugged
                plugged = None
        elif plugged is not None:
            self._plug_seen = None
        if plugged is not None:
            # Edges are taken between *conclusive* states only.  An
            # inconclusive reading leaves _car_connected untouched, so a
            # gateway reboot or an HA restart cannot fabricate an edge.
            if plugged != self._car_connected and self._car_connected is not None:
                self._ev_id_generation += 1
            if plugged and self._car_connected is False:
                # Plug-in: observation only.  The mode is deliberately left
                # alone so the user can pick one while the car sits waiting.
                log.info("Car plugged in (gateway: %r) — mode left untouched", status)
                self._identify_plugged_car()
            elif not plugged and self._car_connected is True:
                self._reset_mode_on_unplug(s, status)
            elif self._car_connected is None:
                # First conclusive reading since start: no edge, but
                # ev_connected may be stale from before the restart.  The
                # mode is not touched either way.
                log.info("Car %s at startup (gateway: %r)",
                         "plugged" if plugged else "not plugged", status)
                if plugged:
                    self._identify_plugged_car()
                else:
                    self._write_ev_connected(EV_NONE)
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

        # 2b. SOLAR_ONLY: cut / restart the Tesla charge with the sun
        self._tesla_solar_pause(s)

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
        self._release_batt_prio(s)

        # 7. Verify the inverter actually kept our discharge setpoint
        self._reconcile_discharge(s)

        # 8. Verify the last BLE wallbox write landed; replay it via the cloud if not
        self.wallbox.poll()


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
