#!/usr/bin/env python3
"""Builds the NRG charging directory from the official AFIR national access points: per charging
location its position, connectors, maximum power and the ad-hoc price.

    python3 build_charging.py            all regions
    python3 build_charging.py nl         only these

Output: site/charging/<region>.json
Netherlands: DOT-NL by NDW (opendata.ndw.nu), open data free for reuse by third parties.
Finland: Fintraffic / digitraffic.fi, CC BY 4.0.
Poland: EIPA by UDT (eipa.udt.gov.pl), free for commercial and non-commercial use; needs the
reader key in EIPA_TOKEN (GitHub secret). EIPA_DIR=<folder> reads saved files instead
(the download limit is 10 per hour for the static files).
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
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Digitraffic-User": USER_AGENT,
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
        packed = resp.headers.get("Content-Encoding") == "gzip" or url.endswith(".gz")
    return json.loads(gzip.decompress(data) if packed else data)


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


def fi_price(tariff):
    """(€/kWh, € per session, € per hour) incl. VAT from a Digitraffic tariff (OCPI-like, camelCase)."""
    included = tariff.get("taxIncluded") == "YES"
    found = {}
    for restricted in (False, True):
        for element in tariff.get("elements") or []:
            if bool(element.get("restrictions")) != restricted:
                continue
            for c in element.get("priceComponents") or []:
                gross = c["price"] if included else c["price"] * (1 + (c.get("vat") if c.get("vat") is not None else 25.5) / 100)
                found.setdefault(c["type"], gross)
        if "ENERGY" in found:
            break
    return found.get("ENERGY"), found.get("FLAT", 0.0), found.get("TIME", 0.0)


def build_fi():
    base = "https://afir.digitraffic.fi/api/charging-network/v1/"
    tariffs = {t["id"]: t for t in get(base + "tariffs?limit=ALL")["tariffs"]}
    features = get(base + "locations?limit=ALL")["features"]
    operators, op_index, rows = [], {}, []
    for f in features:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        groups, best = {}, None
        for evse in p.get("evses") or []:
            for c in evse.get("connectors") or []:
                key = (CONNECTORS.get(c.get("standard"), 0), round((c.get("maxElectricPower") or 0) / 1000))
                groups[key] = groups.get(key, 0) + 1
                # Ad-hoc tariffs first; untyped ones are the ad-hoc price for most operators.
                candidates = [tariffs[t] for t in c.get("tariffIds") or [] if t in tariffs]
                candidates.sort(key=lambda t: t.get("type") != "AD_HOC_PAYMENT")
                for tariff in candidates[:1]:
                    energy, flat, time = fi_price(tariff)
                    if energy is not None and (best is None or energy < best[0]):
                        best = (energy, flat, time)
        if not groups:
            continue
        operator = ((p.get("operator") or {}).get("details") or {}).get("name") or ""
        if operator not in op_index:
            op_index[operator] = len(operators)
            operators.append(operator)
        address = p.get("address") or {}
        street = ", ".join(x for x in (address.get("street"), address.get("city")) if x)
        energy, flat, time = best or (None, 0, 0)
        rows.append([round(lat, 5), round(lon, 5), op_index[operator], street,
                     [[c, kw, n] for (c, kw), n in sorted(groups.items())],
                     None if energy is None else round(energy, 3), round(flat, 2), round(time, 2)])
    return {"source": "Fintraffic / digitraffic.fi, CC BY 4.0", "operators": operators, "locations": rows}


# EIPA connector interfaces → CONNECTORS codes
EIPA_CONNECTORS = {10: 1, 17: 1, 29: 2, 11: 3, 19: 4, 20: 4, 30: 5, 25: 6, 6: 7, 7: 7}


def eipa(name):
    folder = os.environ.get("EIPA_DIR")
    if folder:
        return json.load(open(os.path.join(folder, f"{name}.json")))
    return get(f"https://eipa.udt.gov.pl/reader/export-data/{name}/{os.environ['EIPA_TOKEN']}")


def build_pl():
    operators_by_id = {o["id"]: o.get("short_name") or o.get("name") or "" for o in eipa("operator")["data"]}
    pools = {p["id"]: p for p in eipa("pool")["data"] if p.get("charging")}
    station_pool = {s["id"]: s["pool_id"] for s in eipa("station")["data"] if s.get("type") == "E"}
    prices = {}
    for d in eipa("dynamic")["data"]:
        kwh = [float(x["price"]) for x in d.get("prices") or [] if x.get("unit") == "kWh" and x.get("price")]
        if kwh:
            prices[d["point_id"]] = min(kwh)
    groups, best = {}, {}
    for point in eipa("point")["data"]:
        pool_id = station_pool.get(point.get("station_id"))
        if pool_id not in pools:
            continue
        for c in point.get("connectors") or []:
            code = next((EIPA_CONNECTORS[i] for i in c.get("interfaces") or [] if i in EIPA_CONNECTORS), 0)
            key = (code, round(c.get("power") or 0))
            groups.setdefault(pool_id, {})[key] = groups.setdefault(pool_id, {}).get(key, 0) + 1
        if point["id"] in prices:
            best[pool_id] = min(best.get(pool_id, prices[point["id"]]), prices[point["id"]])
    operators, op_index, rows = [], {}, []
    for pool_id, g in groups.items():
        pool = pools[pool_id]
        operator = operators_by_id.get(pool.get("operator_id"), "")
        if operator not in op_index:
            op_index[operator] = len(operators)
            operators.append(operator)
        street = " ".join(x for x in (pool.get("street"), pool.get("house_number")) if x)
        address = ", ".join(x for x in (street, pool.get("city")) if x)
        price = best.get(pool_id)
        rows.append([round(pool["latitude"], 5), round(pool["longitude"], 5), op_index[operator], address,
                     [[c, kw, n] for (c, kw), n in sorted(g.items())],
                     None if price is None else round(price, 2), 0, 0])
    return {"source": "EIPA (UDT), eipa.udt.gov.pl", "currency": "PLN", "operators": operators, "locations": rows}


REGIONS = {"nl": build_nl, "fi": build_fi, "pl": build_pl}


def main():
    os.makedirs(OUT, exist_ok=True)
    for region in sys.argv[1:] or list(REGIONS):
        if region == "pl" and not (os.environ.get("EIPA_TOKEN") or os.environ.get("EIPA_DIR")):
            print("== pl: übersprungen, EIPA_TOKEN fehlt")
            continue
        doc = REGIONS[region]()
        doc = {"version": 1, "region": region,
               "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "fields": ["lat", "lon", "operator", "address", "connectors[[code,kW,count]]",
                          "kwhPriceInclVAT", "sessionFee", "pricePerHour"],
               "connectors": CONNECTORS, "currency": "EUR", **doc}
        path = os.path.join(OUT, f"{region}.json")
        with open(path, "w") as f:
            json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
        priced = sum(1 for r in doc["locations"] if r[5] is not None)
        print(f"== {region}: {len(doc['locations'])} Standorte, {priced} mit Preis, {os.path.getsize(path) // 1024} KB")


if __name__ == "__main__":
    main()
