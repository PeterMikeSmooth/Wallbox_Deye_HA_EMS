#!/usr/bin/env python3
"""Archive permanente des sessions de recharge Octopus.

L'API Kraken ne garde qu'un mois glissant. Ce script relève chaque jour la
fenêtre complète et l'empile dans une base SQLite qui, elle, garde tout.

Identifiants attendus dans l'environnement (voir EnvironmentFile du service) :
    OCTOPUS_EMAIL, OCTOPUS_PASSWORD

Usage :
    python3 octopus_archive.py                     relevé quotidien
    python3 octopus_archive.py --import-wallbox f.csv   amorçage myWallbox
    python3 octopus_archive.py --import-octopus f.json  amorçage Octopus
    python3 octopus_archive.py --rapport 2026-09        détail d'un mois
    python3 octopus_archive.py --capteurs               repousse les capteurs HA seuls
"""
import os, sys, json, csv, csv as _csv, sqlite3, argparse, traceback
import datetime as dt
import requests

import config  # HA_URL, HA_TOKEN

KRAKEN = "https://api.oefr-kraken.energy/v1/graphql/"
COMPTE = os.environ.get("OCTOPUS_ACCOUNT", "A-5EE1FD14")

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "octopus_archive.sqlite")
CSV_OUT = os.path.join(BASE, "octopus_archive.csv")
JSONL_OUT = os.path.join(BASE, "octopus_archive.jsonl")

EV_ENERGIE = "sensor.shellyem_34945478aee1_channel_2_energy"
NOTIFY = "mobile_app_iphone2"
COPIE_NUC = True

# Plages heures creuses du contrat (HC 1H54-6H54 ; 12H24-15H24)
HC_PLAGES = [(dt.time(1, 54), dt.time(6, 54)), (dt.time(12, 24), dt.time(15, 24))]
REMB_OFFSET = 0.08          # Octopus garantit 0,08 €/kWh smartchargé
TARIF_HC, TARIF_HP = 0.1656, 0.2160   # repli si les capteurs Linky sont muets

HA = {"Authorization": "Bearer " + config.HA_TOKEN, "Content-Type": "application/json"}


# --------------------------------------------------------------------------- outils

def local(s):
    """ISO éventuellement en UTC -> datetime naïf en heure locale."""
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    return d


def secondes_hc(a, b):
    """Secondes de [a,b) tombant en heures creuses."""
    if b <= a:
        return 0.0
    tot, j = 0.0, a.date()
    while j <= b.date():
        for h1, h2 in HC_PLAGES:
            lo = max(a, dt.datetime.combine(j, h1))
            hi = min(b, dt.datetime.combine(j, h2))
            if hi > lo:
                tot += (hi - lo).total_seconds()
        j += dt.timedelta(days=1)
    return tot


# --------------------------------------------------------------------------- Octopus

def jeton():
    email = os.environ.get("OCTOPUS_EMAIL")
    mdp = os.environ.get("OCTOPUS_PASSWORD")
    if not (email and mdp):
        raise SystemExit("OCTOPUS_EMAIL / OCTOPUS_PASSWORD absents de l'environnement")
    r = requests.post(KRAKEN, timeout=60, json={
        "query": 'mutation($e:String!,$p:String!){obtainKrakenToken(input:{email:$e,'
                 'password:$p}){token}}',
        "variables": {"e": email, "p": mdp}})
    t = ((r.json().get("data") or {}).get("obtainKrakenToken") or {}).get("token")
    if not t:
        raise RuntimeError(f"authentification Octopus refusée : {r.json().get('errors')}")
    return t


REQ = """query($acc:String!,$first:Int,$after:DateTime){
  devices(accountNumber:$acc){ id name
    ... on SmartFlexChargePoint {
      chargingSessions(first:$first, after:$after){
        pageInfo{ hasNextPage endCursor }
        edges{ node{
          start end stateOfChargeChange stateOfChargeFinal
          energyAdded{ value unit } cost{ amount currency }
          ... on SmartFlexChargingSession { type targetType }
        } } } } } }"""


def sessions_octopus(tok):
    out, curseur, page = [], None, 0
    while True:
        v = {"acc": COMPTE, "first": 100}
        if curseur:
            v["after"] = curseur
        r = requests.post(KRAKEN, headers={"Authorization": tok}, timeout=90,
                          json={"query": REQ, "variables": v})
        j = r.json()
        if j.get("errors"):
            raise RuntimeError(f"Kraken : {j['errors'][0].get('message')}")
        cs = None
        for d in (j.get("data", {}).get("devices") or []):
            if d.get("chargingSessions"):
                cs = d["chargingSessions"]
                break
        if not cs:
            break
        lot = [e["node"] for e in (cs.get("edges") or [])]
        out += lot
        page += 1
        pi = cs.get("pageInfo") or {}
        if not lot or not pi.get("hasNextPage") or not pi.get("endCursor") or page > 60:
            break
        curseur = pi["endCursor"]
    return out


