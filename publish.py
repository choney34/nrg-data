#!/usr/bin/env python3
"""Assembles the Pages site: the freshly built country files, and for countries whose build
failed the previously published file, plus services/index.json.

    python3 publish.py <artifact dir> <site dir> <published base URL>
"""
import json
import os
import shutil
import sys
import urllib.request

from build_services import COUNTRIES

src, site, base = sys.argv[1], sys.argv[2], sys.argv[3].rstrip("/")
out = os.path.join(site, "services")
os.makedirs(out, exist_ok=True)
index = {"version": 1, "attribution": "© OpenStreetMap contributors, ODbL", "countries": {}}
for country in COUNTRIES:
    target = os.path.join(out, f"{country}.json")
    fresh = os.path.join(src, f"{country}.json")
    if os.path.exists(fresh):
        shutil.copy(fresh, target)
        status = "neu"
    else:
        try:
            with urllib.request.urlopen(f"{base}/services/{country}.json", timeout=60) as resp:
                open(target, "wb").write(resp.read())
            status = "vorherige Version"
        except Exception as error:
            print(f"{country}: weder neu noch veröffentlicht ({error})")
            continue
    doc = json.load(open(target))
    index["countries"][country] = {"generated": doc["generated"], "stations": len(doc["stations"]),
                                   "bytes": os.path.getsize(target)}
    print(f"{country}: {status}, {len(doc['stations'])} Tankstellen")
json.dump(index, open(os.path.join(out, "index.json"), "w"), indent=1)
open(os.path.join(site, "index.html"), "w").write(
    '<!doctype html><meta charset="utf-8"><title>NRG service directory</title>'
    '<p>Service directory for the NRG app. Data © <a href="https://www.openstreetmap.org/copyright">'
    'OpenStreetMap contributors</a>, available under the Open Database License (ODbL). '
    'See <a href="services/index.json">services/index.json</a>.</p>\n')
