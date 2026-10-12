#!/usr/bin/env python3
"""Refresh the reference data the assistant ships with.

Usage:
  python3 refresh_data.py             # everything below
  python3 refresh_data.py databases   # Springshare A-Z catalog -> data/databases_YYYYMMDD.jsonl
  python3 refresh_data.py staff       # librarian roster -> data/staff_YYYYMMDD.json
  python3 refresh_data.py journals    # open-access journals -> data/journals_YYYYMMDD.json.gz

`databases` needs SPRINGSHARE_CLIENT_ID and SPRINGSHARE_CLIENT_SECRET, from the
environment or a .env file in this folder. Journals take about 3 minutes.

Hand-maintained exceptions for the roster live in data/staff_overrides.json.
After refreshing, commit the new file and run ./redeploy.sh to ship it.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
sys.path.insert(0, str(PROJECT_DIR))

from backend import catalog, journals, staff  # noqa: E402


def _load_dotenv() -> None:
    env = PROJECT_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def refresh_databases() -> None:
    _load_dotenv()
    if not catalog.credentials_available():
        sys.exit("Set SPRINGSHARE_CLIENT_ID and SPRINGSHARE_CLIENT_SECRET (environment or .env) to refresh the catalog.")
    print("Fetching the A-Z database catalog from Springshare...")
    records = catalog.fetch_databases()
    if len(records) < 100:
        sys.exit(f"Only {len(records)} records came back - refusing to write a snapshot.")
    out = DATA_DIR / f"databases_{date.today():%Y%m%d}.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=True) + "\n")
    print(f"Saved {len(records)} records -> {out}")


def refresh_staff() -> None:
    print("Fetching staff directory...")
    people = staff.fetch_staff(progress=print, overrides=staff.load_overrides(DATA_DIR))
    out = DATA_DIR / f"staff_{date.today():%Y%m%d}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(people, f, indent=1, ensure_ascii=False)
    with_office = sum(1 for p in people if p.get("office"))
    print(f"Saved {len(people)} research contacts ({with_office} with office) -> {out}")


def refresh_journals() -> None:
    print("Fetching journals from the Open Journal Finder...")
    items = journals.fetch_journals(progress=print)
    out = DATA_DIR / f"journals_{date.today():%Y%m%d}.json.gz"
    journals.save_snapshot(out, items)
    print(f"Saved {len(items)} journals ({out.stat().st_size // 1024} KB) -> {out}")


COMMANDS = {"databases": refresh_databases, "staff": refresh_staff, "journals": refresh_journals}

if __name__ == "__main__":
    wanted = sys.argv[1:] or list(COMMANDS)
    for name in wanted:
        if name not in COMMANDS:
            sys.exit(f"Unknown target '{name}'. Choose from: {', '.join(COMMANDS)}")
        COMMANDS[name]()
