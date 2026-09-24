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
8. [Tesla Solar Pause](#tesla-solar-pause-solar_only-only)
9. [Algorithms](#algorithms)
10. [Project Files](#project-files)
11. [Configuration](#configuration)
12. [Installation & Running](#installation--running)

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
| `tesla_location` | `device_tracker.martine_location` | Tesla location (Tessie) | `home` / `not_home` / … |
| `tesla_charging` | `sensor.martine_charging` | Tesla charge state (Tessie) | `starting`, `charging`, `stopped`, `complete`, `disconnected`, `no_power` |
| `tesla_charge_cable` | `binary_sensor.martine_charge_cable` | Cable plugged into the Tesla | `on` / `off` |
| `tesla_charge_switch` | `switch.martine_charge` | Tesla charge allowed | `on` / `off` |

> The four Tesla entities are **not** part of the 1 Hz sensor batch: they are read on demand
> (`read_tesla_state`), only when the EMS is about to act — see
> [no polling of the car](#vampire-drain-the-ems-never-polls-the-car). They are also read
> **defensively** (`get_text_state_safe`): Tessie is a cloud integration, so a failure yields
> `None` and the [Tesla solar pause](#tesla-solar-pause-solar_only-only) does nothing rather than
> taking down the tick. They are used by that feature only.

### Actuators (write)

| Entity | Description | Range |
|--------|-------------|-------|
| `number.deye_battery_max_charging_current` | Max battery charging current | 0 – 100 A |
| `number.deye_battery_max_discharging_current` | Max battery discharging current | 0 – 100 A |
| `number.wallbox_pulsar_max_sn_429953_maximum_charging_current` | Max wallbox charging current | 6 – 32 A |
| `switch.martine_charge` | Tesla charge on/off (Tessie) — [`SOLAR_ONLY` only](#tesla-solar-pause-solar_only-only) | `on` / `off` |

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

### Car unplug reset

When the wallbox reports the car has just been **unplugged**, `ems_mode` is reset to `DEFAULT_EMS_MODE` (`SOLAR_ONLY`), so the next session starts from a safe, predictable mode. Plugging in only logs; it never touches the mode, so the user can plug in and pick a mode without it being overwritten underneath them.

Doing it at unplug also makes the detection latency free: the mode is only consumed by the *next* session, so a reset that lands minutes late is still correct.

**Detection.** The runtime signal is `sensor.wallbox_pulsar_max_sn_429953_status_description`, whose values were classified by labelling 9 days of history with `sensor.wallbox_pulsar_max_charging_status` (`SMART_CONTROL_IN_PROGRESS` / `_CAPABLE` = plugged, `_NOT_AVAILABLE` = unplugged — a 5-minute-polled sensor used for the study only, never read at runtime):

| Status | Plugged | Unplugged | Verdict |
|---|---:|---:|---|
| `Locked` | 0.1 h | 109.3 h | **unplugged** |
| `Waiting for car demand` | 24.8 h | 0.0 h | plugged |
| `Locked, car connected` | 20.3 h | 0.0 h | plugged |
| `Waiting` | 19.6 h | 0.0 h | plugged |
| `Charging` | 17.9 h | 0.0 h | plugged |
| `Ready` | 1.0 h | 0.4 h | unplugged (see below) |
| `Disconnected` | 0.3 h | 1.0 h | ambiguous → inconclusive |

`Locked` and `Ready` demote a plugged state; `Disconnected`, `unavailable` and `Error` are transparent, leaving the remembered state alone, because those are the shapes a cloud dropout takes. Either no-car status reached from an already-unplugged state produces no edge, which is what neutralises the `Locked ↔ Ready` chatter.

The time-weighted table makes `Ready` look ambiguous, but that is the 5-minute-polled label lagging rather than the status. Replayed as edges — how it is actually used — every `Ready` reached from a plugged state was a real unplug: four with the line at 7-8 W for the next hour, three where power returned only because another car was plugged in one to two minutes later. Trusting it finds 18 unplugs over the window instead of 17, two of them 6 and 7 minutes sooner, and loses none.

No confirmation delay is applied. The dangerous case — a car mid-charge — always shows itself through `ev_power`, and that veto already guards the reset; across all 18 edges the line read 7 W or less and the veto never fired.

> **`ev_power` can only veto, never confirm.** A car sitting plugged and idle draws the same ~6 W standby as an empty cable, so no power does *not* mean no car. The guard in `_reset_mode_on_unplug()` blocks a reset when power is flowing (proof a car is there) and nothing else; it blocked 0 of the 17 real edges. Likewise `_determine_target_state()` returning `IDLE` below 40 W means "nothing to steer", not "unplugged".

### Which car is on the cable

`input_text.ev_connected` carries one of `Tesla connected`, `Ioniq connected`, `other connected`, `disconnected`, `unknown`. Two cars share this wallbox (a Tesla via Tessie and a Hyundai Ioniq via Kia Uvo / Bluelink), so "a car is plugged in" is not enough to know whose charge is being steered.

It is written on the same edges as the mode reset: `disconnected` on unplug, and on plug-in the identification runs:

1. **Tesla** — `binary_sensor.martine_charge_cable` is `on` **and** `device_tracker.martine_location` is `home`. These are HA cache reads: no request reaches Tessie and the car is never woken. The cable is used rather than the charge state because over 9 days the Tesla was plugged in at home for 70.1 h and read `stopped` for 21.0 h of those — the EMS's own solar pause produces exactly that — so keying on the charge state would have missed the car one time in three. The cable never claimed the Tesla during an Ioniq session: the only disagreements totalled 0.2 h, in four 2-4 minute bursts where it was simply quicker than the wallbox's 5-minute-polled sensor.
2. **Ioniq** — otherwise, `kia_uvo.force_update` asks the vehicle directly, then the answer is read once `sensor.moniq_location_last_updated` has advanced. `home` + `binary_sensor.moniq_ev_battery_plug` = `on` identifies it.
3. **Otherwise** — `other connected`. If the Ioniq never answers, the result is `unknown`, which is not the same claim as "some other car".

> The Hyundai API refuses a refresh while another is in flight (`DuplicateRequestError`), times out at 30 s often enough that the hourly HA automation logs it regularly, and occasionally 502s on the token exchange. So `force_update` failing is routine, not fatal: it is retried 3 times 30 s apart, and if the car still has not answered the thread keeps watching for up to 70 minutes and upgrades `unknown` once a fresh payload lands — usually via the hourly automation. Every write is pinned to the plug-in that started it, so a slow thread cannot overwrite a later session's verdict.
>
> The Bluelink cloud cache runs about 2 hours behind, so `kia_uvo.update` cannot answer "is it plugged in right now"; only `force_update` can. That call is synchronous and took 29 s when measured, so the whole sequence runs in a daemon thread with its own `HomeAssistantAPI` client — the 1 Hz loop keeps steering throughout.
>
> Freshness is judged on `sensor.moniq_location_last_updated`, not on `last_changed`: a binary sensor rewritten with the same value never moves its timestamps. The three `moniq_*` entities share one HA device, so that timestamp dates the plug reading too.
>
> Beware of `ioniq_*`: a dead duplicate of the same vehicle, frozen on `unavailable` since 2026-09-11. The live entities are `moniq_*`.

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
- **Entry actions**: `wallbox_current` → 6 A.
- **Fast loop (every tick, not just on entry)**: `max_discharging_current` → 100 A, or **0 A** under `SOLAR_BOOSTED` so the battery is never drained for the EV while it should be charging. Re-evaluating every tick (rather than only at state entry) ensures a mode switch made *while already parked in* `BATTERY_PRIORITY` (e.g. `SOLAR_BOOSTED → SOLAR_ONLY` with no state transition in between) takes effect immediately instead of leaving the battery stuck at the previous mode's discharge setting.
- **Behavior**: the wallbox stays at the 6 A minimum so solar goes to the battery first. Once `SOC ≥ batt_charge_prio` → surplus state.

### SOLAR_ONLY State (solar surplus → EV, grid ≈ 0)

- **Trigger**: `SOC ≥ batt_charge_prio` and solar > 100 W
- **Slow loop (60 s)**: [Surplus Steering Algorithm](#surplus-steering-algorithm) with `grid_target = 0`.
- **Charging**: battery charge capped at `SURPLUS_MAX_CHARGING_A` to absorb transient solar spikes.
- **Hysteresis**: drops to `BATTERY_PRIORITY` if `SOC < (batt_charge_prio − SOC_HYSTERESIS_PCT)`.
- **Tesla solar pause**: when the sun can no longer sustain even the 6 A floor, the Tesla charge is stopped and restarted on its own — see [Tesla Solar Pause](#tesla-solar-pause-solar_only-only).

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

## Tesla Solar Pause (`SOLAR_ONLY` only)

In `SOLAR_ONLY` the wallbox can't go below **6 A** (≈ 1.4 kW). When the sun drops under that
floor, the surplus steering has nothing left to give: the house battery — or the grid — silently
makes up the difference, which is exactly what `SOLAR_ONLY` is supposed to avoid. This feature
stops the car instead, and restarts it when the sun is back.

It only acts on the **Tesla** (Tessie integration) and only in `SOLAR_ONLY` mode. Any other mode
releases the pause immediately. Nothing here touches the wallbox setpoint.

### The metric: sun available to the car

Both decisions read **one** quantity, with a hysteresis band around it:

```
sun_to_ev = ev_power − battery_power − grid_power     (= solar_power − house_load)
```

This is the solar power actually reaching the car. The point is that **it reads the same whether
the car is charging or stopped** — unlike a raw surplus.

That is not a detail. Measured in production on 2026-09-11 at 17:40: the charge was cut, and
20 seconds later the 1.18 kW the car had stopped drawing showed up as 1.23 kW of *surplus*, which
crossed the restart threshold and started the charge again. A threshold on the surplus is crossed
by the cut itself; a threshold on `sun_to_ev` is not.

| Situation (same weather) | `sun_to_ev` | raw surplus `−(grid + battery)` |
|---|---|---|
| Car charging at 6 A | 899 W | −280 W |
| Same instant, car stopped | 899 W | +899 W |

### Cutting the charge

All of these must hold **continuously for `TESLA_PAUSE_CONFIRM_S` (5 min)** — a passing cloud
never cuts the charge:

| Condition | Why |
|-----------|-----|
| `ems_mode = SOLAR_ONLY` and the state is not `IDLE` | Feature is scoped to solar-surplus charging (`SOLAR_ONLY`, `EV_NO_SOLAR` and `BATTERY_PRIORITY` states are all covered) |
| wallbox setpoint at its 6 A floor | The steering is already saturated — there is no smaller current to fall back to |
| `sun_to_ev < TESLA_CUT_BELOW_W` (1000 W) | The sun no longer covers even the 6 A floor (~1.2 kW at this car), so the battery or the grid is making up the rest |
| `device_tracker.martine_location = home`, `binary_sensor.martine_charge_cable = on`, and `sensor.martine_charging` in `charging`/`starting` | **The car on the cable really is the Tesla** — the wallbox itself cannot tell which car is plugged in, and we must never stop someone else's charge |

The first three come from the sensor batch the loop already reads. The last one — the only one
that involves the Tesla — is checked **once, at the end of the 5 minutes**, right before acting.
If it says another car (or Tessie is down), the countdown simply restarts, so the next look is
another 5 minutes away.

Action: `switch.martine_charge → off`. The EV power then drops and the EMS falls back to `IDLE`
(wallbox back to 6 A) on its own. The `ems_mode` is left untouched.

### Restarting the charge

**No confirmation delay** — the charge restarts on the first tick where, the pause being active:

| Condition | Value |
|-----------|-------|
| `sun_to_ev > TESLA_RESUME_ABOVE_W` | 1200 W — with the 1000 W cut floor this is a 200 W hysteresis band on the same quantity |
| at least `TESLA_SETTLE_AFTER_CUT_S` since the cut | 120 s. The Shelly and the Deye do not sample at the same instant, so the readings right after a 1.2 kW load drop are not trustworthy — this is settling time, not policy |
| `device_tracker.martine_location` | `home` |
| `binary_sensor.martine_charge_cable` | `on` |
| `battery_soc ≥ batt_charge_prio` | The house battery has had its share first |

The sun and the SOC come from the Deye sensors the loop already reads; the Tesla entities are
consulted **only once those are satisfied**, to confirm the car is there before writing.

Action: `switch.martine_charge → on`. The surplus steering then ramps the wallbox up from 6 A as
usual. Because both thresholds sit on the same quantity, restarting cannot be triggered by the cut
itself: within one weather condition the charge does not cycle at all, and borderline weather
cannot cycle faster than the 5-minute countdown.

A full house battery changes nothing here: this installation **exports** its surplus rather than
curtailing it (measured over the 3 days to 2026-09-11: 11.7 % of grid samples below −100 W, peak
−1792 W). Exported power is a negative `grid_power`, which `sun_to_ev` counts exactly like power
sent to the battery, so the restart threshold is reachable at any SOC.

### Releasing the pause

The pause is **ours alone**: a charge the *user* stopped by hand is never restarted by the EMS.
It is released as soon as:

- `ems_mode` leaves `SOLAR_ONLY` → charge switched back on immediately;
- the car leaves home or is unplugged → noticed at the 5-minutely check below. If Tessie then
  refuses to start the charge (an unplugged car can't), the pause is dropped anyway and an HA
  persistent notification warns that `switch.martine_charge` may have stayed off;
- `switch.martine_charge` is found back **on** (you, or a Tessie schedule, re-enabled it) → the EMS
  silently drops its pause, since it is no longer the one holding the charge off. Note the normal
  cut logic can then arm again 5 minutes later: to charge on battery/grid, switch the EMS mode
  instead of re-enabling the charge by hand.

While the pause is active, unreadable Tesla entities (Tessie down) **hold** the pause rather than
being read as "car gone".

### Vampire drain: the EMS never polls the car

The Tesla has a heavy vampire drain, so the loop is deliberately silent around it:

| Situation | Tesla reads | Writes to the car |
|-----------|-------------|-------------------|
| Charging normally on solar, or no EV | **0** | 0 |
| Low-sun countdown running (≤ 5 min) | **0** until it elapses, then 1 | 0 |
| Paused, waiting for the sun | **1 per 5 min** (with the heartbeat log) | 0 |
| Cut / restart decision | 1 | 1 |

Two things matter here and they are different:

- **Reads never reach the car.** `GET /api/states/<entity>` is answered from Home Assistant's
  in-memory state machine; no request goes out to Tessie, and nothing wakes the Tesla. Measured on
  2026-09-11: 200 reads of the four entities in 2 s moved no `last_updated` at all. How often
  Tessie itself polls the car is set by the integration, not by this EMS.
- **Writes do reach the car**, and they are the only ones: exactly one `switch.martine_charge` per
  cut and one per restart.

The 5-minutely read while paused is what catches an unplug, a departure, or a manual re-enable —
without it the EMS could sit on a stale pause with the car left unable to charge. Since these
reads hit HA's cache, their cost is a few bytes on the LAN, not battery.

### Persistence

The flag lives in `logs/ems_state.json` (`{"tesla_paused": …, "tesla_paused_at": …}`) and is
reloaded at startup, so restarting `ems.service` mid-pause doesn't leave the car switched off for
the rest of the day.

### Logs

Everything is traced in `logs/ems.log` under the `TESLA PAUSE` / `TESLA RESUME` prefixes:
countdown armed, countdown cancelled, "not the Tesla charging" when another car is on the cable,
cut (with the full power breakdown), a heartbeat every 5 minutes while paused (`sun_to_ev` vs.
target, SOC vs. prio), and the restart with its reason.

### Constants (in `ems.py`)

| Constant | Default | Meaning |
|----------|---------|---------|
| `TESLA_PAUSE_CONFIRM_S` | 300 s | How long the low sun must hold before cutting |
| `TESLA_CUT_BELOW_W` | 1000 W | `sun_to_ev` under this = the sun no longer covers the charge |
| `TESLA_RESUME_ABOVE_W` | 1200 W | `sun_to_ev` above this restarts it (200 W hysteresis) |
| `TESLA_SETTLE_AFTER_CUT_S` | 120 s | Meter settling time before a restart can be judged |
| `TESLA_PAUSE_LOG_INTERVAL_S` | 300 s | Heartbeat log period while paused |
| `TESLA_STATE_FILE` | `logs/ems_state.json` | Persisted pause flag |

They are deliberately **module constants in `ems.py`**, not `config.py` entries: the Pi's
`config.py` is gitignored and never refreshed by `git pull`, so a new key added here but missing
there would raise `AttributeError` inside the main loop, tick after tick.

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
  - `discharge_limit = target = min(MIN_SOC_LFP(20) + range_needed + SAFETY_MARGIN(10), 100)`
  - `batt_charge_prio = MORNING_BATT_CHARGE_PRIO (25)`, then `target` at 12:24 (`BATT_PRIO_RELEASE_AT`)

This keeps enough charge for the following night without over-charging.

**Deferred `batt_charge_prio`.** Octopus refunds EV kWh at the current tariff (HP refund > HC refund), so in the morning the solar surplus goes to the car first: the prio stays low until the midday off-peak window (12:24–15:24), when the battery is recharged. Rules:
- applied every day, whatever the mode (the prio only matters in the SOLAR modes);
- if `target ≤ 25`, or sunrise happens after 12:24, `target` is written at once;
- if the prio is no longer 25 at 12:24 (changed by hand), it is left alone;
- the pending target is persisted in `logs/ems_state.json`: a restart after 12:24, or on a later day, applies it at once.

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
    ├── ems.log               # Log of each wallbox/battery adjustment
    └── ems_state.json        # Persisted state (Tesla solar pause flag, deferred batt_charge_prio)
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
DEFAULT_EMS_MODE = "SOLAR_ONLY"  # Mode restored when the car is unplugged

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
