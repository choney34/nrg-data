#!/usr/bin/env python3
"""Builds the NRG charging directory from the official AFIR national access points: per charging
location its position, connectors, maximum power and the ad-hoc price.

    python3 build_charging.py            all regions
    python3 build_charging.py nl         only these

Output: site/charging/<region>.json
Netherlands: DOT-NL by NDW (opendata.ndw.nu), open data free for reuse by third parties.
Finland: Fintraffic / digitraffic.fi, CC BY 4.0.
Germany: AFIR data from the Mobilithek (ad-hoc prices; offers licensed "free use, open data",
needs a machine certificate: MOBILITHEK_CERT = PEM file with certificate and key,
MOBILITHEK_STATIC / MOBILITHEK_DYNAMIC = subscription ids, comma separated), completed with the
Ladesäulenregister der Bundesnetzagentur (locations without prices, CC BY 4.0).
Poland: EIPA by UDT (eipa.udt.gov.pl), free for commercial and non-commercial use; needs the
reader key in EIPA_TOKEN (GitHub secret). EIPA_DIR=<folder> reads saved files instead
(the download limit is 10 per hour for the static files).
"""
import csv
import datetime
import gzip
import io
import json
import math
import os
import re
import ssl
import sys
import time
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


BNETZA_PAGE = "https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/DownloadundKontakt.html"
BNETZA_CONNECTORS = {"AC Typ 2 Steckdose": 1, "AC Typ 2 Fahrzeugkupplung": 1, "DC Fahrzeugkupplung Typ Combo 2 (CCS)": 2,
                     "DC CHAdeMO": 3, "AC Typ 1 Steckdose": 4, "DC Tesla Fahrzeugkupplung (Typ 2)": 6, "AC Schuko": 7}


_register = {"at": 0, "doc": None}


def register_de():
    """The Ladesäulenregister changes monthly: fetch it at most once a day per process."""
    if _register["doc"] is None or time.time() - _register["at"] > 86_400:
        _register.update(at=time.time(), doc=build_de_register())
    return _register["doc"]


def mobilithek(subscription):
    context = ssl.create_default_context()
    context.load_cert_chain(os.environ["MOBILITHEK_CERT"])
    url = f"https://mobilithek.info:8443/mobilithek/api/v1.0/subscription?subscriptionID={subscription}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=300, context=context) as resp:
        if resp.status == 204:   # nothing in the packet buffer
            return None
        data = resp.read()
    return json.loads(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)


def text(value):
    return ((value or {}).get("values") or [{}])[0].get("value", "")


DATEX_CONNECTORS = {"iec62196T2": 1, "iec62196T2COMBO": 2, "chademo": 3, "iec62196T1": 4,
                    "iec62196T1COMBO": 5, "teslaS": 6, "domesticF": 7}


def datex_price(energy_prices):
    """(€/kWh, € per session, € per hour) incl. VAT from DATEX II energyPrice entries. Minute
    prices that only apply after some time (blocking fees) are not a charging price."""
    found = {}
    for p in energy_prices or []:
        rate = p.get("taxRate") or 0
        rate = rate * 100 if 0 < rate < 1 else rate
        # Net prices without a tax rate: German VAT (prices to consumers are due incl. VAT).
        gross = p["value"] if p.get("taxIncluded") else p["value"] * (1 + (rate or 19) / 100)
        kind = (p.get("priceType") or {}).get("value")
        if kind == "pricePerMinute":
            if p.get("timeBasedApplicability"):
                continue
            kind, gross = "hour", gross * 60
        found[kind] = min(found.get(kind, gross), gross)
    return found.get("pricePerKWh"), found.get("flatRate", 0.0), found.get("hour", 0.0)


_afir = {"at": 0, "sites": None, "updates": {}}
AFIR_STATIC_MAX_AGE_S = int(os.environ.get("MOBILITHEK_STATIC_MAX_AGE_S", 6 * 3600))


