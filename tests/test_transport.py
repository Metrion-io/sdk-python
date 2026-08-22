"""Failure is the interesting case.

Every assertion here is a variation on one question: when Metrion is broken,
is the caller unaffected? A transport that delivers correctly on a good day is
not the hard part.
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metrion.models import Event, STATUS_ERROR
from metrion.transport import MAX_BATCH, Transport


class StubSender:
    def __init__(self, status=202, statuses=None):
        self.batches = []
        self.status = status
        self.statuses = list(statuses or [])
        self.calls = 0

    def send(self, payloads):
        self.calls += 1
        self.batches.append(payloads)
        if self.statuses:
            return self.statuses.pop(0), ""
        return self.status, ""


class ExplodingSender:
    """The worst case: a sender that raises rather than returning a status."""

    def __init__(self):
        self.calls = 0

    def send(self, payloads):
        self.calls += 1
        raise RuntimeError("connection reset")


def transport(sender, **kw):
    kw.setdefault("start_worker", False)
    kw.setdefault("max_retries", 0)
    return Transport(sender, **kw)


def event(i=0):
    return Event(type="generation", trace_id=f"t{i}", name="chat")


def test_a_full_queue_drops_the_oldest_not_the_newest():
    # The newest events describe what is happening now.
    t = transport(StubSender(), queue_size=2)
    for i in range(4):
        t.enqueue(event(i))
    t._drain()
    delivered = [e["trace_id"] for batch in t._sender.batches for e in batch]
    assert delivered == ["t2", "t3"], delivered
    assert t.stats.dropped_queue_full == 2


def test_enqueue_never_blocks_or_raises_when_the_queue_is_full():
    t = transport(StubSender(), queue_size=1)
    done = threading.Event()

    def hammer():
        for i in range(1000):
            t.enqueue(event(i))
        done.set()

    thread = threading.Thread(target=hammer)
    thread.start()
    thread.join(timeout=5)
    assert done.is_set(), "enqueue blocked"


def test_a_sender_that_raises_does_not_kill_the_worker():
    sender = ExplodingSender()
    t = transport(sender)
    t.enqueue(event())
    t._drain()  # must not raise
    assert t.stats.dropped_failed == 1


def test_a_400_is_not_retried():
    # It fails identically the second time; retrying is just load.
    sender = StubSender(status=400)
    t = transport(sender, max_retries=3)
    t.enqueue(event())
    t._drain()
    assert sender.calls == 1, sender.calls
    assert t.stats.dropped_failed == 1


def test_a_503_is_retried():
    # Telemetry answers 503 rather than a false 202 when its own write fails,
    # precisely so a buffering client retries instead of dropping.
    sender = StubSender(statuses=[503, 503, 202])
    t = transport(sender, max_retries=3)
    t.enqueue(event())
    t._drain()
    assert sender.calls == 3, sender.calls
    assert t.stats.sent == 1


def test_a_401_stops_all_further_delivery():
    # The key will not become valid by being used again.
    sender = StubSender(status=401)
    t = transport(sender, max_retries=3)
    t.enqueue(event())
    t._drain()
    assert sender.calls == 1
    t.enqueue(event(9))
    assert len(t._queue) == 0, "events accepted after auth failure"


def test_a_413_splits_the_batch_rather_than_dropping_it():
    # The events are fine; there are simply too many in one request.
    sender = StubSender(statuses=[413, 202, 202])
    t = transport(sender, batch_size=4)
    for i in range(4):
        t.enqueue(event(i))
    t._drain()
    assert sender.calls == 3, sender.calls
    assert t.stats.sent == 4


def test_the_batch_size_never_exceeds_the_documented_cap():
    t = transport(StubSender(), batch_size=5000)
    assert t._batch_size == MAX_BATCH


def test_payloads_are_a_bare_array_not_an_envelope():
    # The batch endpoint takes the array; an {"events": [...]} envelope is a
    # 400 there, while the single-event endpoint takes an object.
    sender = StubSender()
    t = transport(sender)
    t.enqueue(event())
    t._drain()
    assert isinstance(sender.batches[0], list)


def test_an_error_event_keeps_its_status():
    sender = StubSender()
    t = transport(sender)
    e = event()
    e.status = STATUS_ERROR
    e.error = "upstream exploded"
    t.enqueue(e)
    t._drain()
    sent = sender.batches[0][0]
    assert sent["status"] == "error"
    assert sent["error"] == "upstream exploded"


def test_shutdown_delivers_what_is_queued():
    # A short script must not lose its only trace.
    sender = StubSender()
    t = transport(sender)
    t.enqueue(event())
    t.shutdown()
    assert t.stats.sent == 1


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
