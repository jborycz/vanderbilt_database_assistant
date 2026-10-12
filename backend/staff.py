"""Heard Libraries staff directory: scrape, snapshot loading, and prompt formatting.

The directory (https://www.library.vanderbilt.edu/staff/) has no API. One page
holds every person (pagination is client-side), and each person's profile page
adds the office/room number and appointment links. Standard library only, so
the backend needs no extra dependency.
"""

import html
import json
import re
import time
import urllib.request
from pathlib import Path

STAFF_URL = "https://www.library.vanderbilt.edu/staff/"
PROFILE_URL = STAFF_URL + "profile/?id={id}"
USER_AGENT = "VanderbiltDatabaseAssistant/1.0 (library staff-directory sync)"
REQUEST_DELAY_SECONDS = 0.25  # be polite to the library's web server

# Titles containing any of these are people a patron can reasonably be sent to
# for research help...
RESEARCH_TITLE_KEYWORDS = (
    "librarian", "informationist", "specialist", "archivist", "curator",
    "bibliographer", "public services", "research services",
    "research and education services",
)
# ...unless the title also matches one of these (administration, collections,
# technical services, communications, and similar non-patron-facing roles).
EXCLUDE_TITLE_KEYWORDS = (
    "associate university librarian", "assistant college librarian", "college librarian",
    "dean", "assistant to", "special projects", "assessment", "electronic resource",
    "communications", "access and collection", "access services", "for collections",
    "cataloging", "processing", "library assistant", "support assistant",
    "administrative", "supervisor", "evening", "weekend",
)
# Hand-maintained exceptions live in data/staff_overrides.json:
#   {"include_ids": [123], "exclude_ids": [456]}
# Directory ids are stable, so these survive refreshes.


def load_overrides(data_dir: Path) -> dict:
    path = data_dir / "staff_overrides.json"
    if not path.exists():
        return {"include_ids": [], "exclude_ids": []}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {"include_ids": data.get("include_ids", []), "exclude_ids": data.get("exclude_ids", [])}


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_directory(page: str) -> list[dict]:
    """Parse the one-page staff listing into a list of person dicts."""
    people = []
    chunks = re.split(r'<li\s+class="people-list__list-item', page)[1:]
    for chunk in chunks:
        chunk = re.split(r"</div>\s*</li>", chunk, maxsplit=1)[0]
        pid = re.search(r'data-id="(\d+)"', chunk)
        if not pid:
            continue
        name = re.search(r'<h3 class="people-list__name">\s*<a[^>]*>(.*?)</a>', chunk, re.S)
        title = re.search(r'<p class="people-list__primary-title">(.*?)</p>', chunk, re.S)
        email = re.search(r'href="mailto:([^"]+)"', chunk)
        phone = re.search(r'href="tel:([^"]+)"', chunk)
        unit = re.search(r'data-unit="([^"]*)"', chunk)
        people.append({
            "id": int(pid.group(1)),
            "name": _clean(name.group(1)) if name else "",
            "title": _clean(title.group(1)) if title else "",
            "unit": html.unescape(unit.group(1)).strip() if unit else "",
            "email": email.group(1).strip().lower() if email else "",
            "phone": phone.group(1).strip() if phone else "",
        })
    return people


def parse_profile(page: str) -> dict:
    """Extract office/room and helpful links from a profile page."""
    block = re.search(r'<div class="topper-profile__contact">(.*?)</div>', page, re.S)
    if not block:
        return {}
    body = block.group(1)
    first = re.split(r"<br\s*/?>", body, maxsplit=1)[0]
    office = "" if "<a" in first else _clean(first)
    links = {}
    for href, label in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
        label = _clean(label)
        if href.startswith(("tel:", "mailto:")):
            continue
        if "researchguides" in href:
            links["guides_url"] = href
        elif label.lower().startswith("more ways to connect"):
            links["connect_url"] = href
    return {"office": office, **links}


def is_research_contact(person: dict, overrides: dict | None = None) -> bool:
    overrides = overrides or {}
    if not person.get("email"):
        return False
    if person["id"] in overrides.get("exclude_ids", []):
        return False
    if person["id"] in overrides.get("include_ids", []):
        return True
    title = person.get("title", "").lower()
    if not title:
        return False
    if any(k in title for k in EXCLUDE_TITLE_KEYWORDS):
        return False
    return any(k in title for k in RESEARCH_TITLE_KEYWORDS)


def fetch_staff(progress=None, overrides: dict | None = None) -> list[dict]:
    """Scrape the directory plus each research contact's profile page."""
    people = parse_directory(_get(STAFF_URL))
    if len(people) < 50:
        raise RuntimeError(f"Staff directory parse looks wrong: only {len(people)} people found")
    keep = [p for p in people if is_research_contact(p, overrides)]
    for i, person in enumerate(keep, 1):
        try:
            person.update(parse_profile(_get(PROFILE_URL.format(id=person["id"]))))
        except Exception as exc:  # one bad profile shouldn't sink the refresh
            if progress:
                progress(f"  profile {person['id']} failed: {exc}")
        person.setdefault("office", "")
        person["profile_url"] = PROFILE_URL.format(id=person["id"])
        if progress and i % 20 == 0:
            progress(f"  {i}/{len(keep)} profiles")
        time.sleep(REQUEST_DELAY_SECONDS)
    return keep


def latest_snapshot(data_dir: Path) -> Path | None:
    candidates = sorted(data_dir.glob("staff_????????.json"), reverse=True)
    return candidates[0] if candidates else None


def load_snapshot(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def format_roster(staff: list[dict]) -> str:
    """Compact roster text for the system prompt. Contact details are NOT
    included -- the model returns ids and the server attaches real contact
    data, so an email or phone number can never be invented."""
    lines = [f"LIBRARIAN ROSTER ({len(staff)} people; use these ids in `librarians`):\n"]
    for p in staff:
        lines.append(f"id={p['id']} | {p['name']} | {p['title']} | {p['unit']}")
    return "\n".join(lines)
