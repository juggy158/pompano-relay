#!/usr/bin/env python3
"""Render the beach verdict as a GitHub issue title and body.

Why an issue: Gmail's SMTP refused app-password logins from GitHub's runners
(535 BadCredentials, from two independently created passwords), and the
claude.ai Gmail connector can only draft, never send. GitHub's own notification
email needs no credentials at all, so the verdict is posted as an issue and
GitHub mails it out.

The body opens with an @mention of the repo owner. Mentions are emailed under
GitHub's default notification settings even when repository watching is turned
down, which plain issue activity is not - so the mention is what actually makes
this land in an inbox.

Writes issue_title.txt and issue_body.md for the workflow to pick up.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

ET = timezone(timedelta(hours=-4))
OWNER = os.environ.get("REPO_OWNER", "juggy158")


def main():
    with open("conditions.json") as f:
        d = json.load(f)

    v = d.get("verdict") or {}
    call = v.get("call", "UNKNOWN")
    flag = d.get("flag") or {}
    rip = d.get("rip_current") or {}
    win = d.get("swim_window_1_to_6pm") or {}
    now_et = datetime.now(timezone.utc).astimezone(ET)

    title = f"Pompano Beach {now_et.strftime('%-I:%M %p')} - {call}"

    lines = [
        f"@{OWNER}",
        "",
        f"## {call}",
        "",
        v.get("explanation") or v.get("summary", "No summary available."),
        "",
        "### The numbers",
        "",
        "| | |",
        "| --- | --- |",
        f"| Flag | {'/'.join(flag.get('colors') or []) or 'unknown'} |",
        f"| Water / air | {flag.get('water_temp_f')}°F / {flag.get('air_temp_f')}°F |",
        f"| Sea pests | {flag.get('sea_pests') or 'none reported'} |",
        f"| Rip risk (info only) | {rip.get('broward_risk') or 'unknown'} |",
        f"| Surf | {rip.get('surf_height') or 'unknown'} |",
        f"| Waterspouts | {rip.get('waterspout_risk') or 'unknown'} |",
        f"| Max heat index | {rip.get('max_heat_index') or 'unknown'} |",
    ]

    hours = win.get("hours") or []
    if hours:
        lines += ["", "### 1-6 PM hour by hour", "", "| Hour | Rain | Forecast |", "| --- | --- | --- |"]
        for h in hours:
            lines.append(f"| {h.get('hour_et')} | {h.get('precip_pct')}% | {h.get('forecast')} |")

    alerts = d.get("alerts") or []
    if alerts:
        lines += ["", "### Active alerts", ""]
        lines += [f"- **{a.get('event')}** - {a.get('headline')}" for a in alerts]

    if d.get("errors"):
        lines += ["", "### Problems collecting data", ""]
        lines += [f"- `{e}`" for e in d["errors"]]

    lines += [
        "",
        "---",
        "",
        "Green, yellow and purple flags mean the water is open; red and double red mean closed. "
        "Rip current ratings are informational and never a reason on their own to skip the swim. "
        "Lightning is the real veto, and flags do not cover it.",
        "",
        f"Data collected {d.get('generated_et', 'unknown')}.",
    ]

    with open("issue_title.txt", "w") as f:
        f.write(title)
    with open("issue_body.md", "w") as f:
        f.write("\n".join(lines) + "\n")

    print(title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
