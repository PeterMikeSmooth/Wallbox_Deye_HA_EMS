#!/usr/bin/env python3
"""Économie de carburant des deux voitures électriques, publiée dans Home Assistant.

Économie = essence qu'aurait brûlée une thermique sur les mêmes kilomètres
           − ce que la recharge a coûté (maison, superchargeurs, autres).
Les recharges gratuites (travail) n'ont pas de coût : elles sont donc déjà
dans l'économie. Leur valeur (au prix de la maison) est publiée à part.

Hypothèses (validées par l'utilisateur le 22/10/2026) :
  - thermique de référence : 6 L/100 (Ioniq), 7 L/100 (Tesla), essence,
    prix INSEE mensuel du SP95-E10 (série 010596132) ;
  - recharge maison au prix du réseau SANS panneaux, de nuit : 12 c (HSC Total),
    plein tarif HC Urban Solar, ~0,083 €/kWh net chez Octopus. Le solaire mis
    dans les voitures est déjà compté dans les économies des panneaux ;
  - Ioniq avant le Shelly (déc. 2022 → oct. 2023) : 5,4 kWh maison / 100 km ;
  - Tesla jusqu'au 21/10/2026 : chiffres annuels de l'utilisateur ; ensuite
    capteurs sensor.voiture_branchee_* (EMS) pour la maison et sessions Tessie
    (coût réel des superchargeurs ; autres recharges hors maison = gratuites).
    Tessie ne connaît la voiture que depuis le 19/02/2026 ; ses relevés
    d'odomètre remplacent l'estimation des km par kWh.

Usage :
    python3 carburant_archive.py              calcule et publie
    python3 carburant_archive.py --rapport    détail par année et par voiture
"""
import os, sys, json, re, asyncio, argparse, bisect, traceback
import datetime as dt
from zoneinfo import ZoneInfo
import requests, websockets

BASE = os.path.dirname(os.path.abspath(__file__))       # .../voitures
RACINE = os.path.dirname(BASE)
sys.path.insert(0, RACINE)
import config  # noqa: E402

DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
ARCHIVE = os.path.join(DATA, "carburant.json")
HA = {"Authorization": "Bearer " + config.HA_TOKEN, "Content-Type": "application/json"}
TESSIE = "https://api.tessie.com"
MAISON_TESSIE = "477 Chemin"                 # début du lieu enregistré « maison » dans Tessie
TZ = ZoneInfo("Europe/Paris")   # pas l'offset courant : il change à chaque heure d'été/hiver (bug du 25/10/2026)

INSEE = "https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/010596132?startPeriod=2022-12"
L100 = {"ioniq": 6.0, "tesla": 7.0}
BORNE = 1200.0
PRIX = {"ioniq": 23000.0 + BORNE / 2, "tesla": 35000.0 + BORNE / 2}   # base du ROI (borne partagée)

ACHAT = {"ioniq": (dt.date(2022, 12, 15), 18000.0), "tesla": (dt.date(2025, 2, 14), 109000.0)}
ODO = {"ioniq": "sensor.moniq_odometer", "tesla": "sensor.martine_odometer"}
SHELLY = "sensor.shellyem_34945478aee1_channel_2_energy"
SHELLY_RET = "sensor.shellyem_34945478aee1_channel_2_energy_returned"   # pince inversée fin 2023
PAR_VOITURE = {"tesla": "sensor.voiture_branchee_tesla", "ioniq": "sensor.voiture_branchee_ioniq"}
COUPURE = dt.date(2026, 10, 22)          # chiffres utilisateur jusqu'à la veille, capteurs ensuite
SHELLY_DEBUT = dt.date(2023, 11, 1)
IONIQ_MAISON_KWH_KM = 0.054              # avant le Shelly, même rythme qu'en 2024

