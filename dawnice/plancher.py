#!/usr/bin/env python3
"""Plancher de décharge dynamique de la batterie Dawnice, ajusté une fois par jour (8 à 20 %).

Le BMS PACE s'endort quand une cellule reste sous 3,15 V sans courant. C'est le cas au
plancher, où le Deye maintient le SOC par le réseau. Le Deye perd alors ses données CAN,
affiche un SOC fantôme de 99–100 % et réveille le BMS, toutes les 5 min environ (30/10/2026,
cellule 2 à 3,139 V au plancher de 10 %, à 21 °C ; ems.py ignore ces sauts, voir
SOC_GLITCH_JUMP_PCT). Un même SOC ne donne pas la même tension selon la température, la
capacité réapprise par le BMS et l'écart entre cellules : le plancher se règle donc sur la
tension mesurée, pas sur une règle fixe.

Chaque jour, depuis le passage précédent (24 h au plus) :
- marge au plancher = cellule la plus basse au repos (courant nul, SOC au plancher + 1 au
  plus) − 3,15 V, lue dans les relevés Bluetooth du BMS (`dawnice_ble.py` du dépôt
  HA_AppleWatch, `~/.local/share/batterie_dawnice/ble_AAAA-MM.csv`). C'est juste après la
  décharge qu'elle est la plus basse (3,055 V le 30/10, remontée à 3,150 V en 17 h), et le
  BMS s'endort 5 min après l'arrivée au plancher (00:51 → 00:56 UTC le 30/10) ;
- épisodes de veille = sauts de `sensor.deye_battery` du plancher à 95 % ou plus.

Un épisode, ou une marge sous +10 mV : plancher + 2 à 4 points (environ 15 mV par point en
bas de courbe). Une marge au-dessus de +50 mV pendant 3 jours de suite au même plancher, sans
épisode : plancher − 1 point. Pas de passage au plancher : rien ne change. Le plancher est
écrit dans les SOC des 6 créneaux horaires et le seuil faible du Deye (l'arrêt reste à 6 %),
une seule fois par jour au plus : ces réglages vont sans doute en mémoire flash. Si ces
7 valeurs ne sont pas égales (réglage fait à la main), le script n'écrit rien et sort en
erreur.

Une ligne par jour dans `dawnice/data/plancher.csv`, capteur `sensor.ems_plancher_dawnice`
poussé par POST /api/states. Pas de notification (choix de l'utilisateur, 01/11/2026) : le
plancher se lit sur ce capteur et dans power-flow-card-plus (« min N% »).

Les curseurs de l'EMS (`input_number.batt_charge_prio`, `input_number.discharge_limit`,
réglés à la main) suivent le plancher : leur minimum dans HA lui est égal, une valeur restée
en dessous y est remontée, et une valeur posée pile sur l'ancien plancher le suit quand il
descend. Au-dessus, rien n'est touché.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import websockets

BASE = Path(__file__).resolve().parent                  # .../dawnice
# config.py (HA_URL, HA_TOKEN) vit à la racine, ce script dans dawnice/
sys.path.insert(0, str(BASE.parent))
import config  # noqa: E402

log = logging.getLogger("plancher_dawnice")

PLANCHER_MIN, PLANCHER_MAX = 8, 20           # %
HAUSSE_MIN, HAUSSE_MAX, BAISSE = 2, 4, 1     # points
MV_PAR_POINT = 15                            # cellule 2 en bas de courbe : 3,159 V à 17 %, 3,056 V à 10 %
MARGE_HAUSSE_MV = 10                         # sous cette marge au repos : hausse
MARGE_BAISSE_MV = 50                         # au-dessus, jour après jour : baisse
JOURS_BAISSE = 3
SEUIL_VEILLE_V = 3.15                        # paramètre « veille » du BMS, lu le 22/10/2026
SOC_FANTOME = 95                             # SOC affiché quand le BMS s'endort (99–100 %)
COURANT_REPOS_A = 0.5
FENETRE = timedelta(hours=24)
SOC_DEYE = "sensor.deye_battery"
REGLAGES = [f"number.deye_program_{i}_soc" for i in range(1, 7)] + ["number.deye_battery_low_soc"]
CURSEURS = ["batt_charge_prio", "discharge_limit"]   # input_number de l'EMS, minimum = plancher
RELEVES_BLE = Path("~/.local/share/batterie_dawnice").expanduser()
JOURNAL = BASE / "data" / "plancher.csv"
COLONNES = ["horodatage", "plancher_avant", "marge_min_mv", "cellule_min_n", "releves_repos",
            "episodes", "plancher_apres", "raison"]
CAPTEUR = "sensor.ems_plancher_dawnice"


def etat(session: requests.Session, entite: str) -> str:
    resp = session.get(f"{config.HA_URL}/api/states/{entite}", timeout=30)
    resp.raise_for_status()
    return resp.json()["state"]


def marge_au_plancher(debut: datetime, plancher: int) -> tuple[int | None, int | None, int]:
    """Marge minimale (mV) au repos au plancher, n° de la cellule, nombre de relevés retenus."""
    pire, n, compte = None, None, 0
    mois = sorted({f"{debut:%Y-%m}", f"{datetime.now(timezone.utc):%Y-%m}"})
    for m in mois:
        chemin = RELEVES_BLE / f"ble_{m}.csv"
        if not chemin.exists():
            continue
        with chemin.open() as fh:
            for r in csv.DictReader(fh):
                try:
                    if (datetime.fromisoformat(r["horodatage"]) < debut
                            or abs(float(r["courant_a"])) >= COURANT_REPOS_A
                            or float(r["soc"]) > plancher + 1):
                        continue
                    v = float(r["cellule_min_v"])
                except (KeyError, ValueError):
                    continue
                compte += 1
                if pire is None or v < pire:
                    pire, n = v, int(r["cellule_min_n"])
    if pire is None:
        return None, None, 0
    return round((pire - SEUIL_VEILLE_V) * 1000), n, compte


def episodes_veille(session: requests.Session, debut: datetime, fin: datetime, plancher: int) -> int:
    """Sauts du SOC du Deye depuis le plancher jusqu'à 95 % ou plus (BMS endormi)."""
    resp = session.get(
        f"{config.HA_URL}/api/history/period/{debut.isoformat()}",
        params={"end_time": fin.isoformat(), "filter_entity_id": SOC_DEYE,
                "minimal_response": "", "no_attributes": "", "significant_changes_only": "0"},
        timeout=120,
    )
    resp.raise_for_status()
    historique = (resp.json() or [[]])[0]
    sauts, avant = 0, None
    for p in historique:
        try:
            v = float(p["state"])
        except ValueError:
            continue
        if avant is not None and avant <= plancher + 5 and v >= SOC_FANTOME:
            sauts += 1
        avant = v
    return sauts