def mobilithek_static():
    """Sites of all subscribed static offers: (lat, lon, operator, address, connector groups,
    [(charging point id, its ad-hoc energyPrice entries)])."""
    sites = []
    for sub in filter(None, os.environ.get("MOBILITHEK_STATIC", "").split(",")):
        doc = mobilithek(sub.strip()) or {}
        publication = (doc.get("payload") or {}).get("aegiEnergyInfrastructureTablePublication") or {}
        for table in publication.get("energyInfrastructureTable") or []:
            for site in table.get("energyInfrastructureSite") or []:
                location = (site.get("locationReference") or {}).get("locAreaLocation") or {}
                coords = location.get("coordinatesForDisplay") or {}
                if "latitude" not in coords:
                    continue
                address = ((location.get("locLocationExtensionG") or {}).get("FacilityLocation") or {}).get("address") or {}
                street = " ".join(text(line.get("text")) for line in sorted(address.get("addressLine") or [], key=lambda l: l.get("order", 0)))
                groups, points = {}, []
                for station in site.get("energyInfrastructureStation") or []:
                    for point in station.get("refillPoint") or []:
                        cp = point.get("aegiElectricChargingPoint")
                        if not cp:
                            continue
                        for c in cp.get("connector") or []:
                            key = (DATEX_CONNECTORS.get((c.get("connectorType") or {}).get("value"), 0),
                                   round((c.get("maxPowerAtSocket") or 0) / 1000))
                            groups[key] = groups.get(key, 0) + 1
                        points.append((cp.get("idG"), [
                            p for e in cp.get("electricEnergy") or [] for r in e.get("energyRate") or []
                            if (r.get("ratePolicy") or {}).get("value", "adHoc") == "adHoc"
                            for p in r.get("energyPrice") or []]))
                if groups:
                    operator = text(((site.get("operator") or {}).get("afacAnOrganisation") or {}).get("name"))
                    sites.append((round(coords["latitude"], 5), round(coords["longitude"], 5), operator,
                                  ", ".join(x for x in (street.strip(), text(address.get("city"))) if x), groups, points))
    return sites


def mobilithek_updates():
    """Price updates per charging point id from the dynamic offers' current packets."""
    updates = {}
    for sub in filter(None, os.environ.get("MOBILITHEK_DYNAMIC", "").split(",")):
        try:
            doc = mobilithek(sub.strip()) or {}
        except Exception as error:   # dynamic data is an extra
            print(f"   Mobilithek dynamisch {sub}: {error}", file=sys.stderr)
            continue
        payloads = (doc.get("messageContainer") or {}).get("payload") or [doc.get("payload") or {}]
        for payload in payloads:
            for site in (payload.get("aegiEnergyInfrastructureStatusPublication") or {}).get("energyInfrastructureSiteStatus") or []:
                for station in site.get("energyInfrastructureStationStatus") or []:
                    for point in station.get("refillPointStatus") or []:
                        status = point.get("aegiElectricChargingPointStatus") or {}
                        for update in status.get("energyRateUpdate") or []:
                            updates[(status.get("reference") or {}).get("idG")] = update.get("energyPrice")
    return updates


def mobilithek_sites():
    """AFIR sites with their cheapest ad-hoc price. The static offers are large and fetched every
    few hours; the small dynamic packets carry only recent changes, so their price updates are
    collected across calls until the next static fetch."""
    if _afir["sites"] is None or time.time() - _afir["at"] > AFIR_STATIC_MAX_AGE_S:
        _afir.update(at=time.time(), sites=mobilithek_static(), updates={})
    _afir["updates"].update(mobilithek_updates())
    updates, result = _afir["updates"], []
    for lat, lon, operator, address, groups, points in _afir["sites"]:
        best = None
        for point_id, prices in points:
            energy, flat, hour = datex_price(updates.get(point_id, prices))
            if energy is not None and (best is None or energy < best[0]):
                best = (energy, flat, hour)
        result.append((lat, lon, operator, address, groups, best))
    return result


