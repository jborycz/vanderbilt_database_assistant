"""Vanderbilt Open Journal Finder data: fetch, snapshot, index, and matching.

Source: https://vanderbilt.openjournalfinder.com (a public JSON API used by the
site itself; 100 records per page, ~144 pages for the journals that currently
have an active publisher agreement). Standard library only.

What the data does NOT contain: acceptance rates, or Clarivate impact factors.
SJR (Scimago), its quartile, h-index and 2-year mean citedness (OpenAlex) are
used as impact indicators instead. The API's own `apc` field is 0 even for
journals whose benefit is a percentage discount, so prices are computed here
from the list price and the agreement-benefit text.
"""

import difflib
import gzip
import html
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

FINDER_BASE = "https://vanderbilt.openjournalfinder.com"
API_URL = (
    FINDER_BASE + "/api/journals?expand=subjects%2Clicenses%2Cmetadata"
    "&has_active_deal=true&limit=100&page={page}"
)
USER_AGENT = "VanderbiltDatabaseAssistant/1.0 (open-access journal data sync)"
REQUEST_DELAY_SECONDS = 0.3  # the API allows ~200 requests per window


def journal_url(journal_id: str) -> str:
    return f"{FINDER_BASE}/journals/{journal_id}"


def finder_search_url(query: str) -> str:
    from urllib.parse import quote
    return f"{FINDER_BASE}/journals/?has_active_deal=true&q={quote(query)}"


# ── Fetching ────────────────────────────────────────────────────────────────

def _get_json(url: str, retries: int = 4) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Open Journal Finder request failed: {last}")


