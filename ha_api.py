"""Home Assistant REST API wrapper for reading sensors and writing actuators."""

import logging
import requests

logger = logging.getLogger(__name__)

# HA entity IDs
SENSOR_BATTERY_SOC = "sensor.deye_battery"
SENSOR_BATTERY_VOLTAGE = "sensor.deye_battery_voltage"
SENSOR_BATTERY_POWER = "sensor.deye_battery_power"
SENSOR_GRID_POWER = "sensor.shellyem_34945478aee1_channel_1_power"
SENSOR_MICROINVERTER_POWER = "sensor.deye_microinverter_power"
SENSOR_GRID_VOLTAGE = "sensor.deye_load_l1_voltage"
SENSOR_EV_POWER = "sensor.shellyem_34945478aee1_channel_2_power"
SENSOR_BATT_CHARGE_LIMIT = "input_number.batt_charge_limit"
SENSOR_BATT_CHARGE_PRIO = "input_number.batt_charge_prio"
SENSOR_DISCHARGE_LIMIT = "input_number.discharge_limit"
SENSOR_EMS_MODE = "input_select.ems_mode"
SENSOR_WALLBOX_STATUS = "sensor.wallbox_pulsar_max_sn_429953_status_description"
INPUT_GRID_RATIO = "input_number.grid_ratio_value"
INPUT_RANGE_NEEDED = "input_number.range_needed_over_night"

# Tesla (Tessie integration) — used by the SOLAR_ONLY solar-pause feature.
TESLA_LOCATION = "device_tracker.martine_location"        # "home" when parked at home
TESLA_CHARGING = "sensor.martine_charging"                # starting|charging|stopped|complete|disconnected|no_power
TESLA_CHARGE_CABLE = "binary_sensor.martine_charge_cable"  # "on" = cable plugged in
SWITCH_TESLA_CHARGE = "switch.martine_charge"             # on = car allowed to charge

# Hyundai Ioniq (Kia Uvo / Bluelink integration, device "MONIQ") — used to
# identify which car was just plugged in.  Beware: a second, dead set of
# entities named ``ioniq_*`` still exists in HA, frozen since 2026-09-11; the
# live ones are ``moniq_*``.
IONIQ_LOCATION = "device_tracker.moniq_location"          # "home" when parked at home
IONIQ_PLUG = "binary_sensor.moniq_ev_battery_plug"        # "on" = cable plugged in
IONIQ_DATA_TS = "sensor.moniq_location_last_updated"      # ISO ts of the vehicle payload
IONIQ_DEVICE_ID = "902392daab65cd8ce308c9571af29bad"      # kia_uvo services target a device

INPUT_EV_CONNECTED = "input_text.ev_connected"

NUMBER_MAX_CHARGING_CURRENT = "number.deye_battery_max_charging_current"
NUMBER_MAX_DISCHARGING_CURRENT = "number.deye_battery_max_discharging_current"
NUMBER_WALLBOX_MAX_CURRENT = "number.wallbox_pulsar_max_sn_429953_maximum_charging_current"

# Local BLE gateway (ESP32-S3 running botts7/esp32-wallbox, HACS integration
# ``wallbox_gateway``).  The entity ids embed the gateway's IP: they change if
# its DHCP lease does.  The current setpoint goes through it first (cloud as
# fallback, see wallbox_current.py) and the plug state is read from it only;
# SENSOR_WALLBOX_STATUS (cloud) is kept for the log.
GW_BLE_CONNECTED = "binary_sensor.wallbox_192_168_8_188_ble_connected"
GW_NUMBER_MAX_CURRENT = "number.wallbox_192_168_8_188_max_current"
GW_CAR_CONNECTED = "binary_sensor.wallbox_192_168_8_188_car_connected"
GW_CHARGER_STATUS = "sensor.wallbox_192_168_8_188_charger_status"


