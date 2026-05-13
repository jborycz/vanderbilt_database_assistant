import requests
import json
import csv
import re
import html

# --- CONFIG ---
CLIENT_ID = "1025"
CLIENT_SECRET = "eb0a76b9f7cc8202a82aeabd0fbf8bde"

TOKEN_URL = "https://lgapi-us.libapps.com/1.2/oauth/token"
AZ_URL = "https://lgapi-us.libapps.com/1.2/az"

# --- CLEANING HELPERS ---
TAG_RE = re.compile(r"<[^>]+>")

def clean_text(value):
    """
    Remove HTML tags, decode HTML entities, normalize whitespace,
    and strip non-ASCII characters.
    """
    if value is None:
        return ""

    if not isinstance(value, str):
        return value

    text = html.unescape(value)
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.encode("ascii", "ignore").decode("ascii")
    return text

def clean_object(obj):
    """
    Recursively clean strings inside dicts/lists for JSONL output.
    """
    if isinstance(obj, dict):
        return {k: clean_object(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_object(v) for v in obj]
    if isinstance(obj, str):
        return clean_text(obj)
    return obj

def csv_value(value):
    """
    Convert values into CSV-safe cleaned strings.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(csv_value(v) for v in value)
    if isinstance(value, dict):
        return clean_text(json.dumps(value, ensure_ascii=True))
    if isinstance(value, str):
        return clean_text(value)
    return str(value)

# --- STEP 1: Get Access Token ---
def get_access_token():
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        }
    )
    response.raise_for_status()
    return response.json()["access_token"]

# --- STEP 2: Get A–Z Databases ---
def get_az_databases(token):
    response = requests.get(
        AZ_URL,
        headers={
            "Authorization": f"Bearer {token}"
        }
    )
    response.raise_for_status()
    return response.json()

# --- RUN ---
if __name__ == "__main__":
    token = get_access_token()
    data = get_az_databases(token)

    # Clean all records first
    cleaned_data = [clean_object(db) for db in data]

    # --- Save to JSONL ---
    with open("databases.jsonl", "w", encoding="utf-8") as f:
        for db in cleaned_data:
            f.write(json.dumps(db, ensure_ascii=True) + "\n")

    print("Saved to databases.jsonl")

    # --- Save to CSV ---
    fieldnames = ["id", "name", "url", "description"]

    with open("databases.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for db in cleaned_data:
            writer.writerow({
                "id": csv_value(db.get("id")),
                "name": csv_value(db.get("name")),
                "url": csv_value(db.get("url")),
                "description": csv_value(db.get("description"))
            })

    print("Saved to databases.csv")

    # Optional preview
    for db in cleaned_data[:5]:
        print(f"{db.get('name')} -> {db.get('url')}")
