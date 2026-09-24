#!/usr/bin/env python3
"""Récupère les sessions de recharge comptabilisées par Octopus (API Kraken).

À exécuter SUR TA MACHINE. Les identifiants ne sont jamais écrits sur disque :
ils sont lus dans l'environnement, ou demandés au clavier sans écho.

    export OCTOPUS_EMAIL='...'
    read -s OCTOPUS_PASSWORD && export OCTOPUS_PASSWORD
    python3 octopus_sessions.py

Sans argument, le script EXPLORE le schéma GraphQL et écrit ce qu'il trouve
dans octopus_schema.json — c'est cette sortie qu'il faut me renvoyer pour que
j'écrive la requête exacte. Avec --query NOM, il exécute la requête nommée.
"""
import os, sys, json, getpass, datetime as dt
import requests

ENDPOINTS = [
    "https://api.oefr-kraken.energy/v1/graphql/",
    "https://api.oefr-kraken.energy/v1/graphql",
    "https://api.octopus.energy/v1/graphql/",
]
MOTS_CLES = ("charge", "charging", "session", "dispatch", "smart", "device",
             "vehicle", "boost", "krakenflex", "flex")


def creds():
    email = os.environ.get("OCTOPUS_EMAIL") or input("email Octopus : ").strip()
    pwd = os.environ.get("OCTOPUS_PASSWORD") or getpass.getpass("mot de passe : ")
    return email, pwd


def login(email, pwd):
    """Retourne (url, token) sur le premier endpoint qui accepte l'authentification."""
    m = ('mutation($e:String!,$p:String!){obtainKrakenToken(input:{email:$e,password:$p})'
         '{token}}')
    derniere = None
    for url in ENDPOINTS:
        try:
            r = requests.post(url, json={"query": m,
                                         "variables": {"e": email, "p": pwd}}, timeout=30)
        except requests.RequestException as ex:
            print(f"  {url} -> injoignable ({type(ex).__name__})")
            continue
        try:
            j = r.json()
        except ValueError:
            print(f"  {url} -> HTTP {r.status_code}, réponse non JSON")
            continue
        tok = ((j.get("data") or {}).get("obtainKrakenToken") or {}).get("token")
        if tok:
            print(f"  {url} -> authentification OK")
            return url, tok
        derniere = j.get("errors")
        print(f"  {url} -> refusé : {str(derniere)[:160]}")
    sys.exit("Aucun endpoint n'a accepté l'authentification. Vérifie les identifiants.")


def gql(url, token, query, variables=None):
    r = requests.post(url, headers={"Authorization": token},
                      json={"query": query, "variables": variables or {}}, timeout=60)
    j = r.json()
    if j.get("errors"):
        print(f"    !! {str(j['errors'])[:300]}")
    return j.get("data")


INTROSPECTION = """
{ __schema { queryType { fields {
      name
      description
      args { name type { kind name ofType { kind name } } }
      type { kind name ofType { kind name ofType { kind name } } }
} } } }
"""

TYPE_DETAIL = """
query($n:String!){ __type(name:$n){ name kind
  fields { name description type { kind name ofType { kind name ofType { kind name } } } } } }
"""


def explorer(url, token):
    data = gql(url, token, INTROSPECTION)
    if not data:
        sys.exit("Introspection refusée par l'API.")
    champs = data["__schema"]["queryType"]["fields"]
    print(f"\n{len(champs)} requêtes disponibles ; celles qui nous intéressent :\n")
    retenus, sortie = [], {"queries": [], "types": {}}
    for f in champs:
        if not any(k in f["name"].lower() for k in MOTS_CLES):
            continue
        retenus.append(f)
        args = ", ".join(a["name"] for a in f["args"]) or "aucun"
        t = f["type"]
        nom_type = t.get("name") or (t.get("ofType") or {}).get("name") \
            or ((t.get("ofType") or {}).get("ofType") or {}).get("name")
        print(f"  {f['name']:38s} args({args})  -> {nom_type}")
        if f.get("description"):
            print(f"      {f['description'][:140]}")
        sortie["queries"].append({"name": f["name"], "args": args,
                                  "returns": nom_type, "desc": f.get("description")})
        if nom_type and nom_type not in sortie["types"]:
            d = gql(url, token, TYPE_DETAIL, {"n": nom_type})
            ty = (d or {}).get("__type")
            if ty and ty.get("fields"):
                noms = [x["name"] for x in ty["fields"]]
                sortie["types"][nom_type] = noms
                print(f"      champs : {', '.join(noms[:18])}{' …' if len(noms) > 18 else ''}")
    if not retenus:
        print("  (aucune requête ne correspond aux mots-clés — voir octopus_schema.json)")
        sortie["toutes_les_queries"] = [f["name"] for f in champs]
    with open("octopus_schema.json", "w", encoding="utf-8") as fh:
        json.dump(sortie, fh, indent=2, ensure_ascii=False)
    print("\n-> écrit dans octopus_schema.json ; renvoie-moi ce fichier.")


def main():
    email, pwd = creds()
    url, token = login(email, pwd)
    del pwd
    if "--query" in sys.argv:
        nom = sys.argv[sys.argv.index("--query") + 1]
        q = open(nom, encoding="utf-8").read()
        d = gql(url, token, q)
        nom_sortie = f"octopus_data_{dt.date.today()}.json"
        with open(nom_sortie, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2, ensure_ascii=False)
        print(f"-> {nom_sortie}")
    else:
        explorer(url, token)


if __name__ == "__main__":
    main()
