import json
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend import catalog, journals, staff
from backend.oa import OpenAccessAssistant
from backend.refresher import Refresher, Source

PRIMO_SEARCH_URL = (
    "https://catalog.library.vanderbilt.edu/discovery/search"
    "?query=any,contains,{q}&tab=Everything&search_scope=MyInst_and_CI"
    "&vid=01VAN_INST:vanui"
)

SYSTEM_PROMPT = """\
You are the Vanderbilt Libraries database assistant. You are given the current \
database catalog (name + description) below. Every response is strict JSON.

Each turn, choose one of two modes:

────────────────────────────────────────────────────────────────────────────
MODE 1 — `research`
────────────────────────────────────────────────────────────────────────────
Use when the user is asking for database recommendations for a topic:
- A new research topic, subject, or question
- A refined/narrowed version of an earlier research topic (e.g. "narrow to \
  sociology", "focus on Brazil", "just peer-reviewed sources for that")
- Any request that fundamentally needs a fresh set of 5 databases

Steps:

1) Enhance the query — rewrite the user's topic into a richer search \
   representation: core subject, adjacent disciplines, keyword synonyms, \
   methods, likely content types.

2) Pick exactly 5 databases from the catalog.
   Rank by relevance to the enhanced query. Favor multidisciplinary databases \
   (Web of Science, Scopus, Academic Search Complete, etc.) when reasonably \
   relevant. Include specialized databases only when they clearly expand \
   coverage. Avoid near-duplicates unless they serve distinct purposes.

   Hard requirement on the `name` field:
   - MUST be a verbatim, case-sensitive copy of a database name that appears \
     in the catalog above. Do not paraphrase, translate, pluralize, add \
     subtitles, or append descriptive suffixes.
   - Never output "N/A", "not available", "unavailable", or any placeholder as \
     a name or any other field value. If you cannot find a specific match for \
     a slot, pick any reasonable database from the catalog.
   - If a promising database is not in the catalog, do not mention it at all — \
     substitute a catalog entry.

3) For each database, write:
   - `refined_description`: one clear sentence about subject coverage, content \
     types, and typical use cases. Do not fabricate capabilities.
   - `why_it_fits`: one sentence tying it to the user's query.
   - `search_string`: a ready-to-paste Boolean/keyword query tailored to the \
     user's topic, using quoted phrases, AND/OR, and 2-4 core concepts. Keep \
     it under 25 words.

4) Recommend librarians. From the LIBRARIAN ROSTER provided below the \
   catalog, choose 1-3 people best placed to help this patron with this \
   specific topic, matching on the subject areas in their titles (e.g. \
   "Librarian for STEM Research" for an engineering topic). Put their roster \
   ids in `librarians`, best match first.
   - Use ONLY ids that appear in the roster. If nobody is a reasonable \
     subject match, return an empty list rather than guessing.
   - Never output names, emails, phone numbers, or offices — the system \
     attaches those from the roster.

Research schema:

{
  "mode": "research",
  "enhanced_query": "...",
  "recommendations": [
    {
      "name": "<exact catalog name>",
      "refined_description": "...",
      "why_it_fits": "...",
      "search_string": "..."
    },
    ... 5 total
  ],
  "librarians": [<roster id>, ...]
}

────────────────────────────────────────────────────────────────────────────
MODE 2 — `chat`
────────────────────────────────────────────────────────────────────────────
Use for short conversational replies that do NOT require a new set of 5 \
databases. Examples:
- Follow-up questions about the previous recommendations \
  ("which of these covers open access?", "what's the difference between the \
  first two?", "how do I cite from JSTOR?")
- Clarifying questions from the user
- Access, citation, or "how do I use this" questions
- Greetings, thanks, small talk

Keep the reply short: 1-3 short paragraphs, plain prose. You may reference \
databases by name from the previous turn. Do not construct URLs. Do not \
produce Markdown formatting like headings or code fences.

Chat schema:

{
  "mode": "chat",
  "text": "..."
}

────────────────────────────────────────────────────────────────────────────
Universal rules
────────────────────────────────────────────────────────────────────────────
- Output MUST be a single JSON object matching one of the two schemas above.
- No commentary outside the JSON. No markdown fences.
- Never construct URLs — the frontend handles links.
- If the FIRST user message is a genuine research topic, use research mode. \
  If it is a greeting or how-does-this-work question, chat mode.
- When in doubt between the two modes, prefer research mode when the user has \
  described a topic; prefer chat mode when they are asking about existing \
  recommendations or how to use the tool.
"""