# --------------------------------------------------------------------------- Shelly

def profil_shelly(debut, fin):
    """(hc_kwh, hp_kwh, methode) d'après le vrai profil de puissance, si disponible.

    Renvoie (None, None, 'prorata') quand l'historique fin n'existe plus : HA ne
    garde le détail que ~10 jours, au-delà on retombe sur le prorata temporel.
    """
    try:
        r = requests.get(
            config.HA_URL + f"/api/history/period/{debut.isoformat()}",
            headers={"Authorization": HA["Authorization"]},
            params={"filter_entity_id": EV_ENERGIE,
                    "end_time": (fin + dt.timedelta(minutes=5)).isoformat()},
            timeout=60)
        serie = r.json()[0] if r.status_code == 200 and r.json() else []
    except Exception:
        return None, None, "prorata"
    pts = []
    for p in serie:
        try:
            pts.append((local(p["last_changed"]), float(p["state"])))
        except (ValueError, KeyError, TypeError):
            continue
    if len(pts) < 3:
        return None, None, "prorata"
    hc = hp = 0.0
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        d = v1 - v0
        if d <= 0 or t1 <= t0:
            continue
        f = secondes_hc(t0, t1) / (t1 - t0).total_seconds()
        hc += d * f
        hp += d * (1 - f)
    return (hc, hp, "profil") if (hc + hp) > 0 else (None, None, "prorata")


RETENTION_FINE_J = 9   # HA purge le détail à 10 j : on ne le demande pas au-delà


def ventile(debut, fin, kwh):
    """Répartit kwh en (hc, hp). Profil Shelly si possible, prorata sinon."""
    if fin < dt.datetime.now() - dt.timedelta(days=RETENTION_FINE_J):
        duree = (fin - debut).total_seconds()
        f = secondes_hc(debut, fin) / duree if duree > 0 else 0.0
        return kwh * f, kwh * (1 - f), "prorata"
    hc, hp, methode = profil_shelly(debut, fin)
    if methode == "profil" and kwh > 0:
        tot = hc + hp
        # le Shelly mesure à l'entrée de la borne : on garde la FORME, pas l'échelle
        if abs(tot - kwh) / kwh <= 0.35:
            return kwh * hc / tot, kwh * hp / tot, "profil"
    duree = (fin - debut).total_seconds()
    f = secondes_hc(debut, fin) / duree if duree > 0 else 0.0
    return kwh * f, kwh * (1 - f), "prorata"


# --------------------------------------------------------------------------- base

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  source      TEXT NOT NULL,
  debut       TEXT NOT NULL,
  fin         TEXT,
  kwh         REAL,
  hc_kwh      REAL,
  hp_kwh      REAL,
  methode     TEXT,
  type        TEXT,
  target_type TEXT,
  soc_change  REAL,
  soc_final   REAL,
  cout        REAL,
  devise      TEXT,
  releve_le   TEXT,
  PRIMARY KEY (source, debut)
);
CREATE INDEX IF NOT EXISTS idx_debut ON sessions(debut);
"""


def base():
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    return c


def enregistre(c, source, debut, fin, kwh, **kw):
    """Insère ou met à jour. Ne dégrade jamais une ventilation 'profil' en 'prorata'."""
    cur = c.execute("SELECT methode FROM sessions WHERE source=? AND debut=?",
                    (source, debut.isoformat()))
    row = cur.fetchone()
    if row and row[0] == "profil":
        hc = hp = methode = None          # déjà calculé finement, on n'y touche plus
    else:
        hc, hp, methode = ventile(debut, fin, kwh)
    if row:
        if methode:
            c.execute("""UPDATE sessions SET fin=?,kwh=?,hc_kwh=?,hp_kwh=?,methode=?,
                         cout=?,soc_final=?,releve_le=? WHERE source=? AND debut=?""",
                      (fin.isoformat(), kwh, hc, hp, methode, kw.get("cout"),
                       kw.get("soc_final"), dt.datetime.now().isoformat(),
                       source, debut.isoformat()))
        else:
            c.execute("UPDATE sessions SET fin=?,kwh=?,releve_le=? WHERE source=? AND debut=?",
                      (fin.isoformat(), kwh, dt.datetime.now().isoformat(),
                       source, debut.isoformat()))
        return "maj"
    c.execute("""INSERT INTO sessions(source,debut,fin,kwh,hc_kwh,hp_kwh,methode,type,
                 target_type,soc_change,soc_final,cout,devise,releve_le)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (source, debut.isoformat(), fin.isoformat(), kwh, hc, hp, methode,
               kw.get("type"), kw.get("target_type"), kw.get("soc_change"),
               kw.get("soc_final"), kw.get("cout"), kw.get("devise"),
               dt.datetime.now().isoformat()))
    return "neuf"


