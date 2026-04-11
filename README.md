# Wallbox_Deye_HA_EMS

**Solar storage (Deye hybrid inverter) and EV charging management (Wallbox Pulsar Max)**

Script Python tournant sur une Raspberry Pi, pilotant la charge EV et la batterie Deye via l'API Home Assistant.

---

## Table des matières

1. [Objectif](#objectif)
2. [Entités Home Assistant](#entités-home-assistant)
3. [Conventions de signe](#conventions-de-signe)
4. [Architecture logicielle](#architecture-logicielle)
5. [Machine à états](#machine-à-états)
6. [Logique détaillée par état](#logique-détaillée-par-état)
7. [Algorithmes](#algorithmes)
8. [Fichiers du projet](#fichiers-du-projet)
9. [Configuration](#configuration)
10. [Installation & exécution](#installation--exécution)

---

## Objectif

Le Deye (onduleur hybride + batterie) équilibre naturellement la consommation de la maison en déchargeant sa batterie pour maintenir `grid_power ≈ 0`. Problème : lorsqu'un véhicule électrique (EV) est branché, le Deye le considère comme un consommateur lambda et vide la batterie pour l'alimenter.

Ce script a deux missions :

1. **Protéger la batterie** — quand l'EV charge, empêcher la batterie de se décharger pour l'EV. Seule la maison est alimentée par la batterie ; l'EV est alimenté par le grid (et/ou le surplus solaire).
2. **Piloter la charge EV avec le surplus solaire** — quand la batterie a atteint son seuil cible (`batt_charge_limit`), moduler le courant de la wallbox pour consommer le surplus solaire, sans tirer sur le grid ni décharger la batterie.

---

## Entités Home Assistant

### Capteurs (lecture seule)

| Entité | Description | Plage |
|--------|-------------|-------|
| `sensor.deye_battery` | State of Charge (SOC) batterie | 0 – 100 % |
| `sensor.deye_battery_voltage` | Tension batterie | ~50 – 58 V |
| `sensor.deye_battery_power` | Puissance batterie (+ décharge, − charge) | W |
| `sensor.deye_grid_power` | Puissance grid (+ import, − export) | W |
| `sensor.deye_microinverter_power` | Production solaire (micro-onduleurs) | 0 – 5000 W |
| `sensor.deye_load_l1_voltage` | Tension réseau maison | ~230 V |
| `sensor.shellyem_34945478aee1_channel_2_power` | Puissance charge EV (mesure Shelly) | 0 – 7500 W |

### Actionneurs (écriture)

| Entité | Description | Plage |
|--------|-------------|-------|
| `number.deye_battery_max_charging_current` | Courant max charge batterie | 0 – 100 A |
| `number.deye_battery_max_discharging_current` | Courant max décharge batterie | 0 – 100 A |
| `number.wallbox_pulsar_max_sn_429953_maximum_charging_current` | Courant max charge wallbox | 6 – 32 A |

### Variables utilisateur (à créer dans HA)

| Entité | Description | Plage conseillée | Défaut |
|--------|-------------|------------------|--------|
| `input_number.batt_charge_limit` | SOC cible batterie avant activation surplus | 50 – 100 % | 80 % |

---

## Conventions de signe

```
sensor.deye_battery_power :  + = décharge (batterie → maison)
                              − = charge   (solaire → batterie)

sensor.deye_grid_power :     + = import   (grid → maison, on paie)
                              − = export   (maison → grid, on injecte)
```

---

## Architecture logicielle

### Interface unique via Home Assistant

Toutes les lectures de capteurs et les écritures d'actionneurs passent par l'**API REST de Home Assistant**. Pas d'accès direct Modbus ni cloud Wallbox.

**Raisons :**
- Évite les conflits Modbus (un seul maître Modbus autorisé ; HA l'occupe déjà)
- La lib Python Wallbox passe par le cloud (latence, dépendance internet)
- Un seul point d'authentification (token HA)
- Pas de dépendance réseau externe

### Boucle de contrôle

- **Boucle rapide (~1 s)** : lecture des capteurs + évaluation de la machine à états + calcul du courant max de décharge (uniquement en EV_NO_SOLAR). Écriture vers HA **seulement quand la valeur change** pour ne pas marteler le bus Modbus inutilement.
- **Boucle lente (~60 s)** : ajustement du courant wallbox en mode EV_SURPLUS

---

## Machine à états

Le script fonctionne comme une **machine à états unique** — pas de mode "jour/nuit" explicite. Le seuil de production solaire (100 W) détermine naturellement les transitions.

```
                    EV débranchée
                   ┌────────────┐
                   │            │
                   │   IDLE     │
                   │            │
                   └─────┬──────┘
                         │ EV branchée (Shelly > 40 W)
                         ▼
              ┌─────────────────────┐
              │  Solaire > 100 W ?  │
              └──┬──────────────┬───┘
                 │ Non          │ Oui
                 ▼              ▼
    ┌────────────────┐  ┌───────────────────────┐
    │  EV_NO_SOLAR   │  │  SOC < batt_charge_   │
    │                │  │  limit ?               │
    │  Bride décharge│  └──┬────────────────┬────┘
    │  batterie      │     │ Oui            │ Non
    │  (house-only)  │     ▼                ▼
    │                │  ┌──────────────┐ ┌──────────────┐
    │  Ne touche PAS │  │ EV_BATTERY_  │ │ EV_SURPLUS   │
    │  au courant    │  │ PRIORITY     │ │              │
    │  wallbox       │  │              │ │ Stop charge  │
    └────────────────┘  │ Wallbox = 6A │ │ batterie     │
                        │ Charge batt  │ │ Pilote       │
                        │ libre        │ │ wallbox avec │
                        │ Décharge     │ │ surplus      │
                        │ libre (100A) │ │ Décharge     │
                        │              │ │ libre (100A) │
                        └──────┬───────┘ └──────┬───────┘
                               │                │
                               │  SOC ≥ limit   │
                               │───────────────►│
                               │                │
                               │◄───────────────│
                               │ SOC < limit-5% │
                               │ (hystérésis)   │
```

### Transitions

| De → Vers | Condition |
|-----------|-----------|
| **IDLE → EV_NO_SOLAR** | Shelly > 40 W ET microinverter ≤ 100 W |
| **IDLE → EV_BATTERY_PRIORITY** | Shelly > 40 W ET microinverter > 100 W ET SOC < `batt_charge_limit` |
| **IDLE → EV_SURPLUS** | Shelly > 40 W ET microinverter > 100 W ET SOC ≥ `batt_charge_limit` |
| **EV_NO_SOLAR → IDLE** | Shelly ≤ 40 W |
| **EV_NO_SOLAR → EV_BATTERY_PRIORITY** | microinverter > 100 W ET SOC < `batt_charge_limit` |
| **EV_NO_SOLAR → EV_SURPLUS** | microinverter > 100 W ET SOC ≥ `batt_charge_limit` |
| **EV_BATTERY_PRIORITY → IDLE** | Shelly ≤ 40 W |
| **EV_BATTERY_PRIORITY → EV_NO_SOLAR** | microinverter ≤ 100 W (+ wallbox → 6 A) |
| **EV_BATTERY_PRIORITY → EV_SURPLUS** | SOC ≥ `batt_charge_limit` |
| **EV_SURPLUS → IDLE** | Shelly ≤ 40 W |
| **EV_SURPLUS → EV_NO_SOLAR** | microinverter ≤ 100 W (+ wallbox → 6 A) |
| **EV_SURPLUS → EV_BATTERY_PRIORITY** | SOC < (`batt_charge_limit` − 5) |

---

## Logique détaillée par état

### État IDLE (EV non branchée)

- **Déclencheur** : `shellyem_power ≤ 40 W`
- **Actions à l'entrée** :
  - `deye_battery_max_discharging_current` → 100 A
  - `deye_battery_max_charging_current` → 100 A
- **Le script ne touche PAS au courant wallbox** (l'utilisateur gère start/stop manuellement)
- **Comportement** : le Deye fonctionne avec son propre algorithme d'équilibrage. Aucune intervention.

### État EV_NO_SOLAR (EV branchée, pas de solaire)

- **Déclencheur** : `shellyem_power > 40 W` ET `microinverter_power ≤ 100 W`
- **Actions à l'entrée** :
  - `deye_battery_max_charging_current` → 100 A (charge batterie possible si grid le permet)
- **Boucle rapide (1 s)** : calcule et applique `deye_battery_max_discharging_current` → courant house-only (voir [Algorithme de limitation décharge](#algorithme-de-limitation-de-décharge)). Écriture uniquement quand la valeur arrondie change.
- **Ne touche PAS au courant wallbox** : la nuit, le grid alimente l'EV. L'utilisateur contrôle le courant wallbox manuellement.
- **Résultat** : la batterie alimente uniquement la maison. Le grid couvre l'EV.

### État EV_BATTERY_PRIORITY (solaire disponible, batterie en charge)

- **Déclencheur** : `shellyem_power > 40 W` ET `microinverter_power > 100 W` ET `SOC < batt_charge_limit`
- **Actions à l'entrée** :
  - `deye_battery_max_charging_current` → 100 A
  - `deye_battery_max_discharging_current` → 100 A (le Deye gère librement, le soleil couvre les besoins)
  - `wallbox_max_current` → 6 A (minimum, pour laisser le solaire charger la batterie)
- **Pas de limitation de décharge** : le soleil brille suffisamment, la batterie ne se décharge pas naturellement. Pas besoin de brider. Si un transitoire fait brièvement décharger la batterie, c'est le comportement normal et souhaité du Deye.
- **Comportement** : le Deye utilise le surplus solaire pour charger la batterie (son propre algo). La wallbox reste au minimum (6 A ≈ 1,4 kW monophasé, couvert par le grid).
- **Transition** : quand `SOC ≥ batt_charge_limit` → passage en EV_SURPLUS

### État EV_SURPLUS (surplus solaire → charge EV)

- **Déclencheur** : `SOC ≥ batt_charge_limit` (ou retour en surplus après hystérésis)
- **Actions à l'entrée** :
  - `deye_battery_max_charging_current` → 0 A (stop charge batterie, toute la production solaire est disponible)
  - `deye_battery_max_discharging_current` → 100 A (le Deye gère librement les transitoires)
- **Pas de limitation de décharge** : le soleil couvre la maison. La batterie peut brièvement se décharger pour couvrir un pic de consommation (comportement normal du Deye). Notre script ajustera la wallbox à la minute suivante pour compenser.
- **Boucle lente (60 s)** : ajustement du courant wallbox via l'[Algorithme de pilotage surplus](#algorithme-de-pilotage-surplus)
- **Hystérésis** : si `SOC < (batt_charge_limit − 5)` :
  - `wallbox_max_current` → 6 A
  - `deye_battery_max_charging_current` → 100 A
  - Transition vers EV_BATTERY_PRIORITY

---

## Algorithmes

### Algorithme de limitation de décharge

**Objectif** : la batterie ne se décharge que pour alimenter la maison, pas l'EV.

**Bilan de puissance** (conservation de l'énergie) :

```
microinverter + battery_power + grid_power = house_load + ev_power
```

D'où :

```
house_load = microinverter + battery_power + grid_power − ev_power
```

Le courant de décharge maximum autorisé :

```
desired_discharge_A = max(house_load, 0) / battery_voltage + MARGIN
desired_discharge_A = clamp(desired_discharge_A, 0, 100)
```

- `MARGIN` : marge de sécurité de +1 A pour éviter les oscillations
- Lissage par moyenne mobile exponentielle (EMA) pour stabiliser la consigne :
  `smoothed = α × new_value + (1 − α) × previous_smoothed` avec `α ≈ 0.3`
- Calculé toutes les **1 seconde**, mais écriture vers HA **uniquement quand la valeur arrondie (entier) change** par rapport à la dernière écrite
- **Actif uniquement en état EV_NO_SOLAR** — dans les états solaires, le décharge est laissée à 100 A (le soleil couvre les besoins)
- Le calcul de `house_load` est valide même si le courant de décharge est déjà bridé (les mesures reflètent l'état réel du système)

### Algorithme de pilotage surplus

**Objectif** : ajuster le courant wallbox pour que `grid ≈ 0` ET `battery_power ≈ 0`.

En mode surplus, `max_charging_current = 0`, donc la batterie ne se charge pas. On veut que **tout le surplus solaire aille dans l'EV**.

**Calcul du nouveau courant wallbox** :

```
surplus_disponible = ev_power_actuelle − grid_power − battery_power
wallbox_current_new = surplus_disponible / grid_voltage
wallbox_current_new = clamp(round(wallbox_current_new), 6, 32)
```

Explication :
- Si `grid_power > 0` (import) → on consomme trop → on réduit wallbox
- Si `grid_power < 0` (export) → surplus inutilisé → on augmente wallbox
- Si `battery_power > 0` (décharge) → la batterie aide l'EV/maison → on réduit wallbox
- Si `battery_power < 0` (charge) → ne devrait pas arriver (max_charging = 0), si oui → on augmente wallbox

Le calcul intègre automatiquement les variations de consommation maison : si la maison consomme plus, `grid_power` augmente, ce qui réduit `surplus_disponible` et donc le courant wallbox.

**Fréquence** : une fois par **minute**. Entre deux ajustements, la batterie peut brièvement se décharger pour couvrir la maison (comportement normal et souhaité du Deye).

---

## Fichiers du projet

```
Wallbox_Deye_HA_EMS/
├── README.md                 # Ce fichier (spécification)
├── LICENSE
├── config_example.py         # Exemple de configuration (commité)
├── config.py                 # Configuration réelle (non commité, dans .gitignore)
├── requirements.txt          # Dépendances Python (requests uniquement)
├── ha_api.py                 # Wrapper API Home Assistant (lecture capteurs, écriture actionneurs)
├── ems.py                    # Logique EMS : machine à états, algorithmes, boucle principale
└── logs/                     # Répertoire de logs (non commité)
    └── ems.log               # Log de chaque réglage wallbox/batterie
```

---

## Configuration

### `config_example.py`

Fichier Python pur — zéro dépendance pour le parsing. À copier en `config.py` et personnaliser.

```python
# config_example.py — copier en config.py et renseigner vos valeurs

# Home Assistant
HA_URL = "http://192.168.1.XXX:8123"
HA_TOKEN = "VOTRE_LONG_LIVED_ACCESS_TOKEN"

# Seuils
EV_CHARGING_DETECT_W = 40        # Shelly > 40 W = EV branchée
SOLAR_AVAILABLE_W = 100           # Microinverter > 100 W = solaire disponible
SOC_HYSTERESIS_PCT = 5            # Hystérésis SOC en points absolus

# Algorithme
FAST_LOOP_INTERVAL_S = 1          # Boucle rapide (lecture capteurs + limitation décharge)
SLOW_LOOP_INTERVAL_S = 60         # Boucle lente (pilotage wallbox)
DISCHARGE_MARGIN_A = 1.0          # Marge courant décharge (+1 A)
EMA_ALPHA = 0.3                   # Facteur lissage EMA

# Valeurs par défaut (état IDLE)
DEFAULT_MAX_CHARGING_CURRENT_A = 100
DEFAULT_MAX_DISCHARGING_CURRENT_A = 100
WALLBOX_MIN_CURRENT_A = 6
WALLBOX_MAX_CURRENT_A = 32

# Logging
LOG_FILE = "logs/ems.log"
LOG_LEVEL = "INFO"                # DEBUG pour diagnostic
```

### Sécurité

- `config.py` contient le token HA → **ajouté au `.gitignore`**
- Seul `config_example.py` est commité (sans secrets)

---

## Installation & exécution

```bash
# Cloner le repo
git clone <repo_url>
cd Wallbox_Deye_HA_EMS

# Installer les dépendances
pip install -r requirements.txt

# Copier et éditer la configuration
cp config_example.py config.py
nano config.py      # Renseigner IP HA + token

# Créer input_number.batt_charge_limit dans HA :
#   - Aller dans Paramètres > Appareils & Services > Entrées
#   - Créer un "Nombre" nommé "batt_charge_limit"
#   - Min: 50, Max: 100, Pas: 5, Unité: %, Défaut: 80

# Lancer le script
python ems.py

# (Optionnel) Lancer en service systemd pour démarrage automatique
```

### Service systemd (optionnel)

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
