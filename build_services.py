#!/usr/bin/env python3
"""Builds the NRG service directory from OpenStreetMap: for every fuel station, which services
are at the station or within 150 m (food, toilets, shop, EV charging, car wash, air).

Output: site/services/<country>.json and site/services/index.json, published via GitHub Pages.

    python3 build_services.py            all countries
    python3 build_services.py de lu      only these

Data © OpenStreetMap contributors, available under the Open Database License (ODbL).
"""
import datetime
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OVERPASS = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
USER_AGENT = "NRG-service-directory/1.0 (https://github.com/choney34/nrg-data)"
RADIUS_M = 150
CHUNK_DEG = 2.0
MIN_CHUNK_DEG = 0.25
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "services")

# Bit values shared with the app (StationService.bit).
BITS = {"food": 1, "restroom": 2, "atm": 4, "shopping": 8, "evCharger": 16, "carWash": 32, "air": 64}

# Rough bounding boxes (south, west, north, east); neighbouring countries overlap, which only
# means a border station appears in both files.
COUNTRIES = {
    "de": [(47.2, 5.8, 55.1, 15.1)],
    "at": [(46.3, 9.5, 49.1, 17.2)],
    "lu": [(49.4, 5.7, 50.2, 6.6)],
    "fr": [(41.3, -5.2, 51.1, 9.6)],
    "it": [(36.6, 6.6, 47.1, 18.6)],
    "es": [(35.9, -9.4, 43.8, 4.4), (27.6, -18.2, 29.5, -13.4)],
    "pt": [(36.9, -9.6, 42.2, -6.1), (32.6, -17.3, 33.1, -16.2), (36.9, -31.3, 39.8, -25.0)],
    "uk": [(49.9, -8.2, 60.9, 1.8)],
    "mx": [(14.5, -118.4, 32.8, -86.7)],
}

QUERY = """[out:json][timeout:240];
nwr["amenity"="fuel"]({bbox})->.f;
.f out center tags;
(
  nwr(around.f:{r})["amenity"~"^(restaurant|fast_food|cafe|toilets|charging_station|car_wash|atm)$"];
  nwr(around.f:{r})["shop"~"^(supermarket|convenience|bakery)$"];
  nwr(around.f:{r})["amenity"="compressed_air"];
);
out center tags;
"""

# Separate query: combined with the one above, overpass-api.de times out.
SERVICES_QUERY = """[out:json][timeout:120];
way["highway"="services"]({bbox});
out geom;
"""


def overpass(query):
    data = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(6):
        req = urllib.request.Request(OVERPASS, data=data, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):          # busy: wait and retry
                time.sleep(30 * (attempt + 1))
                continue
            if e.code == 504:                 # too big or overloaded: let the caller split
                return None
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(15 * (attempt + 1))
    return None


def position(el):
    if "lat" in el:
        return el["lat"], el["lon"]
    if "center" in el:
        return el["center"]["lat"], el["center"]["lon"]
    return None


