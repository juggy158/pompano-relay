# pompano-relay

Live beach conditions for **Pompano Beach, FL**, refreshed by GitHub Actions and
published as [`conditions.json`](conditions.json).

## Why this repo exists

Claude cloud routines decide GO / NO GO for a swim, but they run in a sandbox
whose network egress proxy blocks nearly every host. Tested from inside it:

| Host | Result |
| --- | --- |
| `fortlauderdalebeachwatch.com` (the lifeguard flag) | `EGRESS_BLOCKED` |
| `api.weather.gov`, `forecast.weather.gov` | `EGRESS_BLOCKED` |
| `floridahealthybeaches.com` | `EGRESS_BLOCKED` |
| `api.open-meteo.com`, `ndbc.noaa.gov` | `EGRESS_BLOCKED` |
| `wikipedia.org`, `example.com` | `EGRESS_BLOCKED` |
| **`raw.githubusercontent.com`** | **reachable** |

Both `WebFetch` and raw `curl` fail identically (`CONNECT tunnel failed, 403`),
so it is a hard allowlist, not a tooling quirk. GitHub is the one way in — hence
this relay. Actions fetches the data where the network is open, commits it here,
and the routines read it from `raw.githubusercontent.com`.

## What gets collected

`fetch_conditions.py` pulls five sources, each independently guarded so one dead
source degrades that section instead of emptying the file. Failures land in the
`errors` array.

- **`flag`** — the live lifeguard flag from `fortlauderdalebeachwatch.com`,
  updated every ~10 min. This is the decisive field. Green / yellow / purple mean
  the water is open; red or double red mean it is closed.
- **`alerts`** — active NWS alerts for the beach coordinates (heat advisories,
  rip current statements, severe weather).
- **`swim_window_1_to_6pm`** — hour-by-hour temperature, precipitation chance and
  forecast text for 1–6 PM ET, the window that actually matters. Daily rain
  percentages are misleading in South Florida; hourly is not.
- **`rip_current`** — today's Coastal Broward block of the NWS Surf Zone
  Forecast: rip risk, surf height, thunderstorm potential, waterspout risk, max
  heat index.
- **`water_quality`** — Florida Healthy Beaches sampling for Broward.

## Reading it correctly

- **The flag governs, not the rip forecast.** The rip rating is a county-wide
  number issued at 4–5 AM and it regularly disagrees with the actual flag on the
  sand. On 2026-08-08 the forecast said High while the real flag was yellow and
  the swim was fine. `rip_current.broward_risk` is information, never a veto.
- **Lightning is the real veto.** Flags rate water only; they say nothing about
  storms. Use `swim_window_1_to_6pm.thunder_in_window` and
  `rip_current.waterspout_risk`.
- **Purple is not a no.** It means jellyfish or man o' war — check the sand.
- **Check freshness.** `generated_et` says when this was built. If it is more
  than ~45 minutes old the scheduled run was delayed or failed, and the flag may
  have changed. `flag.stale` and `flag.unavailable` come from the upstream feed.
- `raw.githubusercontent.com` caches for a few minutes, so a reader may see a
  slightly older commit than `main`.

## Running it by hand

```bash
python3 fetch_conditions.py   # writes conditions.json, prints it, exits 1 if the flag is missing
```

No dependencies beyond the Python standard library.
