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
Spain: DGT national access point (nap.dgt.es), CC BY; locations without prices.
Austria: E-Control Ladestellenverzeichnis, CC BY 4.0; needs ECONTROL_APIKEY (GitHub secret).
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
        groups, best, free, known = {}, None, 0, 0
        for a in p.get("availabilities") or []:
            if a.get("available") is not None:
                free += a["available"]
                known += a.get("total") or 0
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
                     None if energy is None else round(energy, 3), round(flat, 2), round(time, 2),
                     [free, known] if known else None])
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
    try:   # the status is an extra; OCPI values
        states = {x["evseId"]: x["status"] for x in get(base + "locations/statuses?limit=ALL")["statuses"]}
    except Exception as error:
        print(f"   fi Status: {error}", file=sys.stderr)
        states = {}
    busy = {"CHARGING", "OUTOFORDER", "INOPERATIVE", "BLOCKED", "RESERVED"}
    operators, op_index, rows = [], {}, []
    for f in features:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        groups, best, free, known = {}, None, 0, 0
        for evse in p.get("evses") or []:
            state = states.get(evse.get("id"))
            if state == "AVAILABLE" or state in busy:
                free += state == "AVAILABLE"
                known += 1
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
                     None if energy is None else round(energy, 3), round(flat, 2), round(time, 2),
                     [free, known] if known else None])
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
    prices, state = {}, {}
    for d in eipa("dynamic")["data"]:
        kwh = [float(x["price"]) for x in d.get("prices") or [] if x.get("unit") == "kWh" and x.get("price")]
        if kwh:
            prices[d["point_id"]] = min(kwh)
        status = d.get("status") or {}
        if status.get("availability") is not None and status.get("status") is not None:
            state[d["point_id"]] = status["availability"] == 1 and status["status"] == 1   # operational and free
    groups, best, avail = {}, {}, {}
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
        if point["id"] in state:
            a = avail.setdefault(pool_id, [0, 0])
            a[0] += state[point["id"]]
            a[1] += 1
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
                     None if price is None else round(price, 2), 0, 0, avail.get(pool_id)])
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


def mobilithek(subscription, raw=False):
    context = ssl.create_default_context()
    context.load_cert_chain(os.environ["MOBILITHEK_CERT"])
    url = f"https://mobilithek.info:8443/mobilithek/api/v1.0/subscription?subscriptionID={subscription}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=300, context=context) as resp:
        if resp.status == 204:   # nothing in the packet buffer
            return None
        data = resp.read()
    data = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    return data if raw else json.loads(data)


def text(value):
    return ((value or {}).get("values") or [{}])[0].get("value", "")


DATEX_CONNECTORS = {"iec62196T2": 1, "iec62196T2COMBO": 2, "chademo": 3, "iec62196T1": 4,
                    "iec62196T1COMBO": 5, "teslaS": 6, "domesticF": 7}


def datex_price(energy_prices):
    """(€/kWh, € per session, € per hour) incl. VAT from DATEX II energyPrice entries. Entries
    that only apply for part of the charging time (blocking fees after some hours, sometimes
    written as extra "per kWh" entries with 0 for the time before) are not the charging price."""
    found = {}
    for p in energy_prices or []:
        if p.get("timeBasedApplicability"):
            continue
        rate = p.get("taxRate") or 0
        rate = rate * 100 if 0 < rate < 1 else rate
        # Net prices without a tax rate: German VAT (prices to consumers are due incl. VAT).
        gross = p["value"] if p.get("taxIncluded") else p["value"] * (1 + (rate or 19) / 100)
        kind = (p.get("priceType") or {}).get("value")
        if kind == "pricePerMinute":
            kind, gross = "hour", gross * 60
        found[kind] = min(found.get(kind, gross), gross)
    return found.get("pricePerKWh"), found.get("flatRate", 0.0), found.get("hour", 0.0)


# Collected across calls and pushes: price updates and the status of each charging point.
_afir = {"at": 0, "sites": None, "updates": {}, "status": {}}
AFIR_FREE = {"available"}
AFIR_BUSY = {"charging", "blocked", "reserved", "occupied", "inoperative", "outOfOrder", "outOfService", "faulted"}
AFIR_STATUS_MAX_AGE_S = 48 * 3600
AFIR_STATIC_MAX_AGE_S = int(os.environ.get("MOBILITHEK_STATIC_MAX_AGE_S", 6 * 3600))


_rates = {}   # energy rates by id within the offer being read, for energyRateByReference


