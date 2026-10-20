"""Garmin login. Credentials are only ever typed at a prompt; tokens are cached in ~/.garminconnect."""

import getpass
import os
import sys

from garminconnect import Garmin

TOKENSTORE = os.getenv("GARMINTOKENS", "~/.garminconnect")


def login_interactive() -> Garmin:
    if not sys.stdin.isatty():
        sys.exit("Login needs a real terminal for the password prompt. Run it in Terminal, not via `!`.")
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    client = Garmin(email, password, prompt_mfa=lambda: input("MFA code: ").strip())
    client.login(TOKENSTORE)
    return client


def login_with_tokens() -> Garmin:
    """Reuse cached tokens. Raises if `garmin-hub login` has not been run yet."""
    client = Garmin()
    client.login(TOKENSTORE)
    return client
