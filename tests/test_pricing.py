"""Cost estimation: right where it can be, silent where it cannot."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metrion import Metrion
from metrion.pricing import estimate, lookup, normalise


class StubTransport:
    def __init__(self):
        self.events = []

    def enqueue(self, event):
        self.events.append(event)

    def flush(self, timeout=5.0):
        pass

    def shutdown(self, timeout=5.0):
        pass

    class stats:
        @staticmethod
        def as_dict():
            return {}


def test_a_gateway_prefix_is_stripped():
    # orvix/deepseek-v4-flash and deepseek-v4-flash are the same model.
    assert normalise("orvix/deepseek-v4-flash") == "deepseek-v4-flash"
    assert normalise("  OpenAI/GPT-4o  ") == "gpt-4o"


def test_a_dated_variant_prices_as_its_base_model():
    assert lookup("gpt-4o-2026-05-13") == lookup("gpt-4o")


def test_the_longest_prefix_wins():
    # gpt-4o-mini must not price as gpt-4o; it is 16x cheaper.
    assert lookup("gpt-4o-mini") != lookup("gpt-4o")
    assert lookup("gpt-4o-mini")[0] == 0.15


def test_an_unknown_model_returns_none_not_zero():
    # Zero reads as "this was free", which is a different and more
    # misleading claim than "we do not know".
    assert estimate("some-model-we-have-never-seen", 100, 20) is None


def test_missing_token_counts_return_none():
    assert estimate("gpt-4o", None, None) is None


def test_cached_tokens_are_priced_at_the_cache_rate():
    # The whole reason the third column exists: on deepseek-v4-flash cached
    # input is a thirty-first of fresh, and an agentic client resends its
    # conversation every turn.
    cached = estimate("deepseek-v4-flash", 100_000, 100, 95_000)
    fresh = estimate("deepseek-v4-flash", 100_000, 100, 0)
    assert cached < fresh / 10, (cached, fresh)


def test_cached_cannot_exceed_prompt_tokens():
    # A provider reporting more cached than prompt is malformed; the sane
    # reading is "all of it was cached", not a negative charge.
    assert estimate("gpt-4o", 100, 0, 9999) > 0


def test_a_known_model_computes_the_published_rate():
    # gpt-4o: $2.50 in, $10.00 out per 1M.
    assert estimate("gpt-4o", 1_000_000, 0, 0) == 2.50
    assert estimate("gpt-4o", 0, 1_000_000, 0) == 10.00


def test_a_small_call_does_not_round_to_zero():
    # Rounding to zero would claim the call was free.
    assert estimate("gpt-4o", 10, 5) > 0


def test_the_client_fills_in_cost_when_the_caller_does_not():
    m = Metrion(api_key="x", transport=StubTransport())
    g = m.generation(name="a", model="orvix/deepseek-v4-flash")
    g.end(usage={"prompt_tokens": 1000, "completion_tokens": 100})
    assert m._transport.events[0].cost_usd > 0


def test_a_caller_supplied_cost_is_never_overwritten():
    # A gateway that just billed the request is authoritative; an estimate
    # must not replace a measurement.
    m = Metrion(api_key="x", transport=StubTransport())
    g = m.generation(name="a", model="gpt-4o")
    g.end(usage={"prompt_tokens": 10, "completion_tokens": 5}, cost_usd=0.99)
    assert m._transport.events[0].cost_usd == 0.99


def test_an_unknown_model_leaves_cost_unset():
    m = Metrion(api_key="x", transport=StubTransport())
    g = m.generation(name="a", model="a-model-nobody-has-priced")
    g.end(usage={"prompt_tokens": 10, "completion_tokens": 5})
    assert m._transport.events[0].cost_usd is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{failures} failed" if failures else "\nAll passed.")
    sys.exit(1 if failures else 0)
