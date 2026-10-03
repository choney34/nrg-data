#!/usr/bin/env python3
"""Builds the NRG fuel price directory from official price sources: per station its position,
name, address, prices with the time of the last update, opening hours and services.

    python3 build_fuel.py            all regions
    python3 build_fuel.py fr         only these

Output: site/fuel/<region>.json
France: "Prix des carburants en France – flux instantané" (data.economie.gouv.fr), Licence
Ouverte 2.0.

Station rows: [lat, lon, id, name, brand|null, street, city, postcode,
               [[fuel, price, updated|null], …], hours, services]
  fuel      code from FUELS (the app ignores codes it doesn't know)
  updated   Unix time of the last price update
  hours     null = unknown, 1 = always open, else seven entries Monday…Sunday, each a list of
            [open, close] in minutes of the day ([] = closed, null = unknown for that day);
            close < open runs past midnight. Times are in the region's "timezone".
  services  bits as in build_services.py BITS, 0 = none known
"""
import datetime
import gzip
import json
import os
import sys
import urllib.request

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "fuel")
USER_AGENT = "NRG-fuel-directory/1.0 (https://github.com/choney34/nrg-data)"

# Fuel codes shared with the app (FuelDirectory).
FUELS = {"diesel": 1, "e5": 2, "e10": 3, "sp98": 4}
# Service bits shared with the app (StationService.bit) and build_services.py.
BITS = {"food": 1, "restroom": 2, "atm": 4, "shopping": 8, "evCharger": 16, "carWash": 32, "air": 64}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
        packed = resp.headers.get("Content-Encoding") == "gzip"
    return json.loads(gzip.decompress(data) if packed else data)


def epoch(value):
    try:
        return int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (AttributeError, ValueError):
        return None


FR_URL = ("https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
          "prix-des-carburants-en-france-flux-instantane-v2/exports/json")
FR_FUELS = {"gazole": "diesel", "sp95": "e5", "e10": "e10", "sp98": "sp98"}
FR_SERVICES = {"Restauration à emporter": "food", "Restauration sur place": "food", "Bar": "food",
               "Toilettes publiques": "restroom", "DAB (Distributeur automatique de billets)": "atm",
               "Boutique alimentaire": "shopping", "Boutique non alimentaire": "shopping",
               "Bornes électriques": "evCharger", "Lavage automatique": "carWash", "Lavage manuel": "carWash",
               "Station de gonflage": "air"}


def fr_minutes(text):
    """ "07.30" → 450 """
    try:
        hours, minutes = (int(x) for x in str(text).split("."))
        return hours * 60 + minutes
    except ValueError:
        return None


def fr_hours(record):
    """`horaires` is a JSON string with one entry per weekday (@id 1 = Monday … 7 = Sunday,
    @ferme "1" = closed, `horaire` one or several windows "HH.MM"). A 24/7 card machine means
    the station can always be used."""
    if record.get("horaires_automate_24_24") == "Oui":
        return 1
    try:
        doc = json.loads(record.get("horaires") or "")
    except ValueError:
        return None
    if doc.get("@automate-24-24") == "1":
        return 1
    days = {str(d.get("@id")): d for d in doc.get("jour") or [] if isinstance(d, dict)}
    week = []
    for day in range(1, 8):
        entry = days.get(str(day))
        if entry is None:
            week.append(None)
            continue
        if entry.get("@ferme") == "1":
            week.append([])
            continue
        windows = entry.get("horaire") or []
        if isinstance(windows, dict):
            windows = [windows]
        spans = [[fr_minutes(w.get("@ouverture")), fr_minutes(w.get("@fermeture"))] for w in windows]
        spans = [s for s in spans if None not in s]
        week.append(spans or None)
    return week if any(day is not None for day in week) else None


def build_fr():
    rows = []
    for r in get(FR_URL):
        geom = r.get("geom") or {}
        lat, lon = geom.get("lat"), geom.get("lon")
        if lat is None or lon is None:
            continue
        prices = []
        for key, fuel in FR_FUELS.items():
            price = r.get(f"{key}_prix")
            if isinstance(price, (int, float)) and price > 0:
                prices.append([FUELS[fuel], round(price, 3), epoch(r.get(f"{key}_maj"))])
        if not prices:
            continue
        services = 0
        for name in r.get("services_service") or []:
            services |= BITS.get(FR_SERVICES.get(name), 0)
        address = (r.get("adresse") or "").strip()
        rows.append([round(lat, 5), round(lon, 5), str(r.get("id")), address or "Station", None, address,
                     (r.get("ville") or "").strip(), r.get("cp") or "", prices, fr_hours(r), services])
    return {"source": "Prix des carburants en France, data.economie.gouv.fr (Licence Ouverte 2.0)",
            "currency": "EUR", "timezone": "Europe/Paris", "stations": rows}


# Country (ISO 3166-1 alpha-2) and short provider name of each region.
META = {"fr": ("FR", "prix-carburants.gouv.fr")}
_REGIONS = {"fr": build_fr}


def with_meta(name, build):
    def wrapped():
        doc = build()
        doc["iso"], doc["provider"] = META[name]
        doc["fuels"] = FUELS
        return doc
    return wrapped


REGIONS = {name: with_meta(name, build) for name, build in _REGIONS.items()}


def main():
    os.makedirs(OUT, exist_ok=True)
    wanted, failed = sys.argv[1:] or list(REGIONS), []
    for region in wanted:
        try:
            doc = REGIONS[region]()
        except Exception as error:      # one source failing must not stop the others
            print(f"== {region}: fehlgeschlagen ({error})")
            failed.append(region)
            continue
        doc = {"version": 1, "region": region,
               "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **doc}
        path = os.path.join(OUT, f"{region}.json")
        with open(path, "w") as f:
            json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
        print(f"== {region}: {len(doc['stations'])} Tankstellen, {os.path.getsize(path) // 1024} KB")
    if failed and len(failed) == len(wanted):
        sys.exit(1)


if __name__ == "__main__":
    main()