# --------------------------------------------------------------------------- sorties

def exporte(c):
    lignes = c.execute("SELECT * FROM sessions ORDER BY debut").fetchall()
    cols = [d[0] for d in c.execute("SELECT * FROM sessions LIMIT 1").description]
    with open(CSV_OUT, "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(cols)
        w.writerows(lignes)
    with open(JSONL_OUT, "w", encoding="utf-8") as fh:
        for l in lignes:
            fh.write(json.dumps(dict(zip(cols, l)), ensure_ascii=False) + "\n")
    return len(lignes)


def tarifs():
    def lire(eid, defaut):
        try:
            r = requests.get(config.HA_URL + "/api/states/" + eid,
                             headers={"Authorization": HA["Authorization"]}, timeout=15)
            return float(r.json()["state"])
        except Exception:
            return defaut
    return (lire("sensor.linky_25348914432154_off_peak_hours_rate", TARIF_HC),
            lire("sensor.linky_25348914432154_peak_hours_rate", TARIF_HP))


def bilan(c, annee_mois):
    """(kwh, hc, hp, bonus_attendu, nb_sessions) pour 'AAAA-MM', source octopus."""
    r = c.execute("""SELECT COALESCE(SUM(kwh),0), COALESCE(SUM(hc_kwh),0),
                            COALESCE(SUM(hp_kwh),0), COUNT(*)
                     FROM sessions WHERE source='octopus' AND debut LIKE ?""",
                  (annee_mois + "%",)).fetchone()
    r_hc, r_hp = tarifs()
    bonus = r[1] * (r_hc - REMB_OFFSET) + r[2] * (r_hp - REMB_OFFSET)
    return r[0], r[1], r[2], bonus, r[3]


def pousse_capteurs(c):
    mois = dt.date.today().strftime("%Y-%m")
    kwh, hc, hp, bonus, n = bilan(c, mois)
    taux = bonus / kwh if kwh else 0

    for eid, etat, unite, nom, attrs in (
        ("sensor.octopus_smartcharge_mois", round(kwh, 2), "kWh",
         "Octopus smartcharge du mois", {"sessions": n, "hc_kwh": round(hc, 2),
                                         "hp_kwh": round(hp, 2),
                                         "part_hc_pct": round(100 * hc / kwh, 1) if kwh else 0}),
        ("sensor.octopus_bonus_attendu", round(bonus, 2), "€",
         "Octopus bonus attendu", {"mois": mois}),
        ("sensor.octopus_taux_moyen", round(taux, 5), "€/kWh",
         "Octopus taux moyen attendu", {"mois": mois}),
    ):
        try:
            requests.post(config.HA_URL + "/api/states/" + eid, headers=HA, timeout=20,
                          json={"state": etat, "attributes": dict(
                              {"unit_of_measurement": unite, "friendly_name": nom,
                               "state_class": "measurement"}, **attrs)})
        except Exception as e:
            print(f"  capteur {eid} : {e}")


def notifie(titre, message):
    try:
        requests.post(config.HA_URL + "/api/services/notify/" + NOTIFY, headers=HA,
                      timeout=20, json={"title": titre, "message": message})
    except Exception as e:
        print(f"  notification impossible : {e}")


def copie_nuc(mois):
    """Dépose le CSV du mois écoulé dans le dossier média de HA (copie de secours).

    HA n'accepte que des images : on déclare image/png, le contenu est stocké tel
    quel et relu à l'octet près. L'extension est mensongère, le nom la trahit.
    """
    if not COPIE_NUC:
        return
    try:
        with open(CSV_OUT, "rb") as fh:
            data = fh.read()
        r = requests.post(config.HA_URL + "/api/media_source/local_source/upload",
                          headers={"Authorization": HA["Authorization"]}, timeout=120,
                          data={"media_content_id": "media-source://media_source/local/."},
                          files={"file": (f"octopus_archive_{mois}.csv.png",
                                          data, "image/png")})
        print(f"  copie NUC : HTTP {r.status_code}")
    except Exception as e:
        print(f"  copie NUC impossible (sans gravité) : {e}")


# --------------------------------------------------------------------------- imports

def importe_wallbox(c, chemin):
    n = 0
    with open(chemin, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            try:
                d = dt.datetime.strptime(r["Start"], "%Y-%m-%d %H:%M:%S")
                f = dt.datetime.strptime(r["End"], "%Y-%m-%d %H:%M:%S")
                k = float(r["Energy (kWh)"].replace(",", "."))
            except (ValueError, KeyError):
                continue
            if f < d:
                f = d
            enregistre(c, "wallbox", d, f, k, type="WALLBOX_EXPORT")
            n += 1
    c.commit()
    print(f"  {n} sessions myWallbox importées")


def importe_octopus_json(c, chemin):
    n = 0
    for x in json.load(open(chemin, encoding="utf-8")):
        d = local(x["start"])
        f = local(x["end"])
        if f < d:
            f = d
        ea = x.get("energyAdded") or {}
        co = x.get("cost") or {}
        enregistre(c, "octopus", d, f, float(ea.get("value") or 0),
                   type=x.get("type"), target_type=x.get("targetType"),
                   soc_change=x.get("stateOfChargeChange"),
                   soc_final=x.get("stateOfChargeFinal"),
                   cout=co.get("amount"), devise=co.get("currency"))
        n += 1
    c.commit()
    print(f"  {n} sessions Octopus importées")


# --------------------------------------------------------------------------- principal

def releve(c):
    tok = jeton()
    ss = sessions_octopus(tok)
    neufs = majs = 0
    for x in ss:
        d = local(x["start"])
        f = local(x["end"])
        if f < d:
            f = d
        ea = x.get("energyAdded") or {}
        co = x.get("cost") or {}
        r = enregistre(c, "octopus", d, f, float(ea.get("value") or 0),
                       type=x.get("type"), target_type=x.get("targetType"),
                       soc_change=x.get("stateOfChargeChange"),
                       soc_final=x.get("stateOfChargeFinal"),
                       cout=co.get("amount"), devise=co.get("currency"))
        neufs += r == "neuf"
        majs += r == "maj"
    c.commit()
    print(f"  {len(ss)} sessions vues : {neufs} nouvelles, {majs} mises à jour")
    return neufs, majs


def rapport(c, mois):
    kwh, hc, hp, bonus, n = bilan(c, mois)
    print(f"\n{mois} — {n} sessions, {kwh:.2f} kWh "
          f"(HC {hc:.2f} / HP {hp:.2f}, {100*hc/kwh if kwh else 0:.1f} % HC)")
    print(f"bonus attendu : {bonus:.2f} €   taux moyen {bonus/kwh if kwh else 0:.5f} €/kWh")
    for r in c.execute("""SELECT debut,fin,kwh,hc_kwh,hp_kwh,methode FROM sessions
                          WHERE source='octopus' AND debut LIKE ? ORDER BY debut""",
                       (mois + "%",)):
        print(f"  {r[0][:16]} -> {r[1][11:16]}  {r[2]:7.2f} kWh   "
              f"HC {r[3]:6.2f} / HP {r[4]:6.2f}  [{r[5]}]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--import-wallbox")
    ap.add_argument("--import-octopus")
    ap.add_argument("--rapport")
    ap.add_argument("--capteurs", action="store_true",
                    help="repousse seulement les 3 capteurs HA depuis la base locale "
                         "(aucun appel à Kraken) — sert à les recréer après un "
                         "redémarrage de HA, qui les efface")
    ap.add_argument("--sans-notif", action="store_true")
    a = ap.parse_args()
    c = base()

    if a.rapport:
        rapport(c, a.rapport)
        return
    if a.capteurs:
        pousse_capteurs(c)
        return
    if a.import_wallbox:
        importe_wallbox(c, a.import_wallbox)
    if a.import_octopus:
        importe_octopus_json(c, a.import_octopus)
    if a.import_wallbox or a.import_octopus:
        print(f"  {exporte(c)} lignes exportées")
        return

    try:
        releve(c)
    except Exception as e:
        traceback.print_exc()
        if not a.sans_notif:
            notifie("Archive Octopus en échec", f"Le relevé a échoué : {e}")
        raise SystemExit(1)

    total = exporte(c)
    pousse_capteurs(c)
    print(f"  base : {total} sessions au total")

    # Le 1er du mois : résumé du mois écoulé + copie de secours vers le NUC
    if dt.date.today().day == 1:
        veille = dt.date.today() - dt.timedelta(days=1)
        mois = veille.strftime("%Y-%m")
        kwh, hc, hp, bonus, n = bilan(c, mois)
        copie_nuc(mois)
        if not a.sans_notif:
            notifie(f"Octopus — bilan {mois}",
                    f"{kwh:.1f} kWh smartchargés sur {n} sessions\n"
                    f"HC {hc:.1f} / HP {hp:.1f} ({100*hc/kwh if kwh else 0:.0f} % HC)\n"
                    f"Bonus attendu : {bonus:.2f} €")


if __name__ == "__main__":
    main()
