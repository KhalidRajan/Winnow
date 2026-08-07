"""Command-line entry point: python -m concierge.cli [query] [options]."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from typing import Any

from concierge import formatting, interactive, telegram, workflow
from concierge.agents.intake import build_intake_agent
from concierge.auth import TokenProvider
from concierge.config import ConfigError, Settings
from concierge.enums import AgentName
from concierge.mcp_client import McpClient
from concierge.model_factory import build_model
from concierge.models import Query
from concierge.team import TeamError

_DEFAULT_QUERY = "waterproof hiking jacket under $200, shipping to Canada"

_PRICE_RE = re.compile(r"under \$?(\d+(?:\.\d+)?)", re.IGNORECASE)
_SHIPS_TO_RE = re.compile(r"(?:shipping|ship|deliver\w*)\s+to\s+([A-Z][A-Za-z]+)")


def parse_query(text: str) -> Query:
    """Very light NL parsing — full parsing is Full scope."""
    max_price = None
    price_match = _PRICE_RE.search(text)
    if price_match:
        max_price = float(price_match.group(1))

    ships_to = None
    ships_match = _SHIPS_TO_RE.search(text)
    if ships_match:
        ships_to = ships_match.group(1)

    return Query(raw_text=text, max_price=max_price, ships_to=ships_to)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Multi-Agent Shopping Concierge")
    parser.add_argument("query", nargs="?", default=_DEFAULT_QUERY)
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="ask a few questions (product, budget, destination, priority) first",
    )
    parser.add_argument("--weight-budget", type=float, default=0.5)
    parser.add_argument("--weight-logistics", type=float, default=0.5)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="use deterministic scoring instead of the LLM agents",
    )
    parser.add_argument(
        "--debate",
        type=int,
        default=0,
        metavar="ROUNDS",
        help="let the agents debate contested products for up to N rounds (LLM only)",
    )
    parser.add_argument(
        "--team",
        action="store_true",
        help="use an Agno Team to coordinate + synthesize instead of deterministic consensus",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="serve the concierge over Telegram (long-polling, allowlisted chats)",
    )
    return parser


def run_telegram(
    settings: Settings, client: McpClient, model: object, args: Any
) -> int:
    """Serve the same pipeline over a Telegram chat (long-polling, allowlisted)."""
    # Validate before announcing anything, so a misconfigured bot never prints
    # "running" and then dies.
    _, allowed = telegram.require_telegram_settings(settings)

    def handle(session: telegram.ChatSession) -> None:
        query = interactive.collect_query_llm(
            lambda: build_intake_agent(model), read=session.read, write=session.write
        )
        recommendations = workflow.run(
            query,
            client,
            model=model,
            use_llm=True,
            use_team=args.team,
            top_n=args.top,
            debate_rounds=args.debate,
        )
        session.write_html(formatting.render_telegram(recommendations))

    print(
        f"Telegram bot running for chat(s) {', '.join(sorted(allowed))} "
        "— Ctrl-C to stop. Message your bot to start."
    )
    telegram.run_bot(settings, handle)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Agno logs its internal structured-output retries at ERROR even when it
    # recovers, which garbles the conversation. Our own failures surface as
    # exceptions (ConfigError, McpError, TeamError), so silence its logger.
    logging.getLogger("agno").setLevel(logging.CRITICAL)

    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    client = McpClient(settings, TokenProvider(settings))
    model = None if args.no_llm else build_model(settings)

    if args.telegram:
        if model is None:
            print("The Telegram bot needs a model — drop --no-llm.", file=sys.stderr)
            return 1
        try:
            return run_telegram(settings, client, model, args)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 1

    if args.interactive:
        if model is None:
            print(
                "Interactive intake needs a model — drop --no-llm, or pass a query "
                "directly instead.",
                file=sys.stderr,
            )
            return 1
        query = interactive.collect_query_llm(lambda: build_intake_agent(model))
    else:
        query = parse_query(args.query)
        query.weights = {
            AgentName.BUDGET: args.weight_budget,
            AgentName.LOGISTICS: args.weight_logistics,
        }

    try:
        recommendations = workflow.run(
            query,
            client,
            model=model,
            use_llm=not args.no_llm,
            use_team=args.team,
            top_n=args.top,
            debate_rounds=args.debate,
        )
    except TeamError as exc:
        print(f"Team run failed: {exc}", file=sys.stderr)
        return 1

    print(formatting.render(recommendations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
