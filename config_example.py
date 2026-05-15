# config_example.py — copy to config.py and fill in your values

# Home Assistant
HA_URL = "http://192.168.1.XXX:8123"
HA_TOKEN = "YOUR_LONG_LIVED_ACCESS_TOKEN"

# Thresholds
EV_CHARGING_DETECT_W = 40        # Shelly > 40 W = EV plugged in
SOLAR_AVAILABLE_W = 100           # Microinverter > 100 W = solar available
SOC_HYSTERESIS_PCT = 5            # SOC hysteresis in absolute percentage points

# Algorithm
FAST_LOOP_INTERVAL_S = 1          # Fast loop (sensor reads + discharge limitation)
SLOW_LOOP_INTERVAL_S = 60         # Slow loop (wallbox steering)
DISCHARGE_MARGIN_A = 1.0          # Discharge current margin (+1 A)
EMA_ALPHA = 0.3                   # EMA smoothing factor

# Defaults (IDLE state)
DEFAULT_MAX_CHARGING_CURRENT_A = 100
DEFAULT_MAX_DISCHARGING_CURRENT_A = 100
SURPLUS_MAX_CHARGING_A = 80       # In SOLAR_SURPLUS: allow battery to absorb transient solar spikes
STORAGE_TO_EV_SOC_FLOOR = 40      # STORAGE_TO_EV: stop discharging below this SOC %
# Off-peak windows: list of (start, end) as (hour, minute) tuples
OFF_PEAK_WINDOWS = [
    ((1, 54), (6, 54)),
    ((12, 24), (15, 24)),
]

# SOLAR_BOOSTED: share of EV power imported from grid
BOOSTED_GRID_RATIO_OFF_PEAK = 0.50  # Off-peak: grid 50%, solar 50%
BOOSTED_GRID_RATIO_PEAK = 0.60      # Peak: grid 60%, solar 40%

# STORAGE_BOOSTED: share of EV power covered by battery
STORAGE_BATT_RATIO_OFF_PEAK = 0.50  # Off-peak: battery 50%
STORAGE_BATT_RATIO_PEAK = 0.40      # Peak: battery 40%

# SOLAR_BOOSTED_N_STORAGE: battery charge target while boosted
STORAGE_CHARGE_W = 800               # Battery charge power in W

# Global inverter protection: max discharge power
MAX_DISCHARGE_POWER_W = 3600          # Never discharge more than 3.6 kW

WALLBOX_MIN_CURRENT_A = 6
WALLBOX_MAX_CURRENT_A = 32
DEFAULT_EMS_MODE = "SOLAR_ONLY"   # Mode reset when car is plugged in

# Logging
LOG_FILE = "logs/ems.log"
LOG_LEVEL = "INFO"                # DEBUG for diagnostics
