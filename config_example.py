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
WALLBOX_MIN_CURRENT_A = 6
WALLBOX_MAX_CURRENT_A = 32

# Logging
LOG_FILE = "logs/ems.log"
LOG_LEVEL = "INFO"                # DEBUG for diagnostics
