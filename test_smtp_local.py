#!/usr/bin/env python3
"""Test the Gmail app password from THIS machine.

Purpose: isolate a bad credential from a blocked login. GitHub's runners live in
datacenter IP ranges that Google sometimes refuses even when the app password is
valid, and it reports that refusal as the same 535 BadCredentials you get from a
genuinely wrong password. Running the identical login from the user's own home
IP tells the two apart:

  works here, fails on Actions  -> the password is fine, Google is blocking CI
  fails here too                -> the password itself is wrong

Reads the password from a file so it never passes through a chat transcript or
shell history. Delete the file afterwards.
"""

import smtplib
import ssl
import sys
from pathlib import Path

PW_FILE = Path.home() / ".pompano_apppw"
USER = "juggyghei070507@gmail.com"


def main():
    if not PW_FILE.exists():
        print(f"No password file at {PW_FILE}", file=sys.stderr)
        return 1

    password = PW_FILE.read_text().strip().replace(" ", "")
    print(f"user            : {USER}")
    print(f"password length : {len(password)} (expect 16)")
    print(f"all lowercase   : {password.isalpha() and password.islower()}")
    print("connecting to smtp.gmail.com:465 ...")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(USER, password)
            print("\nLOGIN OK — the password is valid and Gmail accepts it from this machine.")
            print("=> The failure on GitHub Actions is a location/IP block, not a bad password.")
    except smtplib.SMTPAuthenticationError as e:
        print(f"\nLOGIN REJECTED: {e.smtp_code} {e.smtp_error}", file=sys.stderr)
        print("=> Gmail refuses this password from here too, so the credential itself is wrong.", file=sys.stderr)
        print("   Most likely it was created on a different Google account.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