# Chiffres donnés par l'utilisateur (Tesla, superchargeurs, travail)
PERIODES = [
    # (voiture, clé, début, fin incluse, total)  -> réparti uniformément par jour
    ("tesla", "maison_kwh", dt.date(2025, 2, 14), dt.date(2025, 12, 31), 1878.0),
    ("tesla", "maison_kwh", dt.date(2026, 1, 1), COUPURE - dt.timedelta(days=1), 3368.0),
    ("tesla", "super_eur", dt.date(2025, 2, 14), dt.date(2025, 12, 31), 531.0),
    ("tesla", "super_eur", dt.date(2026, 1, 1), COUPURE - dt.timedelta(days=1), 76.0),
    ("tesla", "autre_eur", dt.date(2025, 2, 14), COUPURE - dt.timedelta(days=1), 86.0),
    ("tesla", "gratuit_kwh", dt.date(2025, 2, 14), dt.date(2025, 12, 31), 1664.0),
    ("ioniq", "super_eur", dt.date(2022, 12, 15), dt.date(2025, 2, 13), 849.0),
    ("ioniq", "gratuit_kwh", dt.date(2025, 1, 1), dt.date(2025, 12, 31), 800.0),
]
# kWh consommés par la Tesla par an, pour répartir ses km quand l'odomètre manque
TESLA_KWH_AN = {2025: 1878 + 1637 + 1664, 2026: 3368 + 224}


def prix_maison(d):
    """€/kWh d'une recharge de nuit à la maison, sans panneaux."""
    if d < dt.date(2024, 7, 5):
        return 0.12                                   # Total Charg'heure, heures super creuses
    if d < dt.date(2025, 12, 11):                     # Urban Solar, plein tarif HC TTC
        if d < dt.date(2025, 2, 1):
            return (0.1076 + 0.021) * 1.2
        if d < dt.date(2025, 8, 1):
            return (0.1076 + 0.0337) * 1.2
        return (0.1063 + 0.02998) * 1.2
    return 0.1656 - 0.9688 * (0.1656 - 0.08)         # Octopus HC, remboursement déduit


