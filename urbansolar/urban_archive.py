#!/usr/bin/env python3
"""Archive du rachat de surplus Urban Solar (« Boost Mon Surplus »).

L'espace client ne montre que les 12 derniers mois. Ce script les relève une
fois par jour, les empile dans une archive JSON qui garde tout, et publie dans
Home Assistant le solde, le dernier prix de rachat et l'abonnement estimé.

Identifiants dans config.py (gitignoré) : URBAN_EMAIL, URBAN_PASSWORD, URBAN_PDL,
URBAN_KWC.

Sources (API de l'espace client, la même que le site) :
  - GetBMSPricing          grille des prix mensuels, publique
  - getBMSInjectionFromLastYear   kWh injectés et prix par mois civil, avec
                           connexion. Les kWh viennent des index Enedis (du 10 au
                           10) répartis sur les mois civils : ce n'est pas le
                           tableau « Mon surplus » du site, mais c'est ce qui
                           compose « Mon solde », donc ce qui sera payé.
  - GetInjectionIndexes    index Enedis journaliers (Wh) : les kWh injectés exacts
                           par jour, J-1. C'est la source du tableau « Mon surplus »,
                           qui valorise les mois sans prix au dernier prix connu.

Usage :
    python3 urban_archive.py              relevé si le dernier date de plus de 20 h,
                                          puis publication des capteurs
    python3 urban_archive.py --force      relevé immédiat
    python3 urban_archive.py --capteurs   republie seulement, depuis l'archive
    python3 urban_archive.py --rapport    détail mois par mois
"""
import os, sys, json, base64, argparse, traceback, urllib.parse
import datetime as dt
import requests

BASE = os.path.dirname(os.path.abspath(__file__))       # .../urbansolar
RACINE = os.path.dirname(BASE)                          # racine du dépôt
sys.path.insert(0, RACINE)
import config  # noqa: E402

SITE = "https://espace-client.urbansolar.energy"
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
ARCHIVE = os.path.join(DATA, "urban_archive.json")

PRIX_HA = "input_number.prix_rachat_surplus"   # utilisé par les templates d'économie
ABO_HA = "input_number.cout_abonnement_rachat"  # €/kWh injecté, idem
DEBUT_ABO = "2025-12"                          # contrat BMS du 17/12/2025 : la règle du 01/09/2026
                                               # (surplus annuel / 500) est supposée s'appliquer à toute
                                               # la 1re année, réglée à l'anniversaire (à vérifier le 17/12/2026)
NOTIFY = "mobile_app_iphone2"
RELEVE_MAX_AGE = dt.timedelta(hours=20)

# Abonnement Boost depuis les CGA du 01/09/2026 (art. 7.1) :
#   € HT / kWc / mois = surplus annuel / (kWc × 500), gratuit si surplus ≤ 100 kWh/kWc.
# Les kWc s'annulent : au-dessus du seuil, l'abonnement vaut surplus / 500 € HT par mois.
# Il est retenu au règlement annuel (art. 9.2), d'où une simple estimation ici.
SEUIL_KWH_PAR_KWC = 100
TVA = 1.20

HA = {"Authorization": "Bearer " + config.HA_TOKEN, "Content-Type": "application/json"}

REQ_PRIX = "query GetBMSPricing { getBMSPricing { startDate endDate price } }"
REQ_SURPLUS = """query getConsumptions($pdlId: String!) {
  getBMSInjectionFromLastYear(pdlId: $pdlId) {
    consumptions { startDate endDate consumptions { volume notInvoicedVolume type price isInvoiced } }
    injectionIndexes { startDate startIndex endDate endIndex quantity previousMonthInj currentMonthInj }
  }
}"""
REQ_KWC = "query getPdlPVCapacity($pdlId: String!) { getPdlPVCapacity(pdlId: $pdlId) }"
REQ_JOURS = """query GetInjectionIndexes($pdl: String!, $startDate: DateTime!, $endDate: DateTime!) {
  getInjectionIndexes(pdl: $pdl, startDate: $startDate, endDate: $endDate) { d c { c i v } }
}"""
DEBUT_BOOST = dt.date(2025, 12, 1)     # 1er mois facturé par Boost Mon Surplus


def mois_de(iso):
    """'2026-03-01T00:00:01.000Z' -> '2026-03' (les mois commencent à 00:00:01 UTC)."""
    return iso[:7]


