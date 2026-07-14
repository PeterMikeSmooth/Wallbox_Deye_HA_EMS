# Wallbox_Deye_HA_EMS

**Solar storage (Deye hybrid inverter) and EV charging management (Wallbox Pulsar Max)**

Python script running on a Raspberry Pi, managing EV charging and Deye battery via the Home Assistant REST API.

---

## Table of Contents

1. [Goal](#goal)
2. [Home Assistant Entities](#home-assistant-entities)
3. [Sign Conventions](#sign-conventions)
4. [Software Architecture](#software-architecture)
5. [Modes & States](#modes--states)
6. [State Machine Routing](#state-machine-routing)
7. [Detailed State Logic](#detailed-state-logic)
8. [Algorithms](#algorithms)
9. [Project Files](#project-files)
10. [Configuration](#configuration)
11. [Installation & Running](#installation--running)

---

## Goal

The Deye (hybrid inverter + battery) naturally balances household consumption by discharging its battery to keep `grid_power ≈ 0`. Problem: when an electric vehicle (EV) is plugged in, the Deye treats it as a regular load and drains the battery to power it.

This script manages that interaction. Depending on the **mode** the user selects (from a Home Assistant dashboard), it decides how the EV should be powered — from solar surplus only, from the grid, from the battery, or a blend — and modulates both the wallbox charging current and the Deye battery discharge/charge limits accordingly.

Two recurring missions run under every mode:

1. **Protect the battery** — prevent uncontrolled battery drain into the EV, and never discharge below a configurable SOC floor (`discharge_limit`).
2. **Use energy where it's cheapest** — steer solar surplus into the EV, exploit off-peak grid windows, and keep enough battery range for the night.

---

## Home Assistant Entities

### Sensors (read-only)

| Key (code) | Entity | Description | Range |
|-----------|--------|-------------|-------|
| `battery_soc` | `sensor.deye_battery` | Battery State of Charge (SOC) | 0 – 100 % |
| `battery_voltage` | `sensor.deye_battery_voltage` | Battery voltage | ~50 – 58 V |
| `battery_power` | `sensor.deye_battery_power` | Battery power (+ discharge, − charge) | W |
| `grid_power` | `sensor.shellyem_34945478aee1_channel_1_power` | Grid power (+ import, − export) | W |
| `solar_power` | `sensor.deye_microinverter_power` | Solar production (micro-inverters) | 0 – 5000 W |
| `grid_voltage` | `sensor.deye_load_l1_voltage` | House grid voltage | ~230 V |
| `ev_power` | `sensor.shellyem_34945478aee1_channel_2_power` | EV charging power (Shelly meter) | 0 – 7500 W |
| `wallbox_status` | `sensor.wallbox_pulsar_max_sn_429953_status_description` | Wallbox status text | — |

### Actuators (write)

| Entity | Description | Range |
|--------|-------------|-------|
| `number.deye_battery_max_charging_current` | Max battery charging current | 0 – 100 A |
| `number.deye_battery_max_discharging_current` | Max battery discharging current | 0 – 100 A |
| `number.wallbox_pulsar_max_sn_429953_maximum_charging_current` | Max wallbox charging current | 6 – 32 A |

### User Variables (to create in HA)

| Entity | Description | Suggested range | Default |
|--------|-------------|-----------------|---------|
| `input_select.ems_mode` | Active mode (see [Modes](#modes--states)) | 6 options | `SOLAR_ONLY` |
| `input_number.batt_charge_limit` | Max SOC the battery is allowed to charge to | 50 – 100 % | 80 % |
| `input_number.batt_charge_prio` | SOC target reached before solar surplus goes to the EV | 0 – 100 % | auto |
| `input_number.discharge_limit` | SOC floor — battery won't discharge for the EV below this | 20 – 100 % | auto |

### Status Outputs (written by the script, for dashboards)

| Entity | Description |
|--------|-------------|
| `input_text.ems_state` | Current internal state name |
| `input_number.grid_ratio_value` | Share of EV power currently drawn from the grid (%) |
| `input_number.range_needed_over_night` | Measured overnight battery drain (%), auto-computed |

> `batt_charge_prio` and `discharge_limit` are also **auto-set** each morning by the [overnight range tracker](#overnight-range-tracking); the user can override them at any time.

---

## Sign Conventions

```
sensor.deye_battery_power :  + = discharge (battery → house/EV)
                              − = charge   (solar/grid → battery)

grid_power (Shelly ch.1)  :  + = import   (grid → house, you pay)
                              − = export   (house → grid, you inject)

ev_power (Shelly ch.2)    :  always ≥ 0, positive while charging
grid_ratio_value          :  grid_power / ev_power × 100  (% of EV drawn from grid)
```

---

## Software Architecture

### Single Interface via Home Assistant

All sensor reads and actuator writes go through the **Home Assistant REST API** (`ha_api.py`). No direct Modbus access or Wallbox cloud.

**Reasons:**
- Avoids Modbus conflicts (only one Modbus master allowed; HA already occupies it)
- The Python Wallbox library goes through the cloud (latency, internet dependency)
- Single authentication point (HA token)
- No external network dependency

### Control Loops

- **Fast loop (~1 s)**: read all sensors → detect car plug-in → evaluate the state machine → apply per-state continuous work (battery discharge limits, override detection, overnight tracking). Writes to HA **only when a value changes** to avoid hammering the Modbus bus.
- **Slow loop (~60 s)**: wallbox current steering in the surplus/storage modes (`SOLAR_ONLY`, `SOLAR_BOOSTED`, `STORAGE_ONLY`, and the periodic 32 A refresh in `STORAGE_BOOSTED`). Implemented as a monotonic-clock gate inside the fast loop.

### Car plug-in reset

When the wallbox reports the car has just connected, `ems_mode` is reset to `DEFAULT_EMS_MODE` (`SOLAR_ONLY`) so a fresh session always starts from a safe, predictable mode.

---

## Modes & States

The user picks a **mode** via `input_select.ems_mode`. The script maps `(mode, sensors)` to an **internal state** that drives the actuators.

### Modes (`input_select.ems_mode`)

| Mode | Intent |
|------|--------|
| **SOLAR_ONLY** | Only solar surplus feeds the EV; keep `grid ≈ 0`. |
| **SOLAR_BOOSTED** | Solar surplus + a grid share (50 % off-peak / 60 % peak) feed the EV; battery never discharges for the EV. |
| **FULL_SPEED** | Wallbox at 32 A; battery backs the EV+house as long as SOC is above `discharge_limit`. |
| **STORAGE_BOOSTED** | Wallbox at 32 A; battery discharges to cover a target share of EV power (grid pays the rest). |
| **STORAGE_ONLY** | Battery + solar feed the EV with `grid = 0`; wallbox current computed from available discharge power. |
| **MANUAL** | User sets the wallbox current from the app; EMS only manages the battery. |

### Internal States (`State` enum)

| State | Meaning |
|-------|---------|
| **IDLE** | No EV charging detected (internal only). |
| **EV_NO_SOLAR** | EV charging, solar ≤ 100 W → wallbox 6 A, battery throttled to house-only. |
| **BATTERY_PRIORITY** | Solar present but SOC below `batt_charge_prio` → wallbox 6 A, let solar charge the battery first. |
| **FULL_SPEED** | Mode state — see [detailed logic](#full_speed-mode). |
| **SOLAR_ONLY** | Surplus steering, grid target = 0 %. |
| **SOLAR_BOOSTED** | Surplus steering, grid target = share of EV power. |
| **STORAGE_BOOSTED** | Battery-share discharge steering. |
| **STORAGE_ONLY** | Battery+solar → EV, grid = 0. |
| **MANUAL** | User-controlled wallbox; EMS manages battery only. |

**SOC protection (all storage modes):** if `SOC ≤ discharge_limit`, `STORAGE_BOOSTED` and `STORAGE_ONLY` are automatically forced back to `SOLAR_ONLY` (and the `ems_mode` helper is rewritten so the dashboard reflects it).

---

## State Machine Routing

`_determine_target_state()` decides the target state every fast tick:

```
ev_power ≤ EV_CHARGING_DETECT_W (40 W) ?
  └─ yes → IDLE
  └─ no  → dispatch on ems_mode:

     FULL_SPEED  → FULL_SPEED
     MANUAL      → MANUAL

     STORAGE_BOOSTED / STORAGE_ONLY
        SOC ≤ discharge_limit ? → force SOLAR_ONLY (protection)
                                   → SOLAR_ONLY if solar > 100 W else EV_NO_SOLAR
        else                    → STORAGE_BOOSTED / STORAGE_ONLY

     SOLAR_ONLY / SOLAR_BOOSTED
        solar ≤ 100 W ?         → EV_NO_SOLAR
        already in surplus ?    → BATTERY_PRIORITY if SOC < (prio − hysteresis) else surplus
        SOC ≥ batt_charge_prio ?→ surplus state (SOLAR_ONLY or SOLAR_BOOSTED)
        else                    → BATTERY_PRIORITY
```

The `100 W` solar threshold (`SOLAR_AVAILABLE_W`) naturally separates "day" from "night" behavior without an explicit clock.

---

## Detailed State Logic

### IDLE State (EV not plugged in)

- **Trigger**: `ev_power ≤ 40 W`
- **Entry actions**: `max_discharging_current` → 100 A, `wallbox_current` → 6 A
- The Deye runs its own balancing algorithm; the script does not intervene beyond resetting safe defaults.

### FULL_SPEED Mode (wallbox at max, battery backs the EV)

- **Trigger**: `ems_mode = FULL_SPEED` (while EV charging)
- **Entry actions**: `wallbox_current` → 32 A, then **hands off** — the user may lower it manually and the value sticks.
- **Battery discharge policy** (evaluated every fast loop), based on SOC vs `discharge_limit`:
  - **SOC > `discharge_limit`** → `max_discharging_current` = 100 A (capped by `MAX_DISCHARGE_POWER_W`). The battery discharges freely to help power both the EV and the house.
  - **SOC ≤ `discharge_limit` AND solar > 100 W** → `max_discharging_current` = 0 A. The battery is spared — the **solar production covers the house** instead.
  - **SOC ≤ `discharge_limit` AND solar ≤ 100 W** → discharge throttled to **house-only** via the [Discharge Limitation Algorithm](#discharge-limitation-algorithm). The battery powers only the house; the grid covers the EV.

### EV_NO_SOLAR State (EV plugged in, no solar)

- **Trigger**: `ev_power > 40 W` AND `solar_power ≤ 100 W` (in a SOLAR mode)
- **Entry actions**: `wallbox_current` → 6 A
- **Fast loop**: computes and applies `max_discharging_current` = house-only current (see [Discharge Limitation Algorithm](#discharge-limitation-algorithm)); writes only when the rounded value changes.
- **Result**: battery powers only the house; the grid covers the EV.

### BATTERY_PRIORITY State (solar available, battery still charging)

- **Trigger**: solar > 100 W AND `SOC < batt_charge_prio` (in a SOLAR mode)
- **Entry actions**: `wallbox_current` → 6 A; `max_discharging_current` → 100 A (or **0 A** under `SOLAR_BOOSTED`, so the battery is never drained for the EV while it should be charging).
- **Behavior**: the wallbox stays at the 6 A minimum so solar goes to the battery first. Once `SOC ≥ batt_charge_prio` → surplus state.

### SOLAR_ONLY State (solar surplus → EV, grid ≈ 0)

- **Trigger**: `SOC ≥ batt_charge_prio` and solar > 100 W
- **Slow loop (60 s)**: [Surplus Steering Algorithm](#surplus-steering-algorithm) with `grid_target = 0`.
- **Charging**: battery charge capped at `SURPLUS_MAX_CHARGING_A` to absorb transient solar spikes.
- **Hysteresis**: drops to `BATTERY_PRIORITY` if `SOC < (batt_charge_prio − SOC_HYSTERESIS_PCT)`.

### SOLAR_BOOSTED State (solar surplus + grid share → EV)

- Like `SOLAR_ONLY` but the surplus steering targets a **positive grid import** = `ev_power × grid_ratio`, where `grid_ratio` is `BOOSTED_GRID_RATIO_OFF_PEAK` (50 %) or `BOOSTED_GRID_RATIO_PEAK` (60 %) depending on the [off-peak window](#configuration).
- Battery discharge is held at **0 A** so the boost comes from grid + solar, never the battery.

### STORAGE_BOOSTED State (battery discharges to cover a share of the EV)

- **Trigger**: `ems_mode = STORAGE_BOOSTED` AND `SOC > discharge_limit`
- **Entry actions**: `wallbox_current` → 32 A.
- **Fast loop**: [Storage Discharge Algorithm](#storage-discharge-algorithm) targets `grid = grid_ratio × ev_power`, so the battery covers the complementary share.
- **SOC floor** (`discharge_limit`, hysteresis +2 %): below it, wallbox → 6 A and discharge → 0 A until SOC recovers.
- The wallbox 32 A setpoint is re-sent every 60 s (the cloud integration may override it).

### STORAGE_ONLY State (battery + solar → EV, grid = 0)

- **Trigger**: `ems_mode = STORAGE_ONLY` AND `SOC > discharge_limit`
- **Slow loop (60 s)**: wallbox current is **computed directly** as `(MAX_DISCHARGE_POWER_W + solar − house_load) / grid_voltage`, clamped 6–32 A. The incremental steering can't be used here because the Deye keeps `grid ≈ 0` on its own regardless of wallbox current.
- **SOC floor** (hysteresis +2 %): below it, battery reverts to house-only discharge and the wallbox drops to 6 A.

### MANUAL Mode (user drives the wallbox)

- **Trigger**: `ems_mode = MANUAL`
- **Wallbox**: started at 6 A on entry, then **never touched again** — the user sets the current from the app. On leaving MANUAL the wallbox is reset to 6 A.
- **Battery**: discharges normally (100 A, `MAX_DISCHARGE_POWER_W` cap) while `SOC > discharge_limit`. Below the floor (hysteresis +2 %) the EMS **hands off** the Deye discharge current entirely so the user can set it manually in HA.
- Wallbox override detection is disabled in MANUAL (a higher `ev_power` is intended, not a cloud override to fight).

---

## Algorithms

### Discharge Limitation Algorithm

**Goal**: the battery only discharges to power the house, not the EV.

**Power balance** (energy conservation):

```
solar + battery_power + grid_power = house_load + ev_power
⇒ house_load = solar + battery_power + grid_power − ev_power
```

Maximum allowed discharge current:

```
desired_A = max(house_load, 0) / battery_voltage + DISCHARGE_MARGIN_A
desired_A = clamp(desired_A, 0, 100)
```

- `DISCHARGE_MARGIN_A`: +1 A safety margin to avoid oscillation.
- EMA smoothing: `smoothed = α·new + (1−α)·prev` with `α = EMA_ALPHA (0.3)`.
- Computed every second, written **only when the rounded integer changes**.
- Used in `EV_NO_SOLAR`, in the house-only branch of `FULL_SPEED`, and in the SOC-floor branches of `STORAGE_ONLY` / `MANUAL`.

### Surplus Steering Algorithm

**Goal**: adjust wallbox current so that `grid ≈ grid_target` AND the battery isn't discharging into the EV.

**Incremental** — never assumes `P = I × V`, so it works at any charging mode:

```
excess  = −(grid_power + battery_power) + grid_target
delta   = round(excess / grid_voltage)
wallbox = clamp(last_wallbox + delta, 6, 32)
```

- `grid_target = 0` in `SOLAR_ONLY`; `grid_target = ev_power × grid_ratio` in `SOLAR_BOOSTED`.
- Runs once per minute. Between adjustments the battery may briefly discharge to cover a spike (normal Deye behavior).

### Storage Discharge Algorithm

**Goal** (`STORAGE_BOOSTED`): discharge the battery so that `grid = grid_ratio × ev_power`.

```
target_power = battery_power + grid_power − grid_ratio × ev_power
desired_A    = max(target_power, 0) / battery_voltage + DISCHARGE_MARGIN_A
```

EMA-smoothed like the discharge limiter. At equilibrium the battery covers `(1 − grid_ratio)` of the EV plus the house minus solar.

### Overnight Range Tracking

Two-phase state machine that measures how much the battery drains overnight and pre-sets the next day's floor/priority:

- **WAIT_FOR_NIGHT** — on the falling edge of solar (dusk), capture `soc_dusk`. Later edges overwrite it, so the last crossing before 01:00 is the real sunset. At 01:00, lock it in.
- **WAIT_FOR_DAYLIGHT** — on the first solar crossing above 100 W (sunrise), compute `range_needed = soc_dusk − soc_now`, then set:
  - `range_needed_over_night = range_needed`
  - `batt_charge_prio = discharge_limit = min(MIN_SOC_LFP(20) + range_needed + SAFETY_MARGIN(10), 100)`

This keeps enough charge for the following night without over-charging.

### Wallbox Override Protection

The Pulsar's cloud integration sometimes ignores our setpoint. If `ev_power` exceeds the expected `setpoint × grid_voltage` by more than 1500 W for 30 s, the script toggles `setpoint+1 → setpoint` to force a cloud refresh, and logs the event. Disabled in `MANUAL`.

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
# Home Assistant
HA_URL = "http://192.168.1.XXX:8123"
HA_TOKEN = "YOUR_LONG_LIVED_ACCESS_TOKEN"

# Thresholds
EV_CHARGING_DETECT_W = 40        # Shelly > 40 W = EV plugged in
SOLAR_AVAILABLE_W = 100          # Microinverter > 100 W = solar available
SOC_HYSTERESIS_PCT = 5           # SOC hysteresis in absolute percentage points

# Algorithm
FAST_LOOP_INTERVAL_S = 1         # Fast loop (sensor reads + discharge limitation)
SLOW_LOOP_INTERVAL_S = 60        # Slow loop (wallbox steering)
DISCHARGE_MARGIN_A = 1.0         # Discharge current margin (+1 A)
EMA_ALPHA = 0.3                  # EMA smoothing factor

# Defaults
DEFAULT_MAX_CHARGING_CURRENT_A = 100
DEFAULT_MAX_DISCHARGING_CURRENT_A = 100
SURPLUS_MAX_CHARGING_A = 80      # SOLAR modes: absorb transient solar spikes
STORAGE_TO_EV_SOC_FLOOR = 40     # Fallback floor (overridden by input_number.discharge_limit)

# Off-peak windows: list of (start, end) as (hour, minute) tuples
OFF_PEAK_WINDOWS = [
    ((1, 54), (6, 54)),
    ((12, 24), (15, 24)),
]

# Grid share when boosting
BOOSTED_GRID_RATIO_OFF_PEAK = 0.50   # Off-peak: grid 50 %, solar 50 %
BOOSTED_GRID_RATIO_PEAK = 0.60       # Peak: grid 60 %, solar 40 %

# Global inverter protection
MAX_DISCHARGE_POWER_W = 4600     # Never discharge more than 4.6 kW (EV charging only)

WALLBOX_MIN_CURRENT_A = 6
WALLBOX_MAX_CURRENT_A = 32
DEFAULT_EMS_MODE = "SOLAR_ONLY"  # Mode reset when the car is plugged in

# Logging
LOG_FILE = "logs/ems.log"
LOG_LEVEL = "INFO"               # DEBUG for diagnostics
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

# In Home Assistant, create the helpers:
#   input_select.ems_mode  → options: SOLAR_ONLY, SOLAR_BOOSTED, FULL_SPEED,
#                            STORAGE_BOOSTED, STORAGE_ONLY, MANUAL
#   input_number.batt_charge_limit   (50–100 %, step 5, default 80)
#   input_number.batt_charge_prio    (0–100 %)
#   input_number.discharge_limit     (20–100 %)
#   input_text.ems_state             (status display)
#   input_number.grid_ratio_value    (status display, 0–100 %)
#   input_number.range_needed_over_night (status display, 0–100 %)

# Run the script
python ems.py
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
