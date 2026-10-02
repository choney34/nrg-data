# NRG data

Data files for the [NRG](https://nrg-app.com) fuel price app, published on GitHub Pages.

## Service directory

Weekly generated list of fuel stations with the services at or within 150 m of them, for the
[NRG](https://nrg-app.com) fuel price app: food, toilets, shop, EV charging, car wash, air
(and ATM). Built from OpenStreetMap via the Overpass API; stations inside motorway service
areas (`highway=services`) count as having toilets and food.

Published at `https://choney34.github.io/nrg-data/services/<country>.json`:

```json
{"version": 1, "country": "de", "generated": "…", "radius_m": 150,
 "bits": {"food": 1, "restroom": 2, "atm": 4, "shopping": 8, "evCharger": 16, "carWash": 32, "air": 64},
 "stations": [[52.43121, 13.10224, 35], …]}
```

Each station is `[lat, lon, bits]`; only stations with at least one service are listed.
`services/index.json` lists the countries with their generation time and size.

## Data licence

Data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), available under
the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/). The files
published here are a derived database and are likewise available under the ODbL.

## Building

```sh
python3 build_services.py de lu    # writes site/services/de.json, lu.json
```

The GitHub workflow runs daily and rebuilds one country in turn (each country every nine days);
started by hand it takes a list of countries or `all`. It publishes to GitHub Pages; countries
not rebuilt, or whose build fails, keep their previously published file.

## Charging prices

`charging/<region>.json`, rebuilt every hour from the official AFIR national access points
(`build_charging.py`). Per charging location: position, operator, address, connectors
`[[code, kW, count]]`, and the ad-hoc price incl. VAT (€/kWh, session fee, price per hour).

| Region | Source | Terms |
|---|---|---|
| Netherlands | DOT-NL by NDW, opendata.ndw.nu | Open data, free for reuse by third parties |
| Finland | Fintraffic, afir.digitraffic.fi | CC BY 4.0 – Source: Fintraffic / digitraffic.fi |
| Germany | Ladesäulenregister der Bundesnetzagentur (locations only, weekly) | CC BY 4.0 – Source: Bundesnetzagentur.de |
| Spain | DGT national access point, nap.dgt.es (locations only) | CC BY – Source: DGT |
| Poland | EIPA by UDT, eipa.udt.gov.pl (prices in PLN) | Free for commercial and non-commercial use; reader key in the `EIPA_TOKEN` secret |
