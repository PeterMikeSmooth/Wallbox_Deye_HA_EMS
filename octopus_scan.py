#!/usr/bin/env python3
"""Cartographie complète du schéma Kraken + récupération des dispatches.

    export OCTOPUS_EMAIL='...'
    read -s OCTOPUS_PASSWORD && export OCTOPUS_PASSWORD
    python3 octopus_scan.py

Écrit octopus_scan.json. Aucun identifiant stocké.
"""
import os, sys, json, getpass, datetime as dt
import requests

URL = "https://api.oefr-kraken.energy/v1/graphql/"
ERREURS = []


def gql(token, query, variables=None, silencieux=False):
    r = requests.post(URL, headers={"Authorization": token} if token else {},
                      json={"query": query, "variables": variables or {}}, timeout=90)
    try:
        j = r.json()
    except ValueError:
        return None
    if j.get("errors"):
        ERREURS.append({"q": query.strip()[:80], "e": j["errors"][0].get("message")})
        if not silencieux:
            print(f"    !! {str(j['errors'][0].get('message'))[:180]}")
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


FULL = """
{ __schema {
    queryType { fields { name args { name } type { kind name ofType { kind name ofType { kind name } } } } }
    types { name kind fields { name type { kind name ofType { kind name ofType { kind name } } } } }
} }
"""

MOTS = ("energyadded", "session", "charg", "dispatch", "kwh", "smartflex", "telemetry", "history")


def nom_type(t):
    while t:
        if t.get("name"):
            return t["name"]
        t = t.get("ofType")
    return None


def main():
    token = login()
    out = {"date": dt.datetime.now().isoformat()}

    print("\n-- introspection complète --")
    d = gql(token, FULL)
    if not d:
        sys.exit("introspection refusée")
    sch = d["__schema"]
    queries = [{"name": f["name"], "args": [a["name"] for a in f["args"]],
                "type": nom_type(f["type"])} for f in sch["queryType"]["fields"]]
    types = {}
    for t in sch["types"]:
        if t.get("fields"):
            types[t["name"]] = {f["name"]: nom_type(f["type"]) for f in t["fields"]}
    out["queries"] = queries
    out["types"] = types
    print(f"  {len(queries)} requêtes, {len(types)} types")

    print("\n-- types contenant un champ d'énergie ou de session --")
    interessants = {}
    for tn, fs in types.items():
        hits = [f for f in fs if any(m in f.lower() for m in ("energy", "kwh", "session", "consumption"))]
        if hits and any(m in tn.lower() for m in ("charg", "dispatch", "device", "session", "flex")):
            interessants[tn] = fs
            print(f"  {tn}: {hits}")
    out["types_interessants"] = interessants

    print("\n-- requêtes candidates --")
    for q in queries:
        if any(m in q["name"].lower() for m in MOTS):
            print(f"  {q['name']:42s} args({', '.join(q['args']) or 'aucun'}) -> {q['type']}")

    compte = "A-5EE1FD14"
    print(f"\n-- devices (requête corrigée) --")
    d = gql(token, """query($a:String!){devices(accountNumber:$a){
        id name deviceType provider integrationDeviceId propertyId}}""", {"a": compte})
    devs = (d or {}).get("devices") or []
    out["devices"] = devs
    for x in devs:
        print(f"  {x.get('id')}  {x.get('name')}  {x.get('deviceType')}  {x.get('provider')}")

    print("\n-- flexPlannedDispatches par device --")
    for x in devs:
        d = gql(token, """query($d:String!){flexPlannedDispatches(deviceId:$d){
            start end type energyAddedKwh}}""", {"d": x["id"]})
        pd = (d or {}).get("flexPlannedDispatches") or []
        out.setdefault("planned", {})[x["id"]] = pd
        print(f"  {x.get('name')}: {len(pd)}")
        for p in pd[:5]:
            print(f"    {p}")

    out["erreurs"] = ERREURS
    with open("octopus_scan.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print("\n-> octopus_scan.json")


if __name__ == "__main__":
    main()