# ── Rate limiting ────────────────────────────────────────────────────────────
# In-memory per-IP sliding window. Fine for a single instance (App Runner runs
# one uvicorn worker here); would need a shared store (e.g. Redis) if this is
# ever scaled to multiple instances behind the load balancer.
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 12

_request_log: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    """Best-effort client IP. App Runner sits behind a proxy, so prefer
    X-Forwarded-For (its first entry is the original client) when present."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(client_ip: str) -> bool:
    """Return True if this request is allowed, False if the client has
    exceeded RATE_LIMIT_MAX_REQUESTS within the last RATE_LIMIT_WINDOW_SECONDS."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    log = _request_log[client_ip]
    while log and log[0] < window_start:
        log.popleft()
    if len(log) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    log.append(now)
    return True


# ── Pre-model abuse / off-topic screening ────────────────────────────────────
# Catches the most obvious prompt-injection and "do my homework for me"
# requests before spending a model call on them. This is a coarse net, not a
# security boundary -- the system prompt's own rules are the real defense --
# but it's free to run and avoids paying for a Claude call on clear-cut cases.
_INJECTION_PATTERNS = [
    re.compile(r"\bignore (all |any |the )?(previous|prior|above|earlier) instructions\b", re.I),
    re.compile(r"\bdisregard (all |any |the )?(previous|prior|above|earlier)\b", re.I),
    re.compile(r"\b(reveal|show|print|repeat) (your |the )?(system prompt|instructions)\b", re.I),
    re.compile(r"\bwhat (are|is) your (system )?(prompt|instructions)\b", re.I),
    re.compile(r"\byou are now\b.{0,30}\b(dan|jailbroken|unrestricted|no rules)\b", re.I),
    re.compile(r"\bact as (if you (were|are) )?an? (unrestricted|jailbroken)\b", re.I),
]

_HOMEWORK_PATTERNS = [
    re.compile(r"\bwrite (my|me an?|a full|a complete)\b.{0,20}\b(essay|paper|assignment|thesis)\b", re.I),
    re.compile(r"\bdo my (homework|assignment|essay|paper)\b", re.I),
    re.compile(r"\b(give|send|email) me the (full|entire|complete) (text|pdf|article)\b", re.I),
    re.compile(r"\bcomplete my (assignment|paper|essay|thesis)\b", re.I),
]

_SCREEN_INJECTION_MESSAGE = (
    "I'm built specifically to recommend Vanderbilt Libraries databases for research topics, "
    "and I can't share internal instructions or take on a different role. Tell me what you're "
    "researching and I'll suggest relevant databases."
)

_SCREEN_HOMEWORK_MESSAGE = (
    "I help you find databases to research a topic -- I don't write papers, complete assignments, "
    "or provide full-text copies of articles. Tell me your research topic and I'll point you to "
    "databases where you can find and read the sources yourself."
)


def _screen_message(text: str) -> str | None:
    """Return a canned redirect if `text` matches an obvious abuse pattern, else None."""
    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            return _SCREEN_INJECTION_MESSAGE
    for pat in _HOMEWORK_PATTERNS:
        if pat.search(text):
            return _SCREEN_HOMEWORK_MESSAGE
    return None