def _text(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _num(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def slim(raw: dict) -> dict:
    """Reduce an API record to the fields this app uses."""
    benefits = []
    for group in raw.get("metadata") or []:
        for entries in group.values():
            for entry in entries:
                if entry.get("slug") == "agreement-benefit" and entry.get("value"):
                    if entry["value"] not in benefits:
                        benefits.append(entry["value"])
    subjects = []
    for s in raw.get("subjects") or []:
        if s.get("scheme") == "asjc":
            subjects.append({
                "term": s.get("term", ""),
                "area": (s.get("parent") or {}).get("term", ""),
            })
    desc = raw.get("description")
    scope = _text(desc.get("text") if isinstance(desc, dict) else desc)[:700]
    subs = raw.get("subscriptions") or []
    return {
        "id": raw["id"],
        "name": raw.get("name") or "",
        "publisher": raw.get("publisher_name") or "",
        "aliases": [a for a in (raw.get("aliases") or "").split(";") if a.strip()]
        if isinstance(raw.get("aliases"), str) else [],
        "issn": raw.get("issn") or "",
        "eissn": raw.get("eissn") or "",
        "subjects": subjects,
        "is_oa": bool(raw.get("is_oa")),
        "is_hybrid": bool(raw.get("is_hybrid")),
        "sjr": _num(raw.get("sjr")),
        "quartile": int(raw["sjr_quartile"]) if str(raw.get("sjr_quartile") or "").isdigit() else None,
        "h_index": raw.get("h_index"),
        "citedness": _num(raw.get("mean_citedness")),
        "list_apc": _num(raw.get("list_apc")),
        "currency": raw.get("list_apc_currency") or raw.get("apc_currency") or "USD",
        "benefit": "; ".join(benefits),
        "licenses": [l.get("license") for l in (raw.get("licenses") or []) if l.get("license")],
        "country": raw.get("country") or "",
        "scope": scope,
        "homepage": raw.get("url") or "",
        "predatory": bool(raw.get("predatory")),
        "agreement_end": (subs[0].get("end_date") if subs else "") or "",
    }


def fetch_journals(progress=None) -> list[dict]:
    first = _get_json(API_URL.format(page=1))
    last_page = first["meta"]["last_page"]
    total = first["meta"]["total"]
    journals = [slim(r) for r in first["data"]]
    for page in range(2, last_page + 1):
        time.sleep(REQUEST_DELAY_SECONDS)
        journals.extend(slim(r) for r in _get_json(API_URL.format(page=page))["data"])
        if progress and page % 20 == 0:
            progress(f"  page {page}/{last_page}")
    if len(journals) < total * 0.95:
        raise RuntimeError(f"Fetched only {len(journals)} of {total} journals")
    return journals


# ── Snapshots ───────────────────────────────────────────────────────────────

def latest_snapshot(data_dir: Path) -> Path | None:
    candidates = sorted(data_dir.glob("journals_????????.json.gz"), reverse=True)
    return candidates[0] if candidates else None


def save_snapshot(path: Path, journals: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(journals, f, ensure_ascii=False, separators=(",", ":"))


def load_snapshot(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


# ── Benefits and cost ───────────────────────────────────────────────────────

def classify_benefit(benefit: str) -> tuple[str, int | None]:
    """Return (type, discount_pct). type is one of: allocated_out, waived,
    discount, other, none."""
    b = (benefit or "").lower()
    if not b:
        return "none", None
    if "fully allocated" in b:
        return "allocated_out", None
    m = re.search(r"(\d+)\s*%\s*apc discount", b)
    if m:
        return "discount", int(m.group(1))
    if "waive" in b:
        return "waived", None
    return "other", None


def cost_info(j: dict) -> dict:
    kind, pct = classify_benefit(j.get("benefit", ""))
    lst = j.get("list_apc")
    price = None
    if kind == "waived":
        price = 0.0
    elif kind == "discount" and lst is not None:
        price = round(lst * (1 - pct / 100), 2)
    elif kind in ("allocated_out", "other", "none") and lst is not None:
        price = lst
    return {
        "benefit": j.get("benefit", ""),
        "benefit_type": kind,
        "discount_pct": pct,
        "list_apc": lst,
        "price_with_agreement": price,
        "currency": j.get("currency") or "USD",
        "capped": "capped" in (j.get("benefit", "").lower()),
    }


def card_view(j: dict) -> dict:
    """The public, display-ready facts for one journal."""
    return {
        "id": j["id"],
        "name": j["name"],
        "publisher": j["publisher"],
        "finder_url": journal_url(j["id"]),
        "homepage": j.get("homepage") or "",
        "subjects": sorted({s["term"] for s in j["subjects"] if s["term"]}),
        "oa_model": "Fully open access" if j["is_oa"] else "Hybrid (subscription + open access option)",
        "metrics": {
            "sjr": j["sjr"],
            "quartile": j["quartile"],
            "h_index": j["h_index"],
            "citedness": j["citedness"],
        },
        "cost": cost_info(j),
        "licenses": j.get("licenses", []),
        "country": j.get("country", ""),
        "agreement_end": j.get("agreement_end", ""),
    }


# ── Index and matching ──────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", (s or "").lower().replace("&", " and "))).strip()


_STOP = {"the", "of", "and", "in", "for", "on", "a", "an", "to", "with", "journal", "review", "reviews"}


class JournalIndex:
    def __init__(self, journals: list[dict]):
        self.journals = [j for j in journals if not j.get("predatory")]
        self.by_id = {j["id"]: j for j in self.journals}
        self._names: dict[str, dict] = {}
        for j in self.journals:
            for n in [j["name"], *j.get("aliases", [])]:
                self._names.setdefault(_norm(n), j)
        self._name_keys = list(self._names)
        self.subject_terms = sorted({s["term"] for j in self.journals for s in j["subjects"] if s["term"]})
        self._haystack = {
            j["id"]: (" " + _norm(j["name"] + " " + " ".join(j.get("aliases", []))) + " ",
                      " " + _norm(j.get("scope", "")) + " ")
            for j in self.journals
        }

    def find_reference(self, name: str) -> dict | None:
        key = _norm(name)
        if not key:
            return None
        if key in self._names:
            return self._names[key]
        close = difflib.get_close_matches(key, self._name_keys, n=1, cutoff=0.88)
        return self._names[close[0]] if close else None

    def shortlist(self, subjects: list[str], keywords: list[str], prefs: dict | None = None,
                  reference: dict | None = None, n: int = 40) -> list[dict]:
        prefs = prefs or {}
        wanted = {s for s in subjects if s in self.subject_terms}
        ref_subjects = {s["term"] for s in reference["subjects"]} if reference else set()
        targets = wanted | ref_subjects
        kws = [_norm(k) for k in keywords if _norm(k)]
        kws = [k for k in kws if k not in _STOP]
        quartile_max = prefs.get("quartile_max")
        max_price = prefs.get("max_apc")
        oa_type = prefs.get("oa_type", "any")
        waiver_pref = bool(prefs.get("waiver_preferred"))

        scored = []
        for j in self.journals:
            if reference and j["id"] == reference["id"]:
                continue
            if oa_type == "fully_oa" and not j["is_oa"]:
                continue
            if oa_type == "hybrid" and j["is_oa"]:
                continue
            kind, pct = classify_benefit(j["benefit"])
            if max_price is not None:
                price = cost_info(j)["price_with_agreement"]
                if price is not None and price > max_price:
                    continue

            jsubs = {s["term"] for s in j["subjects"]}
            subj_hits = len(jsubs & targets)
            name_h, scope_h = self._haystack[j["id"]]
            name_hits = sum(1 for k in kws if f" {k} " in name_h or k in name_h)
            scope_hits = sum(1 for k in kws if k in scope_h)
            if subj_hits == 0 and name_hits == 0 and scope_hits == 0:
                continue

            score = 3.0 * subj_hits + 2.5 * name_hits + min(scope_hits, 4) * 0.7
            if targets and subj_hits == 0:
                score -= 1.0  # keyword-only match outside the stated subjects
            if reference:
                if reference.get("quartile") and j["quartile"]:
                    score += 1.5 if j["quartile"] == reference["quartile"] else -0.5 * abs(j["quartile"] - reference["quartile"])
                if reference.get("sjr") and j["sjr"]:
                    score -= min(abs(j["sjr"] - reference["sjr"]) / max(reference["sjr"], 0.5), 2.0)
            elif quartile_max and j["quartile"]:
                score += 1.0 if j["quartile"] <= quartile_max else -1.5
            if kind == "waived":
                score += 2.0 if waiver_pref else 1.0
            elif kind == "discount":
                score += 0.4
            elif kind == "allocated_out":
                score -= 2.5
            # tie-breakers: stronger journals first
            score += (5 - (j["quartile"] or 5)) * 0.05 + min((j["sjr"] or 0), 10) * 0.02
            scored.append((score, j))

        scored.sort(key=lambda t: t[0], reverse=True)
        return [j for _, j in scored[:n]]

    def format_shortlist(self, items: list[dict]) -> str:
        lines = ["id | journal | publisher | subjects | SJR quartile | open access model | agreement benefit"]
        for j in items:
            subs = ", ".join(sorted({s["term"] for s in j["subjects"] if s["term"]}))
            q = f"Q{j['quartile']}" if j["quartile"] else "n/a"
            oa = "fully OA" if j["is_oa"] else "hybrid"
            lines.append(f"{j['id']} | {j['name']} | {j['publisher']} | {subs} | {q} | {oa} | {j['benefit'] or 'none'}")
        return "\n".join(lines)
