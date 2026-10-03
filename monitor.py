#!/usr/bin/env python3
"""Checks the NRG API from outside; exits with an error (the workflow fails, GitHub sends an
e-mail) when the service is down, data is stale or the Mobilithek pushes have stopped.

    python3 monitor.py [base URL]
"""
import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://api.nrg-app.com").rstrip("/")
MAX_AGE_S = {"nl": 3600, "fi": 3600, "pl": 3600, "es": 3 * 3600, "at": 3 * 3600, "de": 1800}   # collectors run every 2 to 15 minutes
MAX_PUSH_AGE_S = 1800
MIN_LOCATIONS = {"nl": 50_000, "fi": 2_000, "pl": 3_000, "es": 8_000, "at": 8_000, "de": 60_000}

problems = []


def get(path):
    with urllib.request.urlopen(urllib.request.Request(BASE + path, headers={"User-Agent": "nrg-monitor"}), timeout=30) as resp:
        return json.load(resp)


try:
    status = get("/v1/status")
    for name, limit in MAX_AGE_S.items():
        region = status["regions"].get(name)
        if not region:
            problems.append(f"{name}: Region fehlt")
            continue
        if region["age_s"] > limit:
            problems.append(f"{name}: Daten {region['age_s'] // 60} Minuten alt")
        if region.get("error"):
            problems.append(f"{name}: {region['error']}")
        if region["locations"] < MIN_LOCATIONS[name]:
            problems.append(f"{name}: nur {region['locations']} Standorte")
    push_age = status["push"]["last_age_s"]
    if push_age is None or push_age > MAX_PUSH_AGE_S:
        problems.append("Mobilithek-Push: kein Paket" + ("" if push_age is None else f" seit {push_age // 60} Minuten"))
    berlin = get("/v1/charging?lat=52.52&lon=13.405&radius=2")
    if sum(len(r["locations"]) for r in berlin["regions"]) < 50:
        problems.append("Umkreisabfrage Berlin liefert zu wenige Standorte")
    summary = ", ".join(f"{n} {r['locations']}/{r['priced']}/{r.get('with_status', 0)}" for n, r in status["regions"].items())
    print(f"Standorte/mit Preis/mit Status: {summary}; letzter Push vor {push_age} s")
except Exception as error:
    problems.append(f"Dienst nicht erreichbar oder Antwort unlesbar: {type(error).__name__}: {error}")

if problems:
    print("PROBLEME:\n- " + "\n- ".join(problems))
    sys.exit(1)
print("alles in Ordnung")
