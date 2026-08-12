#!/usr/bin/env python3
"""Fetch live Pompano Beach conditions and write conditions.json.

Runs in GitHub Actions. The Claude cloud routines cannot reach any of these
sources directly (the sandbox egress proxy allows essentially only GitHub), so
this script acts as a relay: it fetches everything here and commits the result
to the repo, where the routines can read it via raw.githubusercontent.com.

Every section is independently guarded — one dead source must not produce an
empty file, because a stale-but-partial file is still useful and a missing file
is not.
"""

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "pompano-beach-relay (github actions; contact juggyghei@proton.me)"
ET = timezone(timedelta(hours=-4))  # EDT; Florida is on DST for the whole beach season

FLAG_API = "https://fortlauderdalebeachwatch.com/api/conditions?beach=pompano"
ALERTS = "https://api.weather.gov/alerts/active?point=26.24,-80.11"
HOURLY = "https://api.weather.gov/gridpoints/MFL/111,71/forecast/hourly"
SRF = "https://api.weather.gov/products/types/SRF/locations/MFL"
WATER = "https://www.floridahealthybeaches.com/county/broward"

errors = []


def get(url, as_json=True, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if as_json else raw


def section(name, fn):
    """Run a fetcher, recording the failure instead of raising."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - we want every failure mode recorded
        errors.append(f"{name}: {type(e).__name__}: {e}")
        return None


def flag():
    d = get(FLAG_API)
    colors = [c.lower() for c in (d.get("flags") or [])]
    # Red or double red closes the water; everything else is swimmable.
    blocking = [c for c in colors if "red" in c]
    return {
        "colors": colors,
        "flag_says": "NO GO" if blocking else ("GO" if colors else "UNKNOWN"),
        "double_red": any("double" in c for c in colors),
        "sea_pests": d.get("seaPests"),
        "water_temp_f": d.get("waterTemp"),
        "air_temp_f": d.get("airTemp"),
        "unavailable": d.get("flagsUnavailable"),
        "stale": d.get("isStale"),
        "demo_data": d.get("isDemo"),
        "last_updated_utc": d.get("lastUpdated"),
    }


def alerts():
    d = get(ALERTS)
    out = []
    for f in d.get("features", []):
        p = f.get("properties", {})
        out.append(
            {
                "event": p.get("event"),
                "severity": p.get("severity"),
                "onset": p.get("onset"),
                "ends": p.get("ends") or p.get("expires"),
                "headline": (p.get("headline") or "").strip(),
            }
        )
    return out


def swim_window():
    """Hour-by-hour 1-6 PM ET today - the window that actually matters."""
    d = get(HOURLY)
    periods = d.get("properties", {}).get("periods", [])
    today = datetime.now(ET).date()
    hours = []
    for p in periods:
        start = datetime.fromisoformat(p["startTime"]).astimezone(ET)
        if start.date() != today or not (13 <= start.hour <= 18):
            continue
        hours.append(
            {
                "hour_et": start.strftime("%-I %p"),
                "temp_f": p.get("temperature"),
                "precip_pct": (p.get("probabilityOfPrecipitation") or {}).get("value"),
                "forecast": p.get("shortForecast"),
                "wind": p.get("windSpeed"),
            }
        )
    probs = [h["precip_pct"] for h in hours if h["precip_pct"] is not None]
    texts = " ".join((h["forecast"] or "").lower() for h in hours)
    return {
        "hours": hours,
        "max_precip_pct": max(probs) if probs else None,
        "thunder_in_window": "thunder" in texts or "t-storm" in texts,
    }


def rip():
    """Today's Coastal Broward block of the Surf Zone Forecast.

    Field layout is `Label*...........Value.` inside a zone block that starts
    with "Coastal Broward-" and runs to the next "$$" separator. We want the
    first ".TODAY..." section only; later sections are future days.
    """
    idx = get(SRF)
    graph = idx.get("@graph") or []
    if not graph:
        raise RuntimeError("no SRF products listed")
    latest = graph[0]
    text = get(latest["@id"]).get("productText", "")

    m = re.search(r"Coastal Broward-.*?(?=\$\$)", text, re.S)
    if not m:
        raise RuntimeError("Coastal Broward block not found")
    block = m.group(0)

    today = re.search(r"\.TODAY\.\.\..*?(?=\n\.[A-Z]|\Z)", block, re.S)
    today_block = today.group(0) if today else block

    def field(label):
        # Labels carry a variable number of trailing '*' footnote markers.
        f = re.search(rf"{label}\**\.+\s*(.+?)(?:\.\s*\n|\n)", today_block)
        return f.group(1).strip().rstrip(".") if f else None

    return {
        "broward_risk": field("Rip Current Risk"),
        "surf_height": field("Surf Height"),
        "thunderstorm_potential": field("Thunderstorm Potential"),
        "waterspout_risk": field("Waterspout Risk"),
        "max_heat_index": field("Max Heat Index"),
        "issued": latest.get("issuanceTime"),
        "note": "Rip risk is informational only, never a veto - the live flag governs. "
        "Thunderstorm and waterspout fields DO matter: those are hard no-goes.",
    }


def water_quality():
    """Broward sampling results from the Healthy Beaches page payload.

    Pompano Beach itself is NOT one of the sampled sites in this program. The
    nearest sampled sites are COMMERCIAL BLVD PIER (immediately south) and
    DEERFIELD BEACH PIER (north), so those are the usable proxies. Sampling is
    weekly, so this is never a same-day reading.
    """
    html = get(WATER, as_json=False)
    i = html.find('"beaches":[')
    if i < 0:
        raise RuntimeError("beaches payload not found")
    seg = html[i + len('"beaches":'):]
    depth = 0
    end = None
    for j, c in enumerate(seg):
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end is None:
        raise RuntimeError("beaches payload truncated")
    beaches = json.loads(seg[:end])

    sites, advisories, nearest = [], [], {}
    for b in beaches:
        latest = (b.get("data") or [{}])[0]
        row = {
            "site": b.get("name"),
            "sampled": latest.get("sampleDate"),
            "enterococcus": latest.get("enterococcusValue"),
            "status": latest.get("enterococcusStatus"),
            "advisory": latest.get("advisoryStatus"),
        }
        sites.append(row)
        if str(row["advisory"]).lower() == "yes":
            advisories.append(row)
        if row["site"] in ("COMMERCIAL BLVD PIER", "DEERFIELD BEACH PIER"):
            nearest[row["site"]] = row

    return {
        "any_broward_advisory": bool(advisories),
        "advisories": advisories,
        "nearest_sites_to_pompano": nearest,
        "all_sites": sites,
        "note": "Pompano Beach is not itself a sampled site. Commercial Blvd Pier (south) "
        "and Deerfield Beach Pier (north) are the proxies. Sampling is weekly, not live. "
        "Only treat this as a veto if an advisory is actually posted.",
    }


def main():
    now = datetime.now(timezone.utc)
    data = {
        "generated_utc": now.isoformat(timespec="seconds"),
        "generated_et": now.astimezone(ET).strftime("%Y-%m-%d %-I:%M %p ET"),
        "beach": "Pompano Beach, FL (Broward County)",
        "flag": section("flag", flag),
        "alerts": section("alerts", alerts),
        "swim_window_1_to_6pm": section("swim_window", swim_window),
        "rip_current": section("rip", rip),
        "water_quality": section("water_quality", water_quality),
        "errors": errors,
        "sources": {
            "flag": FLAG_API,
            "alerts": ALERTS,
            "hourly": HOURLY,
            "surf_zone_forecast": SRF,
            "water_quality": WATER,
        },
    }

    with open("conditions.json", "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print(json.dumps(data, indent=2))

    # The flag is the whole point of the relay. If it is missing, fail loudly so
    # the Actions run goes red and the staleness is visible.
    if data["flag"] is None:
        print("\nFATAL: flag fetch failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
