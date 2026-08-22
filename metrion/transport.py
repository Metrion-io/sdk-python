"""Delivery, and how it fails.

The rule this module exists to honour: telemetry must never sit in the
inference critical path. A customer's LLM call cannot fail, slow down, or
throw because Metrion is unavailable.

Everything here follows from that. The queue is bounded, because an unbounded
one turns a Metrion outage into the customer's OOM. Enqueue never blocks,
because blocking on a full queue puts Metrion back in the path it was designed
to stay out of. The worker never raises, because an exception on a background
thread that nobody catches is still a crash the customer did not ask for.

When the queue is full the *oldest* event is dropped. The newest describe what
is happening now, which is what someone looking at a dashboard during an
incident actually wants.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable

from .models import Event

# The batch endpoint refuses more than this with a 413 rather than truncating,
# so the SDK splits rather than discovering the cap in production.
MAX_BATCH = 1000

# Measured against the live deployment: a 500-event batch round-trips in about
# 250 ms. Half the cap leaves room under the limit without making requests so
# small that a busy app pays for a round trip per handful of spans.
DEFAULT_BATCH_SIZE = 500

# A low-traffic app must not hold its first trace for an hour.
DEFAULT_FLUSH_INTERVAL = 5.0

DEFAULT_QUEUE_SIZE = 10_000

# Retrying a 4xx is just load: it fails identically the second time. Only
# these can plausibly succeed later.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class Stats:
    """What was delivered and what was lost.

    Counted and exposed because silent data loss is indistinguishable from
    "nothing happened" — a customer whose traces stop appearing needs to be
    able to tell which one they are looking at.
    """

    def __init__(self) -> None:
        self.sent = 0
        self.dropped_queue_full = 0
        self.dropped_failed = 0
        self.batches = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "sent": self.sent,
            "dropped_queue_full": self.dropped_queue_full,
            "dropped_failed": self.dropped_failed,
            "batches": self.batches,
        }


class HttpSender:
    """Posts a batch. Split out so tests never touch the network."""

    def __init__(self, base_url: str, api_key: str, user_agent: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_agent = user_agent
        self.timeout = timeout

    def send(self, payloads: list[dict[str, Any]]) -> tuple[int, str]:
        # A bare array, not {"events": [...]}. The batch endpoint takes the
        # array; the single-event endpoint takes an object. Sending the
        # envelope to either is a 400.
        data = json.dumps(payloads).encode()
        request = urllib.request.Request(
            f"{self.base_url}/v1/events/batch", data=data, method="POST"
        )
        request.add_header("Authorization", f"Bearer {self.api_key}")
        request.add_header("Content-Type", "application/json")
        # Cloudflare fronts this host and answers 403 (error 1010) to a request
        # with no User-Agent, which is exactly what urllib sends by default.
        # This header is not optional.
        request.add_header("User-Agent", self.user_agent)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, ""
        except urllib.error.HTTPError as error:
            return error.code, error.reason or ""
        except Exception as error:  # noqa: BLE001 - never propagate
            # A timeout or a DNS failure. Treated as retryable: the network
            # coming back is the whole reason to retry at all.
            return 0, str(error)


class Transport:
    """A bounded queue, a batching worker, and a promise never to raise."""

    def __init__(
        self,
        sender: Any,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        max_retries: int = 3,
        debug: bool = False,
        start_worker: bool = True,
    ) -> None:
        self._sender = sender
        self._queue: deque[Event] = deque()
        self._queue_size = queue_size
        self._batch_size = min(batch_size, MAX_BATCH)
        self._flush_interval = flush_interval
        self._max_retries = max_retries
        self._debug = debug
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self.stats = Stats()
        # Set on a 401. The key will not become valid by being used again, and
        # retrying it forever turns a configuration mistake into sustained load
        # against the receiver.
        self._auth_failed = False

        self._worker: threading.Thread | None = None
        if start_worker:
            self._worker = threading.Thread(
                target=self._run, name="metrion-transport", daemon=True
            )
            self._worker.start()
            atexit.register(self.shutdown)

    def enqueue(self, event: Event) -> None:
        """Accept an event. Never blocks, never raises."""
        if self._auth_failed:
            return
        with self._lock:
            if len(self._queue) >= self._queue_size:
                # Oldest first: the newest events describe what is happening
                # now, which is what someone watching an incident needs.
                self._queue.popleft()
                self.stats.dropped_queue_full += 1
            self._queue.append(event)
            ready = len(self._queue) >= self._batch_size
        if ready:
            self._wake.set()

    def _take(self, limit: int) -> list[Event]:
        with self._lock:
            take = min(limit, len(self._queue))
            return [self._queue.popleft() for _ in range(take)]

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(self._flush_interval)
            self._wake.clear()
            self._drain()
        self._drain()

    def _drain(self) -> None:
        while True:
            batch = self._take(self._batch_size)
            if not batch:
                return
            self._deliver(batch)

    def _deliver(self, batch: list[Event]) -> None:
        payloads = [event.to_payload() for event in batch]
        delay = 0.25
        for attempt in range(self._max_retries + 1):
            try:
                status, detail = self._sender.send(payloads)
            except Exception as error:  # noqa: BLE001 - the worker must not die
                status, detail = 0, str(error)

            if 200 <= status < 300:
                self.stats.sent += len(batch)
                self.stats.batches += 1
                return

            if status == 401:
                # Log once, then stop. Repeating this every flush would bury
                # the one line that says what is actually wrong.
                self._auth_failed = True
                self._log("authentication failed; telemetry disabled for this process")
                self.stats.dropped_failed += len(batch)
                return

            if status == 413 and len(batch) > 1:
                # Over the cap. Split rather than drop — the events are fine,
                # there are simply too many in one request.
                middle = len(batch) // 2
                self._deliver(batch[:middle])
                self._deliver(batch[middle:])
                return

            if status not in RETRYABLE_STATUS and status != 0:
                # A 400 fails identically on retry.
                self._log(f"batch rejected ({status} {detail}); dropping {len(batch)}")
                self.stats.dropped_failed += len(batch)
                return

            if attempt < self._max_retries:
                time.sleep(delay)
                delay *= 2

        self._log(f"delivery failed after retries ({status} {detail}); dropping {len(batch)}")
        self.stats.dropped_failed += len(batch)

    def flush(self, timeout: float = 5.0) -> None:
        """Deliver what is queued. Bounded, so a short script cannot hang."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._queue:
                    return
            self._wake.set()
            time.sleep(0.05)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Flush and stop. Registered with atexit so a short script keeps its
        only trace, and bounded so it cannot delay the exit indefinitely."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._wake.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=timeout)
        else:
            self._drain()

    def _log(self, message: str) -> None:
        # Off unless asked. An SDK that prints during an outage becomes the
        # noisiest thing in the customer's logs.
        if self._debug or os.environ.get("METRION_DEBUG") == "1":
            import sys

            print(f"[metrion] {message}", file=sys.stderr)