def distance(a, b):
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def inside(point, polygon):
    lat, lon = point
    hit = False
    for (y1, x1), (y2, x2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def amenity_bit(tags):
    a, s = tags.get("amenity"), tags.get("shop")
    if a in ("restaurant", "fast_food", "cafe") or s == "bakery":
        return BITS["food"]
    if a == "toilets":
        return BITS["restroom"]
    if a == "atm":
        return BITS["atm"]
    if s in ("supermarket", "convenience"):
        return BITS["shopping"]
    if a == "charging_station":
        return BITS["evCharger"]
    if a == "car_wash":
        return BITS["carWash"]
    if a == "compressed_air":
        return BITS["air"]
    return 0


def own_bits(tags):
    bits = 0
    if tags.get("toilets") == "yes":
        bits |= BITS["restroom"]
    if tags.get("atm") == "yes":
        bits |= BITS["atm"]
    if tags.get("car_wash") == "yes":
        bits |= BITS["carWash"]
    if tags.get("compressed_air") == "yes":
        bits |= BITS["air"]
    if tags.get("shop") and tags.get("shop") != "no":
        bits |= BITS["shopping"]
    return bits


def fetch_chunk(bbox, stations, failed):
    """Fills stations[osm_id] = (lat, lon, bits) for one box, splitting it when Overpass gives up.
    Boxes that still fail at the smallest size go to `failed` for a later retry."""
    s, w, n, e = bbox
    result = overpass(QUERY.format(bbox=f"{s},{w},{n},{e}", r=RADIUS_M))
    if result is None:
        if n - s <= MIN_CHUNK_DEG:
            failed.append(bbox)
            return
        ms, mw = (s + n) / 2, (w + e) / 2
        for sub in ((s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)):
            fetch_chunk(sub, stations, failed)
        return
    areas = overpass(SERVICES_QUERY.format(bbox=f"{s},{w},{n},{e}")) or {"elements": []}
    fuel, others, services = [], [], []
    for el in result["elements"] + areas["elements"]:
        tags = el.get("tags", {})
        if tags.get("highway") == "services" and "geometry" in el:
            services.append([(p["lat"], p["lon"]) for p in el["geometry"]])
        elif tags.get("amenity") == "fuel":
            if (p := position(el)):
                fuel.append((f"{el['type'][0]}{el['id']}", p, tags))
        elif (p := position(el)) and (bit := amenity_bit(tags)):
            others.append((p, bit))
    # Grid of ~1 km cells so each station only checks the amenities next to it.
    grid = {}
    for p, bit in others:
        grid.setdefault((int(p[0] * 100), int(p[1] * 100)), []).append((p, bit))
    for osm_id, p, tags in fuel:
        bits = own_bits(tags)
        cy, cx = int(p[0] * 100), int(p[1] * 100)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for q, bit in grid.get((cy + dy, cx + dx), []):
                    if distance(p, q) <= RADIUS_M:
                        bits |= bit
        # Motorway service areas practically always have toilets and food.
        if any(inside(p, poly) for poly in services):
            bits |= BITS["restroom"] | BITS["food"]
        stations[osm_id] = (round(p[0], 5), round(p[1], 5), bits)
    time.sleep(1)


def chunks(box):
    s, w, n, e = box
    lat = s
    while lat < n:
        lon = w
        while lon < e:
            yield (lat, lon, min(lat + CHUNK_DEG, n), min(lon + CHUNK_DEG, e))
            lon += CHUNK_DEG
        lat += CHUNK_DEG


def build(country):
    started = time.time()
    stations = {}
    boxes = [c for box in COUNTRIES[country] for c in chunks(box)]
    failed = []
    for i, box in enumerate(boxes, 1):
        print(f"  {country} {i}/{len(boxes)} {box}", flush=True)
        fetch_chunk(box, stations, failed)
    # Overpass often refuses when busy; retry the refused boxes after growing pauses.
    for attempt in range(1, 4):
        if not failed:
            break
        print(f"  {country}: {len(failed)} Gebiete abgelehnt, Wiederholung {attempt} nach Pause", flush=True)
        time.sleep(120 * attempt)
        retry, failed = failed, []
        for box in retry:
            fetch_chunk(box, stations, failed)
    if failed:
        print(f"  {country}: {len(failed)} Gebiete fehlen: {failed}", file=sys.stderr, flush=True)
    rows = sorted(v for v in stations.values() if v[2])
    os.makedirs(OUT, exist_ok=True)
    doc = {
        "version": 1,
        "country": country,
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "radius_m": RADIUS_M,
        "bits": BITS,
        "attribution": "© OpenStreetMap contributors, ODbL",
        "stations": rows,
    }
    path = os.path.join(OUT, f"{country}.json")
    with open(path, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    print(f"== {country}: {len(stations)} Tankstellen, {len(rows)} mit Services, "
          f"{os.path.getsize(path) // 1024} KB, {int(time.time() - started)} s", flush=True)
    return {"stations": len(rows), "generated": doc["generated"], "missing_boxes": len(failed)}


def main():
    wanted = sys.argv[1:] or list(COUNTRIES)
    index_path = os.path.join(OUT, "index.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {"version": 1, "countries": {}}
    for country in wanted:
        index["countries"][country] = build(country)
    with open(index_path, "w") as f:
        json.dump(index, f, indent=1)


if __name__ == "__main__":
    main()
