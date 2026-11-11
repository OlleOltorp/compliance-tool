# Compliance checking tool for Swedish webshops

Paste the address of a Swedish webshop and the tool checks it against the 45 requirement points of the Trygg
E-handel certification. Rules do the checking and run locally on your computer. An AI judge can go over the cases
the rules could not settle, but it is optional and off until you add an API key. A person reviews each possible
flaw and makes the decision.

A student project at KTH, 2026.

## Quick start

```
python -m venv .venv
.venv\Scripts\activate            # Windows
source .venv/bin/activate         # macOS / Linux
pip install -r requirements.txt
python -m playwright install chromium
python run.py
```

`run.py` starts a local server and opens the scan page at http://127.0.0.1:8765/ in your browser. Paste a webshop
URL and press **Scan**. Stop the server with Ctrl+C.

Requires Python 3.10 or newer (tested with 3.12).

## The scan page

- One field: paste a webshop URL, for example `https://www.exempelbutiken.example`, and press **Scan**.
- While it runs, the page shows what the crawler is doing. A scan usually takes one to three minutes.
- The result is a list per requirement point:
  - **Possible flaw**: the rule found text on the shop's site that may break the requirement. It shows the quoted
    text and the page it came from.
  - **Met**: the rule found text that meets the requirement (open "Show the text" to see it).
  - **Not checked**: the tool could not check it, for example because only the shop's own documents or a test
    purchase can show it.
  - **Not applicable**: the point does not apply to this shop, for example subscriptions when it sells none.
- "Also walk the checkout" (off by default) puts one cheap product in the cart and goes through the checkout with
  test data up to the buy button, so the checkout points can be checked too. It never clicks the buy button.
- The page shows whether the AI judge is on. Without an API key it says "AI judge: off".

The UI texts that come from the rules (point names, reasons) are in Swedish, because the requirements and the
consumer law terms are Swedish.

Other pages on the same server:

| Address | What |
|---|---|
| `/` | The scan page |
| `/full` | The full interface: live view, report with screenshots, the requirement texts side by side, earlier scans |
| `/full#/scan/<id>/granska` | Review mode: a reviewer confirms or rejects each possible flaw; when all 45 points are reviewed, the final report is available as HTML and PDF |
| `/demo` | A demo of the review workflow with invented shops (see below) |

## Turn on the AI judge

The AI judge reads the shop's saved pages for the cases the rules could not settle and gives its own verdict. It
must quote the shop's text word for word; if the quote does not hold up, the rule's result stands.

1. Get an API key from the Anthropic Console (https://platform.claude.com/).
2. Copy `.env.example` to `.env` and put the key in it:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

3. Restart `python run.py`. The scan page now says "AI judge: on".

| Setting in `.env` | What it does |
|---|---|
| `ANTHROPIC_API_KEY` | Turns the AI judge on |
| `LLM_MODEL` | The model (default `claude-sonnet-5-5`) |
| `LLM_EFFORT` | How hard the model thinks: `low`, `medium` (default) or `high` |
| `LLM_FALLBACKS=off` | Turns off the server-side fallback that retries a declined request on another model |
| `LLM_BACKEND=cli` | Uses the Claude Code command-line tool (`claude -p`) instead of the API; needs Claude Code installed and signed in |
| `LLM_JUDGE=off` | Turns the AI judge off even with a key |

- The judge uses the official `anthropic` Python SDK. It costs API credits; each scan sends one request with a few
  follow-up tool calls.
- It only reads the saved pages and may fetch at most four more pages from the shop's own domain.
- Without a key, nothing is sent anywhere and the tool runs rules only. `python run.py --rules-only` does the same
  even when a key is set.
- Approved precedents in `app/data/precedents/` guide the judge. The files there are fictional examples of the format.

## The review demo

`python run.py --demo` (or `/demo`) shows the review workflow: spot checks, the reviewer's queue, the shop's own
page, a rescan after a fix, and a rule change that reaches every affected shop. It runs on invented shops and quotes
(`app/data/demo_seed.json`, built by `python -m app.demo_seed`) and never calls the AI judge.

`python -m app.demo_static <out.html>` builds the same demo as a single static HTML file that runs without the
server, for example on a project website. Its state is kept in the browser.

## How the checkout walk behaves

- Only test data: a test name, address, phone number and an `example.com` e-mail address.
- Never a personal identity number (personnummer), an account, BankID or card details. It stops and reports
  "kräver personnummer" instead.
- Never clicks the final buy button. Payment methods may be opened, never filled in.
- Declines cookies that are not needed. Stops at bot protection instead of trying to get past it.
- The browser always runs headless. Internal addresses (localhost, private networks) are blocked.

## Saved scans and rescanning

Everything a scan reads (text, HTML, links, screenshots) is saved under `out/`, so the rules can run again on the
same pages. Point 4b (how long a sale lasts) needs scans over several weeks; `app/history.py` compares them:

```
python -m app.history rescan exempelbutiken.example
python -m app.history timeline exempelbutiken.example
```

## Project layout

| Path | What |
|---|---|
| `run.py` | Starts the server and opens the scan page |
| `app/server.py` | FastAPI server and API |
| `app/static/scan.html` | The scan page |
| `app/static/index.html`, `app/static/legacy.html` | The full interface and review mode |
| `app/discover.py`, `app/fetch.py`, `app/sample.py` | Crawler: pages, PDFs, product pages |
| `app/checkout.py`, `app/llm_nav.py` | Checkout walk up to the buy button |
| `app/rules.py`, `app/rules_product.py`, `app/rules_checkout.py`, `app/rules_history.py` | Rule engine |
| `app/rules/*.yaml` | Pattern lists the rules use |
| `app/text/` | Sentence splitting, topic tags and fact extraction |
| `app/llm_judge.py`, `app/llm_fetch.py` | Optional AI judge (Anthropic API or Claude Code CLI) |
| `app/engine.py` | Runs a scan and merges the results |
| `app/review.py` | Review mode and the final report (HTML, PDF) |
| `app/snapshot.py`, `app/history.py`, `app/live.py` | Saved scans, comparisons over time, live events |
| `app/demo.py`, `app/demo_seed.py`, `app/demo_static.py`, `app/static/demo.html` | The review demo and its static build |
| `app/data/points.json` | The 45 requirement points |
| `app/data/sh_text.json` | Svensk Handel's requirement text, by section, with the part each point checks |
| `app/data/method.json` | How each point is checked |
| `app/data/llm_principles.json` | Principles for the AI judge |
| `app/data/gate.json` | Rules whose possible flaws are shown as uncertain until they are precise enough |

## Requirement text

The points follow Svensk Handel's public document "Krav för certifieringen – Trygg E-handel" (last updated
2023-12-19), linked from the Trygg E-handel application page:
https://tryggehandel.svenskhandel.se/wp-content/uploads/2024/01/krav-for-certifieringen-trygg-e-handel.pdf.
`app/data/sh_text.json` quotes that text so each point can be shown next to the requirement it checks. The text
belongs to Svensk Handel. This tool is not made by or connected to Svensk Handel.

## Limits

- The result is support for a person, not a legal decision.
- Pages built entirely with JavaScript may give little or no text.
- Without the checkout walk, the checkout points are checked from the shop's texts only. With it, the walk stops
  where a shop asks for a personal identity number or a login; points it did not reach become "could not be
  checked", never a possible flaw.
- Internal routines (for example complaint handling) cannot be seen from outside and need the shop's own documents.
