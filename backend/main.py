import json
import re
from pathlib import Path
from urllib.parse import quote_plus

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

PRIMO_SEARCH_URL = (
    "https://catalog.library.vanderbilt.edu/discovery/search"
    "?query=any,contains,{q}&tab=Everything&search_scope=MyInst_and_CI"
    "&vid=01VAN_INST:vanui"
)

SYSTEM_PROMPT = """\
You recommend Vanderbilt Libraries databases for a research query. You are given the \
current database catalog (name + description) below. Return strict JSON only.

Steps:

1) Enhance the query.
   Rewrite the user's topic into a richer search representation: core subject, adjacent \
   disciplines, keyword synonyms, methods, and likely content types.

2) Pick exactly 5 databases from the catalog.
   Rank by relevance to the enhanced query. Favor multidisciplinary databases \
   (Web of Science, Scopus, Academic Search Complete, etc.) when reasonably relevant. \
   Include specialized databases only when they clearly expand coverage. Avoid \
   near-duplicates unless they serve distinct purposes.

   Hard requirement on the `name` field:
   - MUST be a verbatim, case-sensitive copy of a database name that appears in the \
     catalog above. Do not paraphrase, translate, pluralize, add subtitles, or \
     append descriptive suffixes.
   - Never output "N/A", "not available", "unavailable", or any placeholder as a \
     name or any other field value. If you cannot find a specific match for a slot, \
     pick any reasonable database from the catalog instead of a placeholder.
   - If a promising database is not in the catalog, do not mention it at all — \
     substitute a catalog entry.

3) For each database, write:
   - `refined_description`: one clear sentence about subject coverage, content types, \
     and typical use cases. Do not fabricate capabilities.
   - `why_it_fits`: one sentence tying it to the user's query.
   - `search_string`: a ready-to-paste Boolean/keyword query tailored to the user's \
     topic, using quoted phrases, AND/OR, and 2-4 core concepts. Keep it under 25 words.

Output format — strict JSON, no prose, no markdown fences:

{
  "enhanced_query": "...",
  "recommendations": [
    {
      "name": "<exact catalog name>",
      "refined_description": "...",
      "why_it_fits": "...",
      "search_string": "..."
    },
    ... 5 total
  ]
}

Rules:
- Output MUST be a single JSON object matching the schema above.
- No commentary, no markdown, no code fences.
- Never construct URLs — the frontend handles links.
- If the topic is too vague, still return 5 broad multidisciplinary databases and set \
  `enhanced_query` to a reasonable interpretation.
"""