class Urban:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 (urban_archive)"

    def connexion(self):
        # Le site envoie les identifiants en Basic, mot de passe encodé comme
        # encodeURIComponent, et reçoit un cookie « bearer ».
        mdp = urllib.parse.quote(config.URBAN_PASSWORD, safe="-_.!~*'()")
        jeton = base64.b64encode(f"{config.URBAN_EMAIL}:{mdp}".encode()).decode()
        r = self.s.post(SITE + "/api/login", json={"rememberMe": False}, timeout=30,
                        headers={"Authorization": "Basic " + jeton})
        if r.status_code != 200 or "bearer" not in self.s.cookies:
            raise RuntimeError(f"connexion refusée ({r.status_code})")

    def gql(self, nom, requete, variables=None):
        h = {}
        if "bearer" in self.s.cookies:
            h["Authorization"] = "Bearer " + self.s.cookies["bearer"]
        r = self.s.post(SITE + "/api/graphql", headers=h, timeout=30,
                        json={"operationName": nom, "query": requete, "variables": variables or {}})
        r.raise_for_status()
        rep = r.json()
        if rep.get("errors"):
            raise RuntimeError(f"{nom} : {rep['errors'][0].get('message')}")
        return rep["data"]


def charge():
    if os.path.exists(ARCHIVE):
        with open(ARCHIVE) as f:
            return json.load(f)
    return {"mois": {}, "grille": {}, "index": [], "jours": {}, "kwc": None, "releve": None}


def sauve(a):
    tmp = ARCHIVE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(a, f, indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, ARCHIVE)


