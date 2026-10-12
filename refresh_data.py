#!/usr/bin/env python3
"""Refresh the reference data the assistant ships with.

Usage:
  python3 refresh_data.py staff       # librarian roster -> data/staff_YYYYMMDD.json

Hand-maintained exceptions for the roster live in data/staff_overrides.json.
After refreshing, commit the new file and run ./redeploy.sh to ship it.
"""

import json
import sys
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
sys.path.insert(0, str(PROJECT_DIR))

from backend import staff  # noqa: E402


def refresh_staff() -> None:
    print("Fetching staff directory...")
    people = staff.fetch_staff(progress=print, overrides=staff.load_overrides(DATA_DIR))
    out = DATA_DIR / f"staff_{date.today():%Y%m%d}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(people, f, indent=1, ensure_ascii=False)
    with_office = sum(1 for p in people if p.get("office"))
    print(f"Saved {len(people)} research contacts ({with_office} with office) -> {out}")


COMMANDS = {"staff": refresh_staff}

if __name__ == "__main__":
    wanted = sys.argv[1:] or list(COMMANDS)
    for name in wanted:
        if name not in COMMANDS:
            sys.exit(f"Unknown target '{name}'. Choose from: {', '.join(COMMANDS)}")
        COMMANDS[name]()
