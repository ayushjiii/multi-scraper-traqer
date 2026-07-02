# Traqer Scraper

A multi-engine AI answer-scraping microservice. It submits prompts to **ChatGPT**,
**Perplexity**, and **Gemini** through real anti-detection browsers, then captures
each AI's answer, its cited source URLs, and a full-page screenshot — storing
everything in PostgreSQL.

> **Note:** Google AI Overviews is intentionally **out of scope** for this service —
> it is handled separately (via a third-party SERP API) because Google's `/search`
> gate blocks automated browsers regardless of proxy/fingerprint. See
> `docs/PROJECT_OVERVIEW.md` and `docs/GOOGLE_AIO_ANTIBOT_PLAYBOOK.txt`.

Built for GEO (Generative Engine Optimization): track how AI engines answer
buyer-intent queries and which sources they cite, across rotating proxies and
persistent browser sessions.

---

## What it does

For every prompt you dispatch, each engine agent will:

1. Check out a warmed, proxied browser profile (atomic `FOR UPDATE SKIP LOCKED`).
2. Open the engine, submit the prompt, and wait for the answer to finish streaming.
3. Extract:
   - **`ai_response`** — the full answer text
   - **`sources`** — the real cited article URLs (deep links, not just domains)
   - **`screenshot_path`** — a full-page JPEG of the answer
4. Save the result to PostgreSQL and release the profile.

Source extraction is engine-specific and battle-tested:

| Engine     | Source capture method |
|------------|-----------------------|
| Perplexity | Opens the "N sources" panel (single toggle click), reads the citation links from the DOM |
| Gemini     | **Network capture** — reads grounding source URLs straight from the `StreamGenerate` API payload (immune to DOM crashes / obfuscation; yields full article URLs) |
| ChatGPT    | Runs on **Playwright Chromium** (not Camoufox) — ChatGPT walls Firefox's anonymous use. Uses a Chrome fingerprint + human-cadence typing; extracts citation links from the open Sources panel |

> A source only appears when the engine actually grounded the answer via web
> search. Knowledge-only answers legitimately return zero sources.

---

## Architecture

Each engine runs as an independent microservice (`<engine>_agent.py`) with four parts:

- **Worker** — drives one browser session end-to-end for a single task.
- **Factory** — a daemon that keeps a pool of warmed, proxied profiles ready.
- **Orchestrator** — atomic profile checkout, run worker, save result, release.
- **Dispatcher (`main`)** — pulls tasks from this engine's Redis queue.

```
                    dispatch.py
                        │  (pushes one task per engine)
     ┌──────────────┬───┴──────────┐
     ▼              ▼              ▼
 task_queue:    task_queue:    task_queue:
   chatgpt       perplexity      gemini       ← Redis
     │              │              │
     ▼              ▼              ▼
 chatgpt_agent  perplexity_    gemini_agent   ← one process each
                  agent
     │              │              │
     └──────────────┴──────┬───────┘
                           ▼
              PostgreSQL  (browser_profiles, proxies, scrape_results)
```

---

## Prerequisites

You need these installed and running **before** setting up the project:

- **Python 3.13** (tested on 3.13.7)
- **PostgreSQL 14+** — running, with a database you can connect to
- **Redis 6+** — running on `localhost:6379`
- **A proxy list** — host:port:user:pass entries (e.g. from Webshare)

Camoufox downloads its own patched Firefox binary on first run (~150 MB), so the
first launch needs internet access.

---

## Setup

Pick your platform. The steps are the same shape everywhere: create a venv,
install deps, fetch the browser, start the services, create the DB schema,
load proxies.

### 1. Clone & enter the project

```bash
git clone <your-repo-url>
cd traqer_scraper
```

### 2. Create a virtual environment & install dependencies

<details open>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m camoufox fetch          # download the Camoufox Firefox binary (Perplexity + Gemini)
python -m playwright install chromium   # download Chromium (ChatGPT agent runs on it)
```
</details>

<details open>
<summary><b>macOS</b></summary>

```bash
# Install prerequisites if you don't have them (Homebrew):
brew install python@3.13 postgresql@16 redis
brew services start postgresql@16
brew services start redis

