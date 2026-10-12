"""Open Access Assistant: recommend journals under Vanderbilt's publisher agreements.

Flow for each user turn:
  1. A small model call reads the conversation and returns structured intent
     (topic, subjects, keywords, an optional reference journal, preferences) -
     or answers a plain question directly.
  2. Code shortlists ~40 journals from the full Open Journal Finder index.
  3. A second model call picks and explains the best few from that shortlist.
  4. The server builds each card from the data itself. The model only supplies
     ids and one-sentence reasons, so metrics, prices and discounts can never be
     invented, and ids outside the shortlist are discarded.
"""

import json
import os
import re

import anthropic

from backend import journals

INTENT_MODEL = os.environ.get("OA_INTENT_MODEL", "claude-haiku-4-5-20251001")
PICK_MODEL = os.environ.get("OA_PICK_MODEL", "claude-sonnet-5")

OPEN_JOURNAL_FINDER_URL = journals.FINDER_BASE
AUTHOR_SERVICES_URL = "https://researchguides.library.vanderbilt.edu/AuthorServices/vuoaagreements"

INTENT_SYSTEM = """\
You help Vanderbilt authors find journals to publish in under Vanderbilt's \
open-access publisher agreements (the Open Journal Finder). You are shown the \
conversation so far. Decide what to do this turn and reply with ONE JSON \
object and nothing else.

Actions:
- "recommend": the author described a manuscript or research topic, named a \
journal they would like something similar to, or asked to adjust earlier \
recommendations (e.g. "only top-quartile", "cheaper", "fully open access", \
"something like X").
- "chat": a question that needs no new recommendations (what is an APC? how do \
these agreements work? am I eligible? a greeting). Answer in `reply`: 1-3 \
short plain-prose paragraphs. Do NOT state specific Vanderbilt policy details, \
funding amounts or eligibility rules. Never say publishing is free or \
guaranteed: the benefit varies by publisher (APC waived, discounted, capped, or \
exhausted for the year) and depends on author eligibility. Point to the \
Vanderbilt Open Author Services guide and the Open Journal Finder as the \
authoritative sources.

Schema:
{
  "action": "recommend" | "chat",
  "reply": "<chat only>",
  "topic": "<one sentence on the manuscript topic, merged with earlier turns>",
  "subjects": [<1-4 terms copied exactly from ALLOWED SUBJECTS>],
  "keywords": [<4-10 short lowercase words or phrases likely to appear in the \
titles or aims of suitable journals>],
  "reference_journal": "<exact journal name the author wants something similar \
to, else null>",
  "preferences": {
    "quartile_max": <1-4, or null>,
    "waiver_preferred": <true/false>,
    "oa_type": "any" | "fully_oa" | "hybrid",
    "max_apc": <number, or null>
  }
}

Rules:
- Carry forward earlier topic, subjects, keywords and preferences unless the \
author changes them.
- "Impact factor" or "high impact"/"top tier" -> quartile_max 1 (journals are \
ranked by SJR quartile; Clarivate impact factors are not available).
- If the author asks about acceptance rates, still recommend; acceptance rates \
are not available in this tool.
- If a reference journal is named, also include the subjects it covers.
"""

PICK_SYSTEM = """\
You choose journals for a Vanderbilt author from a SHORTLIST of journals that \
currently have a Vanderbilt publisher agreement. Pick the best 5 (fewer if \
fewer genuinely fit), ordered best first, judged mainly on how well each \
journal's scope fits the author's topic.

Rules:
- Use ONLY ids that appear in the shortlist.
- For each pick, `why` is ONE sentence naming the specific overlap between the \
journal and the topic, plus a tradeoff if relevant (e.g. broader scope, lower \
quartile).
- If a reference journal is given, favor journals of similar scope and standing.
- Do NOT state any numbers (impact, prices, discounts, acceptance rates); the \
system displays the facts itself.
- `intro` is 1-2 plain sentences summarising what you matched on.

Reply with ONE JSON object and nothing else:
{"intro": "...", "picks": [{"id": "<shortlist id>", "why": "..."}]}
"""


def _parse_json(text: str) -> dict:
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


def _clean_prefs(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}

    def num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    q = num(raw.get("quartile_max"))
    oa = raw.get("oa_type") if raw.get("oa_type") in ("any", "fully_oa", "hybrid") else "any"
    return {
        "quartile_max": int(q) if q and 1 <= q <= 4 else None,
        "waiver_preferred": bool(raw.get("waiver_preferred")),
        "oa_type": oa,
        "max_apc": num(raw.get("max_apc")),
    }


