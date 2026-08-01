# Winnow

Winnows thousands of Shopify products down to the handful worth buying. Chat about what you
want, and specialized agents search the **Shopify Global Catalog** across all Shopify merchants,
debate the trade-offs, and return a ranked shortlist with reasons.

```bash
python -m concierge.cli -i
```

```
🛍  Shopping Concierge — tell me what you're after.
   (press Enter to skip any question)

> I need a rain jacket for hiking
Great choice! What's your budget cap, and which country should I ship to?
> around $150, ship to Canada, men's medium
Perfect — a men's medium hiking rain jacket under $150, shipping to Canada.
Any color preference, or should I just find the best options?
>
Got it — I'll find the best men's medium hiking rain jackets under $150 shipping to Canada.

Searching…

1. Highlander Stow & Go Pack Away Waterproof And Windproof Jacket
   44.00 USD   final score 0.88  █████████·
     budget    1.00 ████████
     logistics 0.75 ██████··
   → [budget] Within budget, lowest price $44 at 0th percentile — best value
     [logistics] In stock, requested size available, ships, 7 sizes but 0 colors reported

2. Cielo Rain Jacket - Women's
   155.00 USD   final score 0.72  ███████···
     budget    0.50 ████····
     logistics 0.95 ████████
   → [budget] Over budget but low tier, $155 at 22nd percentile below median
     [logistics] In stock, requested size available, ships; excellent 11 sizes/8 colors
   ⚖ logistics rates this 0.95 but budget only 0.50
```

Intake is deliberately brief — at most three follow-ups, and Enter skips any of them. Note the
`⚖` line on the second result: the agents genuinely disagreed (great availability, over budget),
and that trade-off is surfaced rather than averaged away. The reasoning trail is the point.

## How it works

Pipeline: **`intake → search → parallel(budget, logistics) → [debate] → weighted consensus`**

An **intake agent** turns the conversation into a structured query (product, budget, destination,
priority, plus product-appropriate attributes). Then the evaluators score in parallel, optionally
debate what they disagree on, and a deterministic weighted vote produces the shortlist.

The central design decision is **hybrid scoring — facts are computed, judgment is reasoned:**

- `features.py` extracts deterministic **feature records** per product (price percentile, price
  tier, over-budget flag, in-stock, size/color assortment). Pure, exact, unit-tested.
- Two **Agno agents** (Budget, Logistics) reason over those features and return a `score` +
  `reasons` — the model *interprets the numbers, never recomputes them*.
- `consensus.py` combines the per-agent scores with configurable weights. Deterministic math, so
  weights stay auditable.

This keeps the recommendation logic testable without a key or network, and confines the LLM to
what it's good at (weighing trade-offs and explaining them).

> **Why Budget + Logistics (not Quality)?** The brief specced a Quality agent, but the live
> `search_catalog` API returns **no ratings/reviews**, so a rating-based agent has nothing to
> score. Logistics (in-stock + size/color assortment + ships-to) works on the data that *is*
> returned. The Quality agent's code and `AgentName.QUALITY` remain for Full scope if a catalog
> ever surfaces reviews.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"     # core + test/lint tooling
.venv/bin/python -m pip install -e ".[agno]"    # Agno + model providers (for LLM runs)
```

### Credentials

Copy `.env.example` to `.env` and fill in:

| Variable | Where it comes from |
|---|---|
| `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` | Dev Dashboard → **Catalogs** → create a catalog → its **Default API Key** |
| `AGENT_PROFILE_URL` | A public HTTPS URL hosting `agent_profile.json` (see below) |
| `LLM_API_KEY` | Your OpenRouter key (default provider) |

Optional overrides: `LLM_PROVIDER` (`openrouter`/`anthropic`/`openai`), `LLM_MODEL`,
`MCP_ENDPOINT`, `AUTH_ENDPOINT`. Missing required vars fail loud at startup, naming all of them.

### Hosting the agent profile

Every Global Catalog request must carry a UCP agent profile URL (`meta.ucp-agent.profile`), which
determines the agent's trust tier. Host the included [`agent_profile.json`](agent_profile.json) at
a stable public HTTPS URL and set `AGENT_PROFILE_URL` to it. Fastest route:

```bash
gh gist create --public agent_profile.json
# then use the raw URL: https://gist.githubusercontent.com/<user>/<id>/raw/agent_profile.json
```

## Usage

```bash
# Conversational intake — chat with the concierge; it asks follow-ups, then searches
python -m concierge.cli -i

# Full run (Budget + Logistics agents via the LLM)
python -m concierge.cli 'waterproof hiking jacket under $200, shipping to Canada'

# Deterministic, no LLM / no key needed — runs the whole pipeline offline of the model
python -m concierge.cli 'trail running shoes' --no-llm

# Tune agent weights and result count
python -m concierge.cli 'winter coat' --weight-budget 0.8 --weight-logistics 0.2 --top 3

# Let the agents debate products they disagree on (up to N rounds) before the vote
python -m concierge.cli 'waterproof jacket under $200' --debate 2

