"""Wallbox charge-current setpoint: local BLE gateway first, cloud as fallback.

Two HA integrations can set the Pulsar Max current:

- the cloud one (``wallbox``, polled every 90 s through wallbox.com);
- the local BLE gateway (``wallbox_gateway``, an ESP32 next to the charger).

Both write the same setting in the charger, so there is only ever one value
to reason about.  The gateway is preferred, the cloud takes over whenever the
gateway cannot prove it delivered.

**Why a read-back and not just the service call's result.** When the gateway
is reachable but the charger does not answer over BLE (unpaired, out of range,
link held by the phone app), the gateway replies ``202 pending`` and the HA
integration reports success.  Seen on 2026-09-24 before the SMP passkey was
set: BLE "connected", 200+ requests sent, zero received.  So a BLE write only
counts once ``number.<gateway>_max_current`` shows it — and that entity only
has a value once the charger has answered at least once.

Only the setpoint goes through here.  The plug state is read from the
gateway directly by ``EMS._car_plugged`` — with no cloud fallback.
"""

import logging
import time

from ha_api import GW_BLE_CONNECTED, GW_NUMBER_MAX_CURRENT, HomeAssistantAPI

log = logging.getLogger("ems.wallbox")

# Module constants rather than config.py entries, for the same reason as the
# discharge policy in ems.py: the Pi's config.py is never updated by git pull.
BLE_VERIFY_S = 20   # time the gateway gets to show a write in its read-back
BLE_RETRY_S = 300   # how long the BLE path stays benched after a failure


class WallboxCurrent:
    def __init__(self, ha: HomeAssistantAPI):
        self.ha = ha
        self._benched_until = 0.0   # monotonic ts before which BLE is not tried
        self._pending = None        # (amps, monotonic ts) of an unverified BLE write
        self._path = None           # "ble" | "cloud" — to log changes only

    def set_current(self, amps: int) -> str:
        """Write the setpoint and return the path used ("ble" or "cloud").

        A failure of the cloud write propagates, as it did before this layer
        existed; a failure of the BLE path never does — it falls back.
        """
        now = time.monotonic()
        self.poll(now, replay=False)   # the new value supersedes a lost one
        if now >= self._benched_until:
            reason = self._ble_unusable()
            if reason is None:
                try:
                    self.ha.set_wallbox_current_ble(amps)
                except Exception as exc:
                    self._bench(now, f"write rejected ({exc})")
                    reason = "BLE write rejected"
                else:
                    self._pending = (amps, now)
                    self._use("ble", "")
                    return "ble"
        else:
            reason = "BLE benched after a failure"
        # A BLE write still awaiting its read-back is now stale: never replay
        # it over this newer value.
        self._pending = None
        self.ha.set_wallbox_current(amps)
        self._use("cloud", reason)
        return "cloud"

    def poll(self, now: float | None = None, replay: bool = True) -> None:
        """Check the last BLE write once it has had time to land.

        Called every fast tick so that a lost write is caught ~20 s later even
        when no other write follows (MANUAL mode writes once, then hands off).
        A lost write is replayed through the cloud.
        """
        if self._pending is None:
            return
        now = time.monotonic() if now is None else now
        amps, ts = self._pending
        if now - ts < BLE_VERIFY_S:
            return
        self._pending = None
        seen = self._read_setpoint()
        if seen == amps:
            return
        self._bench(now, f"{amps} A not confirmed after {BLE_VERIFY_S} s (gateway reports {seen})")
        if replay:
            self.ha.set_wallbox_current(amps)
            self._use("cloud", "BLE write not confirmed")

    # -- helpers --------------------------------------------------------------

    def _ble_unusable(self) -> str | None:
        """Why the BLE path cannot be used right now, or None if it can."""
        link = self._state(GW_BLE_CONNECTED)
        if link != "on":
            return f"gateway BLE link {link}"
        if self._read_setpoint() is None:
            # "unknown" until the charger has answered once: connected but
            # unpaired looks exactly like this.
            return "gateway has no data from the charger"
        return None

    def _read_setpoint(self) -> int | None:
        try:
            return int(float(self._state(GW_NUMBER_MAX_CURRENT)))
        except (TypeError, ValueError):
            return None

    def _state(self, entity_id: str) -> str | None:
        return self.ha.get_text_state_quiet(entity_id)

    def _bench(self, now: float, why: str) -> None:
        self._benched_until = now + BLE_RETRY_S
        self._pending = None
        log.warning(
            "WALLBOX BLE: %s — cloud fallback for %d s", why, BLE_RETRY_S,
        )

    def _use(self, path: str, reason: str) -> None:
        if path == self._path:
            return
        if path == "ble":
            log.info("WALLBOX current now set through the BLE gateway")
        else:
            log.info("WALLBOX current now set through the cloud (%s)", reason)
        self._path = path