# ---------------------------------------------------------------- données HA
async def _ws(msgs):
    async with websockets.connect(config.HA_URL.replace("http", "ws", 1) + "/api/websocket", max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": config.HA_TOKEN}))
        await ws.recv()
        out = []
        for i, m in enumerate(msgs, 1):
            await ws.send(json.dumps({"id": i, **m}))
            out.append(json.loads(await ws.recv())["result"])
        return out


def stats(ids, debut, periode, types):
    return asyncio.run(_ws([{"type": "recorder/statistics_during_period", "start_time": debut.isoformat(),
                             "statistic_ids": ids, "period": periode, "types": types}]))[0]


def etat(eid):
    r = requests.get(config.HA_URL + "/api/states/" + eid, headers=HA, timeout=20)
    return r.json() if r.ok else {}


def historique(ids, debut, fin):
    r = requests.get(f"{config.HA_URL}/api/history/period/{debut.strftime('%Y-%m-%dT%H:%M:%S')}", headers=HA,
                     timeout=120, params={"end_time": fin.strftime('%Y-%m-%dT%H:%M:%S'),
                                          "filter_entity_id": ",".join(ids), "no_attributes": "1"})
    return {l[0]["entity_id"]: l for l in r.json()} if r.ok else {}


# ---------------------------------------------------------------- briques
def prix_essence(a):
    """INSEE mensuel, rafraîchi au plus une fois par jour."""
    if a.get("insee_maj") != dt.date.today().isoformat():
        try:
            t = requests.get(INSEE, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
            obs = re.findall(r'TIME_PERIOD="([0-9-]+)"[^>]*OBS_VALUE="([0-9.]+)"', t) or \
                [(p, v) for v, p in re.findall(r'OBS_VALUE="([0-9.]+)"[^>]*TIME_PERIOD="([0-9-]+)"', t)]
            if obs:
                a["essence"] = {p: float(v) for p, v in obs}
                a["insee_maj"] = dt.date.today().isoformat()
        except Exception as e:
            print(f"  INSEE : {e}")
    return a.get("essence", {})


def odometre_journalier(voiture, aujourd_hui):
    """km au compteur chaque jour, interpolé entre les points connus."""
    d0, km0 = ACHAT[voiture]
    pts = [(d0, km0)]
    rows = stats([ODO[voiture]], dt.datetime.combine(d0, dt.time(), TZ), "month", ["state"]).get(ODO[voiture], [])
    for p in rows:                                   # état en fin de mois
        fin = dt.datetime.fromtimestamp(p["start"] / 1000, TZ).date()
        fin = (fin.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        if p.get("state"):
            pts.append((min(fin, aujourd_hui), float(p["state"])))
    try:
        pts.append((aujourd_hui, float(etat(ODO[voiture])["state"])))
    except (KeyError, ValueError):
        pass
    if voiture == "tesla":
        par_jour = {}
        for x in (TESSIE_SESSIONS or {}).values():       # relevés d'odomètre de chaque session
            par_jour[dt.datetime.fromtimestamp(x["t"], TZ).date()] = x["odo"]
        pts += [(d, km) for d, km in par_jour.items() if d <= aujourd_hui]
    if voiture == "tesla" and not TESSIE_SESSIONS:   # sans Tessie : trou 02/2025 → 08/2026 réparti par kWh
        dernier = max(p[1] for p in pts)
        connu = min((p for p in pts if p[0] > d0), key=lambda p: p[0], default=None)
        if connu:
            tot_kwh = sum(TESLA_KWH_AN.values())
            km_2025 = (connu[1] - km0) * TESLA_KWH_AN[2025] / tot_kwh * (1 if connu[0].year > 2025 else 0)
            if km_2025:
                pts.append((dt.date(2025, 12, 31), km0 + km_2025))
    pts = sorted(set(pts))
    jours = {}
    d = d0
    xs = [p[0].toordinal() for p in pts]
    while d <= aujourd_hui:
        i = bisect.bisect_right(xs, d.toordinal()) - 1
        if i >= len(pts) - 1:
            km = pts[-1][1]
        else:
            (a_, ka), (b_, kb) = pts[i], pts[i + 1]
            km = ka + (kb - ka) * (d.toordinal() - a_.toordinal()) / max(1, b_.toordinal() - a_.toordinal())
        jours[d] = km
        d += dt.timedelta(days=1)
    return jours


def shelly_journalier(aujourd_hui):
    """kWh de la borne par jour ; un trou de données reçoit son rattrapage réparti uniformément."""
    r = stats([SHELLY, SHELLY_RET], dt.datetime.combine(SHELLY_DEBUT, dt.time(), TZ), "hour", ["sum"])
    jours = {}
    for e in (SHELLY, SHELLY_RET):
        rows = [(p["start"] / 1000, p["sum"]) for p in r.get(e, []) if p.get("sum") is not None]
        for (t0, s0), (t1, s1) in zip(rows, rows[1:]):
            delta = max(s1 - s0, 0)
            if delta <= 0:
                continue
            heures = max(1, round((t1 - t0) / 3600))
            for h in range(heures):              # trou : delta étalé sur toutes les heures manquantes
                d = dt.datetime.fromtimestamp(t0 + 3600 * (h + 1), TZ).date()
                jours[d] = jours.get(d, 0) + delta / heures
    return jours


def par_voiture_journalier():
    """kWh par voiture depuis les capteurs de l'EMS (W, moyenne horaire)."""
    r = stats(list(PAR_VOITURE.values()), dt.datetime.combine(COUPURE, dt.time(), TZ), "hour", ["mean"])
    out = {}
    for v, e in PAR_VOITURE.items():
        for p in r.get(e, []):
            d = dt.datetime.fromtimestamp(p["start"] / 1000, TZ).date()
            out.setdefault(v, {})[d] = out.get(v, {}).get(d, 0) + (p.get("mean") or 0) / 1000
    return out


def recharges_ioniq_dehors(a):
    """Sessions de l'Ioniq branchée hors de la maison (travail) sur les 48 dernières heures."""
    fin = dt.datetime.now(TZ)
    h = historique(["device_tracker.moniq_location", "binary_sensor.moniq_ev_battery_plug",
                    "sensor.moniq_ev_battery_level", PAR_VOITURE["ioniq"]], fin - dt.timedelta(hours=48), fin)
    if len(h) < 3:
        return
    borne = [(dt.datetime.fromisoformat(s["last_changed"]).astimezone(TZ), float(s["state"]))
             for s in h.get(PAR_VOITURE["ioniq"], []) if s["state"] not in ("unknown", "unavailable")]
    serie = lambda e: [(dt.datetime.fromisoformat(s["last_changed"]).astimezone(TZ), s["state"]) for s in h[e]]
    lieu, prise, soc = serie("device_tracker.moniq_location"), serie("binary_sensor.moniq_ev_battery_plug"), \
        [(t, float(s)) for t, s in serie("sensor.moniq_ev_battery_level") if s not in ("unknown", "unavailable")]
    def a_t(ser, t):
        v = None
        for tt, s in ser:
            if tt <= t: v = s
        return v
    try:
        cap = float(etat("sensor.moniq_capacite_utile_mesuree")["state"])
        rdt = float(etat("sensor.moniq_rendement_charge_mesure")["state"]) / 100
    except (KeyError, ValueError):
        cap, rdt = 43.0, 0.9
    # sessions branchées ; une débranche de moins de 30 min (souvent un trou du cloud) ne coupe pas
    sessions, debut, coupe = [], None, None
    for t, s in prise + [(fin + dt.timedelta(hours=1), "fin")]:
        if coupe is not None and t - coupe >= dt.timedelta(minutes=30):
            sessions.append((debut, coupe)); debut, coupe = None, None
        if s == "on":
            debut = debut or t
            coupe = None
        elif debut is not None and coupe is None:
            coupe = t
    if debut is not None:
        sessions.append((debut, coupe or fin))
    for debut, t in sessions:
        # la borne a-t-elle alimenté l'Ioniq pendant la session ? -> recharge maison, déjà comptée
        maison = any(debut <= tt <= t and w > 500 for tt, w in borne)
        if not maison and a_t(lieu, debut) not in (None, "home"):
                pts = [v for tt, v in soc if debut - dt.timedelta(minutes=1) <= tt <= t]
                if len(pts) >= 2:
                    gain = max(pts) - pts[0]          # le max ignore les valeurs basses aberrantes
                    if gain > 0:
                        a.setdefault("ioniq_dehors", {})[debut.isoformat(timespec="minutes")] = {
                            "fin": t.isoformat(timespec="minutes"), "soc": [pts[0], max(pts)],
                            "kwh": round(gain / 100 * cap / rdt, 2), "lieu": a_t(lieu, debut)}


TESSIE_SESSIONS = None


def tessie(a):
    """Sessions de recharge de la Tesla (archivées : Tessie ne remonte qu'au 19/02/2026)."""
    global TESSIE_SESSIONS
    a.setdefault("tessie", {})
    jeton = getattr(config, "TESSIE_TOKEN", "")
    if jeton:
        try:
            H = {"Authorization": "Bearer " + jeton}
            vin = requests.get(TESSIE + "/vehicles", headers=H, params={"only_active": "true"}, timeout=30).json()["results"][0]["vin"]
            depuis = max([x["t"] for x in a["tessie"].values()] or [1735689600]) - 7 * 86400
            for s in requests.get(f"{TESSIE}/{vin}/charges", headers=H, timeout=90, params={
                    "distance_format": "km", "format": "json", "from": int(depuis)}).json().get("results", []):
                a["tessie"][str(s["id"])] = {
                    "t": s["started_at"], "odo": s.get("odometer"), "sc": bool(s.get("is_supercharger")),
                    "rapide": bool(s.get("is_fast_charger")), "maison": (s.get("saved_location") or "").startswith(MAISON_TESSIE),
                    "kwh": s.get("energy_used") or s.get("energy_added") or 0, "cout": s.get("cost") or 0}
        except Exception as e:
            print(f"  Tessie : {e}")
    TESSIE_SESSIONS = {k: v for k, v in a["tessie"].items() if v.get("odo")}


# ---------------------------------------------------------------- calcul
def calcule(a):
    auj = dt.date.today()
    essence = prix_essence(a)
    def p_ess(d):
        k = d.strftime("%Y-%m")
        return essence.get(k) or essence[max(essence)]
    odo = {v: odometre_journalier(v, auj) for v in ACHAT}
    shelly = shelly_journalier(auj)
    voit = par_voiture_journalier()
    jours = {}                                       # (voiture, jour) -> dict
    def ajoute(v, d, k, x):
        jours.setdefault((v, d), {}).setdefault(k, 0.0)
        jours[(v, d)][k] += x
    for v, o in odo.items():
        ds = sorted(o)
        for d0, d1 in zip(ds, ds[1:]):
            km = max(o[d1] - o[d0], 0)
            ajoute(v, d1, "km", km)
            ajoute(v, d1, "essence_eur", km * L100[v] / 100 * p_ess(d1))
    for v, k, d0, d1, total in PERIODES:
        if (v, k) == ("tesla", "maison_kwh"):        # au prorata de la borne, pas uniforme
            borne = sum(x for d, x in shelly.items() if d0 <= d <= d1)
            part = min(total / borne, 1.0) if borne else 0
            for d, x in shelly.items():
                if d0 <= d <= d1:
                    ajoute(v, d, k, x * part)
            continue
        n = (d1 - d0).days + 1
        d = d0
        while d <= d1:
            ajoute(v, d, k, total / n)
            d += dt.timedelta(days=1)
    # recharge maison : Tesla (chiffres puis capteurs), Ioniq = borne − Tesla
    for d, kwh in voit.get("tesla", {}).items():
        ajoute("tesla", d, "maison_kwh", kwh)
    d = SHELLY_DEBUT
    while d <= auj:
        tesla = jours.get(("tesla", d), {}).get("maison_kwh", 0)
        ajoute("ioniq", d, "maison_kwh", max(shelly.get(d, 0) - tesla, 0))
        d += dt.timedelta(days=1)
    d = ACHAT["ioniq"][0]
    while d < SHELLY_DEBUT:                          # avant le Shelly
        ajoute("ioniq", d, "maison_kwh", jours.get(("ioniq", d), {}).get("km", 0) * IONIQ_MAISON_KWH_KM)
        d += dt.timedelta(days=1)
    for x in a.get("tessie", {}).values():           # Tesla après la coupure : sessions Tessie hors maison
        d = dt.datetime.fromtimestamp(x["t"], TZ).date()
        if d < COUPURE or x["maison"]:
            continue
        if x["sc"]:
            ajoute("tesla", d, "super_eur", x["cout"])
        else:
            ajoute("tesla", d, "gratuit_kwh", x["kwh"])
    for s in a.get("ioniq_dehors", {}).values():     # recharges gratuites détectées
        ajoute("ioniq", dt.datetime.fromisoformat(s["fin"]).date(), "gratuit_kwh", s["kwh"])
    for (v, d), x in jours.items():
        p = prix_maison(d)
        x["maison_eur"] = x.get("maison_kwh", 0) * p
        x["gratuit_eur"] = x.get("gratuit_kwh", 0) * p
        x["economie"] = x.get("essence_eur", 0) - x["maison_eur"] - x.get("super_eur", 0) - x.get("autre_eur", 0)
    return jours


def somme(jours, pred):
    out = {}
    for (v, d), x in jours.items():
        if pred(d):
            for k, val in x.items():
                out.setdefault(v, {}).setdefault(k, 0.0)
                out[v][k] += val
    for v in out:
        out[v]["litres"] = out[v].get("km", 0) * L100[v] / 100
    tot = {k: sum(out[v].get(k, 0) for v in out) for k in ("economie", "km", "essence_eur", "maison_eur",
                                                           "super_eur", "autre_eur", "gratuit_kwh", "gratuit_eur")}
    return out, tot


def pousse(eid, etat_, nom, attrs):
    try:
        requests.post(config.HA_URL + "/api/states/" + eid, headers=HA, timeout=20, json={"state": etat_, "attributes": {
            "unit_of_measurement": "€", "friendly_name": nom, "state_class": "total", "device_class": "monetary", **attrs}})
    except Exception as e:
        print(f"  capteur {eid} : {e}")


def publie(jours):
    auj = dt.date.today()
    periodes = {"mois": lambda d: (d.year, d.month) == (auj.year, auj.month),
                "annee": lambda d: d.year == auj.year, "vie": lambda d: True}
    r = lambda x: round(x, 2)
    for nom, pred in periodes.items():
        par, tot = somme(jours, pred)
        detail = {v: {"economie": r(x.get("economie", 0)), "km": round(x.get("km", 0)), "litres_evites": round(x["litres"]),
                      "essence_evitee": r(x.get("essence_eur", 0)), "recharge_maison": r(x.get("maison_eur", 0)),
                      "maison_kwh": round(x.get("maison_kwh", 0)), "superchargeurs": r(x.get("super_eur", 0)),
                      "autres": r(x.get("autre_eur", 0))} for v, x in par.items()}
        lib = {"mois": "du mois", "annee": "de l'année", "vie": "à vie"}[nom]
        pousse(f"sensor.economie_carburant_{nom}", r(tot["economie"]), f"Économie carburant {lib}",
               {"km": round(tot["km"]), "par_voiture": detail})
        for v in par:
            pousse(f"sensor.economie_carburant_{v}_{nom}", r(par[v].get("economie", 0)),
                   f"Économie carburant {v.capitalize()} {lib}", detail[v])
        pousse(f"sensor.recharge_gratuite_{nom}", r(tot["gratuit_eur"]), f"Recharges gratuites {lib}",
               {"kwh": round(tot["gratuit_kwh"]), "par_voiture_kwh": {v: round(x.get("gratuit_kwh", 0)) for v, x in par.items()}})
        if nom == "vie":
            pousse("sensor.economie_carburant_nette_vie", r(tot["economie"] - BORNE), "Économie carburant nette (borne déduite)",
                   {"borne": BORNE})
    # ROI : économie des 365 derniers jours / (prix d'achat + moitié de la borne)
    par, _ = somme(jours, lambda d: auj - dt.timedelta(days=365) < d <= auj)
    for v, x in par.items():
        eco = x.get("economie", 0)
        try:
            requests.post(config.HA_URL + f"/api/states/sensor.roi_{v}", headers=HA, timeout=20, json={
                "state": round(100 * eco / PRIX[v], 1), "attributes": {
                    "unit_of_measurement": "%", "state_class": "measurement", "friendly_name": f"ROI {v.capitalize()}",
                    "icon": "mdi:finance", "economie_365j": r(eco), "base": PRIX[v], "km_365j": round(x.get("km", 0))}})
        except Exception as e:
            print(f"  roi {v} : {e}")
    return somme(jours, lambda d: True)


def rapport(jours):
    for an in sorted({d.year for _, d in jours}):
        par, tot = somme(jours, lambda d, an=an: d.year == an)
        for v, x in par.items():
            print(f"  {an} {v:5s} {x.get('km',0):7.0f} km  essence {x.get('essence_eur',0):8.2f}  maison {x.get('maison_kwh',0):6.0f} kWh "
                  f"{x.get('maison_eur',0):7.2f}  superch. {x.get('super_eur',0):6.2f}  autres {x.get('autre_eur',0):5.2f}  "
                  f"gratuit {x.get('gratuit_kwh',0):5.0f} kWh  => {x.get('economie',0):8.2f} €")
    par, tot = somme(jours, lambda d: True)
    print(f"  VIE : économie {tot['economie']:.2f} € ({tot['km']:.0f} km), nette de la borne {tot['economie']-BORNE:.2f} €, "
          f"recharges gratuites {tot['gratuit_kwh']:.0f} kWh ≈ {tot['gratuit_eur']:.2f} €")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rapport", action="store_true")
    o = ap.parse_args()
    a = json.load(open(ARCHIVE)) if os.path.exists(ARCHIVE) else {}
    tessie(a)
    try:
        recharges_ioniq_dehors(a)
    except Exception:
        traceback.print_exc()
    jours = calcule(a)
    tmp = ARCHIVE + ".tmp"
    json.dump(a, open(tmp, "w"), indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, ARCHIVE)
    if o.rapport:
        rapport(jours)
        return
    par, tot = publie(jours)
    print(f"  économie carburant à vie {tot['economie']:.2f} € sur {tot['km']:.0f} km")


if __name__ == "__main__":
    main()