# Use an Agno Team (coordinate mode) to synthesize the ranking instead of consensus
python -m concierge.cli 'waterproof jacket under $200' --team
```

A one-shot query is lightly parsed for `under $NNN` (budget) and `shipping to <Country>` (region).

**`-i` starts a free-form conversation** ([`interactive.py`](src/concierge/interactive.py)): an
intake agent chats, asks follow-ups, and extracts the slots into a `Query` as you talk (its
structured output is both the next reply and the extracted fields + a `done` flag). Answer
naturally; **press Enter to skip** any question you don't care about. Intake is capped at three
follow-ups — the agent is told to spend them on budget and destination first, and the final turn
forces a wrap-up so you're never answering into a closed loop. It probes
**adaptively** — only asking about size/color/gender for products where they apply
(apparel/footwear), not for a laptop or a coffee maker — and those become real `search_catalog`
filters. Give it a size and the Logistics agent checks whether *that* size is actually offered
(`requested_size_available`) instead of scoring generic assortment. Your stated priority becomes
the agent weights (`budget` → 0.8/0.2, etc.).

Intake needs a model, so `-i` can't be combined with `--no-llm` (it exits with a clear message);
`--no-llm` still works for one-shot queries.

### Debate loop

With `--debate N`, after the independent first pass the agents **reconsider products they
disagree on** (score spread ≥ 0.3) for up to `N` rounds, each seeing its peers' scores and
reasoning. Each agent revises *from its own domain* — it may hold or move, but won't adopt
another agent's priorities — and the loop stops early on convergence. The final weighted
consensus then runs on the debated scores. The loop itself ([`debate.py`](src/concierge/debate.py))
is pure and framework-independent; the revision is an LLM call in the agents layer.

### Two orchestrations (default vs. Agno Team)

Two ways to combine the agents, for comparison:

| | **Default** (`consensus`) | **`--team`** (Agno `Team`, coordinate mode) |
|---|---|---|
| How | plain-Python fan-out → deterministic weighted vote | LLM team-leader delegates to members and synthesizes |
| Final ranking | reproducible, weight-configurable, auditable | LLM judgment (not weight-tunable) |
| Per-agent breakdown | yes (each agent's score shown) | no (merged into one blended score) |
| Best for | the product's core value (explainable trade-offs) | showcasing the `Team` primitive |

The default is the recommended path — deterministic consensus keeps the weights auditable. `--team`
([`team.py`](src/concierge/team.py)) exists to demonstrate the framework primitive side by side.

## Development

```bash
.venv/bin/ruff format .          # format (88 cols)
.venv/bin/ruff check .           # lint (+ --fix)
.venv/bin/python -m mypy         # strict type check
.venv/bin/python -m pytest -q    # tests (opt-in eval suite excluded)
.venv/bin/python -m pytest -m eval   # real-model property checks (needs LLM_API_KEY)
```

**Testing tiers** mirror the hybrid design:

- **Features, consensus & debate** — pure, asserted *exactly* (percentiles, tiers, size matching,
  weighted sums, convergence).
- **Agents, intake & workflow** — schema/wiring conformance driven by a fake agent, so the whole
  conversation and pipeline are tested with no network and no key.
- **Scoring sanity** — the opt-in `@pytest.mark.eval` suite calls a real model and asserts
  *tolerant* properties (e.g. cheapest ≥ priciest on budget), never exact ordering.

### Layout

```
src/concierge/
  config.py  models.py  enums.py     # settings + typed domain
  features.py  consensus.py          # deterministic facts + weighted vote (pure)
  debate.py                          # debate loop over per-agent "reviser" callables (pure)
  interactive.py                     # conversational intake loop (agent injected)
  auth.py  mcp_client.py  catalog.py # Global Catalog auth, JSON-RPC transport, search
  ─────────────────────────────────  # everything above is framework-independent
  model_factory.py                   # builds the Agno model from settings
  agents/  base, budget, logistics,  # evaluator agents + the intake agent
           intake
  workflow.py                        # orchestration: search → score → debate → consensus
  team.py                            # alternative: Agno Team coordinate mode (--team)
  formatting.py  cli.py              # terminal render + entry point
scripts/smoke_search.py              # manual one-shot live search (captures the fixture)
```

Everything above the line never imports Agno — the framework is confined to `agents/`,
`workflow.py`, `team.py`, and `model_factory.py`. That keeps the recommendation logic testable
without a key or network, and makes swapping frameworks cheap.

## Status

Complete and running live end-to-end against the real Global Catalog: config, domain models,
deterministic features, weighted consensus, auth, MCP client, the Agno evaluator agents, the
debate loop, conversational intake, and the CLI.

A few design decisions worth calling out, since they were driven by what the live API actually
returns rather than by the original spec:

- **Budget + Logistics, not Budget + Quality.** The spec called for a rating-based Quality agent,
  but `search_catalog` returns no ratings or reviews, so it would have had nothing to score.
  Logistics (stock, size/color assortment, ships-to) works on data that actually exists. The
  Quality scorer and its enum remain for a catalog that does surface reviews.
- **Consensus is deterministic.** The weighted vote is plain Python, never an LLM, so agent
  weights stay reproducible and auditable. The LLM's job is to weigh precomputed facts and
  explain itself — it never recomputes a number.
- **The pure core is framework-independent**, so the recommendation logic is testable without a
  key or network, and swapping orchestration frameworks stays cheap.

Possible next steps: a Sustainability or feature-match agent mining `description.plain` (the
richest unused field), adaptive weighting parsed from free text, and a streaming web UI.