class OpenAccessAssistant:
    def __init__(self, client: anthropic.Anthropic, index: journals.JournalIndex):
        self.client = client
        self.index = index
        self._intent_system = INTENT_SYSTEM + "\nALLOWED SUBJECTS: " + "; ".join(index.subject_terms)

    def _call(self, model: str, system: str, messages: list[dict], max_tokens: int) -> dict:
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages
        )
        return _parse_json("".join(b.text for b in resp.content if b.type == "text"))

    def respond(self, messages: list[dict]) -> dict:
        intent = self._call(INTENT_MODEL, self._intent_system, messages, 700)

        if intent.get("action") == "chat":
            text = str(intent.get("reply") or "").strip() or (
                "I can recommend journals with Vanderbilt open-access agreements. "
                "Describe your manuscript topic, or name a journal you'd like something similar to."
            )
            return {
                "mode": "chat",
                "text": text,
                "raw_assistant_message": json.dumps({"action": "chat", "reply": text}),
            }

        topic = str(intent.get("topic") or "").strip()
        subjects = [s for s in (intent.get("subjects") or []) if isinstance(s, str)]
        keywords = [k for k in (intent.get("keywords") or []) if isinstance(k, str)]
        prefs = _clean_prefs(intent.get("preferences"))
        ref_name = intent.get("reference_journal") if isinstance(intent.get("reference_journal"), str) else None

        notes: list[str] = []
        reference = self.index.find_reference(ref_name) if ref_name else None
        if ref_name and reference is None:
            notes.append(
                f"“{ref_name}” isn't among the journals with a current Vanderbilt agreement, "
                "so these matches are based on your topic instead."
            )

        shortlist = self.index.shortlist(subjects, keywords, prefs, reference, n=40)
        if not shortlist:
            msg = (
                "I couldn't find journals with a current Vanderbilt agreement that match that. "
                "Try describing the topic more broadly, relaxing a preference (like quartile or cost), "
                "or search the Open Journal Finder directly."
            )
            return {
                "mode": "chat",
                "text": msg,
                "finder_search_url": journals.finder_search_url(" ".join(keywords[:3]) or topic),
                "raw_assistant_message": json.dumps({"action": "chat", "reply": msg}),
            }

        ref_line = "none"
        if reference:
            subs = ", ".join(sorted({s["term"] for s in reference["subjects"]}))
            q = f"Q{reference['quartile']}" if reference["quartile"] else "n/a"
            ref_line = f"{reference['name']} (subjects: {subs}; SJR quartile {q})"
        pick_prompt = (
            f"AUTHOR TOPIC: {topic}\nREFERENCE JOURNAL: {ref_line}\n"
            f"PREFERENCES: {json.dumps(prefs)}\n\nSHORTLIST:\n{self.index.format_shortlist(shortlist)}"
        )
        picked = self._call(PICK_MODEL, PICK_SYSTEM, [{"role": "user", "content": pick_prompt}], 900)

        by_id = {j["id"]: j for j in shortlist}
        cards, seen = [], set()
        for pick in picked.get("picks") or []:
            jid = pick.get("id") if isinstance(pick, dict) else None
            if jid in by_id and jid not in seen:
                seen.add(jid)
                card = journals.card_view(by_id[jid])
                card["why"] = str(pick.get("why") or "").strip()
                cards.append(card)
            if len(cards) >= 6:
                break
        if len(cards) < 3:  # model returned too little usable output - fall back to the ranking
            for j in shortlist:
                if j["id"] not in seen and len(cards) < 5:
                    seen.add(j["id"])
                    card = journals.card_view(j)
                    card["why"] = ""
                    cards.append(card)

        intro = str(picked.get("intro") or "").strip()
        history = {
            "action": "recommend", "topic": topic, "subjects": subjects, "keywords": keywords,
            "reference_journal": ref_name, "preferences": prefs,
            "recommended": [c["name"] for c in cards],
        }
        return {
            "mode": "oa",
            "intro": intro,
            "notes": notes,
            "topic": topic,
            "reference": {"name": reference["name"], "finder_url": journals.journal_url(reference["id"])} if reference else None,
            "journals": cards,
            "finder_search_url": journals.finder_search_url(" ".join(keywords[:3]) or topic),
            "raw_assistant_message": json.dumps(history),
        }