# The OA assistant has a different job, so it gets its own canned replies.
_OA_SCREEN_MESSAGES = {
    _SCREEN_INJECTION_MESSAGE: (
        "I'm built to match authors with journals under Vanderbilt's open-access agreements, and I "
        "can't share internal instructions or take on a different role. Describe your manuscript "
        "topic, or name a journal you'd like something similar to."
    ),
    _SCREEN_HOMEWORK_MESSAGE: (
        "I help find journals to submit to -- I don't write, edit or submit manuscripts. Tell me "
        "about your manuscript's topic and I'll suggest journals with a Vanderbilt agreement."
    ),
}


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            return content if isinstance(content, str) else ""
    return ""


def _file_date(path: Path) -> date | None:
    m = re.search(r"_(\d{4})(\d{2})(\d{2})\.", path.name)
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except ValueError:
        return None


def _build_catalog(raw: list[dict]) -> tuple[str, dict[str, dict]]:
    """Format raw A-Z records for the prompt and build the name lookup.

    Returns:
        catalog_text: formatted text block for the Claude system prompt.
        by_name: case-insensitive name -> record lookup for URL enrichment.
    """
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


def _load_catalog() -> tuple[str, dict[str, dict], int, date | None]:
    """Load the newest databases_YYYYMMDD.jsonl file baked into the image."""
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

    text, by_name = _build_catalog(raw)
    return text, by_name, len(raw), _file_date(candidates[0])


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