def ad_hoc_prices(element):
    rates = [r for e in element.get("electricEnergy") or []
             for r in (e.get("energyRate") or []) + [_rates.get(ref.get("idG")) or {} for ref in e.get("energyRateByReference") or []]]
    return [p for r in rates if (r.get("ratePolicy") or {}).get("value", "adHoc") == "adHoc"
            for p in r.get("energyPrice") or []]


def collect_rates(tables):
    """A rate may be written out at one charging point and only referenced at the others."""
    _rates.clear()
    for table in tables:
        for site in table.get("energyInfrastructureSite") or []:
            for station in site.get("energyInfrastructureStation") or []:
                holders = [station] + [p.get("aegiElectricChargingPoint") or {} for p in station.get("refillPoint") or []]
                for holder in holders:
                    for e in holder.get("electricEnergy") or []:
                        for rate in e.get("energyRate") or []:
                            if rate.get("idG"):
                                _rates[rate["idG"]] = rate


def mobilithek_static():
    """Sites of all subscribed static offers: (lat, lon, operator, address, connector groups,
    [(charging point id, its ad-hoc energyPrice entries)])."""
    sites = []
    for sub in filter(None, os.environ.get("MOBILITHEK_STATIC", "").split(",")):
        packet = mobilithek(sub.strip(), raw=True) or b"{}"
        if packet.lstrip()[:1] == b"<":
            sites += static_xml(packet)
            continue
        doc = json.loads(packet)
        del packet
        # Usually {"payload": {...}}; some wrap it as {"messageContainer": {"payload": [{...}]}}.
        payloads = (doc.get("messageContainer") or {}).get("payload") or [doc.get("payload") or {}]
        tables = [t for payload in payloads
                  for t in (payload.get("aegiEnergyInfrastructureTablePublication") or {}).get("energyInfrastructureTable") or []]
        collect_rates(tables)
        for table in tables:
            for site in table.get("energyInfrastructureSite") or []:
                # Providers use an area location, a point location or both; the address sits in either.
                # (Some put the location and the operator on the stations instead of the site.)
                stations = site.get("energyInfrastructureStation") or []
                references = [site.get("locationReference") or {}] + [st.get("locationReference") or {} for st in stations]
                locations = [r.get(k) or {} for r in references for k in ("locAreaLocation", "locPointLocation", "areaLocation", "pointLocation")]
                coords = next((c for l in locations
                               for c in (l.get("coordinatesForDisplay"), (l.get("pointByCoordinates") or {}).get("pointCoordinates"))
                               if c and "latitude" in c), None)
                if not coords:
                    continue
                extensions = [l.get("locLocationExtensionG") or {} for l in locations]
                address = next((a for x in extensions for key in ("FacilityLocation", "facilityLocation")
                                if (a := (x.get(key) or {}).get("address"))), {})
                street = " ".join(text(line.get("text")) for line in sorted(address.get("addressLine") or [], key=lambda l: l.get("order", 0))
                                  if (line.get("type") or {}).get("value", "street") in ("street", "houseNumber"))
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
                        # Rates of the point; some providers give them once for the whole station.
                        points.append((cp.get("idG"), ad_hoc_prices(cp) or ad_hoc_prices(station)))
                if groups:
                    # Operator by name; a legal name where the name is only a code ("DE*EWE"); the
                    # site name where the provider names no operator for the site.
                    organisation = next((o for x in [site] + stations
                                         for key in ("afacAnOrganisation", "afacReferenceableOrganisation")
                                         if (o := (x.get("operator") or {}).get(key))), {})
                    operator = text(organisation.get("name"))
                    if not operator or "*" in operator:
                        operator = (text(organisation.get("legalName")) or operator or text(site.get("name"))
                                    or next((text(st.get("name")) for st in stations if text(st.get("name"))), ""))
                    sites.append((round(coords["latitude"], 5), round(coords["longitude"], 5), operator,
                                  ", ".join(x for x in (street.strip(), text(address.get("city"))) if x), groups, points))
    return sites


def _timestamp(value, fallback):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return fallback


def _afir_point(point_id, value, reported, price_updates):
    """One charging point from a dynamic packet. `reported` is the packet's own time for it, so
    feeds that stopped updating age out instead of looking current."""
    if value in AFIR_FREE or value in AFIR_BUSY:
        known = _afir["status"].get(point_id)
        if not known or reported >= known[1]:
            _afir["status"][point_id] = (value in AFIR_FREE, reported)
    elif value:
        _afir["status"].pop(point_id, None)
    for prices in price_updates:
        _afir["updates"][point_id] = prices


