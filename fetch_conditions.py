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


def decide(d):
    """Turn the collected data into GO / NO GO plus a plain-English paragraph.

    Deliberately permissive. The user's standing complaint is over-cautious
    advice that cost them a week of swimmable beach days, so only genuinely
    dangerous conditions veto. In particular a "chance of thunderstorms" is the
    default South Florida afternoon and is NOT a veto on its own.
    """
    f = d.get("flag") or {}
    w = d.get("swim_window_1_to_6pm") or {}
    r = d.get("rip_current") or {}
    q = d.get("water_quality") or {}
    al = d.get("alerts") or []

    stoppers = []

    colors = f.get("colors") or []
    if f.get("double_red"):
        stoppers.append("double red flag - the water is closed")
    elif any("red" in c for c in colors):
        stoppers.append("red flag - the water is closed")

    for a in al:
        ev = (a.get("event") or "").lower()
        if any(k in ev for k in ("thunderstorm", "severe", "tornado", "waterspout", "hurricane", "tropical")):
            if "watch" not in ev:
                stoppers.append(f"active {a.get('event')}")

    spout = (r.get("waterspout_risk") or "").strip().lower()
    if spout and spout not in ("none", "low"):
        stoppers.append(f"waterspout risk {r.get('waterspout_risk')}")

    tstorm = (r.get("thunderstorm_potential") or "").strip().lower()
    if tstorm == "high":
        stoppers.append("high thunderstorm potential")

    mx = w.get("max_precip_pct")
    likely = any("likely" in (h.get("forecast") or "").lower() for h in (w.get("hours") or []))
    if mx is not None and mx >= 60 and likely:
        stoppers.append(f"storms likely, up to {mx}% in the swim window")

    if q.get("any_broward_advisory"):
        named = ", ".join(a["site"] for a in (q.get("advisories") or []))
        stoppers.append(f"water quality advisory ({named})")

    call = "NO GO" if stoppers else "GO"

    # Human-readable paragraph, same shape the routines are told to write.
    bits = []
    if colors:
        seen = f.get("last_updated_utc")
        when = ""
        if seen:
            try:
                when = " as of " + datetime.fromisoformat(
                    seen.replace("Z", "+00:00")
                ).astimezone(ET).strftime("%-I:%M %p ET")
            except ValueError:
                pass
        bits.append(f"The flag is {'/'.join(colors)}{when}.")
    if stoppers:
        bits.append("Calling it off because: " + "; ".join(stoppers) + ".")
    hrs = w.get("hours") or []
    if hrs:
        peak = max(hrs, key=lambda h: h.get("precip_pct") or 0)
        if (peak.get("precip_pct") or 0) >= 20:
            bits.append(
                f"Rain chance peaks around {peak['precip_pct']}% near {peak['hour_et']}; "
                "get out of the water at the first thunder."
            )
        else:
            bits.append("The 1-6 PM window looks dry.")
    if r.get("max_heat_index"):
        bits.append(f"Heat index {r['max_heat_index'].lower()} - hydrate and take shade breaks.")
    if r.get("broward_risk"):
        bits.append(f"County rip current risk is rated {r['broward_risk']} (information only).")
    if f.get("sea_pests"):
        bits.append(f"Sea pests reported: {f['sea_pests']}.")
    if not q.get("any_broward_advisory"):
        bits.append("No water quality advisories nearby.")
    if f.get("stale") or f.get("unavailable"):
        bits.append("Note: the flag feed reported stale or unavailable data, so treat it as unconfirmed.")

    return {
        "call": call,
        "stoppers": stoppers,
        "summary": " ".join(bits),
        "explanation": explain(call, stoppers, f, w, r, q, al),
    }


