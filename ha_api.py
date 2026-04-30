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
SENSOR_EMS_MODE = "input_select.ems_mode"
SENSOR_WALLBOX_STATUS = "sensor.wallbox_pulsar_max_sn_429953_status_description"
INPUT_EMS_STATE = "input_text.ems_state"
INPUT_GRID_RATIO = "input_number.grid_ratio_value"

NUMBER_MAX_CHARGING_CURRENT = "number.deye_battery_max_charging_current"
NUMBER_MAX_DISCHARGING_CURRENT = "number.deye_battery_max_discharging_current"
NUMBER_WALLBOX_MAX_CURRENT = "number.wallbox_pulsar_max_sn_429953_maximum_charging_current"


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
            "ems_mode": self.get_text_state(SENSOR_EMS_MODE),
            "wallbox_status": self.get_text_state(SENSOR_WALLBOX_STATUS),
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
        self.set_number(NUMBER_WALLBOX_MAX_CURRENT, amps)

    def set_input_number(self, entity_id: str, value: float) -> None:
        """Call input_number.set_value service."""
        resp = self._session.post(
            f"{self._base}/api/services/input_number/set_value",
            json={"entity_id": entity_id, "value": value},
            timeout=10,
        )
        resp.raise_for_status()

    def set_ems_state(self, state: str) -> None:
        """Write the current EMS state to HA input_text."""
        resp = self._session.post(
            f"{self._base}/api/services/input_text/set_value",
            json={"entity_id": INPUT_EMS_STATE, "value": state},
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