def _load_catalog() -> tuple[str, dict[str, dict]]:
    """Load the newest databases_YYYYMMDD.jsonl file.

    Returns:
        catalog_text: formatted text block for the Claude system prompt.
        by_name: case-insensitive name → record lookup for URL enrichment.
    """
    data_dir = Path(__file__).resolve().parent.parent / "data"
    candidates = sorted(data_dir.glob("databases_????????.jsonl"), reverse=True)
    if not candidates:
        raise FileNotFoundError("No database catalog found in data/. Run weekly_update.py first.")

    raw: list[dict] = []
    with open(candidates[0], encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                raw.append(json.loads(line))

    visible = [
        d for d in raw
        if d.get("enable_hidden") != "1" and d.get("description", "").strip()
    ]

    by_name: dict[str, dict] = {}
    lines = [f"DATABASE CATALOG ({len(visible)} databases):\n"]
    for db in visible:
        name = db["name"]
        by_name[name.lower().strip()] = db
        lines.append(f"Name: {name}")
        lines.append(f"Description: {db['description'].strip()}")
        more_info = db.get("meta", {}).get("more_info", "").strip()
        if more_info:
            lines.append(f"Additional info: {more_info}")
        lines.append("")

    return "\n".join(lines), by_name


def _lookup(name: str, by_name: dict[str, dict]) -> dict | None:
    key = name.lower().strip()
    if key in by_name:
        return by_name[key]
    # Fuzzy fallback: substring match either direction. Prefer shortest matching name.
    matches = [
        (n, rec) for n, rec in by_name.items()
        if key in n or n in key
    ]
    if not matches:
        return None
    matches.sort(key=lambda x: len(x[0]))
    return matches[0][1]


def _parse_json_response(text: str) -> dict:
    """Extract a JSON object from Claude's response.

    Tolerates optional ```json fences or leading/trailing prose.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _enrich(rec: dict, by_name: dict[str, dict]) -> dict:
    """Attach direct_url and primo_search_url to a recommendation."""
    record = _lookup(rec.get("name", ""), by_name)
    search_string = rec.get("search_string", "").strip()

    enriched = {
        "name": rec.get("name", ""),
        "refined_description": rec.get("refined_description", ""),
        "why_it_fits": rec.get("why_it_fits", ""),
        "search_string": search_string,
        "direct_url": record["url"].strip() if record and record.get("url") else None,
        "primo_search_url": (
            PRIMO_SEARCH_URL.format(q=quote_plus(search_string))
            if search_string else None
        ),
        "matched": record is not None,
    }
    return enriched


app = FastAPI(title="Vanderbilt Database Recommender API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_catalog_text, _by_name = _load_catalog()
_client = anthropic.Anthropic()
_system = [
    {"type": "text", "text": SYSTEM_PROMPT},
    {"type": "text", "text": _catalog_text, "cache_control": {"type": "ephemeral"}},
]


class ChatRequest(BaseModel):
    messages: list[dict]


TARGET_RECS = 5


def _call_llm(messages: list[dict], max_tokens: int = 1500) -> tuple[dict, str]:
    """Single Claude call. Returns (parsed_json, raw_text)."""
    response = _client.messages.create(
        model="claude-opus-4-7",
        max_tokens=max_tokens,
        system=_system,
        messages=messages,
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    parsed = _parse_json_response(text)
    return parsed, text


@app.get("/health")
def health():
    return {"status": "ok", "catalog_size": len(_by_name)}


@app.post("/chat")
def chat(req: ChatRequest):
    try:
        parsed, raw_text = _call_llm(req.messages)
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Claude API error: {exc}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Model returned invalid JSON: {exc}",
        ) from exc

    enhanced_query = parsed.get("enhanced_query", "")
    initial = [_enrich(r, _by_name) for r in parsed.get("recommendations", [])]
    matched = [r for r in initial if r["matched"]]
    unmatched_count = len(initial) - len(matched)

    # If any recommendation missed the catalog, ask Claude for replacements that
    # exclude the names we already have. One retry only.
    if unmatched_count > 0 and len(matched) < TARGET_RECS:
        needed = TARGET_RECS - len(matched)
        used_names = [r["name"] for r in matched]
        exclude_clause = (
            f" Do not repeat: {', '.join(used_names)}." if used_names else ""
        )
        followup_msg = (
            f"{unmatched_count} of your previous names were not in the catalog. "
            f"Return exactly {needed} REPLACEMENT recommendation(s) using database "
            f"names that appear verbatim in the catalog.{exclude_clause} "
            f"Same JSON schema; the `recommendations` array must have exactly "
            f"{needed} items."
        )
        followup_messages = list(req.messages) + [
            {"role": "assistant", "content": raw_text},
            {"role": "user", "content": followup_msg},
        ]
        try:
            followup_parsed, _ = _call_llm(followup_messages, max_tokens=1200)
        except (anthropic.APIError, json.JSONDecodeError, ValueError):
            followup_parsed = {}

        used_lower = {n.lower() for n in used_names}
        for r in followup_parsed.get("recommendations", []):
            enriched = _enrich(r, _by_name)
            if not enriched["matched"]:
                continue
            if enriched["name"].lower() in used_lower:
                continue
            matched.append(enriched)
            used_lower.add(enriched["name"].lower())
            if len(matched) >= TARGET_RECS:
                break

    final = matched[:TARGET_RECS]

    # Synthesize an assistant message that reflects the FINAL list, so multi-turn
    # history stays consistent with what the user actually saw.
    synthesized = json.dumps({
        "enhanced_query": enhanced_query,
        "recommendations": [
            {
                "name": r["name"],
                "refined_description": r["refined_description"],
                "why_it_fits": r["why_it_fits"],
                "search_string": r["search_string"],
            }
            for r in final
        ],
    })

    return {
        "enhanced_query": enhanced_query,
        "recommendations": final,
        "raw_assistant_message": synthesized,
    }
