"""EMS — Energy Management System for Deye battery + Wallbox Pulsar Max.

State machine with states:
  IDLE, FULL_SPEED, EV_NO_SOLAR, BATTERY_PRIORITY,
  SOLAR_ONLY, SOLAR_BOOSTED, STORAGE_ONLY.

Modes (input_select.ems_mode):
  SOLAR_ONLY, SOLAR_BOOSTED, FULL_SPEED, STORAGE_ONLY.

See README.md for full specification.
"""

import enum
import json
import logging
import os
import time
import sys
import threading
from datetime import datetime, timedelta

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
# Phantom SOC: when the Dawnice BMS falls asleep (cell under 3.15 V at rest, no
# current) the Deye loses its CAN data for ~10 s and reports 99-100 % until it
# wakes the BMS up again (every ~5 min on 2026-10-30, 01:56-18:11 UTC).
SOC_GLITCH_JUMP_PCT = 20   # a real SOC never moves this much between two reads
SOC_GLITCH_HOLD_S = 60     # ...a jump that lasts longer is accepted as real


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
# Which car is on the cable (written to input_text.ev_connected)
#
# Identification runs on the DISCONNECTED -> CONNECTED edge, off the 1 Hz loop:
# the Ioniq's OBD dongle takes 5-90 s to answer, and the Bluelink fallback
# (kia_uvo.force_update) is synchronous and took 29 s when measured on
# 2026-09-20, so neither may block a tick.
# ---------------------------------------------------------------------------

EV_TESLA = "Tesla connected"
EV_IONIQ = "Ioniq connected"
EV_OTHER = "other connected"
EV_NONE = "disconnected"
EV_UNKNOWN = "unknown"           # the Ioniq could not be reached — not "not the Ioniq"

# The label the charger reports when a car has just been put on the cable.
# 20 occurrences over five days (2026-10-06..11), every one a real plug-in,
# always reached from "Locked", never mid-session.  That makes it a reliable
# start-of-session marker, and the only way to notice a swap completed faster
# than the unplug takes to confirm.
GW_NEW_SESSION = "Connected — no current"

# How often the Tesla's own cable sensor is polled while it is the car on the
# cable.  It is the only witness that can beat the gateway, which since the
# 2026-10-11 firmware fix waits for its two fields to agree and so takes ~15 s
# to drop the flag (measured 20:48:41 unplug -> 20:48:56 flag).
TESLA_CABLE_POLL_S = 3

# How long an unplug has to be confirmed by the car after the gateway's
# readings come back.  A gateway that has just booted has not yet asked the
# charger about the cable, and the charger itself may not know: on 2026-10-15
# 17:01 a power cut rebooted the charger while the EMS survived on the
# inverter's backup output, and the first readings said "no car" with the
# Tesla plugged — both gateway fields agreeing, so nothing downstream could
# catch it.  SOLAR_BOOSTED was wiped and had to be set again by hand.
GW_RECOVERY_DOUBT_S = 90


def gw_label_absent(label: str | None) -> bool:
    """Is this gateway label a non-answer rather than a reading?"""
    return (label or "").strip().lower() in ("", "unknown", "unavailable", "none")

TESLA_CABLE_GRACE_S = 30         # Tessie's cable usually beats the wallbox by
TESLA_CABLE_POLL_S = 3           # 6-36 s, but trailed it by 4.5 s on 2026-10-06
TESLA_DOOR_FRESH_S = 120         # how recently cover.martine_charge_port_door
                                 # must have opened to vouch for this plug
TESLA_DOOR_GRACE_S = 90          # extra wait it buys the cable sensor
IONIQ_REFRESH_TIMEOUT_S = 60     # wait for the payload after force_update returns
IONIQ_REFRESH_POLL_S = 3         # how often to look while waiting
IONIQ_MAX_DATA_AGE_S = 120       # a payload older than this is not trusted
IONIQ_FORCE_ATTEMPTS = 3         # DuplicateRequestError is transient — retry
IONIQ_RETRY_DELAY_S = 30         # let the in-flight request finish first
IONIQ_LATE_WATCH_S = 4200        # 70 min: long enough to catch the hourly
IONIQ_LATE_POLL_S = 60           # HA automation that refreshes the car anyway

# The Ioniq's OBD dongle, asked before Bluelink since 2026-10-14 (the Hyundai
# API was down all that day: three Ioniq plug-ins published "unknown").
IONIQ_OBD_TIMEOUT_S = 150        # a mute BMS is retried 6 × 10 s: ~90 s worst
IONIQ_OBD_POLL_S = 2
IONIQ_OBD_RETRY_S = 30           # second try after "injoignable"
IONIQ_OBD_MAX_READS = 3

# A car plugged in between sunset and sunrise starts in FULL_SPEED (user
# decision of 2026-10-18): without sun, SOLAR_ONLY only gives it 6 A.
SUN_ENTITY = "sun.sun"

# What the dongle says about the Ioniq (see _ask_ioniq_dongle).
OBD_PLUGGED = "plugged"
OBD_UNPLUGGED = "unplugged"
OBD_ASLEEP = "asleep"
OBD_GONE = "gone"