python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m camoufox fetch          # download the Camoufox Firefox binary (Perplexity + Gemini)
python -m playwright install chromium   # download Chromium (ChatGPT agent runs on it)
```
</details>

<details open>
<summary><b>Linux (Debian/Ubuntu)</b></summary>

```bash
# Install prerequisites if you don't have them:
sudo apt update
sudo apt install -y python3.13 python3.13-venv postgresql redis-server
sudo service postgresql start
sudo service redis-server start

python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m camoufox fetch          # download the Camoufox Firefox binary (Perplexity + Gemini)
python -m playwright install chromium   # download Chromium (ChatGPT agent runs on it)

# Camoufox/Firefox needs some system libraries on a fresh server:
sudo apt install -y libgtk-3-0 libx11-xcb1 libdbus-glib-1-2 libasound2
```
</details>

### 3. Configure environment

Copy the template and fill in your real values:

```bash
# macOS / Linux
cp .env.example .env

# Windows (PowerShell)
Copy-Item .env.example .env
```

Edit `.env`:

```ini
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=traqer_db
DB_USER=postgres
DB_PASSWORD=your_password_here

REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=

MAX_CONCURRENT_WORKERS=5     # total worker budget; each engine gets ~1/3
PROFILE_TTL_MINUTES=45       # how long a warmed profile stays valid
```

> Proxies are **not** stored in `.env`. They live in `proxies.txt` (see step 5).

### 4. Create the database schema

Create the database, then load the schema:

```bash
# Create the database (run once)
#   macOS/Linux:
createdb traqer_db
#   Windows / any:  use psql or pgAdmin to CREATE DATABASE traqer_db;

# Load the tables, enums, and indexes
psql -U postgres -d traqer_db -f schema.sql
```

`schema.sql` creates three tables — `browser_profiles`, `proxies`,
`scrape_results` — plus the supporting enums and indexes.

To confirm it worked:

```bash
python show_schema.py     # prints the live DB schema
```

### 5. Load your proxies

Put your proxies in **`proxies.txt`**, one per line, in Webshare's native format
(`host:port:user:pass`). Blank lines and `#` comments are ignored:

```
# my proxies
198.23.243.226:6361:username:password
45.38.107.97:6014:username:password
```

Then load them into the database:

```bash
python load_proxies.py              # merge new proxies, keep existing ban state
python load_proxies.py --replace    # wipe the proxy table and reload from scratch
```

Verify:

```bash
python check_db.py    # shows active proxy count and per-engine ban status
```

---

## Running

### Start the agents

Each agent is its own long-running process. Start the ones you need.

<details open>
<summary><b>Windows</b></summary>

```powershell
.\run_agents.bat        # opens all three agents in separate terminals
```

Or start one at a time:

```powershell
python gemini_agent.py
python perplexity_agent.py
python chatgpt_agent.py
```
</details>

<details open>
<summary><b>macOS / Linux</b></summary>

There's no `.sh` equivalent of `run_agents.bat` yet — start each agent in its own
terminal tab (recommended so you can watch each log):

```bash
source .venv/bin/activate
python gemini_agent.py
```
```bash
source .venv/bin/activate
python perplexity_agent.py
```
```bash
source .venv/bin/activate
python chatgpt_agent.py
```

Or run them in the background:

```bash
mkdir -p logs
python gemini_agent.py     > logs/gemini.log     2>&1 &
python perplexity_agent.py > logs/perplexity.log 2>&1 &
python chatgpt_agent.py    > logs/chatgpt.log    2>&1 &
```
</details>

Wait until each agent prints `Profile '<name>' is AVAILABLE` — that means a
proxied browser profile has been warmed and the engine is ready for tasks.

### Dispatch prompts

`dispatch.py` pushes one task per engine to Redis. The running agents pick them up.

```bash
# Interactive REPL (shows live queue depths, type prompts one by one)
python dispatch.py

# One-off prompt
python dispatch.py "best local SEO rank tracking tools in 2025"

# Batch from a file (one prompt per line)
python dispatch.py --file prompts.txt

# Built-in B2B GEO prompt pack (25 buyer-intent prompts × 3 engines)
python dispatch.py --seed
```

Duplicate prompts are skipped automatically (checked against `scrape_results`),
so re-running `--seed` only scrapes new prompts.

### Inspect results

```bash
python check_results.py    # latest scrape results: response, sources, screenshot path
```