def lit_journal() -> list[dict]:
    if not JOURNAL.exists():
        return []
    with JOURNAL.open() as fh:
        return list(csv.DictReader(fh))


def jours_marge_haute(lignes: list[dict], plancher: int) -> int:
    """Jours consécutifs, en fin de journal, au même plancher, marge haute et sans épisode."""
    n = 0
    for r in reversed(lignes):
        try:
            if (int(r["plancher_avant"]) != plancher or int(r["plancher_apres"]) != plancher
                    or int(r["episodes"]) or int(r["marge_min_mv"]) <= MARGE_BAISSE_MV):
                break
        except ValueError:  # marge vide : pas de passage au plancher ce jour-là
            break
        n += 1
    return n


def hausse(marge: int | None) -> int:
    """Points à ajouter pour ramener la marge à +10 mV, entre 2 et 4 par jour."""
    if marge is None:
        return HAUSSE_MIN
    return max(HAUSSE_MIN, min(HAUSSE_MAX, math.ceil((MARGE_HAUSSE_MV - marge) / MV_PAR_POINT)))


def decide(plancher: int, marge: int | None, episodes: int, jours_hauts: int) -> tuple[int, str]:
    if episodes:
        return min(PLANCHER_MAX, plancher + hausse(marge)), f"{episodes} mise(s) en veille du BMS"
    if marge is None:
        return plancher, "pas de passage au plancher"
    if marge < MARGE_HAUSSE_MV:
        return min(PLANCHER_MAX, plancher + hausse(marge)), f"marge {marge:+d} mV < +{MARGE_HAUSSE_MV} mV"
    if marge > MARGE_BAISSE_MV and jours_hauts + 1 >= JOURS_BAISSE:
        return max(PLANCHER_MIN, plancher - BAISSE), f"marge > +{MARGE_BAISSE_MV} mV depuis {JOURS_BAISSE} jours"
    return plancher, f"marge {marge:+d} mV"


def ecrit_plancher(session: requests.Session, valeur: int) -> None:
    for entite in REGLAGES:
        resp = session.post(f"{config.HA_URL}/api/services/number/set_value",
                            json={"entity_id": entite, "value": valeur}, timeout=60)
        resp.raise_for_status()
        log.info("%s = %d", entite, valeur)


