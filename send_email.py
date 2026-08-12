#!/usr/bin/env python3
"""Email the current beach verdict.

The claude.ai Gmail connector can only create drafts - it has no send tool - so
real delivery has to happen here, where we can talk to Gmail's SMTP server
directly. Reads conditions.json (write it first with fetch_conditions.py) and
sends one short email.

Credentials come from the environment, never the repo:
  GMAIL_USER          the sending Gmail address
  GMAIL_APP_PASSWORD  a Google App Password (not the account password)
  MAIL_TO             recipient; defaults to GMAIL_USER
"""

import json
import os
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

ET = timezone(timedelta(hours=-4))


def main():
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO") or user

    if not user or not password:
        print("GMAIL_USER / GMAIL_APP_PASSWORD not set - cannot send", file=sys.stderr)
        return 1

    with open("conditions.json") as f:
        d = json.load(f)

    verdict = d.get("verdict") or {}
    call = verdict.get("call", "UNKNOWN")
    summary = verdict.get("summary", "No summary available.")
    now_et = datetime.now(timezone.utc).astimezone(ET)

    body = [
        summary,
        "",
        f"Data collected {d.get('generated_et', 'unknown')}.",
    ]
    if d.get("errors"):
        body += ["", "Problems collecting data:"] + [f"  - {e}" for e in d["errors"]]
    body += [
        "",
        "Flag colours: green / yellow / purple mean the water is open; red and",
        "double red mean it is closed. Rip current ratings are informational and",
        "never a reason on their own to skip the swim. Lightning is the real veto,",
        "and flags do not cover it - watch the sky.",
        "",
        "https://github.com/juggy158/pompano-relay",
    ]

    msg = EmailMessage()
    msg["Subject"] = f"Pompano Beach {now_et.strftime('%-I:%M %p')} - {call}"
    msg["From"] = user
    msg["To"] = to
    msg.set_content("\n".join(body))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, password)
        s.send_message(msg)

    print(f"sent to {to}: {msg['Subject']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
