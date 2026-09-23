# gtfs-travelski-night-express

A [GTFS](https://gtfs.org) feed for the Travelski Night Express, the winter night trains that take skiers from Paris and the Benelux to the Tarentaise valley in the French Alps.

## Why this exists

Travelski sells the Night Express as part of ski packages and publishes its timetable as images on a landing page. There's no official GTFS feed, so this rebuilds one from the booking API behind their search widget.

This feed also feeds into [Panto](https://getpanto.app), a real-time train tracking app currently in beta.

## Download

**[Download latest GTFS package (`gtfs-travelski-night-express.zip`)](./gtfs-travelski-night-express.zip)**

See `feed_info.txt` in the zip for the date range and `feed_version` (generation date) it currently covers.

## Trains

Two lines, each running once a week in each direction during the ski season: out on Friday night, back on Saturday night.

- **Paris**: Paris Gare d'Austerlitz → Moûtiers → Aime-La Plagne → Bourg-Saint-Maurice
- **Benelux**: Amsterdam Centraal → Rotterdam Centraal → Anvers-Central (Antwerpen-Centraal) → Bruxelles-Midi → Moûtiers → Aime-La Plagne → Bourg-Saint-Maurice

The trains are run by [Pegasus Trains](https://pegasus-trains.com) for Travelski.

The timetable isn't the same every week: departure and arrival times shift on some dates (holidays, engineering works). Each distinct timetable becomes its own trip, tied to the dates it runs on through `calendar_dates.txt`.

## Where the data comes from

| Source | What it provides |
|--------|-------------------|
| `api.travelski.com/flight-connector/travelski-night-express` | The endpoint the train search widget on travelski.com calls (found in `cms.travelfactory.fr/component/standalone-component/tf-train-results.js`). With `full_list=true` it pages through every bookable offer of the season, one-way and round trip, with departure and arrival times for each origin/destination pair. |
| [Wikidata](https://www.wikidata.org) | Coordinates and UIC codes for the eight stations, stored in `generate.py` (the Wikidata item is noted next to each). |

The API never gives a train's full stop list, only the pairs it sells (Amsterdam → Aime, Rotterdam → Moûtiers, …). `generate.py` puts each train back together by linking legs that share a stop at the same moment: Amsterdam → Moûtiers and Rotterdam → Moûtiers arrive at Moûtiers at the same minute, so they're the same train. So there's no hardcoded list of lines or stops. A new origin station shows up by itself, as long as its code is added to the station table (the run fails loudly if it isn't).

## Generating

Requires Python 3.10+.

```bash
pip install requests
python3 generate.py
```

About two dozen HTTP requests, a few seconds end to end. Runs automatically every Monday via GitHub Actions.

## Limitations

- **Boarding and alighting only.** Travelski sells tickets from the hubs to the Alps and back, never between two hubs or two Alpine stations. So hub stops are pickup-only and Alpine stops are drop-off-only, and each stop has a single time (the one printed on the ticket), with no separate arrival and departure.
- **No Albertville.** The Paris train also calls at Albertville (shown on Travelski's timetable image), but it isn't sold and the API has no time for it, so it's left out.
- **Sold-out legs might drop out.** Each offer carries a `stock` count, and it's not known yet whether sold-out offers stay listed. If they don't and every combination involving a given stop on a given date sold out, that stop would disappear from that date's trip. The trip itself stays, since other pairs still describe it.
- No fares: prices are dynamic and mostly bundled with accommodation.
- No shapes: the only stops known are hours apart (Bruxelles → Moûtiers is one hop), and the actual routing isn't published, so map-matching would only guess the path.

## License

Feed: [CC0](https://creativecommons.org/publicdomain/zero/1.0/). Station data: © Wikidata contributors, CC0.

Not affiliated with Travelski, Travel Factory, Compagnie des Alpes or Pegasus Trains.
