# CLAUDE.md

Guidance for working in this repository.

## Project

Multi-Agent Shopping Concierge: takes a natural-language shopping query, searches the
**Shopify Global Catalog** (via its MCP endpoint), and has specialized agents evaluate the
results before producing a ranked, reasoned shortlist. See `README.md` for setup and usage.

(`project-brief.md` and `IMPLEMENTATION_PLAN.md` are kept locally but gitignored — they hold the
original spec and the phased build plan, and remain the source of truth for scope/status when
working in this checkout.)

MVP scope: **Budget + Logistics agents, weighted scoring, CLI, in-memory state.** Quality was
swapped for Logistics because the live `search_catalog` API returns no rating/review data;
Quality's code/enum remain for Full scope if a catalog ever returns reviews.

## Commands

All commands use the project virtualenv at `.venv`.

```bash
# Install (core + dev; the Agno/model deps are a separate optional group)
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pip install -e ".[agno]"   # only needed from Phase 6 on

# Test (the opt-in real-model eval suite is excluded by default via pyproject)
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -m eval             # run the real-model property checks (needs LLM_API_KEY)

# Format, lint, type-check (run all before completing a code change)
.venv/bin/ruff format .
.venv/bin/ruff check .            # add --fix to auto-fix
.venv/bin/python -m mypy
```

## Architecture

**Orchestration framework: Agno** (Workflows 2.0), for both MVP and Full scope.

**Hybrid scoring — the central design decision.** Facts are computed deterministically; judgment
is left to the LLM:
- `features.py` extracts per-domain **feature records** (`BudgetFeatures`, `LogisticsFeatures`,
  and `QualityFeatures` for Full scope) — percentiles, tiers, over-budget flags, in-stock,
  size/color assortment. Pure, exact, unit-tested.
- The Agno agents (Phase 6) reason over those features and return a lightweight `ScoreItem`
  (`score` + `reasons`) — the model **scores but never recomputes numbers**.
- `consensus.py` combines per-agent scores with configurable weights. Deterministic math, never
  an LLM — so weights stay auditable.

Pipeline: `search → parallel(budget, logistics) → consensus → reason`. `--no-llm` swaps the two
Agno agents for deterministic scoring so the whole pipeline runs without a model key.

## Conventions (important)

- **Keep the pure core framework-independent.** `config`, `models`, `features`, `consensus`,
  `auth`, `mcp_client`, `catalog` must NOT import Agno. Agno is confined to `agents/`,
  `workflow.py`, and `model_factory.py`. This keeps the recommendation logic testable without a
  key/network and makes framework swaps cheap.
- **LLM = OpenRouter by default** (`llm_provider="openrouter"`, key in `LLM_API_KEY`). Switch
  models via `LLM_MODEL` (e.g. `anthropic/claude-opus-4-8`, `openai/gpt-4o`) with no code change.
- **Defensive parsing at integration seams.** The Global Catalog response shape and the auth
  request body are only partly specified; `Product.from_mcp` tries multiple key spellings and
  tolerates missing fields, and `auth.py` notes where to switch form-encoding ↔ JSON. Confirm
  against the live endpoint in Phase 5 and capture a real response as `tests/fixtures/`.
- **Testing tiers for the hybrid model:** features/consensus asserted *exactly* (pure);
  agents/workflow tested for *schema conformance* with a stub model; scoring *sanity* covered by
  the opt-in `@pytest.mark.eval` suite (real model, tolerant property checks). Do not assert exact
  score ordering against a real model.
- Config **fails loud**: `Settings.load()` raises naming every missing env var at once.
- `agno` is pinned (`==2.8.3`) — fast-moving API; keep it pinned on upgrades.

## Status

Feature-complete and running **live end-to-end**: config, models, features, consensus, auth, MCP
client + catalog, the Budget/Logistics Agno agents, the debate loop (`--debate N`), the Agno
`Team` variant (`--team`), conversational intake (`-i`), and the CLI. `Product.from_mcp` is
verified against real API responses. 61 tests + 2 opt-in eval, ruff + mypy clean.
See the local `IMPLEMENTATION_PLAN.md` (gitignored) for phase history and open ideas.

## Python & Code Style
- **Formatting:** Formatted with `ruff format` (88-char line limit).
- **Type Checking:** Strict `mypy` typing required on all public signatures.
- **Imports:** Absolute imports only (`from my_package.module import ...`).
- **No magic strings — use enums.** Domain vocabulary (agent names, price tiers, providers,
  and any future closed set of string constants) lives in `concierge/enums.py` as `StrEnum`s.
  Never scatter bare literals like `"budget"` or `"low"` through the code — a typo becomes a
  silent 0.0 weight or a dead branch, whereas an enum member fails at import/lookup. `StrEnum`
  members still compare/serialize as their string, so they drop into Pydantic and JSON cleanly.
  Ruff's `PLR2004` is configured to flag bare-string comparisons as a backstop (numbers are
  exempt); tests are exempt.
- **Commands:** Run `ruff format .`, `ruff check .`, `mypy`, and `pytest` before completing code changes.