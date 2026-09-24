#!/usr/bin/env python3
"""Récupère TOUTES les sessions de recharge comptabilisées par Octopus.

    export OCTOPUS_EMAIL='...'
    read -s OCTOPUS_PASSWORD && export OCTOPUS_PASSWORD
    python3 octopus_sessions_fetch.py

Écrit octopus_sessions.json et octopus_sessions.csv. Aucun identifiant stocké.
"""
import os, sys, json, csv, getpass, datetime as dt
import requests

URL = "https://api.oefr-kraken.energy/v1/graphql/"
COMPTE = os.environ.get("OCTOPUS_ACCOUNT", "A-5EE1FD14")


def gql(token, query, variables=None, silencieux=False):
    r = requests.post(URL, headers={"Authorization": token} if token else {},
                      json={"query": query, "variables": variables or {}}, timeout=90)
    try:
        j = r.json()
    except ValueError:
        print(f"  réponse non JSON (HTTP {r.status_code})")
        return None
    if j.get("errors") and not silencieux:
        for e in j["errors"][:3]:
            print(f"    !! {str(e.get('message'))[:220]}")
    return j.get("data")


def login():
    email = os.environ.get("OCTOPUS_EMAIL") or input("email Octopus : ").strip()
    pwd = os.environ.get("OCTOPUS_PASSWORD") or getpass.getpass("mot de passe : ")
    d = gql(None, 'mutation($e:String!,$p:String!){obtainKrakenToken(input:{email:$e,'
                  'password:$p}){token}}', {"e": email, "p": pwd})
    tok = ((d or {}).get("obtainKrakenToken") or {}).get("token")
    if not tok:
        sys.exit("authentification refusée")
    print("authentification OK")
    return tok


ARGS_Q = """query($n:String!){__type(name:$n){fields{name
  args{name type{kind name ofType{kind name ofType{kind name}}}}}}}"""


def nom(t):
    while t:
        if t.get("name"):
            return t["name"]
        t = t.get("ofType")
    return None


def args_de(token, type_name, field_name):
    d = gql(token, ARGS_Q, {"n": type_name}, silencieux=True)
    for f in ((d or {}).get("__type") or {}).get("fields") or []:
        if f["name"] == field_name:
            return {a["name"]: nom(a["type"]) for a in f["args"]}
    return {}


def main():
    token = login()

    a = args_de(token, "SmartFlexChargePoint", "chargingSessions")
    print(f"\nargs de chargingSessions : {a or '(introuvables, on tente first/after)'}")
    a_first = "first" if "first" in a else None
    a_after = "after" if "after" in a else None
    type_after = a.get("after", "DateTime")

    decl, passe = ["$acc:String!"], []
    if a_first:
        decl.append("$first:Int")
        passe.append("first:$first")
    if a_after:
        decl.append(f"$after:{type_after}")
        passe.append("after:$after")
    argstr = ("(" + ", ".join(passe) + ")") if passe else ""

    Q = """query(%s){ devices(accountNumber:$acc){
      id name deviceType
      ... on SmartFlexChargePoint {
        chargingSessions%s {
          pageInfo { hasNextPage endCursor }
          edges { cursor node {
            start end stateOfChargeChange stateOfChargeFinal
            energyAdded { value unit }
            cost { amount currency }
            ... on SmartFlexChargingSession {
              type targetType
              dispatches { start end type energyAddedKwh }
            }
          } }
        } } } }""" % (", ".join(decl), argstr)

    sessions, curseur, page = [], None, 0
    while True:
        v = {"acc": COMPTE}
        if a_first:
            v["first"] = 100
        if a_after and curseur:
            v["after"] = curseur
        d = gql(token, Q, v)
        if not d:
            break
        dev = None
        for x in (d.get("devices") or []):
            if x.get("chargingSessions"):
                dev = x
                break
        if not dev:
            print("  aucun device avec chargingSessions dans la réponse")
            break
        cs = dev["chargingSessions"]
        lot = [e["node"] for e in (cs.get("edges") or [])]
        sessions += lot
        page += 1
        print(f"  page {page}: {len(lot)} sessions (cumul {len(sessions)})")
        pi = cs.get("pageInfo") or {}
        if not (a_after and pi.get("hasNextPage") and pi.get("endCursor")) or not lot:
            break
        curseur = pi["endCursor"]
        if page > 60:
            print("  arrêt de sécurité à 60 pages")
            break

    with open("octopus_sessions.json", "w", encoding="utf-8") as fh:
        json.dump(sessions, fh, indent=2, ensure_ascii=False)

    if sessions:
        with open("octopus_sessions.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["start", "end", "kWh", "unite", "cout", "devise", "type",
                        "soc_change", "soc_final", "nb_dispatches", "kwh_dispatches"])
            for s in sessions:
                ea = s.get("energyAdded") or {}
                co = s.get("cost") or {}
                dp = s.get("dispatches") or []
                w.writerow([s.get("start"), s.get("end"), ea.get("value"), ea.get("unit"),
                            co.get("amount"), co.get("currency"), s.get("type"),
                            s.get("stateOfChargeChange"), s.get("stateOfChargeFinal"),
                            len(dp), sum(float(x.get("energyAddedKwh") or 0) for x in dp)])
        ds = sorted(x.get("start") or "" for x in sessions)
        print(f"\n{len(sessions)} sessions, du {ds[0][:10]} au {ds[-1][:10]}")
        print("-> octopus_sessions.json + octopus_sessions.csv")
    else:
        print("\naucune session récupérée")


if __name__ == "__main__":
    main()
