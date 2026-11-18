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

**The cloud path is read back too.**  A cloud write is not checked by the
service call either, and on 2026-11-18, with the gateway down, FULL_SPEED's
32 A was lost: three cloud writes in 6 s (32, 6, 32 A at 20:03:42-48), the
charger ended at 6 A, and FULL_SPEED writes only once on entry, so the Tesla
charged at 6 A for over an hour.  HA shows a written value at once
(optimistic) and the cloud is polled every 90 s, so the check waits
CLOUD_VERIFY_S, long enough for at least one real poll.

Only the setpoint goes through here.  The plug state is read from the
gateway directly by ``EMS._car_plugged``, and from the cloud only while the
gateway has no reading at all (``EMS._car_plugged_cloud``).
"""

import logging
import time

from ha_api import (
    GW_BLE_CONNECTED,
    GW_NUMBER_MAX_CURRENT,
    NUMBER_WALLBOX_MAX_CURRENT,
    HomeAssistantAPI,
)

log = logging.getLogger("ems.wallbox")

# Module constants rather than config.py entries, for the same reason as the
# discharge policy in ems.py: the Pi's config.py is never updated by git pull.
BLE_VERIFY_S = 20   # time the gateway gets to show a write in its read-back
BLE_RETRY_S = 300   # how long the BLE path stays benched after a failure
CLOUD_VERIFY_S = 180    # > the cloud integration's 90 s poll, so HA shows the charger's value
CLOUD_MAX_RESENDS = 3   # then give up on that value until the next write


class WallboxCurrent:
    def __init__(self, ha: HomeAssistantAPI):
        self.ha = ha
        self._benched_until = 0.0   # monotonic ts before which BLE is not tried
        self._pending = None        # (amps, monotonic ts) of an unverified BLE write
        self._strikes = 0           # consecutive read-backs that did not match
        self._path = None           # "ble" | "cloud" — to log changes only
        self._cloud_pending = None  # (amps, monotonic ts, re-sends) of an unverified cloud write

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
                    self._cloud_pending = None   # the BLE read-back takes over
                    self._use("ble", "")
                    return "ble"
        else:
            reason = "BLE benched after a failure"
        # A BLE write still awaiting its read-back is now stale: never replay
        # it over this newer value.
        self._pending = None
        self._cloud_write(amps, now)
        self._use("cloud", reason)
        return "cloud"

    def poll(self, now: float | None = None, replay: bool = True) -> None:
        """Check the last BLE write once it has had time to land.

        Called every fast tick so that a lost write is caught ~20 s later even
        when no other write follows (FULL_SPEED writes 32 A once, then hands off).

        A first mismatch is re-sent over BLE, not blamed on the gateway: the
        charger resets its own current to 32 A at every unplug (5 unplugs out
        of 5 since pairing), so a correct write can be overwritten under us.
        Only a second mismatch in a row benches BLE and replays the value
        through the cloud.
        """
        now = time.monotonic() if now is None else now
        self._poll_cloud(now, replay)
        if self._pending is None:
            return
        amps, ts = self._pending
        if now - ts < BLE_VERIFY_S:
            return
        self._pending = None
        seen = self._read_setpoint()
        if seen == amps:
            self._strikes = 0
            return
        self._strikes += 1
        why = f"{amps} A not confirmed after {BLE_VERIFY_S} s (gateway reports {seen})"
        if self._strikes < 2:
            if not replay:
                return   # set_current() is about to write a newer value anyway
            log.info("WALLBOX BLE: %s — re-sending over BLE", why)
            try:
                self.ha.set_wallbox_current_ble(amps)
            except Exception as exc:
                why = f"re-send rejected ({exc})"
            else:
                self._pending = (amps, now)
                return
        self._bench(now, why)
        if replay:
            self._cloud_write(amps, now)
            self._use("cloud", "BLE write not confirmed")

    def _poll_cloud(self, now: float, replay: bool) -> None:
        """Re-send a cloud write the charger does not show CLOUD_VERIFY_S later.

        An unreadable cloud value (integration down) proves nothing: wait for
        the next check without re-sending, within the same budget.
        """
        # From set_current() (replay=False) leave it to _cloud_write(): a new
        # value replaces the check, the same value keeps it running.
        if self._cloud_pending is None or not replay:
            return
        amps, ts, resends = self._cloud_pending
        if now - ts < CLOUD_VERIFY_S:
            return
        seen = self._read_int(NUMBER_WALLBOX_MAX_CURRENT)
        if seen == amps:
            self._cloud_pending = None
            return
        if resends >= CLOUD_MAX_RESENDS:
            self._cloud_pending = None
            log.warning(
                "WALLBOX cloud: %d A still not applied after %d re-sends (cloud reports %s) — giving up",
                amps, resends, seen,
            )
            return
        if seen is None:
            self._cloud_pending = (amps, now, resends + 1)
            return
        log.info(
            "WALLBOX cloud: %d A not applied after %d s (cloud reports %d A) — re-sending",
            amps, CLOUD_VERIFY_S, seen,
        )
        self.ha.set_wallbox_current(amps)
        self._cloud_pending = (amps, now, resends + 1)

    def _cloud_write(self, amps: int, now: float) -> None:
        self.ha.set_wallbox_current(amps)
        # Steering modes re-write the same value every slow tick: keep the
        # original timestamp so the check still comes due.
        if self._cloud_pending is None or self._cloud_pending[0] != amps:
            self._cloud_pending = (amps, now, 0)

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
        return self._read_int(GW_NUMBER_MAX_CURRENT)

    def _read_int(self, entity_id: str) -> int | None:
        try:
            return int(float(self._state(entity_id)))
        except (TypeError, ValueError):
            return None

    def _state(self, entity_id: str) -> str | None:
        return self.ha.get_text_state_quiet(entity_id)

    def _bench(self, now: float, why: str) -> None:
        self._benched_until = now + BLE_RETRY_S
        self._pending = None
        self._strikes = 0
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