def build_de():
    """Mobilithek sites with prices, plus the register's sites that have no Mobilithek site within 50 m."""
    register = register_de()
    if not (os.environ.get("MOBILITHEK_CERT") and os.environ.get("MOBILITHEK_STATIC")):
        return register
    sites = mobilithek_sites()
    operators, op_index, rows, taken = [], {}, [], {}

    def op(name):
        if name not in op_index:
            op_index[name] = len(operators)
            operators.append(name)
        return op_index[name]

    for lat, lon, operator, address, groups, best in sites:
        energy, flat, hour = best or (None, 0, 0)
        rows.append([lat, lon, op(operator), address, [[c, kw, n] for (c, kw), n in sorted(groups.items())],
                     None if energy is None else round(energy, 3), round(flat, 2), round(hour, 2)])
        taken.setdefault((int(lat * 1000), int(lon * 1000)), []).append((lat, lon))
    for row in register["locations"]:
        cy, cx = int(row[0] * 1000), int(row[1] * 1000)
        near = any(math.hypot((row[0] - lat) * 111_320, (row[1] - lon) * 111_320 * math.cos(math.radians(lat))) <= 50
                   for dy in (-1, 0, 1) for dx in (-1, 0, 1) for lat, lon in taken.get((cy + dy, cx + dx), ()))
        if not near:
            rows.append([row[0], row[1], op(register["operators"][row[2]])] + row[3:])
    return {"source": "Mobilithek (AFIR, Datenlizenz: freie Nutzung/Open Data); " + register["source"],
            "operators": operators, "locations": rows}


def build_de_register():
    """Ladesäulenregister (monthly CSV, file name carries the date): one row per site and operator."""
    page = urllib.request.urlopen(urllib.request.Request(BNETZA_PAGE, headers={"User-Agent": USER_AGENT}), timeout=60).read().decode("utf-8", "replace")
    url = re.search(r'href="(https://data\.bundesnetzagentur\.de/[^"]*Ladesaeulenregister[^"]*\.csv)"', page).group(1)
    raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=300).read()
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    header_at = next(i for i, r in enumerate(rows) if r and r[0] == "Ladeeinrichtungs-ID")
    idx = {h: i for i, h in enumerate(rows[header_at])}
    sites = {}
    for r in rows[header_at + 1:]:
        if len(r) < len(idx) - 5 or not r[0]:
            continue
        try:
            lat = round(float(r[idx["Breitengrad"]].replace(",", ".")), 5)
            lon = round(float(r[idx["Längengrad"]].replace(",", ".")), 5)
        except ValueError:
            continue
        operator = (r[idx["Anzeigename (Karte)"]] or r[idx["Betreiber"]]).strip()
        site = sites.setdefault((lat, lon, operator), {
            "address": ", ".join(x for x in (" ".join(y for y in (r[idx["Straße"]].strip(), r[idx["Hausnummer"]].strip()) if y),
                                             r[idx["Ort"]].strip()) if x),
            "groups": {}})
        for k in range(1, 7):
            plugs = r[idx[f"Steckertypen{k}"]] if f"Steckertypen{k}" in idx else ""
            if not plugs.strip():
                continue
            code = next((BNETZA_CONNECTORS[t.strip()] for t in plugs.split(";") if t.strip() in BNETZA_CONNECTORS), 0)
            try:
                kw = round(float(r[idx[f"Nennleistung Stecker{k}"]].replace(",", ".") or 0))
            except ValueError:
                kw = 0
            site["groups"][(code, kw)] = site["groups"].get((code, kw), 0) + 1
    operators, op_index, out = [], {}, []
    for (lat, lon, operator), site in sites.items():
        if not site["groups"]:
            continue
        if operator not in op_index:
            op_index[operator] = len(operators)
            operators.append(operator)
        out.append([lat, lon, op_index[operator], site["address"],
                    [[c, kw, n] for (c, kw), n in sorted(site["groups"].items())], None, 0, 0])
    return {"source": f"Ladesäulenregister der Bundesnetzagentur ({url.rsplit('_', 1)[-1][:-4]}), CC BY 4.0",
            "operators": operators, "locations": out}


REGIONS = {"nl": build_nl, "fi": build_fi, "pl": build_pl, "de": build_de}


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
