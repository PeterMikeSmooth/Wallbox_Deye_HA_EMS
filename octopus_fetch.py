#!/usr/bin/env python3
"""Étape 2 : récupère les dispatches (sessions comptabilisées) chez Octopus.

    export OCTOPUS_EMAIL='...'
    read -s OCTOPUS_PASSWORD && export OCTOPUS_PASSWORD
    python3 octopus_fetch.py

Écrit octopus_dispatches.json. Aucun identifiant n'est stocké.
"""
import os, sys, json, getpass, datetime as dt
import requests

URL = "https://api.oefr-kraken.energy/v1/graphql/"
ERREURS = []
COMPTE = os.environ.get("OCTOPUS_ACCOUNT", "A-5EE1FD14")


def gql(token, query, variables=None, silencieux=False):
    r = requests.post(URL, headers={"Authorization": token} if token else {},
                      json={"query": query, "variables": variables or {}}, timeout=60)
    try:
        j = r.json()
    except ValueError:
        print(f"  réponse non JSON (HTTP {r.status_code})")
        return None
    if j.get("errors"):
        ERREURS.append({"query": query.strip()[:90], "errors": j["errors"]})
        if not silencieux:
            print(f"  !! {str(j['errors'])[:260]}")
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


TYPE_Q = """query($n:String!){__type(name:$n){name kind
  fields{name type{kind name ofType{kind name ofType{kind name}}}}}}"""


def champs(token, nom):
    d = gql(token, TYPE_Q, {"n": nom}, silencieux=True)
    t = (d or {}).get("__type")
    if not t or not t.get("fields"):
        return {}
    out = {}
    for f in t["fields"]:
        ty = f["type"]
        n = ty.get("name") or (ty.get("ofType") or {}).get("name") \
            or ((ty.get("ofType") or {}).get("ofType") or {}).get("name")
        out[f["name"]] = n
    return out


def main():
    token = login()
    res = {"recupere_le": dt.datetime.now().isoformat()}

    print("\n-- comptes du viewer --")
    comptes = []
    for q in ("{viewer{accounts{number}}}",
              "{viewer{accounts{number status}}}",
              "{accountNumbers}"):
        d = gql(token, q, silencieux=True)
        if not d:
            continue
        v = d.get("viewer") or {}
        for a in (v.get("accounts") or []):
            if a.get("number"):
                comptes.append(a["number"])
        if d.get("accountNumbers"):
            comptes += [x for x in d["accountNumbers"] if x]
        if comptes:
            break
    comptes = comptes or [COMPTE]
    print(f"  {comptes}")
    compte = comptes[0]
    res["compte"] = compte
    res["comptes_trouves"] = comptes

    print("\n-- structure des types --")
    for t in ("UpsideDispatchType", "SmartFlexDispatch"):
        c = champs(token, t)
        print(f"  {t}: {c}")
        res.setdefault("types", {})[t] = c
        for sous in c.values():
            if sous and sous not in ("String", "Int", "Float", "Boolean", "ID", "DateTime", "Decimal"):
                sc = champs(token, sous)
                if sc:
                    print(f"      {sous}: {sc}")
                    res["types"][sous] = sc

    print("\n-- appareils --")
    d = gql(token, """query($a:String!){devices(accountNumber:$a){
        id name deviceType provider integrationDeviceId status propertyId}}""", {"a": compte})
    res["devices"] = (d or {}).get("devices")
    for dev in (res["devices"] or []):
        print(f"  {dev.get('id')}  {dev.get('name')}  {dev.get('deviceType')}  "
              f"{dev.get('provider')}  status={dev.get('status')}")

    print("\n-- completedDispatches --")
    meta_f = res.get("types", {}).get("UpsideDispatchType", {}).get("meta")
    sel = "start end delta"
    if meta_f:
        mf = res.get("types", {}).get(meta_f, {})
        if mf:
            sel += " meta{" + " ".join(mf.keys()) + "}"
    d = gql(token, "query($a:String!){completedDispatches(accountNumber:$a){%s}}" % sel, {"a": compte})
    cd = (d or {}).get("completedDispatches") or []
    res["completedDispatches"] = cd
    print(f"  {len(cd)} dispatches")
    if cd:
        print(f"  du {cd[-1].get('start')} au {cd[0].get('start')}")
        print(f"  exemple : {json.dumps(cd[0], ensure_ascii=False)[:240]}")
        tot = sum(abs(float(x.get("delta") or 0)) for x in cd)
        print(f"  somme des |delta| : {tot:.2f}")

    print("\n-- flexPlannedDispatches --")
    for dev in (res["devices"] or []):
        d = gql(token, """query($d:String!){flexPlannedDispatches(deviceId:$d){
            start end type energyAddedKwh}}""", {"d": dev["id"]})
        pd = (d or {}).get("flexPlannedDispatches") or []
        res.setdefault("plannedDispatches", {})[dev["id"]] = pd
        print(f"  {dev.get('name')}: {len(pd)} planifiés")
        if pd:
            print(f"    exemple : {json.dumps(pd[0], ensure_ascii=False)[:200]}")

    res["erreurs"] = ERREURS
    if ERREURS:
        print(f"\n-- {len(ERREURS)} erreurs GraphQL (détail dans le JSON) --")
        for e in ERREURS[:6]:
            msg = e["errors"][0].get("message") if e["errors"] else "?"
            print(f"  {e['query'][:60]} -> {str(msg)[:150]}")

    with open("octopus_dispatches.json", "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2, ensure_ascii=False)
    print("\n-> octopus_dispatches.json")


if __name__ == "__main__":
    main()
