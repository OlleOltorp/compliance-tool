# Compliance checking tool for Swedish webshops

Checks a webshop against the 45 Trygg E-handel requirement points. Rules run locally; an optional AI judge reviews unclear cases. A person decides on each possible flaw. Student project, KTH 2026.

## Run

```
pip install -r requirements.txt
python -m playwright install chromium
python run.py
```

Opens http://127.0.0.1:8765/. Paste a shop's address and press **Scan**. Python 3.10+.

## AI judge (optional)

Copy `.env.example` to `.env`, add `ANTHROPIC_API_KEY=...`, restart. Without a key the tool runs rules only and sends nothing anywhere.

## Demo

`python run.py --demo`: the review workflow with invented shops.

## Notes

- The checkout walk (off by default) uses test data only and never clicks the buy button.
- Results support a person; they are not legal decisions.
- Requirement text: Svensk Handel, "Krav för certifieringen – Trygg E-handel" (2023). Not made by or connected to Svensk Handel.
