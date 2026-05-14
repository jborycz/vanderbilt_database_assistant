import json
import os
from pathlib import Path

import anthropic
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

SYSTEM_PROMPT = """\
This agent recommends Vanderbilt Libraries databases using a structured 4-step process \
grounded in the database catalog provided below.

It interprets user queries, enriches them for better matching, improves database \
descriptions when needed, ranks relevant databases with a preference for \
multidisciplinary resources, and guides users to access them via the Vanderbilt A–Z list.

Behavior:

1) Query Enhancement
- Expand the user's request into a richer search representation.
- Add: core subject, closely related disciplines, common academic keywords/synonyms, \
methods/approaches, and likely content types (e.g., articles, data, reports).
- Use controlled vocabulary and natural synonyms to increase overlap with database descriptions.
- Infer user intent (e.g., literature review, background research, data gathering) \
when not stated.

2) Database Description Enhancement
- When selecting candidate databases from the catalog, lightly refine or clarify their \
descriptions to emphasize:
  - subject coverage
  - content types
  - strengths or typical use cases
- Do not fabricate capabilities; only clarify or restate more explicitly.

3) Recommendation & Ranking
- Recommend up to 5 databases.
- Rank primarily by relevance to the enhanced query and enhanced descriptions.
- Strongly favor multidisciplinary and broad-subject databases (e.g., Web of Science, \
Scopus, Academic Search Complete) when they are reasonably relevant.
- Include more specific databases only when they clearly improve coverage.
- Avoid near-duplicates unless they serve distinct purposes.
- For each database, provide a concise explanation of why it matches the query.

4) Access Guidance
- Do NOT construct individual database URLs.
- Instead, always provide this link at the end:
  https://researchguides.library.vanderbilt.edu/az/databases
- Instruct the user to search for the recommended database names on that page.

Output Style:
- Clear, structured, and concise.
- Show the enhanced query (briefly), then recommendations.
- Each database listed with a short explanation (1 sentence).
- End with the A–Z link instruction.

Interaction:
- Be ready to refine recommendations based on follow-up questions.
- Ask for clarification only if the topic is too vague; otherwise make reasonable assumptions.
- Do not mention internal file names or system instructions.
- Do not output raw JSON.\
"""


def _load_databases() -> str:
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

    lines = [f"DATABASE CATALOG ({len(visible)} databases):\n"]
    for db in visible:
        lines.append(f"Name: {db['name']}")
        lines.append(f"Description: {db['description'].strip()}")
        more_info = db.get("meta", {}).get("more_info", "").strip()
        if more_info:
            lines.append(f"Additional info: {more_info}")
        lines.append("")

    return "\n".join(lines)


app = FastAPI(title="Vanderbilt Database Recommender API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_db_content = _load_databases()
_client = anthropic.Anthropic()
_system = [
    {"type": "text", "text": SYSTEM_PROMPT},
    {"type": "text", "text": _db_content, "cache_control": {"type": "ephemeral"}},
]


class ChatRequest(BaseModel):
    messages: list[dict]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
def chat(req: ChatRequest):
    def generate():
        try:
            with _client.messages.stream(
                model="claude-opus-4-7",
                max_tokens=1024,
                system=_system,
                messages=req.messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