def afir_apply(doc):
    """Takes a dynamic DATEX II packet in JSON (pulled, or pushed by the Mobilithek): price
    updates and the status of each charging point in it. Returns the number of points."""
    payloads = (doc.get("messageContainer") or {}).get("payload") or [doc.get("payload") or doc]
    now, count = time.time(), 0
    for payload in payloads:
        for site in (payload.get("aegiEnergyInfrastructureStatusPublication") or {}).get("energyInfrastructureSiteStatus") or []:
            for station in site.get("energyInfrastructureStationStatus") or []:
                for point in station.get("refillPointStatus") or []:
                    status = point.get("aegiElectricChargingPointStatus") or {}
                    point_id = (status.get("reference") or {}).get("idG")
                    if not point_id:
                        continue
                    count += 1
                    _afir_point(point_id, (status.get("status") or {}).get("value"),
                                _timestamp(status.get("lastUpdated"), now),
                                [u.get("energyPrice") for u in status.get("energyRateUpdate") or []])
    return count


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(element, *names):
    """First descendant along a path of local names."""
    for name in names:
        element = next((c for c in element if _local(c.tag) == name), None)
        if element is None:
            return None
    return element


def _xml_text(element, *names):
    found = _child(element, *names)
    if found is None:
        return ""
    value = _child(found, "values", "value")   # multilingual strings
    return ((value.text if value is not None else found.text) or "").strip()


def static_xml(data):
    """Static sites from providers that deliver DATEX II as XML (e.g. Smartlab); same tuples as
    the JSON reader."""
    import xml.etree.ElementTree as ET
    sites = []
    for site in ET.fromstring(data).iter():
        if _local(site.tag) != "energyInfrastructureSite":
            continue
        reference = _child(site, "locationReference")
        try:
            lat = float(_xml_text(reference, "coordinatesForDisplay", "latitude"))
            lon = float(_xml_text(reference, "coordinatesForDisplay", "longitude"))
        except (TypeError, ValueError):
            continue
        address = _child(reference, "_locationReferenceExtension", "facilityLocation", "address")
        lines = sorted((c for c in address if _local(c.tag) == "addressLine"), key=lambda c: int(c.get("order") or 0)) if address is not None else []
        street = " ".join(_xml_text(line, "text") for line in lines).strip()
        city = _xml_text(address, "city") if address is not None else ""
        groups, points = {}, []
        for point in site.iter():
            if _local(point.tag) != "refillPoint":
                continue
            for c in (c for c in point if _local(c.tag) == "connector"):
                try:
                    kw = round(float(_xml_text(c, "maxPowerAtSocket") or 0) / 1000)
                except ValueError:
                    kw = 0
                key = (DATEX_CONNECTORS.get(_xml_text(c, "connectorType"), 0), kw)
                groups[key] = groups.get(key, 0) + 1
            prices = []
            for price in (e for e in point.iter() if _local(e.tag) == "energyPrice"):
                try:
                    prices.append({"priceType": {"value": _xml_text(price, "priceType")},
                                   "value": float(_xml_text(price, "value")),
                                   "taxIncluded": _xml_text(price, "taxIncluded") == "true",
                                   "taxRate": float(_xml_text(price, "taxRate") or 0)})
                except ValueError:
                    pass
            points.append((point.get("id"), prices))
        if groups:
            # Operator by name; where it is only a code ("DESTA"), the site name stands in.
            operator = _xml_text(site, "operator", "name")
            if not operator or (operator.isupper() and " " not in operator and len(operator) <= 8):
                operator = _xml_text(site, "name") or operator
            sites.append((round(lat, 5), round(lon, 5), operator,
                          ", ".join(x for x in (street, city) if x), groups, points))
    return sites


def afir_apply_xml(data):
    """The same for providers that deliver DATEX II as XML (e.g. Smartlab)."""
    import xml.etree.ElementTree as ET

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    def child_text(element, name):
        return next((c.text for c in element if local(c.tag) == name), None)

    now, count = time.time(), 0
    for element in ET.fromstring(data).iter():
        if local(element.tag) != "refillPointStatus":
            continue
        point_id = next((c.get("id") for c in element if local(c.tag) == "reference"), None)
        if not point_id:
            continue
        count += 1
        updates = []
        for update in (c for c in element if local(c.tag) == "energyRateUpdate"):
            updates.append([{
                "priceType": {"value": child_text(price, "priceType")},
                "value": float(child_text(price, "value") or 0),
                "taxIncluded": child_text(price, "taxIncluded") == "true",
                "taxRate": float(child_text(price, "taxRate") or 0),
            } for price in update if local(price.tag) == "energyPrice"])
        _afir_point(point_id, child_text(element, "status"), _timestamp(child_text(element, "lastUpdated"), now), updates)
    return count


