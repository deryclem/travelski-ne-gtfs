#!/usr/bin/env python3
"""
Travelski Night Express GTFS generator.

Fetches the full season catalogue from Travelski's booking API, rebuilds
each night train from the origin/destination pairs it sells, and writes
a GTFS zip.

Usage:
    python3 generate.py
"""

import zipfile
import requests  # pip install requests
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": "travelski-ne-gtfs/1.0 (https://github.com/deryclem/travelski-ne-gtfs)",
})
HTTP.mount("https://", HTTPAdapter(max_retries=_retry))


# ── Settings ──────────────────────────────────────────────────────────────────

OUTPUT_ZIP = Path("gtfs-travelski-night-express.zip")

# The endpoint behind the train search widget on travelski.com
# (cms.travelfactory.fr/component/standalone-component/tf-train-results.js).
# full_list=true returns every bookable combination for the season, one-way
# and round trip, which together cover every leg of every train.
TNE_API = "https://api.travelski.com/flight-connector/travelski-night-express"
PAGE_SIZE = 200  # the API caps page_size at 200

AGENCY_TIMEZONE = "Europe/Paris"

# Railway undertaking running the trains on Travelski's behalf ("PEGASUS" in the API).
PEGASUS_URL = "https://pegasus-trains.com"

# Customer service for the train, as given in Travelski's conditions of sale
# for rail tickets (travelski.com/instit/cgv-transport-sec).
CUSTOMER_PHONE = "+33 4 79 96 30 69"
CUSTOMER_EMAIL = "support.clients@travelski.com"
BOOKING_URL = "https://www.travelski.com/transport/travelski-night-express"

# Travelski Night Express blue (timetable accents on travelski.com).
ROUTE_COLOR = "0042B8"
ROUTE_TEXT_COLOR = "FFFFFF"

# ── Stations ──────────────────────────────────────────────────────────────────
#
# The API only returns Travelski's own station codes. Names, UIC codes and
# coordinates below come from Wikidata (item in the comment). Belgian stations
# use SNCB's French name, with SNCB's Dutch name as a translation. The network is
# small and stable, so a fixed table is simpler than a lookup at every run;
# an unknown code makes the run fail rather than write an orphan stop_id.
STATIONS = {
    "PAZ": {"name": "Paris Gare d'Austerlitz",               "uic": "8754700", "lat": 48.842222, "lon": 2.365833, "timezone": "Europe/Paris"},      # Q734017
    "AMS": {"name": "Amsterdam Centraal",                    "uic": "8400058", "lat": 52.378950, "lon": 4.900160, "timezone": "Europe/Amsterdam"},  # Q50719
    "RTM": {"name": "Rotterdam Centraal",                    "uic": "8400530", "lat": 51.925000, "lon": 4.469444, "timezone": "Europe/Amsterdam"},  # Q801388
    "ANR": {"name": "Anvers-Central",                        "uic": "8821006", "lat": 51.216944, "lon": 4.421111, "timezone": "Europe/Brussels",    # Q800398
            "translations": {"nl": "Antwerpen-Centraal", "en": "Anvers-Central / Antwerpen-Centraal"}},
    "BRU": {"name": "Bruxelles-Midi",                        "uic": "8814001", "lat": 50.835278, "lon": 4.335833, "timezone": "Europe/Brussels",    # Q800587
            "translations": {"nl": "Brussel-Zuid", "en": "Bruxelles-Midi / Brussel-Zuid"}},
    "XMO": {"name": "Moûtiers-Salins-Brides-les-Bains",      "uic": "8774172", "lat": 45.486389, "lon": 6.531389, "timezone": "Europe/Paris"},      # Q3097131
    "XAP": {"name": "Aime-La Plagne",                        "uic": "8774176", "lat": 45.554444, "lon": 6.648611, "timezone": "Europe/Paris"},      # Q2653180
    "XBM": {"name": "Bourg-Saint-Maurice",                   "uic": "8774179", "lat": 45.618056, "lon": 6.771667, "timezone": "Europe/Paris"},      # Q2003411
}

# Ski-side stations in the Tarentaise valley. Trains heading there are
# direction 0 (outbound, Friday night), trains leaving them direction 1.
TARENTAISE = {"XMO", "XAP", "XBM"}


# ── Fetching data from the Travelski API ──────────────────────────────────────