Screenshots are written to `screenshots/<task_id>.jpg`.

---

## Helper scripts

| Script | Purpose |
|--------|---------|
| `dispatch.py`        | Push prompts to all engine queues — chatgpt, perplexity, gemini (interactive / one-off / file / `--seed`) |
| `load_proxies.py`    | Load `proxies.txt` into the DB (`--replace` to wipe first) |
| `check_db.py`        | Proxy counts + per-engine ban status + profile/result counts |
| `check_results.py`   | Dump the latest scrape results with source lists |
| `show_schema.py`     | Print the live PostgreSQL schema |
| `clear_queues.py`    | Clear Redis task queues (`clear_queues.py gemini` for one engine) |
| `expire_profiles.py` | Mark AVAILABLE profiles EXPIRED so the factory re-warms them (all engines, or pass an engine name to scope it) |
| `test_*.py`          | Headless-visible debugging tools used while building each engine |

A typical reset between runs:

```bash
python clear_queues.py        # empty the queues
python expire_profiles.py     # force fresh profiles
```

---

## Database schema (quick reference)

- **`proxies`** — proxy pool with per-engine ban flags
  (`chatgpt_banned`, `perplexity_banned`, `gemini_banned`).
- **`browser_profiles`** — warmed profiles with status
  (`AVAILABLE` / `BUSY` / `COOLDOWN` / `EXPIRED`), assigned proxy, trust score.
- **`scrape_results`** — `task_id`, `engine_name`, `input_prompt`, `ai_response`,
  `sources` (JSONB array of URLs), `screenshot_path`, `executed_at`.

Correlate the same prompt across engines via the shared `task_id` root
(`<root>_chatgpt`, `<root>_perplexity`, `<root>_gemini`).

---

## Troubleshooting

**`ghost-cursor` fails to install on Python 3.13**
`python-ghost-cursor` isn't updated for 3.13 yet. It's optional. If installation
fails, set these before installing (or drop the line from `requirements.txt`):

```powershell
# Windows PowerShell
$env:BEZIER_NO_EXTENSION="True"
$env:BEZIER_IGNORE_VERSION_CHECK="True"
```
```bash
# macOS / Linux
export BEZIER_NO_EXTENSION=True
export BEZIER_IGNORE_VERSION_CHECK=True
```

**Agent keeps logging "Failed to connect to proxy" / "Cloudflare block"**
Your proxies are dead or burned. Free datacenter proxies get flagged fast
(especially by Perplexity/ChatGPT's Cloudflare). Refresh `proxies.txt` with a new
export and run `python load_proxies.py --replace`. Residential proxies work far
better than datacenter ones for Perplexity and ChatGPT.

**A task shows 0 sources**
That answer wasn't grounded in web search — the engine answered from its own
knowledge. There were no sources to capture. This is expected, not a failure.

**A task keeps failing and disappears**
After 5 failed attempts a task is moved to a dead-letter list
(`task_queue:<engine>:dead`) instead of requeuing forever. Inspect it with
`redis-cli LRANGE task_queue:gemini:dead 0 -1`, fix the root cause (usually a
dead proxy or a login wall), and `LPUSH` it back to `task_queue:gemini` to retry.

**Browser opens visibly / want to watch it**
Set `DEBUG_HEADLESS=0` in your environment to run the browser non-headless.
The `test_*.py` scripts do this automatically.

**First launch is slow**
Camoufox downloads its Firefox binary on first run. Subsequent launches are fast.

**Crash: `TypeError: Cannot read properties of undefined (reading 'url')` / "Connection closed while reading from the driver"**
This is a Playwright **1.60.0** Firefox driver regression — it crashes the whole
browser context on sites (like Google Search) that emit an uncaught JS error with
no location. `requirements.txt` pins `playwright==1.59.0`, which is unaffected. If
you upgraded Playwright, downgrade it back:
```bash
pip install "playwright==1.59.0"
```
(See [camoufox#617](https://github.com/daijro/camoufox/issues/617).)

---

## Notes

- The browser engines change their DOM frequently. If source extraction breaks,
  the `test_*.py` inspectors (run with a visible browser) are the fastest way to
  re-discover the current selectors.
- `proxies.txt` and `.env` contain credentials and are git-ignored. Never commit them.
