#!/usr/bin/env python3
"""Assembles the Pages site: everything published so far, with the freshly built files from this
run laid over it, plus the index files.

    python3 publish.py <artifact dir> <site dir> <published base URL>

The artifact dir mirrors the site layout (services/<country>.json, charging/<region>.json).
"""
import glob
import json
import os
import shutil
import sys
import urllib.request

from build_services import COUNTRIES

src, site, base = sys.argv[1], sys.argv[2], sys.argv[3].rstrip("/")
os.makedirs(site, exist_ok=True)


def fetch(path):
    try:
        with urllib.request.urlopen(f"{base}/{path}", timeout=120) as resp:
            return resp.read()
    except Exception:
        return None


# 1. What was published before (files.json lists it; the first runs had no such list).
listing = fetch("files.json")
previous = json.loads(listing) if listing else [f"services/{c}.json" for c in COUNTRIES] + ["charging/nl.json", "charging/fi.json", "charging/pl.json", "charging/de.json", "charging/es.json"]
for path in previous:
    if os.path.exists(os.path.join(src, path)):
        continue
    data = fetch(path)
    if data:
        os.makedirs(os.path.dirname(os.path.join(site, path)), exist_ok=True)
        open(os.path.join(site, path), "wb").write(data)
        print(f"{path}: vorherige Version")

# 2. This run's files.
for file in glob.glob(os.path.join(src, "**", "*.json"), recursive=True):
    path = os.path.relpath(file, src)
    os.makedirs(os.path.dirname(os.path.join(site, path)), exist_ok=True)
    shutil.copy(file, os.path.join(site, path))
    print(f"{path}: neu")

# 3. Indexes.
files = sorted(os.path.relpath(f, site) for f in glob.glob(os.path.join(site, "*", "*.json"))
               if not f.endswith("index.json"))
json.dump(files, open(os.path.join(site, "files.json"), "w"), indent=1)
for folder, attribution in (("services", "© OpenStreetMap contributors, ODbL"),
                            ("charging", "Official AFIR national access points, see each file's source")):
    entries = {}
    for file in sorted(glob.glob(os.path.join(site, folder, "*.json"))):
        if file.endswith("index.json"):
            continue
        doc = json.load(open(file))
        key = doc.get("country") or doc.get("region")
        entries[key] = {"generated": doc["generated"], "bytes": os.path.getsize(file),
                        "entries": len(doc.get("stations") or doc.get("locations") or [])}
    if entries:
        json.dump({"version": 1, "attribution": attribution, "files": entries},
                  open(os.path.join(site, folder, "index.json"), "w"), indent=1)
open(os.path.join(site, "index.html"), "w").write(
    '<!doctype html><meta charset="utf-8"><title>NRG data</title>'
    '<p>Data for the NRG app. Services: © <a href="https://www.openstreetmap.org/copyright">'
    'OpenStreetMap contributors</a>, ODbL (<a href="services/index.json">index</a>). '
    'Charging: official AFIR national access points: DOT-NL by NDW; Fintraffic / digitraffic.fi, CC BY 4.0; EIPA by UDT; Ladesäulenregister der Bundesnetzagentur, CC BY 4.0 '
    '(<a href="charging/index.json">index</a>).</p>\n')