async def _ws(msgs: list[dict]) -> list:
    async with websockets.connect(config.HA_URL.replace("http", "ws", 1) + "/api/websocket") as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": config.HA_TOKEN}))
        await ws.recv()
        out = []
        for i, m in enumerate(msgs, 1):
            await ws.send(json.dumps({"id": i, **m}))
            rep = json.loads(await ws.recv())
            if not rep.get("success"):
                raise RuntimeError(f"{m['type']} : {rep.get('error')}")
            out.append(rep["result"])
        return out


def aligne_curseurs(session: requests.Session, plancher: int) -> None:
    """Minimum des curseurs de l'EMS = plancher ; valeur sous le plancher remontée, valeur
    posée sur l'ancien plancher descendue avec lui."""
    helpers = {h["id"]: h for h in asyncio.run(_ws([{"type": "input_number/list"}]))[0]}
    anciens = {c: round(helpers[c]["min"]) for c in CURSEURS}
    maj = []
    for c in CURSEURS:
        h = helpers[c]
        if round(h["min"]) != plancher:
            maj.append({"type": "input_number/update", "input_number_id": c,
                        **{k: v for k, v in h.items() if k != "id"}, "min": plancher})
            log.info("input_number.%s : minimum %d → %d", c, round(h["min"]), plancher)
    if maj:
        asyncio.run(_ws(maj))
    for c in CURSEURS:
        entite = f"input_number.{c}"
        valeur = float(etat(session, entite))
        if valeur < plancher:
            raison = "remonté au plancher"
        elif plancher < anciens[c] and round(valeur) == anciens[c]:
            raison = f"descendu avec le plancher (était sur l'ancien, {anciens[c]})"
        else:
            continue
        resp = session.post(f"{config.HA_URL}/api/services/input_number/set_value",
                            json={"entity_id": entite, "value": plancher}, timeout=30)
        resp.raise_for_status()
        log.info("%s %s : %d", entite, raison, plancher)


def ajoute_journal(ligne: list) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    entete = not JOURNAL.exists() or JOURNAL.stat().st_size == 0
    with JOURNAL.open("a", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        if entete:
            w.writerow(COLONNES)
        w.writerow(ligne)
        fh.flush()
        os.fsync(fh.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="affiche la décision sans rien écrire")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {config.HA_TOKEN}"

    reglages = {e: etat(session, e) for e in REGLAGES}
    try:
        valeurs = {round(float(v)) for v in reglages.values()}
    except ValueError:  # unavailable, unknown : illisible
        valeurs = set()
    if len(valeurs) != 1:
        log.warning("Réglages du Deye différents ou illisibles, plancher non touché : %s",
                    ", ".join(f"{e.split('.')[1]} {v}" for e, v in reglages.items()))
        return 1
    plancher = valeurs.pop()

    lignes = lit_journal()
    fin = datetime.now(timezone.utc)
    debut = fin - FENETRE
    if lignes:  # une nuit n'est jugée qu'une fois
        debut = max(debut, datetime.fromisoformat(lignes[-1]["horodatage"]))
    marge, cellule, repos = marge_au_plancher(debut, plancher)
    episodes = episodes_veille(session, debut, fin, plancher)
    nouveau, raison = decide(plancher, marge, episodes, jours_marge_haute(lignes, plancher))
    log.info("Plancher %d %% → %d %% (%s ; marge %s mV cellule %s, %d relevés au repos, %d épisodes)",
             plancher, nouveau, raison, marge, cellule, repos, episodes)
    if args.dry_run:
        return 0

    if nouveau != plancher:
        ecrit_plancher(session, nouveau)
    ajoute_journal([fin.isoformat(timespec="seconds"), plancher, "" if marge is None else marge,
                    cellule or "", repos, episodes, nouveau, raison])
    resp = session.post(f"{config.HA_URL}/api/states/{CAPTEUR}", timeout=30, json={
        "state": nouveau,
        "attributes": {"friendly_name": "EMS plancher batterie Dawnice", "unit_of_measurement": "%",
                       "state_class": "measurement", "icon": "mdi:battery-arrow-down-outline",
                       "releve": fin.isoformat(timespec="seconds"), "avant": plancher, "raison": raison,
                       "marge_min_mv": marge, "cellule_min_n": cellule, "releves_repos": repos,
                       "episodes": episodes, "bornes": [PLANCHER_MIN, PLANCHER_MAX]},
    })
    resp.raise_for_status()
    aligne_curseurs(session, nouveau)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