def fetch_catalogue() -> list[dict]:
    """Page through the full season catalogue and return every row."""
    rows = []
    page = 1
    while True:
        response = HTTP.get(
            TNE_API,
            params={"format": "trips", "leg": "outbound", "full_list": "true",
                    "page": page, "page_size": PAGE_SIZE},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(f"Travelski API returned an error on page {page}: {payload}")

        rows.extend(payload["data"])
        total_pages = payload["pagination"]["totalPages"]
        print(".", end="", flush=True)
        if page >= total_pages:
            break
        page += 1

    season = payload.get("filters", {}).get("season")
    print(f" ✓  {len(rows)} catalogue rows (season {season})\n")
    return rows


def extract_legs(rows: list[dict]) -> set[tuple]:
    """
    Pull every distinct train leg out of the catalogue rows.

    Each row is a one-way or round-trip offer. Its "outbound" and "inbound"
    blocks each describe one origin → destination leg, e.g.
        {"departureDate": "2026-12-18", "departureTime": "16:16", "departureLocation": "AMS",
         "arrivalDate": "2026-12-19", "arrivalTime": "08:56", "arrivalLocation": "XAP", ...}
    The same leg shows up in many rows (one per class and per return date).
    """
    legs = set()
    for row in rows:
        for side in ("outbound", "inbound"):
            leg = row.get(side)
            if not leg:
                continue
            legs.add((
                leg["departureLocation"], leg["departureDate"][:10], leg["departureTime"],
                leg["arrivalLocation"],   leg["arrivalDate"][:10],   leg["arrivalTime"],
            ))
    return legs


# ── Rebuilding trains from origin/destination legs ─────────────────────────────

def to_datetime(day: str, hhmm: str, station: str) -> datetime:
    """Local clock time at a station, as a timezone-aware datetime."""
    return datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=ZoneInfo(STATIONS[station]["timezone"]))


def group_into_trains(legs: set[tuple]) -> list[dict]:
    """
    Merge origin/destination legs into whole trains.

    The API only sells hub → Tarentaise (and back) pairs, never the full
    stop list. Two legs belong to the same train when they share a stop at
    the same moment: AMS→XMO and AMS→XAP share "AMS departs 18 Dec 16:16",
    AMS→XMO and RTM→XMO share "XMO arrival 19 Dec 08:37", and so on. A
    small union-find over those (station, time, event) nodes does the rest,
    without having to know in advance which stations each train serves.
    """
    parent = {}

    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a, b):
        parent[find(a)] = find(b)

    for dep_station, dep_day, dep_time, arr_station, arr_day, arr_time in legs:
        for station in (dep_station, arr_station):
            if station not in STATIONS:
                raise RuntimeError(f"Unknown Travelski station code {station!r}: add it to STATIONS.")
        departure = (dep_station, to_datetime(dep_day, dep_time, dep_station), "dep")
        arrival   = (arr_station, to_datetime(arr_day, arr_time, arr_station), "arr")
        union(departure, arrival)

    trains = {}
    for node in list(parent):
        trains.setdefault(find(node), []).append(node)

    result = []
    for nodes in trains.values():
        # A station is either boarded (dep) or alighted (arr) on a given
        # train, never both, since the API doesn't sell hub→hub tickets.
        stops = sorted(nodes, key=lambda n: n[1])
        stations = [station for station, _, _ in stops]
        if len(stations) != len(set(stations)):
            raise RuntimeError(f"Station served twice in one train, cannot build stop sequence: {stops}")
        result.append({
            "stops": [{"station": station, "time": moment, "event": event} for station, moment, event in stops],
        })
    return result


def service_day_origin(service_date: date) -> datetime:
    """GTFS 'noon minus 12h' reference for a service day, in the agency timezone."""
    noon = datetime(service_date.year, service_date.month, service_date.day, 12, tzinfo=ZoneInfo(AGENCY_TIMEZONE))
    return noon - timedelta(hours=12)


