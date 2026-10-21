#!/usr/bin/env python3
"""Surveillance des chutes de charge à ~1,2 kW (lecture seule).

Le 15/10, la Tesla est tombée 9 fois à 6 A pendant ~8 min alors que l'EMS
demandait 20-32 A, et chaque chute coïncidait avec un gel de la passerelle BLE.
Ni la passerelle (journal de ~16 Ko) ni HA n'en gardent trace : ce service
enregistre chaque épisode, avec les 10 minutes qui le précèdent.

Deux déclencheurs, voiture branchée :
  - chute    : Shelly entre 600 et 1700 W alors que la consigne EMS >= 10 A ;
  - gel      : la passerelle ne reçoit plus de trame fraîche de la borne
               (compteurs de trames figés) ou ne répond plus en HTTP.

Chaque épisode donne un dossier data/AAAA-MM-JJ_HHMMSS/ :
  samples.jsonl  relevés toutes les 5 s (dont les 10 min d'avant)
  gw_logs.txt    journal de la passerelle (/api/logs), au déclenchement puis toutes les 10 s
  ems.log        journal de ems.service sur la fenêtre
  resume.json    durée, minima/maxima, verdict
et une ligne dans data/chutes.jsonl, plus le capteur HA sensor.ems_chutes_charge.

Rien n'est écrit vers la borne, la passerelle ou l'EMS. Seule écriture : le
capteur HA, poussé par POST /api/states comme ceux d'Octopus.
"""
import os, sys, json, time, subprocess, collections
import datetime as dt
import requests

BASE = os.path.dirname(os.path.abspath(__file__))       # .../chutes
RACINE = os.path.dirname(BASE)                          # racine du dépôt

# config.py (HA_URL, HA_TOKEN) vit à la racine, ce script dans chutes/
sys.path.insert(0, RACINE)
import config  # noqa: E402

DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
INDEX = os.path.join(DATA, "chutes.jsonl")

PASSERELLE = "http://192.168.8.188"
EV_PUISSANCE = "sensor.shellyem_34945478aee1_channel_2_power"
EV_BRANCHE = "input_text.ev_connected"                  # géré par ems.py
CONSIGNE = "number.wallbox_192_168_8_188_max_current"   # ce que l'EMS a écrit
TESLA_DEMANDE = "number.martine_charge_current"         # suit le pilote de la borne
TESLA_COURANT = "sensor.martine_charger_current"
CAPTEUR = "sensor.ems_chutes_charge"

PERIODE_S = 5             # voiture branchée
PERIODE_REPOS_S = 30      # pas de voiture : HA seul, passerelle laissée tranquille
AVANT_S = 600             # historique gardé en mémoire, vidé au déclenchement
APRES_S = 300             # on continue d'enregistrer après la fin
LOGS_S = 10               # rythme de /api/logs pendant un épisode

BAS_MIN_W, BAS_MAX_W = 600, 1700    # 6 A ≈ 1,1-1,4 kW au Shelly
CONSIGNE_MIN_A = 10                 # en dessous, 1,2 kW peut être voulu
CONFIRME_S = 20                     # durée avant de déclarer une chute, ou sa fin
GEL_S = 60                          # trames figées ou HTTP en échec depuis

HA = {"Authorization": "Bearer " + config.HA_TOKEN, "Content-Type": "application/json"}
ha = requests.Session()
ha.headers.update(HA)
gw = requests.Session()


def maintenant():
    return dt.datetime.now().replace(microsecond=0)


def journal(msg):
    print(f"{maintenant():%H:%M:%S} {msg}", flush=True)


def chrono(f, *a, **kw):
    t0 = time.monotonic()
    try:
        return f(*a, **kw), None, round((time.monotonic() - t0) * 1000)
    except Exception as e:
        return None, repr(e)[:200], round((time.monotonic() - t0) * 1000)