def afir_state():
    return {"updates": dict(_afir["updates"]), "status": dict(_afir["status"])}


def afir_restore(state):
    _afir["updates"].update(state.get("updates") or {})
    _afir["status"].update({k: tuple(v) for k, v in (state.get("status") or {}).items()})


def mobilithek_updates():
    """Pulls the dynamic offers' current packets (a fallback: packets only hold the last minute's
    changes, so pulls miss some; the Mobilithek should push them to the server instead)."""
    for sub in filter(None, os.environ.get("MOBILITHEK_DYNAMIC", "").split(",")):
        try:
            packet = mobilithek(sub.strip(), raw=True)
            if packet:
                afir_apply_xml(packet) if packet.lstrip()[:1] == b"<" else afir_apply(json.loads(packet))
        except Exception as error:   # dynamic data is an extra
            print(f"   Mobilithek dynamisch {sub}: {error}", file=sys.stderr)


def merge_sites(sites):
    """One site per coordinate: some providers list every charging point as its own site, and
    the same network can come in through two offers. Points already seen are not counted twice."""
    merged, seen = {}, set()
    for lat, lon, operator, address, groups, points in sites:
        fresh = [p for p in points if p[0] not in seen]
        if points and not fresh:
            continue
        seen.update(p[0] for p in fresh)
        site = merged.get((lat, lon))
        if site is None:
            merged[(lat, lon)] = [lat, lon, operator, address, dict(groups), list(fresh)]
            continue
        share = len(fresh) / len(points) if points else 1   # only the new points' connectors
        for key, n in groups.items():
            site[4][key] = site[4].get(key, 0) + max(1, round(n * share))
        site[5] += fresh
    return [tuple(site) for site in merged.values()]


def mobilithek_sites():
    """AFIR sites with their cheapest ad-hoc price. The static offers are large and fetched every
    few hours; the small dynamic packets carry only recent changes, so their price updates are
    collected across calls until the next static fetch."""
    if _afir["sites"] is None or time.time() - _afir["at"] > AFIR_STATIC_MAX_AGE_S:
        sites = merge_sites(mobilithek_static())
        _afir["updates"].clear()   # the fresh static data has the current prices
        _afir.update(at=time.time(), sites=sites)
    mobilithek_updates()
    updates, status, result = _afir["updates"], _afir["status"], []
    oldest = time.time() - AFIR_STATUS_MAX_AGE_S
    for lat, lon, operator, address, groups, points in _afir["sites"]:
        best, free, known = None, 0, 0
        for point_id, prices in points:
            energy, flat, hour = datex_price(updates.get(point_id, prices))
            if energy is not None and (best is None or energy < best[0]):
                best = (energy, flat, hour)
            state = status.get(point_id)
            if state and state[1] >= oldest:
                free += state[0]
                known += 1
        result.append((lat, lon, operator, address, groups, best, [free, known] if known else None))
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

    for lat, lon, operator, address, groups, best, availability in sites:
        if not (46 < lat < 56 and 5 < lon < 16):   # missing or wrong coordinates (0/0)
            continue
        energy, flat, hour = best or (None, 0, 0)
        rows.append([lat, lon, op(operator), address, [[c, kw, n] for (c, kw), n in sorted(groups.items())],
                     None if energy is None else round(energy, 3), round(flat, 2), round(hour, 2), availability])
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
                    [[c, kw, n] for (c, kw), n in sorted(site["groups"].items())], None, 0, 0, None])
    return {"source": f"Ladesäulenregister der Bundesnetzagentur ({url.rsplit('_', 1)[-1][:-4]}), CC BY 4.0",
            "operators": operators, "locations": out}


ES_URL = "https://infocar.dgt.es/datex2/v3/miterd/EnergyInfrastructureTablePublication/electrolineras.xml"