def explain(call, stoppers, f, w, r, q, al):
    """A plain-English paragraph or two saying what today looks like and why.

    The `summary` field above is a clipped fact list, fine for a push
    notification. This is the version for reading: it says what the call is,
    what drove it, what to expect hour to hour, and what would change it.
    """
    colors = f.get("colors") or []
    color = "/".join(colors) if colors else None
    hours = w.get("hours") or []
    peak = max(hours, key=lambda h: h.get("precip_pct") or 0) if hours else None
    peak_pct = (peak or {}).get("precip_pct") or 0

    p1 = []

    if call == "NO GO":
        p1.append(f"Not today. {stoppers[0].capitalize()}" + ("." if not stoppers[0].endswith(".") else ""))
        if len(stoppers) > 1:
            p1.append("There's also " + ", and ".join(stoppers[1:]) + ".")
        p1.append(
            "This is the kind of thing that genuinely changes during the day, so it's "
            "worth a re-check before you write the afternoon off entirely."
        )
    else:
        if color in ("green", "yellow"):
            calm = "calm" if color == "green" else "a bit of surf or current, but open"
            p1.append(
                f"Good to go. The lifeguards have the {color} flag up, which means the water is "
                f"open and conditions are {calm}."
            )
        elif color == "purple":
            p1.append(
                "Good to go, with one caveat. The purple flag is up, which sounds alarming but "
                "only means marine pests - jellyfish or man o' war. The water itself is open. "
                "Have a look along the sand before you get in; if you see blue balloon-like "
                "things washed up, pick a different stretch of beach."
            )
        else:
            p1.append("Good to go - nothing dangerous is showing in any of the feeds.")

    # The storm story, which is what actually decides most South Florida afternoons.
    p2 = []
    if hours:
        if peak_pct < 20:
            p2.append("The afternoon looks genuinely clear, which is unusual for August - no real storm signal in the 1-6 PM window.")
        else:
            wet = [h for h in hours if (h.get("precip_pct") or 0) >= 30]
            if wet:
                span = f"{wet[0]['hour_et']} to {wet[-1]['hour_et']}" if len(wet) > 1 else wet[0]["hour_et"]
                p2.append(
                    f"Storms are the thing to watch. Rain chances climb through the afternoon, "
                    f"running highest from about {span} and peaking near {peak_pct}% around "
                    f"{peak['hour_et']}."
                )
            else:
                p2.append(f"There's a modest storm signal, peaking near {peak_pct}% around {peak['hour_et']}.")
            p2.append(
                "That is the normal South Florida summer pattern rather than a washout - it usually "
                "means a cell builds, rains hard somewhere nearby for twenty minutes, and moves on. "
                "The rule that matters is lightning: flags say nothing about it, so get out of the "
                "water at the first rumble and wait half an hour after the last one."
            )

    # Everything that is worth a mention but never changes the answer.
    p3 = []
    if r.get("broward_risk"):
        p3.append(
            f"For what it's worth, the county rip current risk is rated {r['broward_risk'].lower()}. "
            "That number is a forecast written before dawn for the whole coastline and it regularly "
            "disagrees with the flag actually flying at Pompano, so treat it as background, not a veto."
        )
    if r.get("max_heat_index"):
        p3.append(
            f"Heat is the underrated one today - the index runs {r['max_heat_index'].lower()}°F, so "
            "bring more water than feels necessary and get in the shade periodically."
        )
    if f.get("sea_pests"):
        pest = f["sea_pests"]
        if "lice" in pest.lower():
            p3.append(
                "Sea lice have been reported. They're tiny jellyfish larvae that get trapped under "
                "swimwear and leave an itchy rash. Not dangerous, and easy to avoid - rinse off and "
                "change out of your suit soon after you get out."
            )
        else:
            p3.append(f"Sea pests reported: {pest}. Worth a glance along the waterline before you get in.")
    if f.get("stale") or f.get("unavailable"):
        p3.append(
            "One caveat: the flag feed flagged its own data as stale, so the colour above may be "
            "behind what is actually flying. Trust your eyes when you arrive."
        )

    paras = [" ".join(p) for p in (p1, p2, p3) if p]
    return "\n\n".join(paras)


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
    data["verdict"] = decide(data)

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
