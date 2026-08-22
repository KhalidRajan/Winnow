"""Tuning knobs — the values you'd reach for to change how the concierge behaves.

Deliberately *not* a home for every constant in the codebase. Facts dictated by
an external protocol (Telegram's 4096-character message cap, the set of
retryable HTTP statuses) stay next to the code that honours them, where the
comment explaining them is in reading distance. What lives here is the judgment
calls: thresholds, limits, and timeouts that a person might reasonably want to
find in one place and tune without reading five modules first.

Like the rest of the pure core, this module imports nothing from Agno — and
nothing from the rest of the package either, so it can never cause a cycle.
"""

from __future__ import annotations

# --- Scoring and consensus ---

# Prior weight for the Bayesian rating shrinkage. Larger -> ratings with few
# reviews are pulled harder toward the set mean.
BAYES_PRIOR_WEIGHT = 20.0

# Minimum spread between the highest and lowest agent score for a product before
# we surface it as an explicit trade-off.
TRADEOFF_THRESHOLD = 0.3

# --- Debate ---

# Agents must disagree by at least this much before a product is contested and
# worth spending a debate round on.
DEBATE_SPREAD_THRESHOLD = 0.3

# Stop debating once a round moves every score by less than this — further
# rounds cost model calls without changing the ranking.
DEBATE_CONVERGENCE_EPSILON = 0.05

# --- Conversation and search ---

# One opening message plus at most three follow-ups. Intake should feel brief;
# the agent is told to spend its questions on budget/destination first.
MAX_INTAKE_TURNS = 4

# Default number of products to request per search.
DEFAULT_SEARCH_LIMIT = 10

# Give up on a silent conversation after this long (raises EOFError, which the
# intake loop already treats as "search with what we have").
CONVERSATION_TIMEOUT = 600.0

# --- Auth ---

# Refresh this many seconds before the token actually expires, so an in-flight
# request never races the expiry boundary.
TOKEN_EXPIRY_MARGIN = 60.0

# Assumed token lifetime when the auth response omits `expires_in`.
TOKEN_DEFAULT_TTL = 3600.0
