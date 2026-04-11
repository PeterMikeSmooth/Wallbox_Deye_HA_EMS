# Wallbox_Deye_HA_EMS

**Solar storage (Deye hybrid inverter) and EV charging management (Wallbox Pulsar Max)**

Python script running on a Raspberry Pi, managing EV charging and Deye battery via the Home Assistant REST API.

---

## Table of Contents

1. [Goal](#goal)
2. [Home Assistant Entities](#home-assistant-entities)
3. [Sign Conventions](#sign-conventions)
4. [Software Architecture](#software-architecture)
5. [State Machine](#state-machine)
6. [Detailed State Logic](#detailed-state-logic)
7. [Algorithms](#algorithms)
8. [Project Files](#project-files)
9. [Configuration](#configuration)
10. [Installation & Running](#installation--running)

---

## Goal

The Deye (hybrid inverter + battery) naturally balances household consumption by discharging its battery to keep `grid_power ≈ 0`. Problem: when an electric vehicle (EV) is plugged in, the Deye treats it as a regular load and drains the battery to power it.

This script has two missions:

1. **Protect the battery** — when the EV is charging, prevent the battery from discharging for the EV. Only the house is powered by the battery; the EV is powered by the grid (and/or solar surplus).
2. **Drive EV charging with solar surplus** — once the battery has reached its target SOC (`batt_charge_limit`), modulate the wallbox current to consume the solar surplus, without drawing from the grid or discharging the battery.

---

## Home Assistant Entities

### Sensors (read-only)

| Entity | Description | Range |
|--------|-------------|-------|
| `sensor.deye_battery` | Battery State of Charge (SOC) | 0 – 100 % |
| `sensor.deye_battery_voltage` | Battery voltage | ~50 – 58 V |
| `sensor.deye_battery_power` | Battery power (+ discharge, − charge) | W |
| `sensor.deye_grid_power` | Grid power (+ import, − export) | W |
| `sensor.deye_microinverter_power` | Solar production (micro-inverters) | 0 – 5000 W |
| `sensor.deye_load_l1_voltage` | House grid voltage | ~230 V |
| `sensor.shellyem_34945478aee1_channel_2_power` | EV charging power (Shelly meter) | 0 – 7500 W |

### Actuators (write)

| Entity | Description | Range |
|--------|-------------|-------|
| `number.deye_battery_max_charging_current` | Max battery charging current | 0 – 100 A |
| `number.deye_battery_max_discharging_current` | Max battery discharging current | 0 – 100 A |
| `number.wallbox_pulsar_max_sn_429953_maximum_charging_current` | Max wallbox charging current | 6 – 32 A |

### User Variables (to create in HA)

| Entity | Description | Suggested range | Default |
|--------|-------------|-----------------|---------|
| `input_number.batt_charge_limit` | Target battery SOC before surplus activation | 50 – 100 % | 80 % |

---

## Sign Conventions

```
sensor.deye_battery_power :  + = discharge (battery → house)
                              − = charge   (solar → battery)

sensor.deye_grid_power :     + = import   (grid → house, you pay)
                              − = export   (house → grid, you inject)
```

---

## Software Architecture

### Single Interface via Home Assistant

All sensor reads and actuator writes go through the **Home Assistant REST API**. No direct Modbus access or Wallbox cloud.

**Reasons:**
- Avoids Modbus conflicts (only one Modbus master allowed; HA already occupies it)
- The Python Wallbox library goes through the cloud (latency, internet dependency)
- Single authentication point (HA token)
- No external network dependency

### Control Loops

- **Fast loop (~1 s)**: read sensors + evaluate state machine + compute max discharge current (only in EV_NO_SOLAR). Writes to HA **only when the value changes** to avoid hammering the Modbus bus.
- **Slow loop (~60 s)**: adjust wallbox current in EV_SURPLUS mode

---

## State Machine

The script runs as a **single state machine** — no explicit "day/night" mode. The solar production threshold (100 W) naturally determines transitions.

```
                    EV unplugged
                   ┌────────────┐
                   │            │
                   │   IDLE     │
                   │            │
                   └─────┬──────┘
                         │ EV plugged in (Shelly > 40 W)
                         ▼
              ┌─────────────────────┐
              │  Solar > 100 W ?    │
              └──┬──────────────┬───┘
                 │ No           │ Yes
                 ▼              ▼
    ┌────────────────┐  ┌───────────────────────┐
    │  EV_NO_SOLAR   │  │  SOC < batt_charge_   │
    │                │  │  limit ?               │
    │  Throttle      │  └──┬────────────────┬────┘
    │  battery       │     │ Yes            │ No
    │  discharge     │     ▼                ▼
    │  (house-only)  │  ┌──────────────┐ ┌──────────────┐
    │                │  │ EV_BATTERY_  │ │ EV_SURPLUS   │
    │  Does NOT      │  │ PRIORITY     │ │              │
    │  touch wallbox │  │              │ │ Stop battery │
    │  current       │  │ Wallbox = 6A │ │ charging     │
    └────────────────┘  │ Free battery │ │ Drive wallbox│
                        │ charge       │ │ with surplus │
                        │ Free         │ │ Free         │
                        │ discharge    │ │ discharge    │
                        │ (100A)       │ │ (100A)       │
                        └──────┬───────┘ └──────┬───────┘
                               │                │
                               │  SOC ≥ limit   │
                               │───────────────►│
                               │                │
                               │◄───────────────│
                               │ SOC < limit-5% │
                               │ (hysteresis)   │
```

### Transitions

| From → To | Condition |
|-----------|-----------|
| **IDLE → EV_NO_SOLAR** | Shelly > 40 W AND microinverter ≤ 100 W |
| **IDLE → EV_BATTERY_PRIORITY** | Shelly > 40 W AND microinverter > 100 W AND SOC < `batt_charge_limit` |
| **IDLE → EV_SURPLUS** | Shelly > 40 W AND microinverter > 100 W AND SOC ≥ `batt_charge_limit` |
| **EV_NO_SOLAR → IDLE** | Shelly ≤ 40 W |
| **EV_NO_SOLAR → EV_BATTERY_PRIORITY** | microinverter > 100 W AND SOC < `batt_charge_limit` |
| **EV_NO_SOLAR → EV_SURPLUS** | microinverter > 100 W AND SOC ≥ `batt_charge_limit` |
| **EV_BATTERY_PRIORITY → IDLE** | Shelly ≤ 40 W |
| **EV_BATTERY_PRIORITY → EV_NO_SOLAR** | microinverter ≤ 100 W (+ wallbox → 6 A) |
| **EV_BATTERY_PRIORITY → EV_SURPLUS** | SOC ≥ `batt_charge_limit` |
| **EV_SURPLUS → IDLE** | Shelly ≤ 40 W |
| **EV_SURPLUS → EV_NO_SOLAR** | microinverter ≤ 100 W (+ wallbox → 6 A) |
| **EV_SURPLUS → EV_BATTERY_PRIORITY** | SOC < (`batt_charge_limit` − 5) |

---

## Detailed State Logic

### IDLE State (EV not plugged in)

- **Trigger**: `shellyem_power ≤ 40 W`
- **Entry actions**:
  - `deye_battery_max_discharging_current` → 100 A
  - `deye_battery_max_charging_current` → 100 A
- **Does NOT touch wallbox current** (user manages start/stop manually)
- **Behavior**: the Deye runs its own balancing algorithm. No intervention.

### EV_NO_SOLAR State (EV plugged in, no solar)

- **Trigger**: `shellyem_power > 40 W` AND `microinverter_power ≤ 100 W`
- **Entry actions**:
  - `deye_battery_max_charging_current` → 100 A (battery charging allowed if grid permits)
- **Fast loop (1 s)**: computes and applies `deye_battery_max_discharging_current` → house-only current (see [Discharge Limitation Algorithm](#discharge-limitation-algorithm)). Writes only when the rounded value changes.
- **Does NOT touch wallbox current**: at night, the grid powers the EV. The user controls wallbox current manually.
- **Result**: battery powers only the house. Grid covers the EV.

### EV_BATTERY_PRIORITY State (solar available, battery charging)

- **Trigger**: `shellyem_power > 40 W` AND `microinverter_power > 100 W` AND `SOC < batt_charge_limit`
- **Entry actions**:
  - `deye_battery_max_charging_current` → 100 A
  - `deye_battery_max_discharging_current` → 100 A (Deye manages freely, sun covers demand)
  - `wallbox_max_current` → 6 A (minimum, to let solar charge the battery)
- **No discharge limitation**: the sun provides enough energy, the battery doesn't naturally discharge. No need to throttle. If a transient briefly discharges the battery, that's normal and desired Deye behavior.
- **Behavior**: the Deye uses solar surplus to charge the battery (its own algorithm). The wallbox stays at minimum (6 A ≈ 1.4 kW single-phase, covered by grid).
- **Transition**: when `SOC ≥ batt_charge_limit` → switch to EV_SURPLUS

### EV_SURPLUS State (solar surplus → EV charging)

- **Trigger**: `SOC ≥ batt_charge_limit` (or return to surplus after hysteresis)
- **Entry actions**:
  - `deye_battery_max_charging_current` → 0 A (stop battery charging, all solar production is available)
  - `deye_battery_max_discharging_current` → 100 A (Deye freely handles transients)
- **No discharge limitation**: the sun covers the house. The battery may briefly discharge to cover a consumption spike (normal Deye behavior). Our script will adjust the wallbox at the next minute to compensate.
- **Slow loop (60 s)**: adjust wallbox current via the [Surplus Steering Algorithm](#surplus-steering-algorithm)
- **Hysteresis**: if `SOC < (batt_charge_limit − 5)`:
  - `wallbox_max_current` → 6 A
  - `deye_battery_max_charging_current` → 100 A
  - Transition to EV_BATTERY_PRIORITY

---

## Algorithms

### Discharge Limitation Algorithm

**Goal**: the battery only discharges to power the house, not the EV.

**Power balance** (energy conservation):

```
microinverter + battery_power + grid_power = house_load + ev_power
```

Therefore:

```
house_load = microinverter + battery_power + grid_power − ev_power
```

Maximum allowed discharge current:

```
desired_discharge_A = max(house_load, 0) / battery_voltage + MARGIN
desired_discharge_A = clamp(desired_discharge_A, 0, 100)
```

- `MARGIN`: safety margin of +1 A to avoid oscillations
- Smoothing via exponential moving average (EMA) to stabilize the setpoint:
  `smoothed = α × new_value + (1 − α) × previous_smoothed` with `α ≈ 0.3`
- Computed every **1 second**, but writes to HA **only when the rounded integer value changes** compared to the last written value
- **Active only in EV_NO_SOLAR state** — in solar states, discharge is left at 100 A (sun covers demand)
- The `house_load` calculation is valid even if discharge current is already throttled (measurements reflect actual system state)

### Surplus Steering Algorithm

**Goal**: adjust wallbox current so that `grid ≈ 0` AND `battery_power ≈ 0`.

In surplus mode, `max_charging_current = 0`, so the battery isn't charging. We want **all solar surplus to go to the EV**.

**New wallbox current calculation**:

```
available_surplus = ev_power_actual − grid_power − battery_power
wallbox_current_new = available_surplus / grid_voltage
wallbox_current_new = clamp(round(wallbox_current_new), 6, 32)
```

Explanation:
- If `grid_power > 0` (import) → consuming too much → reduce wallbox
- If `grid_power < 0` (export) → unused surplus → increase wallbox
- If `battery_power > 0` (discharge) → battery helping EV/house → reduce wallbox
- If `battery_power < 0` (charge) → shouldn't happen (max_charging = 0), if so → increase wallbox

The calculation automatically accounts for house consumption variations: if the house consumes more, `grid_power` increases, reducing `available_surplus` and thus the wallbox current.

**Frequency**: once per **minute**. Between adjustments, the battery may briefly discharge to cover the house (normal and desired Deye behavior).

---

## Project Files

```
Wallbox_Deye_HA_EMS/
├── README.md                 # This file (specification)
├── LICENSE
├── config_example.py         # Configuration example (committed)
├── config.py                 # Actual configuration (not committed, in .gitignore)
├── requirements.txt          # Python dependencies (requests only)
├── ha_api.py                 # Home Assistant API wrapper (read sensors, write actuators)
├── ems.py                    # EMS logic: state machine, algorithms, main loop
└── logs/                     # Log directory (not committed)
    └── ems.log               # Log of each wallbox/battery adjustment
```

---

## Configuration

### `config_example.py`

Pure Python file — zero dependency for parsing. Copy to `config.py` and customize.

```python
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
WALLBOX_MIN_CURRENT_A = 6
WALLBOX_MAX_CURRENT_A = 32

# Logging
LOG_FILE = "logs/ems.log"
LOG_LEVEL = "INFO"                # DEBUG for diagnostics
```

### Security

- `config.py` contains the HA token → **added to `.gitignore`**
- Only `config_example.py` is committed (no secrets)

---

## Installation & Running

```bash
# Clone the repo
git clone <repo_url>
cd Wallbox_Deye_HA_EMS

# Install dependencies
pip install -r requirements.txt

# Copy and edit configuration
cp config_example.py config.py
nano config.py      # Fill in HA IP + token

# Create input_number.batt_charge_limit in HA:
#   - Go to Settings > Devices & Services > Helpers
#   - Create a "Number" named "batt_charge_limit"
#   - Min: 50, Max: 100, Step: 5, Unit: %, Default: 80

# Run the script
python ems.py

# (Optional) Run as a systemd service for auto-start
```

### systemd Service (optional)

```ini
[Unit]
Description=Wallbox Deye EMS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Wallbox_Deye_HA_EMS
ExecStart=/usr/bin/python3 ems.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```