def to_gtfs_time(moment: datetime, origin: datetime) -> str:
    """
    Elapsed time since the service day origin, formatted as GTFS HH:MM:SS.

    Counting real elapsed time (rather than reading the wall clock) is what
    the GTFS spec asks for, and it keeps the last weekend of March correct:
    a Sunday 14:28 arrival after the switch to summer time is written as
    37:28:00, the same as a regular 13:28 winter-time arrival.
    """
    # Subtract in UTC: Python ignores DST when both datetimes share a tzinfo.
    utc = ZoneInfo("UTC")
    seconds = int((moment.astimezone(utc) - origin.astimezone(utc)).total_seconds())
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def build_variants(trains: list[dict]) -> list[dict]:
    """
    Turn each train into GTFS stop times, then group trains running the
    exact same timetable on different dates into one trip variant.
    """
    by_pattern = {}
    for train in trains:
        stops = train["stops"]
        first_departure = stops[0]["time"].astimezone(ZoneInfo(AGENCY_TIMEZONE))
        service_date = first_departure.date()
        origin = service_day_origin(service_date)

        stop_times = tuple(
            (
                stop["station"],
                to_gtfs_time(stop["time"], origin),
                0 if stop["event"] == "dep" else 1,  # pickup_type: 1 = no pickup
                0 if stop["event"] == "arr" else 1,  # drop_off_type: 1 = no drop-off
            )
            for stop in stops
        )
        entry = by_pattern.setdefault(stop_times, {"stop_times": stop_times, "dates": []})
        entry["dates"].append(service_date)

    variants = []
    counters = {}
    for pattern in sorted(by_pattern.values(), key=lambda p: (min(p["dates"]), p["stop_times"][0][1])):
        stations = [stop[0] for stop in pattern["stop_times"]]
        outbound = stations[-1] in TARENTAISE
        hub = stations[0] if outbound else stations[-1]  # the far end of the line, e.g. PAZ or AMS
        route_id = f"TNE-{hub}"

        counters[route_id, outbound] = counters.get((route_id, outbound), 0) + 1
        variant_id = f"{route_id}-{'OUT' if outbound else 'RET'}-v{counters[route_id, outbound]}"
        dates = sorted(pattern["dates"])

        variants.append({
            "id":          variant_id,
            "route_id":    route_id,
            "hub":         hub,
            "direction":   0 if outbound else 1,
            "stop_times":  pattern["stop_times"],
            "dates":       dates,
        })

        path = " → ".join(f"{s[0]} {s[1][:5]}" for s in pattern["stop_times"])
        print(f"  {variant_id:18}  {len(dates):2} day(s)  {dates[0]} → {dates[-1]}")
        print(f"  {'':18}  {path}")

    return variants


# ── Building GTFS files ────────────────────────────────────────────────────────

def make_csv(headers: list[str], rows: list[list]) -> str:
    """Build a CSV string with quoted fields."""
    def quote(value):
        if value is None:
            return '""'
        return '"' + str(value).replace('"', '""') + '"'

    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(quote(cell) for cell in row))

    return "\n".join(lines) + "\n"


def build_agency_file() -> str:
    return make_csv(
        ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang", "agency_phone",
         "agency_fare_url", "agency_email"],
        [["TNE", "Travelski Night Express", "https://www.travelski.com/travelski-night-express",
          AGENCY_TIMEZONE, "fr", CUSTOMER_PHONE, BOOKING_URL, CUSTOMER_EMAIL]],
    )


def build_stops_file(variants: list[dict]) -> str:
    used = {stop[0] for v in variants for stop in v["stop_times"]}
    rows = [
        [code, s["name"], s["lat"], s["lon"], s["uic"], s["timezone"]]
        for code, s in STATIONS.items() if code in used
    ]
    return make_csv(["stop_id", "stop_name", "stop_lat", "stop_lon", "stop_code", "stop_timezone"], rows)


def build_routes_file(variants: list[dict]) -> str:
    seen = set()
    rows = []
    for v in variants:
        if v["route_id"] in seen:
            continue
        seen.add(v["route_id"])
        # Name the line after its two ends, hub first.
        terminus = v["stop_times"][-1][0] if v["direction"] == 0 else v["stop_times"][0][0]
        rows.append([
            v["route_id"], "TNE", "TNE",
            f"{STATIONS[v['hub']]['name']} ↔ {STATIONS[terminus]['name']}",
            2,  # route_type 2 = rail
            ROUTE_COLOR, ROUTE_TEXT_COLOR,
        ])
    return make_csv(
        ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type",
         "route_color", "route_text_color"],
        rows,
    )


def build_trips_file(variants: list[dict]) -> str:
    rows = [
        [
            v["route_id"], v["id"], v["id"], STATIONS[v["stop_times"][-1][0]]["name"], v["direction"],
            2,  # wheelchair_accessible: 2 = no (Travelski: steps, corridors and cabins too narrow for a wheelchair)
        ]
        for v in variants
    ]
    return make_csv(
        ["route_id", "service_id", "trip_id", "trip_headsign", "direction_id", "wheelchair_accessible"],
        rows,
    )


def build_calendar_dates_file(variants: list[dict]) -> str:
    rows = [[v["id"], d.strftime("%Y%m%d"), 1] for v in variants for d in v["dates"]]
    return make_csv(["service_id", "date", "exception_type"], rows)


