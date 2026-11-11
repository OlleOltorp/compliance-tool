# Precedents for the AI judge

The files here are **fictional examples** that show the format. Shops, quotes and comments are made up.

- One file per requirement point, named after the point, e.g. `7.1d.json`. Each file is a list of cases.
- Only cases with `"status": "godkänd"` (approved) reach the AI judge. New cases start as `"status": "utkast"` (draft).
- The judge never sees cases from the shop it is judging.

| Field | Meaning |
|---|---|
| `kind` | `fall` = a correct judgement to follow; `fallgrop` = a pitfall where a tool or the AI judged wrong |
| `shop` | The shop the quote comes from |
| `quote` | The shop's text |
| `page` | The page file the quote was found in |
| `quote_checked` | `false` when the quote was not found in the saved pages |
| `state` | The right outcome: `brist`, `uppfyllt` or `ej tillämplig` |
| `why` | One sentence on why |
| `wrong` | For a `fallgrop`: what went wrong |
| `kommentar` | Optional note from the reviewer; the judge weighs it before `why` |
| `source` | Where the case comes from (shown to the judge) |
| `certainty` | How sure the case is (not read by the judge) |