def releve(a):
    u = Urban()
    # 1. grille publique des prix
    for p in u.gql("GetBMSPricing", REQ_PRIX)["getBMSPricing"]:
        a["grille"][mois_de(p["startDate"])] = p["price"]
    # 2. surplus mensuel (connexion)
    u.connexion()
    d = u.gql("getConsumptions", REQ_SURPLUS, {"pdlId": config.URBAN_PDL})["getBMSInjectionFromLastYear"]
    now = dt.datetime.now().isoformat(timespec="seconds")
    for m in d["consumptions"]:
        lignes = [c for c in m["consumptions"] if c.get("type") == "Injection"]
        if not lignes:
            continue
        a["mois"][mois_de(m["startDate"])] = {
            "kwh": sum(c["volume"] for c in lignes),
            "non_facture_kwh": sum(c["notInvoicedVolume"] for c in lignes),
            "prix": max(c["price"] for c in lignes),     # 0 = pas encore publié
            "facture": all(c["isInvoiced"] for c in lignes),
            "maj": now,
        }
    a["index"] = d["injectionIndexes"]
    try:
        a["kwc"] = float(u.gql("getPdlPVCapacity", REQ_KWC, {"pdlId": config.URBAN_PDL})["getPdlPVCapacity"])
    except Exception:
        pass      # facultatif : on garde config.URBAN_KWC
    # 3. index journaliers Enedis, mois par mois (fin ≤ aujourd'hui, sinon refus Enedis)
    a.setdefault("jours", {})
    aujourd_hui = dt.date.today()
    debut = DEBUT_BOOST if not a["jours"] else (aujourd_hui.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    while debut < aujourd_hui:
        fin = min((debut + dt.timedelta(days=32)).replace(day=1), aujourd_hui)
        for p in u.gql("GetInjectionIndexes", REQ_JOURS, {"pdl": config.URBAN_PDL,
                       "startDate": f"{debut}T00:00:00.000Z", "endDate": f"{fin}T00:00:00.000Z"})["getInjectionIndexes"]:
            v = sum(x["v"] for x in (p.get("c") or []) if x.get("v") is not None)
            if any(x.get("v") is not None for x in (p.get("c") or [])):
                a["jours"][p["d"]] = round(v / 1000, 3)          # Wh -> kWh
        debut = fin
    a["releve"] = now


def bilan(a):
    mois = dict(sorted(a["mois"].items()))
    valorise = {k: v for k, v in mois.items() if v["prix"] > 0}
    solde = sum(v["non_facture_kwh"] * v["prix"] for v in valorise.values() if not v["facture"])
    vie = sum(v["kwh"] * v["prix"] for v in valorise.values())
    en_attente = {k: v["kwh"] for k, v in mois.items() if v["prix"] <= 0}
    # dernier prix connu : le tableau personnel est publié avant la grille publique
    prix = {**{k: p for k, p in a["grille"].items() if p > 0}, **{k: v["prix"] for k, v in valorise.items()}}
    dernier = max(prix) if prix else None
    # abonnement estimé sur les 12 derniers mois connus
    douze = list(mois.values())[-12:]
    surplus12 = sum(v["kwh"] for v in douze)
    kwc = a.get("kwc") or config.URBAN_KWC
    abo_ht_mois = 0.0 if surplus12 <= SEUIL_KWH_PAR_KWC * kwc else surplus12 / 500
    # coût marginal : 12 mois × (1/500) € HT par kWh de surplus annuel, TTC
    abo_kwh = (12 / 500 * TVA) if abo_ht_mois > 0 and dt.date.today().strftime("%Y-%m") >= DEBUT_ABO else 0.0
    # kWh exacts par mois civil (Enedis journalier) et solde estimé façon site : les mois
    # postérieurs au dernier mois valorisé comptent au dernier prix connu
    enedis = {}
    for j, kwh in a.get("jours", {}).items():
        enedis[j[:7]] = enedis.get(j[:7], 0) + kwh
    dernier_valorise = max(valorise) if valorise else ""
    provisoire = {k: kwh for k, kwh in sorted(enedis.items()) if k > dernier_valorise}
    estime = solde + sum(provisoire.values()) * (prix.get(dernier) or 0)
    return {
        "solde": solde, "vie": vie, "en_attente": en_attente,
        "dernier_mois": dernier, "dernier_prix": prix.get(dernier),
        "surplus12": surplus12, "nb_mois12": len(douze), "kwc": kwc,
        "abo_ttc_an": abo_ht_mois * 12 * TVA, "abo_kwh": abo_kwh, "mois": mois,
        "enedis": dict(sorted(enedis.items())), "provisoire": provisoire, "solde_estime": estime,
        "dernier_jour": max(a.get("jours", {}) or [None]),
    }


def pousse(eid, etat, unite, nom, attrs, classe="measurement", device=None):
    att = {"unit_of_measurement": unite, "friendly_name": nom, "state_class": classe, **attrs}
    if device:
        att["device_class"] = device
    try:
        requests.post(config.HA_URL + "/api/states/" + eid, headers=HA, timeout=20,
                      json={"state": etat, "attributes": att})
    except Exception as e:
        print(f"  capteur {eid} : {e}")


def pousse_capteurs(a):
    b = bilan(a)
    detail = {k: f"{v['kwh']} kWh × {v['prix']*100:.3f} c" if v["prix"] > 0 else f"{v['kwh']} kWh, prix à venir"
              for k, v in b["mois"].items()}
    commun = {"releve": a.get("releve")}
    pousse("sensor.urban_solde", round(b["solde"], 2), "€", "Urban Solar solde",
           {**commun, "kwh_en_attente_de_prix": sum(b["en_attente"].values()),
            "mois_en_attente": list(b["en_attente"]), "detail": detail}, "total", "monetary")
    pousse("sensor.urban_solde_estime", round(b["solde_estime"], 2), "€", "Urban Solar solde estimé",
           {**commun, "provisoire_kwh": {k: round(v, 1) for k, v in b["provisoire"].items()},
            "prix_provisoire": b["dernier_prix"],
            "note": "mois sans prix valorisés au dernier prix connu, comme le tableau du site"}, "total", "monetary")
    mois_courant = dt.date.today().strftime("%Y-%m")
    pousse("sensor.urban_injection_mois", round(b["enedis"].get(mois_courant, 0), 2), "kWh",
           "Urban Solar injection du mois (Enedis)",
           {**commun, "jusqu_au": b["dernier_jour"], "par_mois": {k: round(v, 1) for k, v in b["enedis"].items()}},
           "total", "energy")
    pousse("sensor.urban_rachat_vie", round(b["vie"], 2), "€", "Urban Solar rachat valorisé à vie",
           {**commun, "premier_mois": next(iter(b["mois"]), None)}, "total", "monetary")
    if b["dernier_prix"]:
        pousse("sensor.urban_prix_rachat", round(b["dernier_prix"], 5), "€/kWh", "Urban Solar prix de rachat",
               {**commun, "mois": b["dernier_mois"]})
    pousse("sensor.urban_abonnement_estime", round(b["abo_ttc_an"], 2), "€", "Urban Solar abonnement estimé (an, TTC)",
           {**commun, "surplus_12_mois_kwh": b["surplus12"], "mois_comptes": b["nb_mois12"],
            "kwc": b["kwc"], "seuil_gratuite_kwh": SEUIL_KWH_PAR_KWC * b["kwc"],
            "cout_par_kwh_injecte": round(b["abo_kwh"], 4),
            "regle": "CGA 01/09/2026 : surplus annuel / (kWc × 500) € HT/kWc/mois, retenu au règlement annuel"})
    # les réglages des templates d'économie suivent le dernier prix publié et l'abonnement
    if b["dernier_prix"]:
        regle(PRIX_HA, b["dernier_prix"], b["dernier_mois"])
    regle(ABO_HA, b["abo_kwh"], f"{b['surplus12']} kWh sur 12 mois")
    return b


def regle(eid, valeur, pourquoi):
    try:
        r = requests.get(config.HA_URL + "/api/states/" + eid, headers=HA, timeout=20)
        actuel = float(r.json()["state"]) if r.ok else None
        cible = round(valeur, 4)                     # pas des input_number : 0,0001
        if actuel is None or abs(actuel - cible) > 1e-6:
            requests.post(config.HA_URL + "/api/services/input_number/set_value", headers=HA, timeout=20,
                          json={"entity_id": eid, "value": cible})
            print(f"  {eid} : {actuel} -> {cible} ({pourquoi})")
    except Exception as e:
        print(f"  {eid} : {e}")


def rapport(a):
    b = bilan(a)
    for k, v in b["mois"].items():
        p = f"{v['prix']*100:6.3f} c" if v["prix"] > 0 else "   à venir"
        print(f"  {k}  {v['kwh']:5} kWh  {p}  {v['kwh']*v['prix']:6.2f} €  {'facturé' if v['facture'] else ''}")
    print(f"  solde {b['solde']:.2f} €   vie {b['vie']:.2f} €   en attente {b['en_attente']}")
    print("  Enedis journalier :", {k: round(v, 1) for k, v in b["enedis"].items()}, "jusqu'au", b["dernier_jour"])
    print(f"  solde estimé {b['solde_estime']:.2f} € (provisoire {b['provisoire']} au dernier prix)")
    print(f"  dernier prix {b['dernier_prix']} ({b['dernier_mois']})   "
          f"abonnement estimé {b['abo_ttc_an']:.2f} € TTC/an sur {b['surplus12']} kWh ({b['kwc']} kWc)")


def notifie(titre, message):
    try:
        requests.post(config.HA_URL + "/api/services/notify/" + NOTIFY, headers=HA,
                      timeout=20, json={"title": titre, "message": message})
    except Exception as e:
        print(f"  notification impossible : {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="relève même si le dernier relevé est récent")
    ap.add_argument("--capteurs", action="store_true",
                    help="republie seulement les capteurs depuis l'archive (aucun appel à Urban Solar)")
    ap.add_argument("--rapport", action="store_true")
    ap.add_argument("--sans-notif", action="store_true")
    a_ = ap.parse_args()
    a = charge()

    if a_.rapport:
        rapport(a)
        return
    dernier = dt.datetime.fromisoformat(a["releve"]) if a.get("releve") else None
    if not a_.capteurs and (a_.force or not dernier or dt.datetime.now() - dernier > RELEVE_MAX_AGE):
        try:
            releve(a)
            sauve(a)
            print(f"  relevé : {len(a['mois'])} mois en archive")
        except Exception as e:
            traceback.print_exc()
            # un échec isolé n'est pas grave (relevé suivant dans 1 h) ; on prévient au-delà de 3 jours
            if not a_.sans_notif and (not dernier or dt.datetime.now() - dernier > dt.timedelta(days=3)):
                notifie("Urban Solar : relevé en échec", f"{e} — identifiants ou site à vérifier")
    if a["mois"]:
        b = pousse_capteurs(a)
        print(f"  solde {b['solde']:.2f} €, prix {b['dernier_prix']} ({b['dernier_mois']})")


if __name__ == "__main__":
    main()
