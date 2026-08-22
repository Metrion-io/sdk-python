"""Best-effort cost, for callers who report tokens but not money.

Deliberately best-effort. Prices move, a table shipped in a package goes stale
between releases, and an SDK is the wrong place to be authoritative about
billing. Anything computed here is an estimate to make a dashboard useful, not
a number to invoice from — the receiver stores what it is told, so a caller who
knows the real figure should pass `cost_usd` and skip this entirely.

Unknown models return None rather than zero. Zero reads as "this was free",
which is a different and much more misleading claim than "we do not know".
"""

from __future__ import annotations

# USD per 1M tokens: (input, output, cached input).
#
# Cached input is listed separately because it is not a rounding difference —
# on deepseek-v4-flash it is a thirty-first of fresh input, and an agentic
# client resends its whole conversation every turn. Pricing those at the input
# rate overstates cost by an order of magnitude.
#
# Sources are the providers' own published rates as of August 2026.
PRICES: dict[str, tuple[float, float, float | None]] = {
    # OpenAI
    "gpt-5.6-luna": (0.20, 1.20, 0.02),
    "gpt-5.6-terra": (2.00, 12.00, 0.20),
    "gpt-5.6-sol": (5.00, 30.00, 0.50),
    "gpt-5.4-mini": (0.75, 4.50, 0.075),
    "gpt-5.4-nano": (0.20, 1.25, 0.02),
    "gpt-4o": (2.50, 10.00, 1.25),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    # Anthropic
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-haiku-4.5": (1.00, 5.00, 0.10),
    # DeepSeek — peak rates; off-peak is half, and which applies depends on
    # the hour, so the more expensive one is used rather than guessing.
    "deepseek-v4-flash": (0.44, 1.32, 0.014),
    "deepseek-v4-pro": (1.32, 3.96, 0.044),
    # Others
    "kimi-k3": (3.00, 15.00, 0.30),
    "glm-5.2": (1.40, 4.40, 0.26),
    "gemini-3.7-flash": (1.50, 7.50, 0.15),
    "grok-4.6": (2.00, 6.00, 0.50),
    "muse-spark-1.2": (1.25, 4.25, 0.15),
    # Meta's Contributor tier is a twelfth of Standard for the same model. The
    # difference is what the caller pays with: Contributor prompts are used to
    # improve Meta's products.
    "muse-spark-1.2-contributor": (0.10, 0.20, 0.002),
}


def normalise(model: str) -> str:
    """Strips the decorations a gateway adds to a model id.

    `orvix/deepseek-v4-flash`, `openai/gpt-4o` and a bare `gpt-4o` all name the
    same model as far as price is concerned, and a router prefix is exactly the
    kind of thing a caller should not have to strip by hand.
    """
    name = model.strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


def lookup(model: str) -> tuple[float, float, float | None] | None:
    """Rates for a model, or None if it is not in the table."""
    name = normalise(model)
    if name in PRICES:
        return PRICES[name]
    # A dated variant — "gpt-4o-2026-05-13" — prices as its base model. Longest
    # prefix wins, so gpt-4o-mini-x does not match gpt-4o.
    matches = [key for key in PRICES if name.startswith(key)]
    return PRICES[max(matches, key=len)] if matches else None


def estimate(
    model: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cached_tokens: int | None = None,
) -> float | None:
    """Estimated USD, or None when the model or the token counts are unknown.

    `cached_tokens` is a subset of `prompt_tokens`, matching how every provider
    reports it — passing the same number in both is what the caller's usage
    object already says.
    """
    rates = lookup(model)
    if rates is None:
        return None
    if prompt_tokens is None and completion_tokens is None:
        return None

    input_rate, output_rate, cache_rate = rates
    prompt = prompt_tokens or 0
    cached = min(cached_tokens or 0, prompt)
    fresh = prompt - cached

    # An absent cache rate falls back to the input rate. That overstates rather
    # than understates, which is the safe direction for an estimate.
    cache_rate = input_rate if cache_rate is None else cache_rate

    total = (
        fresh * input_rate
        + cached * cache_rate
        + (completion_tokens or 0) * output_rate
    ) / 1_000_000

    # Below a hundredth of a cent the figure is noise, but rounding it to zero
    # would claim the call was free. Six places keeps small calls visible.
    return round(total, 8)