# A BLE contact report younger than this means the Ioniq is within range of
# its OBD gateway, so it is the car here even if the exchange itself failed.
IONIQ_BLE_FRESH_S = 300


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class State(enum.Enum):
    IDLE = "IDLE"                        # Internal: no EV charging detected
    FULL_SPEED = "FULL_SPEED"            # Mode: wallbox 32A, grid pays 50-60% of EV, battery the rest
    EV_NO_SOLAR = "EV_NO_SOLAR"          # Internal: no solar, wallbox 6A, discharge limited to house
    BATTERY_PRIORITY = "BATTERY_PRIORITY"  # Internal: solar present but SOC < priority
    SOLAR_ONLY = "SOLAR_ONLY"            # Mode: solar surplus → wallbox, grid ≈ 0
    SOLAR_BOOSTED = "SOLAR_BOOSTED"      # Mode: wallbox boosted, grid pays 50-60% of EV
    STORAGE_ONLY = "STORAGE_ONLY"        # Mode: battery + solar → wallbox, grid = 0


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
        self._car_connected = None          # wallbox: car plugged in (None = unknown yet)
        self._ev_id_generation = 0          # bumped on every plug/unplug edge
        self._last_wallbox_status = None    # last logged wallbox status string (cloud)
        self._last_gw_status = None         # last logged gateway charger_status
        self._plug_seen = None              # plug change read once, awaiting confirmation
        self._ev_label = None               # last value published to ev_connected
        self._tesla_cable_ts = 0.0          # monotonic ts of the last cable poll
        self._tesla_cable_off = False       # last cable reading, cached between polls
        self._gw_back_ts = 0.0              # monotonic ts of the gateway's last recovery
        self._dongle_lock = threading.Lock()  # one plug reading at a time: one shared button
        self._swap_check_running = False    # a thread is asking the Ioniq about a swap
        self._swap_verdict = None           # (generation, accept) handed back by that thread
        self._last_written_grid_ratio = None
        self._battery_voltage = 52.0        # last known battery voltage
        self._soc_trusted = None            # last SOC that was not a phantom jump
        self._soc_jump_since = None         # monotonic ts of the first read of a jump
        # Wallbox override detection
        self._wallbox_override_since = None  # timestamp when override first detected
        self._wallbox_override_retries = 0   # number of toggle retries attempted
        # Overnight range tracking — two-phase state machine (see _track_overnight_range)
        self._overnight_phase = "WAIT_FOR_NIGHT"  # WAIT_FOR_NIGHT | WAIT_FOR_DAYLIGHT
        self._soc_overnight_start = None          # SOC captured at dusk (last sub-threshold crossing)
        self._overnight_night = None              # _night_key() the dusk SOC belongs to
        self._solar_was_available = None          # previous tick solar state; None = first tick
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
        # Overnight tracking — persisted so a restart neither re-captures the
        # dusk SOC (2026-09-25: a 21:10 deploy replaced 84 % by 78 %, hiding
        # 2 h of evening drain) nor, after 01:00, loses the pre-dawn phase.
        # Only restored for the night it was captured for.
        if persisted.get("overnight_night") == self._night_key():
            self._overnight_phase = persisted.get("overnight_phase") or "WAIT_FOR_NIGHT"
            self._soc_overnight_start = persisted.get("soc_overnight_start")
            self._overnight_night = persisted["overnight_night"]
            log.info(
                "OVERNIGHT: restored from %s — phase %s, SOC at dusk = %s%%",
                TESLA_STATE_FILE, self._overnight_phase, self._soc_overnight_start,
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


    def _enter_storage_only(self) -> None:
        log.info("→ STORAGE_ONLY")
        self.state = State.STORAGE_ONLY
        self._ema_discharge = None
        self._set_max_discharging(config.DEFAULT_MAX_DISCHARGING_CURRENT_A, force=True)
        self._set_wallbox(config.WALLBOX_MIN_CURRENT_A)
        self._last_slow_tick = 0.0  # force immediate first wallbox adjustment

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

    def _compute_full_speed_discharge(self, s: dict, grid_ratio: float) -> int:
        """Discharge algorithm for FULL_SPEED above discharge_limit.

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

    _PREDAWN_HOUR = 1  # local hour: lock dusk SOC and start watching for daylight

    @staticmethod
    def _night_key() -> str:
        """Date of the evening the current night belongs to (noon to noon)."""
        return (datetime.now() - timedelta(hours=12)).date().isoformat()

    def _capture_dusk(self, soc: float, how: str) -> None:
        self._soc_overnight_start = soc
        self._overnight_night = self._night_key()
        log.info("DUSK: %s, SOC at dusk = %.0f%%", how, soc)
        self._save_state_file()

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
            range_needed = soc_dusk - soc_now, publish it, and switch back to
            WAIT_FOR_NIGHT.  Information only: batt_charge_prio and
            discharge_limit are set by hand.

        Robust against solar dropping out and returning for >30 min: daylight is
        only ever evaluated after 01:00, and the first crossing ends the phase,
        so dawn oscillation cannot reset range_needed_over_night.
        """
        solar_available = s["solar_power"] > config.SOLAR_AVAILABLE_W
        soc = s["battery_soc"]
        hour = datetime.now().hour

        if self._solar_was_available is None:
            # First tick after a start: there is no edge to read.  It used to
            # count as one (the flag started True), so any evening restart
            # re-captured the dusk SOC.  Now only an evening start with no
            # dusk SOC restored for tonight captures — the best estimate left.
            if (not solar_available and hour >= 12
                    and self._overnight_phase == "WAIT_FOR_NIGHT"
                    and self._overnight_night != self._night_key()):
                self._capture_dusk(soc, "no sun at startup and none persisted")
            self._solar_was_available = solar_available
            return

        if self._overnight_phase == "WAIT_FOR_NIGHT":
            # Falling edge: solar just dropped below threshold → record dusk SOC.
            if self._solar_was_available and not solar_available:
                self._capture_dusk(soc, "solar dropped below threshold")
            # At 01:00, lock in the dusk SOC and wait for sunrise.
            if hour == self._PREDAWN_HOUR and self._soc_overnight_start is not None:
                self._overnight_phase = "WAIT_FOR_DAYLIGHT"
                self._save_state_file()
                log.info(
                    "OVERNIGHT: pre-dawn reached — SOC at dusk = %.0f%%, "
                    "waiting for daylight", self._soc_overnight_start,
                )

        elif self._overnight_phase == "WAIT_FOR_DAYLIGHT":
            if solar_available:
                range_needed = max(self._soc_overnight_start - soc, 0)
                log.info(
                    "DAYLIGHT: SOC dusk=%.0f%% now=%.0f%% → range_needed=%.0f%%",
                    self._soc_overnight_start, soc, range_needed,
                )
                try:
                    self.ha.set_input_number("input_number.range_needed_over_night", range_needed)
                except Exception:
                    log.warning("Failed to set range_needed_over_night", exc_info=True)
                self._overnight_phase = "WAIT_FOR_NIGHT"
                self._save_state_file()

        self._solar_was_available = solar_available

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
                           "overnight_phase": self._overnight_phase,
                           "soc_overnight_start": self._soc_overnight_start,
                           "overnight_night": self._overnight_night},
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
        """Which car is on the cable?  Tesla, then the Ioniq's dongle, then Bluelink."""
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
        #
        # The cable usually flips 6-36 s before the wallbox sees the plug, but
        # on 2026-10-06 it trailed it by 4.5 s and the Tesla was published as
        # "other connected".  So give it a short grace before waking the Ioniq
        # — a needless force_update costs a Bluelink request and a car wake-up.
        started = time.monotonic()
        deadline = started + TESLA_CABLE_GRACE_S
        door_extended = False
        while True:
            if self._tesla_on_cable(ha):
                return EV_TESLA
            if generation != self._ev_id_generation:
                break
            if time.monotonic() >= deadline:
                if door_extended or not self._tesla_port_door_vouches(ha):
                    break
                door_extended = True
                deadline = time.monotonic() + TESLA_DOOR_GRACE_S
            time.sleep(TESLA_CABLE_POLL_S)
        self._tesla_on_cable(ha, log_miss=True, waited=time.monotonic() - started)

        # 2. Ioniq — its own OBD dongle first.  An Ioniq just put on the cable
        # is awake and answers in 3-6 s (5 plug-ins out of 5); one asleep
        # nearby does not answer, and the dongle does not wake it.  So
        # "asleep" means the Ioniq is parked here but is not the car on the
        # cable (user decision 2026-10-14).
        dongle = self._ask_ioniq_dongle(ha, generation)
        if dongle == OBD_PLUGGED:
            label = EV_IONIQ
        elif dongle in (OBD_UNPLUGGED, OBD_ASLEEP):
            log.info("EV id: not the Ioniq (dongle: %s)", dongle)
            label = EV_OTHER
        elif dongle == OBD_GONE:
            # "Unreachable" says nothing about which car is on the cable,
            # unlike "unplugged" and "asleep" which do.  Treating it as "some
            # other car" published "other connected" twice on 2026-10-20 for
            # an Ioniq that was plugged in, and an "other" label disarms every
            # witness downstream — the swap check then accepts a swap without
            # asking anyone.  So fall through to the car's other witnesses,
            # and only a positive "away" rules it out (user decision
            # 2026-10-20).
            label = self._ioniq_present_despite_silence(ha, generation)
        elif generation != self._ev_id_generation:
            return EV_UNKNOWN  # superseded: _write_ev_connected drops it anyway
        else:
            # 3. Dongle unavailable — the Bluelink cache runs ~2 h behind, so
            # ask the car itself through the Hyundai API.
            ioniq = self._refresh_ioniq(ha, generation)
            if ioniq is None:
                # The car never answered.  That is "we do not know", not "some
                # other car" — saying EV_OTHER here would invent a third vehicle.
                label = EV_UNKNOWN
            else:
                label = self._verdict_from_ioniq(ioniq)

        # 4. Last look at the Tesla: free, and the Ioniq round-trip gave a
        # late cable another 5-90 s to land.
        if label != EV_IONIQ and self._tesla_on_cable(ha):
            log.info("EV id: Tesla cable showed up late — overriding %r", label)
            return EV_TESLA
        return label

    @staticmethod
    def _tesla_on_cable(ha: HomeAssistantAPI, log_miss: bool = False,
                        waited: float = TESLA_CABLE_GRACE_S) -> bool:
        tesla = ha.read_tesla_state()
        cable = ha_text(tesla, "tesla_charge_cable")
        location = ha_text(tesla, "tesla_location")
        if cable == "on" and location == "home":
            return True
        if log_miss:
            log.info("EV id: not the Tesla after %.0f s (cable=%r location=%r)",
                     waited, cable, location)
        return False

    @staticmethod
    def _tesla_port_door_vouches(ha: HomeAssistantAPI) -> bool:
        """Is cover.martine_charge_port_door evidence the cable sensor is late?

        On 2026-10-29 the Tesla was re-plugged at about 08:32:45.  The flap
        opened at 08:32:40, 15 s before the gateway itself saw the plug, and
        binary_sensor.martine_charge_cable only followed at 08:34:00 — 32 s
        after the 30 s grace had run out.  The Tesla was ruled out, the Ioniq
        fallback took over, and input_text.ev_connected read "Ioniq connected"
        from 08:34:26 to 08:38:43 while the Tesla charged.  The flap was the
        fastest of the three witnesses, so when it has just opened, wait for
        the cable rather than give up on the car.

        "Just opened" is the whole point: the house has a second charging
        point, and a flap that has been open for a while says nothing about
        which car is on the wallbox.  So this only ever buys the cable sensor
        more time — the verdict still comes from the cable.
        """
        state, age = ha.read_tesla_charge_port_door_dated()
        if state != "open" or age is None or age > TESLA_DOOR_FRESH_S:
            return False
        log.info("EV id: binary_sensor.martine_charge_cable still reads 'off', "
                 "but cover.martine_charge_port_door opened %.0f s ago — "
                 "giving the cable another %d s", age, TESLA_DOOR_GRACE_S)
        return True

    def _ioniq_present_despite_silence(self, ha: HomeAssistantAPI,
                                       generation: int) -> str:
        """The dongle is unreachable — is the Ioniq here anyway?

        Two witnesses left, and the bias is deliberate: only a positive "away"
        rules the Ioniq out.  A silence must not become a verdict.

        1. The BLE contact age.  The gateway reporting its link to the car
           means the car is within range, whatever the level — but the report
           has to be recent, see read_ioniq_ble_contact_age_s().
        2. Bluelink.  Its location is what decides: "not_home" is the only
           answer that makes this another car.  No answer at all, or an
           unknown location, leaves the Ioniq as the conclusion.
        """
        age = ha.read_ioniq_ble_contact_age_s(IONIQ_BLE_FRESH_S)
        if age is not None and age <= IONIQ_BLE_FRESH_S:
            log.info("EV id: dongle silent but its BLE link to the car was "
                     "reported %.0f s ago — the Ioniq is here", age)
            return EV_IONIQ
        log.info("EV id: dongle silent and no recent BLE contact (%s) — asking Bluelink",
                 ("%.0f s" % age) if age is not None else "never")
        ioniq = self._refresh_ioniq(ha, generation)
        location = ha_text(ioniq, "ioniq_location") if ioniq else None
        if location is not None and location != "home":
            log.info("EV id: Bluelink puts the Ioniq %r — it is another car", location)
            return EV_OTHER
        log.info("EV id: Bluelink does not place the Ioniq away (location=%r) — "
                 "keeping the Ioniq", location)
        return EV_IONIQ

    @staticmethod
    def _verdict_from_ioniq(ioniq: dict) -> str:
        loc = ha_text(ioniq, "ioniq_location")
        plug = ha_text(ioniq, "ioniq_plug")
        if loc == "home" and plug == "on":
            return EV_IONIQ
        log.info("EV id: not the Ioniq (location=%r plug=%r)", loc, plug)
        return EV_OTHER

    def _ask_ioniq_dongle(self, ha: HomeAssistantAPI, generation: int) -> str | None:
        """Is the Ioniq on the cable?  Asked of its own OBD dongle.

        Returns OBD_PLUGGED, OBD_UNPLUGGED, OBD_ASLEEP, OBD_GONE, or None
        when the dongle cannot tell (gateway down, ELM mute, request refused
        or interrupted, no result in time, or a newer session took over).

        Two answers need a second read before they count:

        - "not plugged": on 2026-10-14 at 10:23:43 one well-formed 220105
          cleared the plug bit between two set ones, Ioniq plugged and locked;
        - "injoignable": the same evening, the first call after the gateway's
          Bluetooth started failed twice with the Ioniq parked right there.
          Asked again 30 s later; a second failure means it has left.

        The gateway keeps the link one minute after a reading, so the
        confirmation of a "not plugged" is immediate.
        """
        with self._dongle_lock:
            unplugged = unreachable = 0
            for _ in range(IONIQ_OBD_MAX_READS):
                if generation != self._ev_id_generation:
                    return None
                reading = self._read_ioniq_dongle_once(ha, generation)
                if reading is None:
                    return None
                result, attrs = reading
                if result == "ok":
                    if attrs.get("branche") is True:
                        return OBD_PLUGGED
                    unplugged += 1
                    if unplugged >= 2:
                        return OBD_UNPLUGGED
                    log.info("EV id: dongle reads the Ioniq unplugged (raw 0x%02X) — confirming",
                             attrs.get("brut") or 0)
                elif result == "endormie":
                    return OBD_ASLEEP
                elif result == "injoignable":
                    unreachable += 1
                    if unreachable >= 2:
                        return OBD_GONE
                    log.info("EV id: dongle unreachable — trying again in %d s", IONIQ_OBD_RETRY_S)
                    time.sleep(IONIQ_OBD_RETRY_S)
                else:
                    log.info("EV id: dongle cannot tell (%r)", result)
                    return None
            return None

    def _read_ioniq_dongle_once(self, ha: HomeAssistantAPI,
                                generation: int) -> tuple[str, dict] | None:
        """Press the gateway's read button and wait for this request's result.

        The result entity carries a counter that changes with every result,
        so even an identical answer moves its ``last_updated``.
        """
        before = ha.read_ioniq_obd_result()
        if before is None or before.get("state") == "unavailable":
            log.info("EV id: OBD gateway unavailable (%r)",
                     before and before.get("state"))
            return None
        try:
            ha.press_ioniq_obd_read()
        except Exception:
            log.warning("EV id: could not press the OBD read button", exc_info=True)
            return None
        start = time.monotonic()
        deadline = start + IONIQ_OBD_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(IONIQ_OBD_POLL_S)
            if generation != self._ev_id_generation:
                return None
            now = ha.read_ioniq_obd_result()
            if now and now.get("last_updated") != before.get("last_updated"):
                attrs = now.get("attributes") or {}
                log.info("EV id: dongle answered %r in %.0f s (plugged=%r)", now.get("state"),
                         time.monotonic() - start, attrs.get("branche"))
                return now.get("state"), attrs
        log.warning("EV id: no answer from the OBD gateway within %d s", IONIQ_OBD_TIMEOUT_S)
        return None

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
        self._ev_label = label
        try:
            (ha or self.ha).set_ev_connected(label)
        except Exception:
            log.warning("Failed to write ev_connected=%s", label, exc_info=True)

    def _full_speed_after_dark(self, s: dict) -> None:
        """Plug-in between sunset and sunrise: start the session in FULL_SPEED.

        Only the default mode is replaced: a mode the user picked before
        plugging in is theirs.  Once set, FULL_SPEED stays until the unplug,
        sunrise included, like a mode picked by hand.  Any car, so there is no
        waiting for the identification.  An unreadable sun leaves the mode
        alone.  An EMS start with a car already on the cable is not a plug-in
        and changes nothing.
        """
        mode = (s.get("ems_mode") or "").upper()
        if mode != config.DEFAULT_EMS_MODE:
            log.info("Plug-in: mode %s chosen beforehand — left untouched", mode)
            return
        sun = self.ha.get_text_state_safe(SUN_ENTITY)
        if sun != "below_horizon":
            log.info("Plug-in: sun %r — mode left untouched", sun)
            return
        log.info("Plug-in after sunset — ems_mode %s → FULL_SPEED", mode)
        try:
            self.ha.set_ems_mode("FULL_SPEED")
            s["ems_mode"] = "FULL_SPEED"
        except Exception:
            log.warning("Failed to set ems_mode to FULL_SPEED", exc_info=True)

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
                "Car unplugged per %s but EV still draws %.0f W — "
                "mode left untouched", status, s["ev_power"],
            )
            return
        log.info("Car unplugged (%s) — resetting ems_mode to %s",
                 status, config.DEFAULT_EMS_MODE)
        self._write_ev_connected(EV_NONE)
        try:
            self.ha.set_ems_mode(config.DEFAULT_EMS_MODE)
            s["ems_mode"] = config.DEFAULT_EMS_MODE
        except Exception:
            log.warning("Failed to reset ems_mode", exc_info=True)

    # Gateway charger_status labels (r_sta.charger_status, as named by the
    # wallbox_gateway integration) that say a car is on the cable, and the two
    # that say none is.  Measured 2026-09-24 → 10-01 against the cloud
    # charging_status (SMART_CONTROL_*): Locked 0.03 h plugged / 120 h not,
    # "Ready" chatters with Locked for hours after an unplug, and every other
    # label seen (Charging, Connected — …, OCPP charge finishing) only with a
    # car.  Anything else (Error, Updating, …) is inconclusive.
    def _tesla_cable_says_unplugged(self) -> bool:
        """Does the Tesla itself report its cable out?

        Asked independently of the gateway, because the gateway can no longer
        be beaten from behind: since the 2026-10-11 firmware fix its flag
        waits for st and the label to agree, so the window this used to key on
        — flag off while the label still named a car — no longer exists.  On
        the 20:48:41 unplug that night the flag only dropped at 20:48:56 and
        ev_connected followed at 20:49:04, 23 s after the cable.

        Two entities, because binary_sensor.martine_charge_cable follows the
        *latch* and the car cannot tell "unlatched" from "removed": the charger
        releases the latch with the cable still in, at the end of a session and
        right through a firmware update, and the sensor then reads 'off' for
        about a minute.  cover.martine_charge_port_door only closes once the
        plug is physically out — 16 real unplugs closed it within 0-30 s
        (2026-10-18..25), 6 latch releases left it open.

        Neither is power-based, which matters: the cable held through the five
        charge pauses of the 09-11 night, where the Shelly or the Ioniq's OBD
        dongle would have reported an unplug at each one.

        Polled every TESLA_CABLE_POLL_S rather than every tick, and only while
        the Tesla is the car on the cable, so the Tesla entities stay out of
        the 1 Hz batch as agreed.  An HA state read never reaches Tessie
        (verified 2026-09-11, 200 reads in 2 s moved no last_updated).
        """
        now = time.monotonic()
        if now - self._tesla_cable_ts >= TESLA_CABLE_POLL_S:
            self._tesla_cable_ts = now
            try:
                cable = self.ha.read_tesla_cable()
                door = self.ha.read_tesla_charge_port_door()
                self._tesla_cable_off = cable == "off" and door == "closed"
                if cable == "off" and door == "open":
                    log.info("binary_sensor.martine_charge_cable reads 'off' but "
                             "cover.martine_charge_port_door is still open — the "
                             "charger released the latch, the cable is still in")
                elif cable == "off" and door != "closed":
                    log.info("binary_sensor.martine_charge_cable reads 'off' and "
                             "cover.martine_charge_port_door reads %r — no verdict, "
                             "session kept", door)
            except Exception:
                log.warning("Could not read the Tesla cable and port door",
                            exc_info=True)
                self._tesla_cable_off = False
        # The answer is cached between polls, not withheld: an edge has to be
        # read on two consecutive ticks, so returning False while throttled
        # would mean the witness never got confirmed and never concluded.
        return self._tesla_cable_off

    def _reset_mode_for_new_session(self, s: dict) -> None:
        """Hand a new session the default mode, no questions asked.

        Unlike :meth:`_reset_mode_on_unplug` this skips the ev_power veto: the
        charger has just announced a new session, which settles the question of
        whether the previous one ended.
        """
        log.info("New session — resetting ems_mode to %s", config.DEFAULT_EMS_MODE)
        try:
            self.ha.set_ems_mode(config.DEFAULT_EMS_MODE)
            s["ems_mode"] = config.DEFAULT_EMS_MODE
        except Exception:
            log.warning("Failed to reset ems_mode", exc_info=True)

    def _on_new_session_marker(self, s: dict) -> None:
        """The charger announces a new session while a car is still on record.

        A real swap, or the charger relabelling a session in progress?  Both
        happen: of the four times the label arrived mid-session from 09-24 to
        10-14, three were genuine swaps (10-06 09:32, 09:34 and 10:09, none of
        them through Locked, the flag twice never dropping) and one was not
        (10-14 10:21: the Ioniq's charger paused, nobody touched the cable,
        and FULL_SPEED was wiped).  Neither the previous label nor the flag
        tells them apart; the car on record does, by its own sensor:

        - Tesla: its cable sensor still "on" means it is still plugged;
        - Ioniq (or unknown, which on 10-14 was the Ioniq every time): its
          OBD dongle is asked in a thread.  In doubt — asleep, no answer —
          the session is kept: a car just unplugged is awake and answers,
          and the next charge restarts the relevé and corrects us if needed;
        - any other car has no witness: the swap is taken, as before.
        """
        label = self._ev_label
        if label == EV_TESLA:
            cable = self.ha.read_tesla_cable()
            door = self.ha.read_tesla_charge_port_door()
            # binary_sensor.martine_charge_cable alone is not enough here: it
            # follows the latch, and the charger releases the latch with the
            # cable still in — at the end of a session and throughout a
            # charger firmware update.  On 2026-10-25 at 14:04:09 that read
            # "off" for 60 s while sensor.wallbox_192_168_8_188_charger_status
            # had just announced a new session, and this check closed the
            # session and reset input_select.ems_mode on a Tesla that had
            # never moved.  cover.martine_charge_port_door separates the two:
            # 16 real unplugs closed it within 0-30 s, 6 latch releases left
            # it open.  And it closes well before the marker arrives — 27 s
            # ahead on the real swap of 10-15 17:33 — so requiring it costs
            # nothing here.
            if cable == "on" or door != "closed":
                log.info("Gateway announces a new session (%r) but the Tesla still "
                         "holds the cable (binary_sensor.martine_charge_cable=%r, "
                         "cover.martine_charge_port_door=%r) — session kept",
                         GW_NEW_SESSION, cable, door)
                return
            log.info("Gateway announces a new session (%r), the Tesla's cable reads %r "
                     "and its port door is closed — treating it as a swap",
                     GW_NEW_SESSION, cable)
            self._close_session_for_swap(s)
        elif label in (EV_IONIQ, EV_UNKNOWN):
            if self._swap_check_running:
                log.info("Gateway announces a new session again — swap check already running")
                return
            log.info("Gateway announces a new session (%r) while %r is on record — "
                     "asking the Ioniq before closing anything", GW_NEW_SESSION, label)
            self._swap_check_running = True
            threading.Thread(target=self._swap_check_blocking,
                             args=(self._ev_id_generation, label),
                             name="ev-swap-check", daemon=True).start()
        else:
            log.info("Gateway announces a new session (%r) while %r is on record "
                     "— treating it as a swap", GW_NEW_SESSION, label)
            self._close_session_for_swap(s)

    def _unplug_survives_recovery_doubt(self) -> bool:
        """Does the car on record agree it has left, so soon after a recovery?

        Same witnesses as :meth:`_on_new_session_marker`, same bias: in doubt
        the session is kept, because a mode wiped by mistake costs the user
        more than a reset that lands a minute late.
        """
        label = self._ev_label
        if label == EV_TESLA:
            cable = self.ha.read_tesla_cable()
            if cable == "on":
                log.info("Gateway just came back and reads no car, but the Tesla's "
                         "cable is still plugged — session kept")
                return False
            log.info("Gateway just came back and the Tesla's cable reads %r — "
                     "unplug accepted", cable)
            return True
        if label in (EV_IONIQ, EV_UNKNOWN):
            if not self._swap_check_running:
                log.info("Gateway just came back and reads no car while %r is on "
                         "record — asking the Ioniq before closing anything", label)
                self._swap_check_running = True
                threading.Thread(target=self._swap_check_blocking,
                                 args=(self._ev_id_generation, label),
                                 name="ev-recovery-check", daemon=True).start()
            return False          # the thread's verdict closes the session if it must
        return True               # no witness for any other car

    def _swap_check_blocking(self, generation: int, label: str) -> None:
        """Thread body: ask the Ioniq's dongle, hand the verdict to tick()."""
        ha = HomeAssistantAPI(config.HA_URL, config.HA_TOKEN)
        try:
            dongle = self._ask_ioniq_dongle(ha, generation)
            if dongle == OBD_PLUGGED:
                log.info("Swap check: the Ioniq is still plugged — session kept")
                if label == EV_UNKNOWN:
                    self._write_ev_connected(EV_IONIQ, ha, generation)
                accept = False
            elif dongle in (OBD_UNPLUGGED, OBD_GONE):
                log.info("Swap check: the Ioniq is %s — it is a swap", dongle)
                accept = True
            else:
                log.info("Swap check: no clear answer from the Ioniq (%s) — session kept",
                         dongle)
                accept = False
            self._swap_verdict = (generation, accept)
        except Exception:
            log.warning("Swap check failed — session kept", exc_info=True)
        finally:
            self._swap_check_running = False

    def _close_session_for_swap(self, s: dict) -> None:
        """End the session on record; the plug-in edge then identifies the newcomer."""
        self._ev_id_generation += 1
        self._write_ev_connected(EV_NONE)
        self._reset_mode_for_new_session(s)
        self._car_connected = False
        self._plug_seen = None

    _GW_NO_CAR = ("Locked", "Ready")
    _GW_CAR = ("Charging", "Paused", "Schedule end", "Queued by Power Boost",
               "Queued by Eco-Smart", "Discharging", "OCPP charge finishing",
               "MID safety margin exceeded")

    @classmethod
    def _gw_label_names_car(cls, label: str | None) -> bool:
        """Does sensor.wallbox_192_168_8_188_charger_status name a car?"""
        return label in cls._GW_CAR or (label or "").startswith("Connected")

    @classmethod
    def _car_plugged(cls, s: dict) -> bool | None:
        """Is a car plugged in?  ``None`` when the answer is not conclusive.

        Read from the BLE gateway, not from the cloud ``status_description``,
        which polls every 90 s and can freeze (2026-09-25 16:41: stuck on
        "Ready" while the Tesla charged at 1.2 kW, so no plug-in edge).

        Two gateway fields, and neither is enough alone:

        - ``car_connected`` is the firmware's own flag, from ``r_dat.st``.  It
          never claimed a car that was not there, but it drops to off with the
          car still plugged when the charger reports a code missing from its
          list: st 15 at the end of a charge (10-01 19:17 local, Ioniq full,
          still plugged — ev_connected went to "disconnected" and the mode the
          user had picked five minutes earlier was reset), and 12 s blips at
          the start of a charge (09-30 16:29, 10-01 18:59).  It also reads off before the gateway has any data.
        - the ``charger_status`` label comes from a separate read
          (``r_sta.charger_status``).  In all three false offs it still named
          a car ("OCPP charge finishing", "Connected — …").

        So: a car if either says so; no car only if the flag is off **and**
        the label is Locked or Ready.  A real unplug is then seen once the
        label follows, 10 to 26 s after the flag.
        """
        label = s.get("gw_charger_status")
        flag = s.get("gw_car_connected")
        if flag == "on" or cls._gw_label_names_car(label):
            return True
        if flag == "off" and label in cls._GW_NO_CAR:
            return False
        return None

    _CLOUD_CAR = ("Charging", "Locked, car connected", "Waiting",
                  "Waiting for car demand")
    _CLOUD_NO_CAR = ("Locked", "Ready")

    @classmethod
    def _car_plugged_cloud(cls, s: dict) -> bool | None:
        """Is a car plugged in, while the gateway has no reading at all?

        The cloud ``status_description`` drove the detection until 2026-09-25
        and was dropped for lagging (90 s polling) and once freezing.  Without
        it, a gateway outage froze the plug state: on 2026-10-21 the gateway
        was off the network 15:08 → 17:15, the Ioniq plugged in at 17:09 went
        unseen — no identification, no plug-in rule — until it came back.

        Replayed over 10 days (10-11 → 10-21) against the gateway's verdict:
        the cloud never said "no car" mid-session.  Its only disagreements
        are lags — 2 to 117 s behind each plug-in, and once "Charging" 16 min
        after an unplug (10-18 12:56) — which here only delay an edge.

        Power flowing proves a car whatever the cloud says: on 09-25 it sat on
        "Ready" while the Tesla charged.  An unplug read here still goes
        through the ``ev_power`` veto of :meth:`_reset_mode_on_unplug`.
        """
        if s["ev_power"] > config.EV_CHARGING_DETECT_W:
            return True
        label = s.get("wallbox_status")
        if label in cls._CLOUD_CAR:
            return True
        if label in cls._CLOUD_NO_CAR:
            return False
        return None

    # -- state evaluation -----------------------------------------------------

    def _leave_storage_on_low_soc(self, s: dict) -> State:
        """SOC floor reached in STORAGE_ONLY: fall back to SOLAR_ONLY.

        STORAGE_ONLY promises nothing from the grid, and SOLAR_ONLY keeps that
        promise once the battery is spent.  The helper is rewritten so the
        dashboard shows it, and the choice sticks: the EMS does not return to
        STORAGE_ONLY when the SOC recovers.  This overrides what the user
        picked, so it targets SOLAR_ONLY literally rather than
        ``DEFAULT_EMS_MODE``: it is a battery protection, not the end of a
        session like :meth:`_reset_mode_on_unplug`.

        (FULL_SPEED needs no such switch: it handles the floor itself.)
        """
        self.ha.set_ems_mode("SOLAR_ONLY")
        s["ems_mode"] = "SOLAR_ONLY"
        log.info("SOC %.0f%% <= discharge_limit %.0f%% — STORAGE_ONLY → SOLAR_ONLY",
                 s["battery_soc"], s["discharge_limit"])
        if s["solar_power"] > config.SOLAR_AVAILABLE_W:
            return State.SOLAR_ONLY
        return State.EV_NO_SOLAR

    def _determine_target_state(self, s: dict) -> State:
        """Determine the target state based on current sensor readings."""
        ev_charging = s["ev_power"] > config.EV_CHARGING_DETECT_W

        if not ev_charging:
            return State.IDLE

        mode = s.get("ems_mode", "SOLAR_ONLY").upper()

        # STORAGE_BOOSTED was merged into FULL_SPEED on 2026-10-06; a helper
        # still offering the old option must not fall through to SOLAR_ONLY.
        if mode in ("FULL_SPEED", "STORAGE_BOOSTED"):
            return State.FULL_SPEED


        if mode == "STORAGE_ONLY":
            if s["battery_soc"] <= s["discharge_limit"]:
                return self._leave_storage_on_low_soc(s)
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
            self._enter_battery_priority()
        elif target == State.SOLAR_ONLY:
            self._enter_solar_only()
        elif target == State.SOLAR_BOOSTED:
            self._enter_solar_boosted()
        elif target == State.STORAGE_ONLY:
            self._enter_storage_only()

    # -- per-tick logic -------------------------------------------------------

    def _filter_soc(self, s: dict) -> None:
        """Hold the last trusted SOC through a phantom jump (see SOC_GLITCH_JUMP_PCT)."""
        soc = s["battery_soc"]
        if self._soc_trusted is None or abs(soc - self._soc_trusted) < SOC_GLITCH_JUMP_PCT:
            if self._soc_jump_since is not None:
                log.info("SOC jump gone after %.0f s — back to %.0f%%",
                         time.monotonic() - self._soc_jump_since, soc)
            self._soc_trusted = soc
            self._soc_jump_since = None
            return
        now = time.monotonic()
        if self._soc_jump_since is None:
            self._soc_jump_since = now
            log.warning("SOC jump %.0f%% → %.0f%% ignored (BMS asleep?)", self._soc_trusted, soc)
        if now - self._soc_jump_since >= SOC_GLITCH_HOLD_S:
            log.warning("SOC jump held %d s — accepting %.0f%%", SOC_GLITCH_HOLD_S, soc)
            self._soc_trusted = soc
            self._soc_jump_since = None
            return
        s["battery_soc"] = self._soc_trusted

    def tick(self, s: dict) -> None:
        """Called every fast-loop iteration with fresh sensor data *s*."""

        self._filter_soc(s)

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
        prev_gw = self._last_gw_status
        if gw_status != self._last_gw_status:
            log.info("Gateway status: %r → %r (car_connected=%s)",
                     self._last_gw_status, gw_status, s.get("gw_car_connected"))
            self._last_gw_status = gw_status
        status = f"gateway: {gw_status!r}"
        if gw_label_absent(prev_gw) and not gw_label_absent(gw_status):
            self._gw_back_ts = time.monotonic()
            log.info("Gateway readings are back (%r) — an unplug needs the car's "
                     "own confirmation for the next %d s", gw_status, GW_RECOVERY_DOUBT_S)

        # A swap can be quicker than the unplug takes to confirm: the label
        # trails the flag by up to 19 s, so putting the second car on the
        # cable inside that window leaves BOTH edges unseen — the previous
        # car's identification and the mode chosen for it would carry over.
        # The charger announces the new session itself — but it also does so
        # mid-session, so the car on record is asked first (see
        # _on_new_session_marker); on a swap the old session is closed and
        # the normal plug-in edge identifies the newcomer.
        if (gw_status == GW_NEW_SESSION and prev_gw != GW_NEW_SESSION
                and self._car_connected is True):
            self._on_new_session_marker(s)
        # Verdict of a swap check that had to ask the Ioniq (thread).
        verdict, self._swap_verdict = self._swap_verdict, None
        if verdict is not None:
            generation, accept = verdict
            if accept and generation == self._ev_id_generation and self._car_connected is True:
                self._close_session_for_swap(s)

        plugged = self._car_plugged(s)
        if plugged is None and gw_label_absent(gw_status):
            plugged = self._car_plugged_cloud(s)
            status = (f"cloud: {s.get('wallbox_status')!r}, "
                      f"{s['ev_power']:.0f} W — gateway {gw_status!r}")
        # Tesla session: the car's own word, now that it takes two entities to
        # say it — see _tesla_cable_says_unplugged().  This replaces the guard
        # added on 10-23, which demanded that
        # sensor.wallbox_192_168_8_188_charger_status stop naming a car and so
        # left the witness nearly inert: over 10 885 samples since 10-11 that
        # window opened nine times, all of them plug-in transients.  The port
        # door blocks the same false unplugs — it stayed 'open' through every
        # latch release — while letting a real one through in 0 to 12 s
        # instead of the gateway's 15 to 23 s.
        if (plugged is True and self._car_connected is True
                and self._ev_label == EV_TESLA
                and self._tesla_cable_says_unplugged()):
            log.info("Tesla reports its cable out — unplug confirmed ahead of "
                     "the gateway (flag=%r label=%r)",
                     s.get("gw_car_connected"), gw_status)
            plugged = False
        # An unplug read just after the gateway came back is not trusted on the
        # gateway's word alone — see GW_RECOVERY_DOUBT_S.  Asking the car on
        # record is the same question the swap check asks, so it reuses it.
        if (plugged is False and self._car_connected is True
                and self._gw_back_ts
                and time.monotonic() - self._gw_back_ts < GW_RECOVERY_DOUBT_S
                and not self._unplug_survives_recovery_doubt()):
            plugged = None
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
                log.info("Car plugged in (%s)", status)
                self._full_speed_after_dark(s)
                self._identify_plugged_car()
            elif not plugged and self._car_connected is True:
                self._reset_mode_on_unplug(s, status)
            elif self._car_connected is None:
                # First conclusive reading since start: no edge, but
                # ev_connected may be stale from before the restart.  The
                # mode is not touched either way.
                log.info("Car %s at startup (%s)",
                         "plugged" if plugged else "not plugged", status)
                if plugged:
                    self._identify_plugged_car()
                else:
                    self._write_ev_connected(EV_NONE)
            if plugged != self._car_connected:
                # A new session must not inherit the previous one's cable
                # reading: a stale "off" would fire the moment the Tesla is
                # identified again.
                self._tesla_cable_ts = 0.0
                self._tesla_cable_off = False
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
                # Above discharge_limit: the battery is dosed so the grid pays a
                # fixed share of the car (BOOSTED_GRID_RATIO_*: 60 % peak, 50 %
                # off-peak); the battery covers the rest plus the house, less
                # the sun.  This was STORAGE_BOOSTED, merged here on 2026-10-06
                # — until then FULL_SPEED emptied the battery at 4.6 kW first.
                off_peak = is_off_peak()
                grid_ratio = config.BOOSTED_GRID_RATIO_OFF_PEAK if off_peak else config.BOOSTED_GRID_RATIO_PEAK
                amps = self._compute_full_speed_discharge(s, grid_ratio)
                self._set_max_discharging(amps)
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

        elif self.state == State.STORAGE_ONLY:
            # No SOC floor here: _determine_target_state leaves the mode for
            # SOLAR_ONLY as soon as SOC <= discharge_limit.
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
        self._check_wallbox_override(s)

        # 6. Dusk/sunrise tracking for overnight range
        self._track_overnight_range(s)

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
