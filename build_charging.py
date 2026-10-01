#!/usr/bin/env python3
"""Builds the NRG charging directory from the official AFIR national access points: per charging
location its position, connectors, maximum power and the ad-hoc price.

    python3 build_charging.py            all regions
    python3 build_charging.py nl         only these

Output: site/charging/<region>.json
Netherlands: DOT-NL by NDW (opendata.ndw.nu), open data free for reuse by third parties.
"""
import datetime
import gzip
import json
import os
import sys
import urllib.request

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "charging")
USER_AGENT = "NRG-charging-directory/1.0 (https://github.com/choney34/nrg-data)"

# Connector codes shared with the app (ChargingDirectory.connectorName).
CONNECTORS = {"IEC_62196_T2": 1, "IEC_62196_T2_COMBO": 2, "CHADEMO": 3, "IEC_62196_T1": 4,
              "IEC_62196_T1_COMBO": 5, "TESLA_S": 6, "DOMESTIC_F": 7}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    return json.loads(gzip.decompress(data) if url.endswith(".gz") else data)


def ad_hoc_price(tariff, default_vat):
    """(€/kWh, € per session, € per hour) incl. VAT from an OCPI tariff; prefers unrestricted
    price components (restricted ones apply e.g. only at certain hours)."""
    found = {}
    for restricted in (False, True):
        for element in tariff.get("elements") or []:
            if bool(element.get("restrictions")) != restricted:
                continue
            for c in element.get("price_components") or []:
                vat = c.get("vat")
                gross = c["price"] * (1 + (default_vat if vat is None else vat) / 100)
                found.setdefault(c["type"], gross)
        if "ENERGY" in found:
            break
    return found.get("ENERGY"), found.get("FLAT", 0.0), found.get("TIME", 0.0)


def build_nl():
    base = "https://opendata.ndw.nu/"
    tariffs = {t["id"]: t for t in get(base + "charging_point_tariffs_ocpi.json.gz")}
    features = get(base + "charging_point_locations.geojson.gz")["features"]
    operators, op_index, rows = [], {}, []
    for f in features:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        groups, best = {}, None
        for a in p.get("availabilities") or []:
            code = CONNECTORS.get(a.get("connector_type"), 0)
            kw = round((a.get("power_max") or 0) / 1000)
            key = (code, kw)
            groups[key] = groups.get(key, 0) + (a.get("total") or 1)
            for tid in a.get("tariff_ids") or []:
                if tid in tariffs:
                    energy, flat, time = ad_hoc_price(tariffs[tid], 21)
                    if energy is not None and (best is None or energy < best[0]):
                        best = (energy, flat, time)
        if not groups:
            continue
        operator = p.get("operator_name") or p.get("cpo_id") or ""
        if operator not in op_index:
            op_index[operator] = len(operators)
            operators.append(operator)
        energy, flat, time = best or (None, 0, 0)
        rows.append([round(lat, 5), round(lon, 5), op_index[operator], (p.get("address") or "").strip(),
                     [[c, kw, n] for (c, kw), n in sorted(groups.items())],
                     None if energy is None else round(energy, 3), round(flat, 2), round(time, 2)])
    return {"source": "DOT-NL (NDW), opendata.ndw.nu", "operators": operators, "locations": rows}


REGIONS = {"nl": build_nl}


def main():
    os.makedirs(OUT, exist_ok=True)
    for region in sys.argv[1:] or list(REGIONS):
        doc = REGIONS[region]()
        doc = {"version": 1, "region": region,
               "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "fields": ["lat", "lon", "operator", "address", "connectors[[code,kW,count]]",
                          "kwhPriceInclVAT", "sessionFee", "pricePerHour"],
               "connectors": CONNECTORS, **doc}
        path = os.path.join(OUT, f"{region}.json")
        with open(path, "w") as f:
            json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
        priced = sum(1 for r in doc["locations"] if r[5] is not None)
        print(f"== {region}: {len(doc['locations'])} Standorte, {priced} mit Preis, {os.path.getsize(path) // 1024} KB")


if __name__ == "__main__":
    main()
