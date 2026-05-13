#!/usr/bin/env python3
"""
Weekly update: fetch fresh database records from the LibApps API and write
a dated JSONL file to the data/ directory.

Output file:
  data/databases_YYYYMMDD.jsonl  - raw fetch from LibApps API
"""

import json
import sys
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
sys.path.insert(0, str(PROJECT_DIR))

from api_get_json_clean import clean_object, get_access_token, get_az_databases


def fetch_and_save(datestamp: str) -> None:
    """Fetch databases from LibApps API and save as a dated JSONL file."""
    print("Fetching databases from LibApps API...")
    token = get_access_token()
    raw = get_az_databases(token)
    cleaned = [clean_object(db) for db in raw]

    DATA_DIR.mkdir(exist_ok=True)
    out = DATA_DIR / f"databases_{datestamp}.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for db in cleaned:
            f.write(json.dumps(db, ensure_ascii=True) + "\n")

    print(f"Saved {len(cleaned)} records -> {out}")


def main() -> None:
    datestamp = date.today().strftime("%Y%m%d")
    fetch_and_save(datestamp)
    print("Weekly update complete.")


if __name__ == "__main__":
    main()
