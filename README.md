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

## The web app

The hosted app (`docs/` frontend + `backend/` API) has two assistants, each with
its own saved chats in the sidebar:

- **Vanderbilt Database Assistant** — recommends five Vanderbilt databases with
  ready-to-paste search strings and ends each result with an **Ask a Librarian**
  card (subject librarians matched to the topic: title, email, phone, office).
- **Open Access Assistant** — matches a manuscript topic, or a journal you'd like
  something similar to, with journals that have a current Vanderbilt open-access
  publisher agreement. Cards show scope fit, SJR quartile and other impact
  metrics, the fee waiver or discount and the resulting price, and link to the
  [Open Journal Finder](https://vanderbilt.openjournalfinder.com) and the
  [Vanderbilt Open Author Services guide](https://researchguides.library.vanderbilt.edu/AuthorServices/vuoaagreements).
  Acceptance rates and Clarivate impact factors are not in the source data.

An **Export** menu copies, downloads or prints the current chat.

### Run it locally

```bash
export ANTHROPIC_API_KEY=<your real key>
./run-local.sh            # then open http://localhost:8123  (--open does it for you)
```

The first run creates a private `.venv` and installs dependencies. Ctrl+C stops
both servers. Background data refresh is off locally (`DATA_REFRESH=1` turns it on).

### Reference data and how it stays fresh

| Data | Source | Snapshot in `data/` |
|---|---|---|
| Database catalog | Springshare LibApps API | `databases_YYYYMMDD.jsonl` |
| Librarian roster | library.vanderbilt.edu/staff (scraped) | `staff_YYYYMMDD.json` (+ `staff_overrides.json` for hand edits) |
| Open-access journals | Open Journal Finder public API | `journals_YYYYMMDD.json.gz` |

`python3 refresh_data.py [databases|staff|journals]` rebuilds the snapshots
(databases needs `SPRINGSHARE_CLIENT_ID`/`SPRINGSHARE_CLIENT_SECRET` in the
environment or a `.env` file; journals take about 3 minutes). `./redeploy.sh
--refresh-data` does that and deploys in one step.

The hosted backend also refreshes itself: a background thread checks every 6 hours
and re-fetches any source older than 14 days (`DATA_MAX_AGE_DAYS`,
`DATA_CHECK_INTERVAL_HOURS`; `DATA_REFRESH=0` disables it). New data is swapped in
only after a sanity check, failures back off and keep the old data, and `/health`
reports each source's date. The databases source needs the Springshare credentials
in the container's environment (see AWS_DEPLOYMENT.md). Refreshed data lives in
memory only, so it is lost on restart; ship fresh snapshots in the image too.

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

## Running the backend in Docker

The FastAPI backend in `backend/` can be run as a container. Requires Docker Desktop (or any Docker daemon) running locally.

**Build the image:**

```bash
docker build -t vanderbilt-db-assistant .
```

**Run it (foreground, logs in terminal, `Ctrl-C` to stop):**

```bash
docker run --rm -p 8080:8080 -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" vanderbilt-db-assistant
```

**Run it (detached / background):**

```bash
docker run -d --name vdba -p 8080:8080 -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" vanderbilt-db-assistant
```

**Smoke-test it:**

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

**Manage the detached container:**

```bash
docker logs -f vdba     # tail logs
docker stop vdba        # stop
docker rm vdba          # remove
```

The container listens on `8080` by default; override with `-e PORT=9000 -p 9000:9000`.

**Create and activate a virtual environment inside the container:**

Open an interactive shell in the running container:

```bash
docker exec -it vdba bash
```

From inside the container, create the venv (one-time):

```bash
python -m venv vdba
```

Activate it (each time you enter the container):

```bash
source vdba/bin/activate
```

Your prompt will change to `(vdba) root@<container-id>:/app#`, confirming the
environment is active. Run `deactivate` to exit it, or `exit` to leave the
container shell.

## Deploying to AWS

The live site (https://jborycz.github.io/vanderbilt_database_assistant/) runs
its backend on AWS App Runner, built from this same Docker image. See
[AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md) for the one-time setup. Once that's
done, redeploying after a code change is one command:

```bash
./redeploy.sh
```

This rebuilds the image, pushes it to ECR, and tells App Runner to redeploy —
see the script for the individual steps if you'd rather run them by hand.
`./redeploy.sh --refresh-data` refreshes the reference data first.

## Project structure

```
vanderbilt_database_assistant.py   # main assistant (run this)
weekly_update.py                   # fetch a fresh catalog from the LibApps API
api_get_json_clean.py              # LibApps API client and data cleaning helpers
backend/                           # FastAPI app: main.py, oa.py (Open Access Assistant),
                                   #   journals.py, staff.py, catalog.py, refresher.py
docs/index.html                    # the web page (GitHub Pages)
run-local.sh                       # run backend + web page locally
refresh_data.py                    # rebuild the dated snapshots in data/
redeploy.sh                        # rebuild + push + redeploy the backend to AWS
deploy-env.sh                      # AWS shell variables for redeploy.sh (gitignored)
requirements.txt                   # Python dependencies
data/                              # dated snapshots: catalog, librarian roster, journals
outputs/                           # saved session transcripts
```