def build_es():
    """Spain: national access point of the DGT (nap.dgt.es), DATEX II XML, CC BY. Locations and
    connectors; the publication carries no prices."""
    req = urllib.request.Request(ES_URL, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    data = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    operators, op_index, rows = [], {}, []
    for lat, lon, operator, address, groups, points in merge_sites(static_xml(data)):
        # "Dirección: Camí dels Reis 166 Municipio: Palma Provincia: …" → "Camí dels Reis 166, Palma"
        match = re.match(r"Dirección:\s*(.*?)\s*Municipio:\s*(.*?)\s*(?:Provincia:|$)", address)
        if match:
            address = ", ".join(x for x in match.groups() if x)
        if operator not in op_index:
            op_index[operator] = len(operators)
            operators.append(operator)
        prices = [datex_price(p) for _, p in points]
        best = min((x for x in prices if x[0] is not None), default=(None, 0, 0))
        rows.append([lat, lon, op_index[operator], address, [[c, kw, n] for (c, kw), n in sorted(groups.items())],
                     None if best[0] is None else round(best[0], 3), round(best[1], 2), round(best[2], 2), None])
    return {"source": "DGT, Punto de Acceso Nacional (nap.dgt.es), CC BY", "operators": operators, "locations": rows}


AT_URL = "https://api.e-control.at/charge/1.0/search/stations"
AT_CONNECTORS = {"TYPE_2_AC": 1, "COMBO2_CCS_DC": 2, "CHADEMO_DC": 3, "TYPE_1_AC": 4, "COMBO1_CCS_DC": 5,
                 "TESLA_S": 6, "SCHUKO": 7}


def build_at():
    """Austria: Ladestellenverzeichnis of E-Control (ladestellen.at), CC BY 4.0. The key is bound
    to a domain that must be sent as Referer: ECONTROL_APIKEY, ECONTROL_REFERER. The search
    returns all stations by distance from a point, 1000 per request."""
    headers = {"User-Agent": USER_AGENT, "Apikey": os.environ["ECONTROL_APIKEY"],
               "Referer": os.environ.get("ECONTROL_REFERER", "https://api.nrg-app.com")}
    stations, start = [], 0
    while True:
        url = f"{AT_URL}?latitude=47.6&longitude=13.6&fromIndex={start}&endIndex={start + 999}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as resp:
            page = json.load(resp)
        stations += page.get("stations") or []
        start += 1000
        if start >= page.get("totalResults", 0) or not page.get("stations"):
            break
        time.sleep(1)
    operators, op_index, rows = [], {}, []
    for st in stations:
        location = st.get("location") or {}
        if st.get("status") != "ACTIVE" or location.get("latitude") is None:
            continue
        groups, best = {}, None
        for point in st.get("points") or []:
            code = next((AT_CONNECTORS[c["consumerName"]] for c in point.get("connectorTypes") or []
                         if c.get("consumerName") in AT_CONNECTORS), 0)
            key = (code, round(point.get("energyInKw") or 0))
            groups[key] = groups.get(key, 0) + 1
            # 0 ct/kWh without "free of charge" is a time-based tariff, not a free one.
            energy = 0.0 if point.get("freeOfCharge") else ((point.get("priceInCentPerKwh") or 0) / 100 or None)
            if energy is not None and (best is None or energy < best[0]):
                best = (energy, (point.get("startFeeCent") or 0) / 100, (point.get("priceInCentPerMin") or 0) * 60 / 100)
        if not groups:
            continue
        operator = st.get("operatorName") or st.get("contactName") or ""
        if operator not in op_index:
            op_index[operator] = len(operators)
            operators.append(operator)
        energy, flat, hour = best or (None, 0, 0)
        rows.append([round(location["latitude"], 5), round(location["longitude"], 5), op_index[operator],
                     ", ".join(x for x in (st.get("street"), st.get("city")) if x),
                     [[c, kw, n] for (c, kw), n in sorted(groups.items())],
                     None if energy is None else round(energy, 3), round(flat, 2), round(hour, 2), None])
    return {"source": "E-Control Ladestellenverzeichnis (ladestellen.at), CC BY 4.0", "operators": operators, "locations": rows}


REGIONS = {"nl": build_nl, "fi": build_fi, "pl": build_pl, "de": build_de, "es": build_es, "at": build_at}


def main():
    os.makedirs(OUT, exist_ok=True)
    for region in sys.argv[1:] or list(REGIONS):
        if region == "pl" and not (os.environ.get("EIPA_TOKEN") or os.environ.get("EIPA_DIR")):
            print("== pl: übersprungen, EIPA_TOKEN fehlt")
            continue
        if region == "at" and not os.environ.get("ECONTROL_APIKEY"):
            print("== at: übersprungen, ECONTROL_APIKEY fehlt")
            continue
        doc = REGIONS[region]()
        doc = {"version": 1, "region": region,
               "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "fields": ["lat", "lon", "operator", "address", "connectors[[code,kW,count]]",
                          "kwhPriceInclVAT", "sessionFee", "pricePerHour", "availability[free,known]|null"],
               "connectors": CONNECTORS, "currency": "EUR", **doc}
        path = os.path.join(OUT, f"{region}.json")
        with open(path, "w") as f:
            json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
        priced = sum(1 for r in doc["locations"] if r[5] is not None)
        print(f"== {region}: {len(doc['locations'])} Standorte, {priced} mit Preis, {os.path.getsize(path) // 1024} KB")


if __name__ == "__main__":
    main()
