"""The wrapper must be transparent.

Same return value, same exception type, same chunks. A wrapper that alters
behaviour is worse than no wrapper, because the difference only surfaces in
production.

No network and no openai package: the wrapper's own classes are exercised
against fakes, which is the part this repo owns.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metrion import Metrion
from metrion.openai import _Completions, _StreamProxy


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


class Usage:
    prompt_tokens = 100
    completion_tokens = 20
    total_tokens = 120


class Message:
    content = "hello"


class Choice:
    message = Message()


class Response:
    choices = [Choice()]
    usage = Usage()


class FakeCompletions:
    """Stands in for openai's own completions object."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.seen = None
        self.extra_attribute = "forwarded"

    def create(self, **kwargs):
        self.seen = kwargs
        if self.error:
            raise self.error
        return self.result


def wrapped(inner):
    m = Metrion(api_key="x", transport=StubTransport())
    return _Completions(inner, m), m._transport


def test_the_response_is_returned_unchanged():
    response = Response()
    completions, _ = wrapped(FakeCompletions(result=response))
    assert completions.create(model="gpt-4o", messages=[]) is response


def test_usage_and_output_are_recorded():
    completions, transport = wrapped(FakeCompletions(result=Response()))
    completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    event = transport.events[0]
    assert event.model == "gpt-4o"
    assert event.provider == "openai"
    assert (event.prompt_tokens, event.completion_tokens, event.total_tokens) == (100, 20, 120)
    assert event.input is not None and "hi" in event.input
    assert event.output is not None and "hello" in event.output
    assert event.status == "ok"


def test_the_providers_exception_reaches_the_caller_unchanged():
    class ProviderError(Exception):
        pass

    completions, transport = wrapped(FakeCompletions(error=ProviderError("rate limited")))
    try:
        completions.create(model="gpt-4o", messages=[])
    except ProviderError:
        pass
    else:
        raise AssertionError("exception was swallowed or replaced")
    event = transport.events[0]
    assert event.status == "error"
    assert "ProviderError: rate limited" in event.error


def test_unknown_attributes_are_forwarded():
    # Anything the official SDK adds later must keep working.
    inner = FakeCompletions(result=Response())
    completions, _ = wrapped(inner)
    assert completions.extra_attribute == "forwarded"


def test_arguments_reach_the_provider_untouched():
    inner = FakeCompletions(result=Response())
    completions, _ = wrapped(inner)
    completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}],
                       temperature=0.2, max_tokens=50)
    assert inner.seen["temperature"] == 0.2
    assert inner.seen["max_tokens"] == 50


# -- streaming -------------------------------------------------------------
class Delta:
    def __init__(self, content):
        self.content = content


class StreamChoice:
    def __init__(self, content):
        self.delta = Delta(content)


class Chunk:
    def __init__(self, content=None, usage=None):
        self.choices = [StreamChoice(content)] if content is not None else []
        self.usage = usage


def stream_proxy(chunks):
    m = Metrion(api_key="x", transport=StubTransport())
    generation = m.generation(name="chat.completions", model="gpt-4o")
    return _StreamProxy(iter(chunks), generation, 0.0), m._transport


def test_streaming_yields_the_same_chunks():
    chunks = [Chunk("he"), Chunk("llo")]
    proxy, _ = stream_proxy(chunks)
    assert list(proxy) == chunks


def test_streaming_records_only_once_the_stream_is_consumed():
    proxy, transport = stream_proxy([Chunk("hi")])
    assert transport.events == [], "recorded before the stream was read"
    list(proxy)
    assert len(transport.events) == 1


def test_streaming_assembles_the_text_and_keeps_usage():
    chunks = [Chunk("he"), Chunk("llo"), Chunk(None, usage=Usage())]
    proxy, transport = stream_proxy(chunks)
    list(proxy)
    event = transport.events[0]
    assert event.output == "hello"
    assert event.total_tokens == 120


def test_a_stream_that_breaks_midway_still_records_and_reraises():
    def chunks():
        yield Chunk("he")
        raise RuntimeError("stream died")

    m = Metrion(api_key="x", transport=StubTransport())
    generation = m.generation(name="chat.completions", model="gpt-4o")
    proxy = _StreamProxy(chunks(), generation, 0.0)
    try:
        list(proxy)
    except RuntimeError:
        pass
    else:
        raise AssertionError("exception was swallowed")
    event = m._transport.events[0]
    assert event.status == "error"
    assert event.output == "he", "partial output was discarded"


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
