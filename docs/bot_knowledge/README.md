# UnionBot Knowledge Base

These markdown files are the bot's editable knowledge base. The AI helper reads
the most relevant files for each question and sends them to the local model as
context.

Editing notes:

- Keep facts short and practical.
- Tag durable source quality with `Source tier: A/B/C/D` inside the relevant
  section when it matters. A = official Albion, B = wiki/mechanics fallback,
  C = community/tooling, D = weak hearsay/opinion.
- Prefer official Albion sources for rules, patches, and current systems. Use
  the wiki for mechanics and community guides for practical play patterns.
- Prefer workflows over long speeches.
- Put broad gameplay Q&A in `albion_member_field_manual.md`; put exact guild
  workflow/policy in the specific UnionBot files.
- Do not add exact Albion item stats unless you plan to maintain them after
  patches.
- If a policy needs officer approval, say so clearly.
- Keep risk-watch, blacklist, theft, regear disputes, loot disputes, and external
  guild warnings officer-only and evidence-first.
- If a fact can change in-game, tell the bot to point members to current
  in-game UI, patch notes, or an officer.

Eval notes:

- Put repeatable member questions in `ai_eval_cases.json`.
- Run `python3 scripts/ai_eval.py --verbose` before/after knowledge changes.
- Add a new eval case whenever the bot gives a wrong or weird answer that we
  want to prevent from coming back.

Retrieval notes:

- The bot builds a cached in-memory section index from these markdown files.
- Ranking uses token/phrase matches, file hints, source-tier nudges, and
  rare-term keyword weighting.
- Keep headings specific. A heading like `Deadwater Eel` or `Loot Splits` helps
  more than a generic heading like `Notes`.