class HomeAssistantAPI:
    """Thin wrapper around the HA REST API."""

    def __init__(self, url: str, token: str):
        self._base = url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    # -- read helpers ----------------------------------------------------------

    def get_state(self, entity_id: str) -> float:
        """Return the numeric state of an entity. Raises on error."""
        resp = self._session.get(
            f"{self._base}/api/states/{entity_id}", timeout=10
        )
        resp.raise_for_status()
        value = resp.json()["state"]
        return float(value)

    def get_text_state(self, entity_id: str) -> str:
        """Return the string state of an entity (for input_select, etc.)."""
        resp = self._session.get(
            f"{self._base}/api/states/{entity_id}", timeout=10
        )
        resp.raise_for_status()
        return resp.json()["state"]

    def get_text_state_safe(self, entity_id: str) -> str | None:
        """Like :meth:`get_text_state` but never raises.

        Used for the Tesla entities: they come from a cloud integration
        (Tessie) that can disappear or return ``unavailable``.  A failure there
        must not take down the whole tick — the caller treats ``None`` as
        "unknown" and simply does nothing.
        """
        try:
            return self.get_text_state(entity_id)
        except Exception:
            logger.warning("Could not read %s", entity_id, exc_info=True)
            return None

    def get_text_state_quiet(self, entity_id: str) -> str | None:
        """Like :meth:`get_text_state_safe` but without logging.

        For entities read at 1 Hz whose absence is survivable: a removed
        integration would otherwise log a traceback every second.
        """
        try:
            return self.get_text_state(entity_id)
        except Exception:
            return None

    def read_all_sensors(self) -> dict:
        """Read every sensor needed in one batch and return a dict."""
        return {
            "ev_power": self.get_state(SENSOR_EV_POWER),
            "solar_power": self.get_state(SENSOR_MICROINVERTER_POWER),
            "battery_soc": self.get_state(SENSOR_BATTERY_SOC),
            "battery_voltage": self.get_state(SENSOR_BATTERY_VOLTAGE),
            "battery_power": self.get_state(SENSOR_BATTERY_POWER),
            "grid_power": self.get_state(SENSOR_GRID_POWER),
            "grid_voltage": self.get_state(SENSOR_GRID_VOLTAGE),
            "batt_charge_limit": self.get_state(SENSOR_BATT_CHARGE_LIMIT),
            "batt_charge_prio": self.get_state(SENSOR_BATT_CHARGE_PRIO),
            "discharge_limit": self.get_state(SENSOR_DISCHARGE_LIMIT),
            "ems_mode": self.get_text_state(SENSOR_EMS_MODE),
            "wallbox_status": self.get_text_state(SENSOR_WALLBOX_STATUS),
            "gw_car_connected": self.get_text_state_quiet(GW_CAR_CONNECTED),
            "gw_charger_status": self.get_text_state_quiet(GW_CHARGER_STATUS),
            # Read-back of our own setpoint: the inverter does not always keep
            # what we write (see _reconcile_discharge in ems.py).
            "max_discharging_actual": self.get_state(NUMBER_MAX_DISCHARGING_CURRENT),
        }

    def read_tesla_state(self) -> dict:
        """Read the Tesla entities — deliberately NOT part of read_all_sensors.

        These are Home Assistant state-machine reads: HA answers from its
        in-memory cache, no request reaches Tessie and the car is never woken
        (verified 2026-09-11: 200 reads in 2 s moved no ``last_updated``).  The
        EMS still asks only when it is about to act, so the 1 Hz loop generates
        no Tesla traffic at all in normal operation — see
        ``_tesla_solar_pause`` in ems.py.

        Every value may be None (cloud integration down).
        """
        return {
            "tesla_location": self.get_text_state_safe(TESLA_LOCATION),
            "tesla_charging": self.get_text_state_safe(TESLA_CHARGING),
            "tesla_charge_cable": self.get_text_state_safe(TESLA_CHARGE_CABLE),
            "tesla_charge_switch": self.get_text_state_safe(SWITCH_TESLA_CHARGE),
        }

    def read_ioniq_state(self) -> dict:
        """Read the Ioniq entities.  Every value may be None (integration down).

        The three entities belong to the same HA device, so they are refreshed
        by the same Bluelink payload: ``ioniq_data_ts`` therefore dates the
        plug reading too, which is the only usable freshness signal — a
        binary_sensor that keeps the same value never moves its ``last_updated``.
        """
        return {
            "ioniq_location": self.get_text_state_safe(IONIQ_LOCATION),
            "ioniq_plug": self.get_text_state_safe(IONIQ_PLUG),
            "ioniq_data_ts": self.get_text_state_safe(IONIQ_DATA_TS),
        }

    # -- write helpers ---------------------------------------------------------

    def set_number(self, entity_id: str, value: float) -> None:
        """Call number.set_value service."""
        resp = self._session.post(
            f"{self._base}/api/services/number/set_value",
            json={"entity_id": entity_id, "value": value},
            timeout=10,
        )
        resp.raise_for_status()
        logger.debug("SET %s = %s", entity_id, value)

    def set_max_charging_current(self, amps: int) -> None:
        self.set_number(NUMBER_MAX_CHARGING_CURRENT, amps)

    def set_max_discharging_current(self, amps: int) -> None:
        self.set_number(NUMBER_MAX_DISCHARGING_CURRENT, amps)

    def set_wallbox_current(self, amps: int) -> None:
        """Cloud path only — the EMS goes through ``WallboxCurrent``."""
        self.set_number(NUMBER_WALLBOX_MAX_CURRENT, amps)

    def set_wallbox_current_ble(self, amps: int) -> None:
        self.set_number(GW_NUMBER_MAX_CURRENT, amps)

    def set_input_number(self, entity_id: str, value: float) -> None:
        """Call input_number.set_value service."""
        resp = self._session.post(
            f"{self._base}/api/services/input_number/set_value",
            json={"entity_id": entity_id, "value": value},
            timeout=10,
        )
        resp.raise_for_status()

    def notify(self, title: str, message: str, notification_id: str) -> None:
        """Raise a persistent notification in the HA UI."""
        resp = self._session.post(
            f"{self._base}/api/services/persistent_notification/create",
            json={"title": title, "message": message,
                  "notification_id": notification_id},
            timeout=10,
        )
        resp.raise_for_status()

    def set_ems_mode(self, mode: str) -> None:
        """Write the EMS mode to HA input_select."""
        resp = self._session.post(
            f"{self._base}/api/services/input_select/select_option",
            json={"entity_id": SENSOR_EMS_MODE, "option": mode},
            timeout=10,
        )
        resp.raise_for_status()

    def set_switch(self, entity_id: str, on: bool) -> None:
        """Turn a switch on or off."""
        service = "switch/turn_on" if on else "switch/turn_off"
        resp = self._session.post(
            f"{self._base}/api/services/{service}",
            json={"entity_id": entity_id},
            timeout=10,
        )
        resp.raise_for_status()

    def set_tesla_charge(self, on: bool) -> None:
        """Start (True) or stop (False) the Tesla charge via Tessie."""
        self.set_switch(SWITCH_TESLA_CHARGE, on)

    def set_select(self, entity_id: str, option: str) -> None:
        """Set a select entity option."""
        resp = self._session.post(
            f"{self._base}/api/services/select/select_option",
            json={"entity_id": entity_id, "option": option},
            timeout=10,
        )
        resp.raise_for_status()

    def force_update_ioniq(self) -> bool:
        """Ask the Ioniq itself for fresh data (wakes the car).

        ``kia_uvo.update`` only re-reads the Bluelink cloud cache, which runs
        about 2 h behind; only ``force_update`` reaches the vehicle.  The call
        is **synchronous and slow**: measured at 29 s on 2026-09-20, with the
        entities updated 32 s after the request and the payload 7 s old.  Never
        call this from the 1 Hz loop.

        Returns False instead of raising, because failing is routine: the
        Hyundai API answers ``DuplicateRequestError`` when a refresh is already
        in flight (seen 2026-09-20 12:27) and times out at 30 s often enough
        that the hourly HA automation logs it regularly.  A caller that treats
        this as fatal reports "unknown" for a car that is simply busy.
        """
        try:
            resp = self._session.post(
                f"{self._base}/api/services/kia_uvo/force_update",
                json={"device_id": IONIQ_DEVICE_ID},
                timeout=90,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("kia_uvo.force_update failed: %s", exc)
            return False

    def set_ev_connected(self, value: str) -> None:
        """Write which car is on the cable to the HA helper."""
        resp = self._session.post(
            f"{self._base}/api/services/input_text/set_value",
            json={"entity_id": INPUT_EV_CONNECTED, "value": value},
            timeout=10,
        )
        resp.raise_for_status()
        logger.debug("SET %s = %s", INPUT_EV_CONNECTED, value)