def _extract_partial_field(buf: str, field: str) -> str | None:
    """Best-effort extraction of a top-level string field's value from a
    possibly-incomplete JSON object, for a live "typing" preview while
    streaming. Returns the decoded text read so far, or None if the field
    hasn't started yet. This is only ever used for the live preview -- the
    authoritative value always comes from parsing the complete JSON once the
    stream ends, so any drift here just gets corrected at the end, never
    shown as final."""
    marker = f'"{field}"'
    idx = buf.find(marker)
    if idx == -1:
        return None
    i = idx + len(marker)
    while i < len(buf) and buf[i] in " \t\n\r:":
        i += 1
    if i >= len(buf) or buf[i] != '"':
        return None
    i += 1
    out: list[str] = []
    simple_escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
    while i < len(buf):
        c = buf[i]
        if c == "\\":
            if i + 1 >= len(buf):
                break  # incomplete escape sequence -- wait for more data
            nc = buf[i + 1]
            if nc in simple_escapes:
                out.append(simple_escapes[nc])
                i += 2
                continue
            if nc == "u" and i + 6 <= len(buf):
                try:
                    out.append(chr(int(buf[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    break
            break  # unknown/incomplete escape -- stop here
        if c == '"':
            break  # field value is complete
        out.append(c)
        i += 1
    return "".join(out)


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


@asynccontextmanager
async def _lifespan(_app):
    _refresher.start()
    yield
    _refresher.stop()


app = FastAPI(title="Vanderbilt Database Recommender API", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_catalog_text, _by_name, _catalog_total, _catalog_date = _load_catalog()
_staff_path = staff.latest_snapshot(_DATA_DIR)
_staff_date = _file_date(_staff_path) if _staff_path else None
_staff = staff.load_snapshot(_staff_path) if _staff_path else []
_staff_by_id = {p["id"]: p for p in _staff}
_client = anthropic.Anthropic()
_journals_path = journals.latest_snapshot(_DATA_DIR)
_journals_date = _file_date(_journals_path) if _journals_path else None
_oa = (
    OpenAccessAssistant(_client, journals.JournalIndex(journals.load_snapshot(_journals_path)))
    if _journals_path else None
)


def _build_system() -> list[dict]:
    blocks = [
        {"type": "text", "text": SYSTEM_PROMPT},
        {"type": "text", "text": _catalog_text, "cache_control": {"type": "ephemeral"}},
    ]
    if _staff:
        blocks.append(
            {"type": "text", "text": staff.format_roster(_staff), "cache_control": {"type": "ephemeral"}}
        )
    return blocks


_system = _build_system()


# ── Background data refresh ──────────────────────────────────────────────────
# Each apply function swaps in already-fetched, already-validated data in one
# step; requests in flight keep using whatever they already read.

def _apply_catalog(raw: list[dict]) -> None:
    global _catalog_text, _by_name, _catalog_total, _system
    text, by_name = _build_catalog(raw)
    _catalog_text, _by_name, _catalog_total = text, by_name, len(raw)
    _system = _build_system()


def _apply_staff(people: list[dict]) -> None:
    global _staff, _staff_by_id, _system
    _staff, _staff_by_id = people, {p["id"]: p for p in people}
    _system = _build_system()


def _apply_journals(items: list[dict]) -> None:
    if _oa is not None:
        _oa.set_index(journals.JournalIndex(items))


def _fetch_staff() -> list[dict]:
    return staff.fetch_staff(overrides=staff.load_overrides(_DATA_DIR))


_refresher = Refresher([
    Source(
        "databases", _catalog_date, catalog.fetch_databases,
        validate=lambda new: len(new) > 100 and len(new) >= 0.8 * _catalog_total,
        apply=_apply_catalog,
        enabled=catalog.credentials_available(),
        disabled_reason="SPRINGSHARE_CLIENT_ID / SPRINGSHARE_CLIENT_SECRET not set",
    ),
    Source(
        "librarians", _staff_date, _fetch_staff,
        validate=lambda new: len(new) >= max(20, 0.6 * len(_staff)),
        apply=_apply_staff,
    ),
    Source(
        "journals", _journals_date, journals.fetch_journals,
        validate=lambda new: _oa is not None and len(new) >= 0.8 * len(_oa.index.journals),
        apply=_apply_journals,
        enabled=_oa is not None,
        disabled_reason="no journal snapshot found",
    ),
])


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


_LIBRARIAN_FIELDS = (
    "id", "name", "title", "unit", "email", "phone", "office",
    "profile_url", "connect_url", "guides_url",
)


def _resolve_librarians(raw_ids) -> list[dict]:
    """Turn model-chosen roster ids into full contact records. Anything that
    isn't a real roster id is dropped, and all contact details come from the
    scraped directory, never from model text."""
    if not isinstance(raw_ids, list):
        return []
    out: list[dict] = []
    seen: set[int] = set()
    for rid in raw_ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        person = _staff_by_id.get(rid)
        if person is None or rid in seen:
            continue
        seen.add(rid)
        out.append({k: person.get(k, "") for k in _LIBRARIAN_FIELDS})
        if len(out) >= 3:
            break
    return out


def _finish_research_mode(parsed: dict, raw_text: str, messages: list[dict]) -> dict:
    """Shared enrichment/verification/retry pipeline for research-mode
    responses, used by both the buffered and streaming endpoints."""
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
            f"Return exactly {needed} REPLACEMENT recommendation(s) in research "
            f"mode, using database names that appear verbatim in the catalog."
            f"{exclude_clause} Same research schema; the `recommendations` array "
            f"must have exactly {needed} items."
        )
        followup_messages = list(messages) + [
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
    librarians = _resolve_librarians(parsed.get("librarians"))

    # Synthesize an assistant message that reflects the FINAL list, so multi-turn
    # history stays consistent with what the user actually saw.
    synthesized = json.dumps({
        "mode": "research",
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
        "librarians": [p["id"] for p in librarians],
    })

    return {
        "mode": "research",
        "enhanced_query": enhanced_query,
        "recommendations": final,
        "librarians": librarians,
        "raw_assistant_message": synthesized,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "catalog_size": len(_by_name),
        "librarians": len(_staff),
        "journals": len(_oa.index.journals) if _oa else 0,
        "data": _refresher.status(),
    }


@app.post("/chat")
def chat(req: ChatRequest, request: Request):
    if not _check_rate_limit(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a moment and try again.")

    screened = _screen_message(_last_user_text(req.messages))
    if screened:
        synthesized = json.dumps({"mode": "chat", "text": screened})
        return {"mode": "chat", "text": screened, "raw_assistant_message": synthesized}

    try:
        parsed, raw_text = _call_llm(req.messages)
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Claude API error: {exc}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Model returned invalid JSON: {exc}",
        ) from exc

    # Fall back to research mode when the model omits a mode field (for
    # backward-compat with any pre-mode responses in history).
    mode = parsed.get("mode") or ("research" if "recommendations" in parsed else "chat")

    if mode == "chat":
        text = parsed.get("text", "").strip()
        synthesized = json.dumps({"mode": "chat", "text": text})
        return {
            "mode": "chat",
            "text": text,
            "raw_assistant_message": synthesized,
        }

    return _finish_research_mode(parsed, raw_text, req.messages)


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


@app.post("/chat/stream")
def chat_stream(req: ChatRequest, request: Request):
    """Streaming counterpart to /chat. Emits newline-delimited SSE events:
      {"type": "delta", "text": "..."}   -- incremental text (chat mode only)
      {"type": "final", ...}             -- the same shape /chat returns
      {"type": "error", "error": "..."}  -- something went wrong

    Research-mode responses never stream token-by-token -- there is no
    top-level "text" field to preview, and the recommendations only become
    trustworthy after the same catalog-verification/retry pass /chat uses, so
    they are only ever delivered as one "final" event, same as before. Only
    chat-mode replies get the live "typing" effect.
    """
    client_ip = _client_ip(request)

    def single_event(data: dict):
        yield _sse(data)

    if not _check_rate_limit(client_ip):
        return StreamingResponse(
            single_event({"type": "error", "error": "Too many requests. Please wait a moment and try again."}),
            media_type="text/event-stream",
        )

    screened = _screen_message(_last_user_text(req.messages))
    if screened:
        synthesized = json.dumps({"mode": "chat", "text": screened})
        return StreamingResponse(
            single_event({
                "type": "final", "mode": "chat", "text": screened,
                "raw_assistant_message": synthesized,
            }),
            media_type="text/event-stream",
        )

    def generate():
        full_text = ""
        sent_len = 0
        try:
            with _client.messages.stream(
                model="claude-opus-4-7",
                max_tokens=1500,
                system=_system,
                messages=req.messages,
            ) as stream:
                for delta in stream.text_stream:
                    full_text += delta
                    extracted = _extract_partial_field(full_text, "text")
                    if extracted is not None and len(extracted) > sent_len:
                        chunk = extracted[sent_len:]
                        sent_len = len(extracted)
                        yield _sse({"type": "delta", "text": chunk})
        except anthropic.APIError as exc:
            yield _sse({"type": "error", "error": f"Claude API error: {exc}"})
            return

        try:
            parsed = _parse_json_response(full_text)
        except (json.JSONDecodeError, ValueError) as exc:
            yield _sse({"type": "error", "error": f"Model returned invalid JSON: {exc}"})
            return

        mode = parsed.get("mode") or ("research" if "recommendations" in parsed else "chat")

        if mode == "chat":
            text = parsed.get("text", "").strip()
            synthesized = json.dumps({"mode": "chat", "text": text})
            yield _sse({"type": "final", "mode": "chat", "text": text, "raw_assistant_message": synthesized})
            return

        result = _finish_research_mode(parsed, full_text, req.messages)
        yield _sse({"type": "final", **result})

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/oa/chat")
def oa_chat(req: ChatRequest, request: Request):
    """Open Access Assistant: journal recommendations under Vanderbilt's
    publisher agreements (see backend/oa.py)."""
    if not _check_rate_limit(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a moment and try again.")
    if _oa is None:
        raise HTTPException(status_code=503, detail="Journal data is not loaded on this server.")

    screened = _screen_message(_last_user_text(req.messages))
    if screened:
        screened = _OA_SCREEN_MESSAGES.get(screened, screened)
        return {
            "mode": "chat", "text": screened,
            "raw_assistant_message": json.dumps({"action": "chat", "reply": screened}),
        }
    try:
        return _oa.respond(req.messages)
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Claude API error: {exc}") from exc
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=502, detail=f"Model returned unusable output: {exc}") from exc
