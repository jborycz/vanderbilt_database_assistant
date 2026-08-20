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
   near-duplicates unless they serve distinct purposes. Never invent database names — \
   they must match names in the catalog exactly.

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


@app.get("/health")
def health():
    return {"status": "ok", "catalog_size": len(_by_name)}


@app.post("/chat")
def chat(req: ChatRequest):
    try:
        response = _client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1500,
            system=_system,
            messages=req.messages,
        )
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Claude API error: {exc}") from exc

    text = "".join(block.text for block in response.content if block.type == "text")

    try:
        parsed = _parse_json_response(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Model returned invalid JSON: {exc}. Raw: {text[:400]}",
        ) from exc

    recommendations = [_enrich(r, _by_name) for r in parsed.get("recommendations", [])]

    return {
        "enhanced_query": parsed.get("enhanced_query", ""),
        "recommendations": recommendations,
        "raw_assistant_message": text,
    }
