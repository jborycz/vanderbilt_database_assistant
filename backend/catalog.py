"""Springshare LibApps A-Z database catalog: fetch and clean.

The hosted backend can't use the repo-root scripts (weekly_update.py and
api_get_json_clean.py are not in the Docker image), so this is a standalone,
standard-library copy of the same fetch-and-clean logic. Credentials come from
the environment (SPRINGSHARE_CLIENT_ID / SPRINGSHARE_CLIENT_SECRET); without
them the catalog simply isn't refreshed in-app.
"""

import html
import json
import os
import re
import urllib.parse
import urllib.request

TOKEN_URL = "https://lgapi-us.libapps.com/1.2/oauth/token"
AZ_URL = "https://lgapi-us.libapps.com/1.2/az"
USER_AGENT = "VanderbiltDatabaseAssistant/1.0 (catalog sync)"
_TAG_RE = re.compile(r"<[^>]+>")


def credentials_available() -> bool:
    return bool(os.environ.get("SPRINGSHARE_CLIENT_ID") and os.environ.get("SPRINGSHARE_CLIENT_SECRET"))


def clean_text(value):
    """Strip HTML, decode entities, normalize whitespace, drop non-ASCII."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    text = html.unescape(value)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.encode("ascii", "ignore").decode("ascii")


def clean_object(obj):
    if isinstance(obj, dict):
        return {k: clean_object(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_object(v) for v in obj]
    if isinstance(obj, str):
        return clean_text(obj)
    return obj


def fetch_databases() -> list[dict]:
    """Fetch and clean every A-Z record (the same shape weekly_update.py writes)."""
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": os.environ["SPRINGSHARE_CLIENT_ID"],
        "client_secret": os.environ["SPRINGSHARE_CLIENT_SECRET"],
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        token = json.loads(resp.read().decode())["access_token"]

    req = urllib.request.Request(
        AZ_URL, headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = json.loads(resp.read().decode())
    return [clean_object(db) for db in raw]