def build_stop_times_file(variants: list[dict]) -> str:
    rows = []
    for v in variants:
        for sequence, (station, time, pickup, drop_off) in enumerate(v["stop_times"], start=1):
            # The API only gives the time passengers board or alight, so
            # arrival and departure are the same moment at every stop.
            booking_rule = BOOKING_RULE_ID if pickup == 0 else None
            rows.append([v["id"], time, time, station, sequence, pickup, drop_off, 1, booking_rule])
    return make_csv(
        ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence",
         "pickup_type", "drop_off_type", "timepoint", "pickup_booking_rule_id"],
        rows,
    )


BOOKING_RULE_ID = "TNE_RESA"
BOOKING_MESSAGE = {
    "fr": "Réservation obligatoire auprès de Travelski, en ligne ou par téléphone.",
    "nl": "Reserveren verplicht bij Travelski, online of telefonisch.",
    "en": "Booking required with Travelski, online or by phone.",
}


def build_booking_rules_file() -> str:
    # Tickets are only sold through Travelski. Its conditions of sale give
    # no booking deadline, so the rule says "bookable up to departure".
    return make_csv(
        ["booking_rule_id", "booking_type", "message", "phone_number", "info_url", "booking_url"],
        [[BOOKING_RULE_ID, 0, BOOKING_MESSAGE["fr"], CUSTOMER_PHONE,
          "https://www.travelski.com/travelski-night-express", BOOKING_URL]],
    )


def build_translations_file(variants: list[dict]) -> str:
    used = {stop[0] for v in variants for stop in v["stop_times"]}
    rows = [
        ["stops", "stop_name", language, translation, code]
        for code, s in STATIONS.items() if code in used
        for language, translation in s.get("translations", {}).items()
    ]
    rows += [
        ["booking_rules", "message", language, message, BOOKING_RULE_ID]
        for language, message in BOOKING_MESSAGE.items() if language != "fr"
    ]
    return make_csv(["table_name", "field_name", "language", "translation", "record_id"], rows)


def build_feed_info_file(variants: list[dict]) -> str:
    all_dates = sorted(d for v in variants for d in v["dates"])
    # Return trips end the day after their service date.
    end = all_dates[-1] + timedelta(days=1)
    return make_csv(
        ["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_start_date", "feed_end_date",
         "feed_version", "feed_contact_url"],
        [["travelski-ne-gtfs", "https://github.com/deryclem/travelski-ne-gtfs", "fr",
          all_dates[0].strftime("%Y%m%d"), end.strftime("%Y%m%d"),
          date.today().strftime("%Y%m%d"), "https://github.com/deryclem/travelski-ne-gtfs/issues"]],
    )


def build_attributions_file() -> str:
    return make_csv(
        ["attribution_id", "organization_name", "is_producer", "is_operator", "is_authority", "attribution_url"],
        [
            ["1", "Pegasus Trains", "0", "1", "0", PEGASUS_URL],
            ["2", "Wikidata contributors", "1", "0", "0", "https://www.wikidata.org"],
        ],
    )


def build_gtfs(variants: list[dict]) -> dict[str, str]:
    return {
        "agency.txt":         build_agency_file(),
        "stops.txt":          build_stops_file(variants),
        "routes.txt":         build_routes_file(variants),
        "calendar_dates.txt": build_calendar_dates_file(variants),
        "trips.txt":          build_trips_file(variants),
        "stop_times.txt":     build_stop_times_file(variants),
        "booking_rules.txt":  build_booking_rules_file(),
        "translations.txt":   build_translations_file(variants),
        "feed_info.txt":      build_feed_info_file(variants),
        "attributions.txt":   build_attributions_file(),
    }


def write_zip(files: dict[str, str], path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for filename, content in files.items():
            z.writestr(filename, content)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Fetching Travelski Night Express catalogue…")
    rows = fetch_catalogue()
    legs = extract_legs(rows)
    if not legs:
        raise RuntimeError("No legs found. Possible API change or outage, not writing an empty feed.")

    trains = group_into_trains(legs)
    print(f"{len(legs)} legs → {len(trains)} trains\n")
    variants = build_variants(trains)

    print("\nBuilding GTFS…")
    files = build_gtfs(variants)

    for filename, content in files.items():
        record_count = content.count("\n") - 1
        print(f"  {filename:22}  {record_count} records")

    write_zip(files, OUTPUT_ZIP)
    print(f"\n✓ Written to {OUTPUT_ZIP}")


if __name__ == "__main__":
    main()