def nombre(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- relevés

def releve_ha(s):
    ha_ms, erreurs = 0, 0
    for eid, cle in ((EV_PUISSANCE, "ev_w"), (EV_BRANCHE, "ev_branche"), (CONSIGNE, "consigne_a"),
                     (TESLA_DEMANDE, "tesla_demande_a"), (TESLA_COURANT, "tesla_courant_a")):
        r, err, ms = chrono(ha.get, f"{config.HA_URL}/api/states/{eid}", timeout=8)
        ha_ms = max(ha_ms, ms)
        if err or not r.ok:
            erreurs += 1
            continue
        etat = r.json()["state"]
        s[cle] = etat if cle == "ev_branche" else nombre(etat)
    s["ha_ms"], s["ha_err"] = ha_ms, erreurs


def releve_passerelle(s):
    r, err, ms = chrono(gw.get, PASSERELLE + "/api/charger", timeout=8)
    s["gw_ms"] = ms
    if err or not r.ok:
        s["gw_err"] = err or f"HTTP {r.status_code}"
        return
    # Juste après un redémarrage, la passerelle répond avant d'avoir parlé à la
    # borne : blocs à null (21/10 17:15:46, AttributeError en boucle 20 s).
    d = r.json() or {}
    st, rt = d.get("status") or {}, d.get("realtime") or {}
    sr, rr = st.get("r") or {}, rt.get("r") or {}
    s.update(gw_cache_age=d.get("cache_age"), gw_ble=d.get("ble"),
             gw_status_id=st.get("id"), gw_rt_id=rt.get("id"),
             gw_st=sr.get("st"), gw_cur=sr.get("cur"), gw_cp_kw=sr.get("cp"),
             gw_mcc=rr.get("max_charging_current"),
             gw_charger_status=rr.get("charger_status"),
             gw_ocpp=rr.get("ocpp_status"),
             gw_error_code=rr.get("error_code"))
    r, err, _ = chrono(gw.get, PASSERELLE + "/api/status", timeout=8)
    if not err and r.ok:
        d = r.json() or {}
        for k in ("wifi_rssi", "rssi", "ble_last_activity_s", "last_command_by",
                  "last_command_age_s", "uptime", "ble_paused", "heap"):
            s["gw_" + k] = d.get(k)


def logs_passerelle():
    r, err, ms = chrono(gw.get, PASSERELLE + "/api/logs", timeout=8)
    return (r.text if not err and r.ok else None), (err or (None if r.ok else f"HTTP {r.status_code}")), ms


# --------------------------------------------------------------------------- épisode

class Episode:
    def __init__(self, motif, avant, recul_s):
        # daté du début du phénomène, pas de sa confirmation
        self.debut = maintenant() - dt.timedelta(seconds=int(recul_s))
        self.motif = motif
        self.dossier = os.path.join(DATA, f"{self.debut:%Y-%m-%d_%H%M%S}")
        os.makedirs(self.dossier, exist_ok=True)
        self.f = open(os.path.join(self.dossier, "samples.jsonl"), "a")
        self.logs = open(os.path.join(self.dossier, "gw_logs.txt"), "a")
        self.queue_logs = ""
        self.dernier_logs = 0.0
        self.fin = None           # fin du phénomène ; on enregistre encore APRES_S
        self.pendant = []         # relevés entre le déclenchement et la fin
        for s in avant:
            self.ecrit(s)
        self.prends_logs()

    def ecrit(self, s):
        self.f.write(json.dumps(s) + "\n")
        self.f.flush()

    def prends_logs(self):
        self.dernier_logs = time.monotonic()
        txt, err, ms = logs_passerelle()
        if txt is None:
            self.logs.write(f"\n===== {maintenant():%H:%M:%S} échec ({err}, {ms} ms)\n")
        else:
            # le tampon tourne : on n'ajoute que ce qui suit la fin déjà vue
            i = txt.rfind(self.queue_logs) if self.queue_logs else -1
            neuf = txt[i + len(self.queue_logs):] if i >= 0 else txt
            if neuf.strip():
                self.logs.write(f"\n===== {maintenant():%H:%M:%S} ({ms} ms, "
                                f"{'suite' if i >= 0 else 'tampon complet'})\n{neuf}")
            self.queue_logs = txt[-400:]
        self.logs.flush()

    def ajoute(self, s):
        self.ecrit(s)
        if self.fin is None:
            self.pendant.append(s)
        if time.monotonic() - self.dernier_logs >= LOGS_S:
            self.prends_logs()

    def termine(self):
        self.f.close()
        self.logs.close()
        fin = self.fin or maintenant()
        try:
            out = subprocess.run(["journalctl", "-u", "ems", "--no-pager", "-o", "short-iso",
                                  "--since", f"{self.debut - dt.timedelta(seconds=AVANT_S)}",
                                  "--until", f"{fin + dt.timedelta(seconds=APRES_S)}"],
                                 capture_output=True, text=True, timeout=30).stdout
            open(os.path.join(self.dossier, "ems.log"), "w").write(out)
        except Exception as e:
            journal(f"journal EMS illisible : {e}")
        resume = self.resume(fin)
        json.dump(resume, open(os.path.join(self.dossier, "resume.json"), "w"),
                  indent=2, ensure_ascii=False)
        with open(INDEX, "a") as f:
            f.write(json.dumps(resume, ensure_ascii=False) + "\n")
        journal(f"épisode clos : {resume['verdict']} ({resume['duree_s']} s)")
        return resume

    def resume(self, fin):
        p = self.pendant

        def vals(k):
            return [s[k] for s in p if s.get(k) is not None]

        def mini(k):
            v = vals(k)
            return min(v) if v else None

        def maxi(k):
            v = vals(k)
            return max(v) if v else None

        r = {
            "debut": f"{self.debut}", "fin": f"{fin}",
            "duree_s": int((fin - self.debut).total_seconds()),
            "motif": self.motif, "dossier": os.path.basename(self.dossier),
            "ev_branche": next((s["ev_branche"] for s in p if s.get("ev_branche")), None),
            "ev_w_min": mini("ev_w"), "ev_w_max": maxi("ev_w"),
            "consigne_a_min": mini("consigne_a"), "consigne_a_max": maxi("consigne_a"),
            "tesla_demande_a_min": mini("tesla_demande_a"),
            "gw_mcc_min": mini("gw_mcc"), "gw_cur_min": mini("gw_cur"),
            "gw_gel_max_s": maxi("gw_gel_s"),
            "gw_gel_http_max_s": maxi("gw_gel_http_s"),
            "gw_echecs_http": sum(1 for s in p if s.get("gw_err")),
            "gw_cache_age_max": maxi("gw_cache_age"),
            "gw_wifi_rssi_min": mini("gw_wifi_rssi"),
            "gw_uptime_min": mini("gw_uptime"),
            "ha_ms_max": maxi("ha_ms"), "ha_echecs": sum(s.get("ha_err", 0) for s in p),
        }
        r["verdict"] = verdict(r)
        return r


def verdict(r):
    """Une première lecture ; les relevés bruts restent la référence."""
    if r["gw_mcc_min"] is not None and r["gw_mcc_min"] <= 6:
        return f"la borne annonce elle-même {r['gw_mcc_min']} A (limite côté borne, OCPP ?)"
    # Uptime en secondes : plus court que l'épisode = redémarrage pendant celui-ci.
    reboot = (f", redémarrée (uptime {r['gw_uptime_min']} s)"
              if r["gw_uptime_min"] is not None and r["gw_uptime_min"] < r["duree_s"]
              else "")
    # Un gel HTTP n'est pas un silence BLE : le 21/10 la passerelle était hors
    # réseau (ConnectTimeout), le verdict disait « borne muette en BLE ? ».
    if (r["gw_gel_http_max_s"] or 0) >= GEL_S:
        return (f"passerelle injoignable en HTTP pendant {r['gw_gel_http_max_s']} s "
                f"({r['gw_echecs_http']} échecs, hors réseau ou plantée){reboot}")
    if (r["gw_gel_max_s"] or 0) >= GEL_S:
        return f"passerelle sans trame fraîche de la borne pendant {r['gw_gel_max_s']} s (borne muette en BLE ?){reboot}"
    if r["gw_echecs_http"]:
        return f"passerelle injoignable en HTTP ({r['gw_echecs_http']} échecs){reboot}"
    if r["ha_echecs"]:
        return f"HA en échec ({r['ha_echecs']} lectures ratées)"
    return "borne, passerelle et HA normales : limite côté voiture ?"


# --------------------------------------------------------------------------- capteur HA

def pousse_capteur(dernier=None):
    jour = f"{maintenant():%Y-%m-%d}"
    n, derniers = 0, []
    if os.path.exists(INDEX):
        for ligne in open(INDEX):
            try:
                e = json.loads(ligne)
            except ValueError:
                continue
            if e["debut"].startswith(jour):
                n += 1
            derniers.append(e)
    dernier = dernier or (derniers[-1] if derniers else None)
    attrs = {"friendly_name": "EMS chutes de charge du jour", "icon": "mdi:ev-plug-type2",
             "unit_of_measurement": "épisodes", "state_class": "measurement"}
    if dernier:
        attrs.update({"dernier_debut": dernier["debut"], "dernier_duree_s": dernier["duree_s"],
                      "dernier_motif": dernier["motif"], "dernier_verdict": dernier["verdict"],
                      "dernier_dossier": dernier["dossier"]})
    _, err, _ = chrono(ha.post, f"{config.HA_URL}/api/states/{CAPTEUR}", timeout=10,
                       json={"state": n, "attributes": attrs})
    if err:
        journal(f"capteur {CAPTEUR} : {err}")


# --------------------------------------------------------------------------- boucle

def main():
    journal("surveillance des chutes démarrée")
    avant = collections.deque(maxlen=AVANT_S // PERIODE_S)
    episode = None
    bas_depuis = None
    anormal_vu = 0.0                          # dernier relevé bas ou gelé
    # dernière trame de la borne vue par la passerelle, par type : status.id avance
    # toutes les ~10 s, realtime.id toutes les ~30 s (29 s au plus en charge le 15/10)
    trames = {}                               # clé -> [dernier id, depuis]
    gw_ok_depuis = time.monotonic()
    suivi = False                             # passerelle interrogée au tour précédent
    pousse_capteur()
    capteur_pousse = time.monotonic()

    while True:
        t0 = time.monotonic()
        s = {"t": f"{maintenant()}"}
        try:
            releve_ha(s)
            ev = s.get("ev_w")
            branche = (s.get("ev_branche") not in (None, "disconnected")) or (ev or 0) > 40
            if branche or episode:
                if not suivi:
                    # reprise après une période sans voiture : pas de gel hérité
                    trames, gw_ok_depuis = {}, t0
                suivi = True
                releve_passerelle(s)
                if "gw_err" not in s:
                    gw_ok_depuis = t0
                    for cle in ("gw_status_id", "gw_rt_id"):
                        if trames.get(cle, [None])[0] != s.get(cle):
                            trames[cle] = [s.get(cle), t0]
                gel_http = t0 - gw_ok_depuis
                gel_trames = max((t0 - depuis for _, depuis in trames.values()), default=0)
                s["gw_gel_s"] = int(max(gel_http, gel_trames))
                s["gw_gel_http_s"] = int(gel_http)
            else:
                suivi = False

            # ----- chute de puissance ?
            if ev is not None:
                consigne = s.get("consigne_a") or 0
                bas = branche and BAS_MIN_W <= ev <= BAS_MAX_W and consigne >= CONSIGNE_MIN_A
                bas_depuis = (bas_depuis or t0) if bas else None
            chute = bas_depuis is not None and t0 - bas_depuis >= CONFIRME_S
            gel = (s.get("gw_gel_s") or 0) >= GEL_S and branche
            if bas_depuis is not None or gel:
                anormal_vu = t0

            if episode is None:
                if chute or gel:
                    motif = "chute" if chute else "gel passerelle"
                    journal(f"{motif} : ev={ev} W consigne={s.get('consigne_a')} A "
                            f"gel={s.get('gw_gel_s')} s → enregistrement")
                    depuis = bas_depuis if chute else t0 - s["gw_gel_s"]
                    episode = Episode(motif, avant, t0 - depuis)
                    avant.clear()
                    episode.ajoute(s)
                else:
                    avant.append(s)
            else:
                episode.ajoute(s)
                calme = t0 - anormal_vu
                if episode.fin is None and (calme >= CONFIRME_S or not branche):
                    episode.fin = maintenant() - dt.timedelta(seconds=int(calme))
                    journal("fin du phénomène, enregistrement encore "
                            f"{APRES_S // 60} min")
                if episode.fin is not None and (chute or gel):
                    episode.fin = None          # ça repart : même épisode
                if episode.fin and (maintenant() - episode.fin).total_seconds() >= APRES_S:
                    pousse_capteur(episode.termine())
                    capteur_pousse = t0
                    episode = None

            # les états poussés par REST ne survivent pas à un redémarrage de HA
            if t0 - capteur_pousse >= 1800:
                pousse_capteur()
                capteur_pousse = t0
        except Exception as e:
            journal(f"erreur : {e!r}")

        periode = PERIODE_S if (episode or s.get("gw_ms") is not None) else PERIODE_REPOS_S
        time.sleep(max(0.0, periode - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
