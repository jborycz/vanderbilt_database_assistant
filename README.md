# Vanderbilt Libraries Database Recommender

A conversational AI assistant that recommends Vanderbilt Libraries databases based on your research topic. Describe what you're researching and it suggests up to 5 relevant databases from the Vanderbilt A–Z list, ranked by fit and with links to access them.

## What it does

1. Takes a free-text research question or topic from the user.
2. Enriches the query internally (adds synonyms, related disciplines, likely content types).
3. Matches against the full Vanderbilt Libraries database catalog (fetched from the LibApps API).
4. Returns ranked recommendations with a short explanation for each and a link to the Vanderbilt A–Z database page.
5. Supports follow-up questions to refine recommendations.
6. Saves a markdown transcript of each session to `outputs/`.

The catalog is automatically refreshed from the LibApps API if the local copy is more than 7 days old.

## Requirements

- Python 3.12+
- An `ANTHROPIC_API_KEY` environment variable set to a valid Anthropic API key
- The `anthropic` Python package (`pip install -r requirements.txt`)

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here
```

The assistant ships with a recent catalog snapshot in `data/`. If it is stale, the first run will automatically fetch a fresh copy.

To manually refresh the catalog at any time:

```bash
python weekly_update.py
```

## Usage

**Interactive mode** — start a conversation:

```bash
python vanderbilt_database_assistant.py
```

**With an opening query** — skip the first prompt:

```bash
python vanderbilt_database_assistant.py "I'm researching the economic impact of climate change"
```

**With a specific catalog file:**

```bash
python vanderbilt_database_assistant.py data/databases_20260513.jsonl "nursing informatics"
```

Type `quit`, `exit`, or press `Ctrl-C` to end the session. Sessions time out automatically after 60 seconds of inactivity.

## Output

Each session is saved as a markdown file in `outputs/`:

```
outputs/user_output_YYYYMMDD_HHMMSS.md
```

The file contains the full conversation transcript.

## Project structure

```
vanderbilt_database_assistant.py   # main assistant (run this)
weekly_update.py                   # fetch a fresh catalog from the LibApps API
api_get_json_clean.py              # LibApps API client and data cleaning helpers
requirements.txt                   # Python dependencies
data/                              # catalog snapshots (JSONL, dated)
outputs/                           # saved session transcripts
```
