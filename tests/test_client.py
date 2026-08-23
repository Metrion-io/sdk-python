"""The manual API: nesting, exceptions, and never raising into the caller."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metrion import Metrion
from metrion.context import current_span_id, current_trace_id


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


def client():
    t = StubTransport()
    return Metrion(api_key="mt_live_x", transport=t), t


def test_children_share_the_trace_and_parent_to_it():
    m, t = client()
    with m.trace(name="agent") as trace:
        with trace.span(name="retrieve"):
            pass
    by_type = {e.type: e for e in t.events}
    assert by_type["span"].trace_id == by_type["trace"].trace_id
    assert by_type["span"].parent_span_id == by_type["trace"].span_id


def test_a_trace_is_its_own_root():
    m, t = client()
    with m.trace(name="agent"):
        pass
    trace = t.events[0]
    assert trace.parent_span_id is None
    assert trace.span_id == trace.trace_id


def test_children_inherit_session_and_user():
    m, t = client()
    with m.trace(name="agent", session_id="sess_1", user_id="user_9") as trace:
        with trace.generation(name="answer", model="m", input="q") as gen:
            gen.end(output="a")
    generation = next(e for e in t.events if e.type == "generation")
    assert generation.session_id == "sess_1"
    assert generation.user_id == "user_9"


def test_an_exception_marks_the_span_and_still_propagates():
    # Swallowing the caller's exception would be far worse than losing a span.
    m, t = client()
    try:
        with m.trace(name="agent"):
            raise ValueError("boom")
    except ValueError:
        pass
    else:
        raise AssertionError("exception was swallowed")
    event = t.events[0]
    assert event.status == "error"
    assert "ValueError: boom" in event.error


def test_context_is_restored_to_the_parent_not_cleared():
    # Spans nest: ending a child must return the parent to being current.
    m, _ = client()
    with m.trace(name="outer") as outer:
        with outer.span(name="inner"):
            assert current_span_id() != outer.span_id
        assert current_span_id() == outer.span_id
    assert current_trace_id() is None


def test_usage_accepts_both_spellings():
    # OpenAI says prompt/completion; the plan's examples say input/output.
    m, t = client()
    g1 = m.generation(name="a", model="m")
    g1.end(usage={"input_tokens": 10, "output_tokens": 5})
    g2 = m.generation(name="b", model="m")
    g2.end(usage={"prompt_tokens": 10, "completion_tokens": 5})
    for event in t.events:
        assert event.prompt_tokens == 10
        assert event.completion_tokens == 5
        assert event.total_tokens == 15


def test_ending_twice_emits_once():
    m, t = client()
    g = m.generation(name="a", model="m")
    g.end(output="x")
    g.end(output="y")
    assert len(t.events) == 1


def test_no_api_key_disables_rather_than_raising():
    # An app that forgot to configure a key should still run.
    m = Metrion(api_key="", base_url="http://localhost")
    assert m.enabled is False
    with m.trace(name="agent"):
        pass  # must not raise


def test_a_transport_that_raises_never_reaches_the_caller():
    class Exploding:
        def enqueue(self, event):
            raise RuntimeError("queue exploded")

    m = Metrion(api_key="x", transport=Exploding())
    with m.trace(name="agent"):
        pass  # must not raise


def test_structured_payloads_are_encoded_not_dropped():
    m, t = client()
    g = m.generation(name="a", model="m", input=[{"role": "user", "content": "hi"}])
    g.end(output={"text": "hello"})
    event = t.events[0]
    assert isinstance(event.input, str) and "user" in event.input
    assert isinstance(event.output, str) and "hello" in event.output


def test_an_oversized_payload_is_truncated_not_refused():
    # The ingest accepts 200KB; a larger body is refused outright, which would
    # lose the whole event rather than the tail of one field.
    m, t = client()
    g = m.generation(name="a", model="m", input="x" * 300_000)
    g.end()
    assert len(t.events[0].input) == 200_000


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
