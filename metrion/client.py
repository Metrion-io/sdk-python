"""The public entry point: config, and the objects a caller builds traces from.

Server-side, one key. The plan's examples use a `mt_pk_` / `mt_sk_` split, and
its own recommendation is to defer that until a browser SDK is real: a public
key with no rate limit or origin control is a way to have someone else's
traffic billed to a customer's project. The gateway issues `mt_live_` today,
so that is what this authenticates with.
"""

from __future__ import annotations

import os
import time
import traceback
from typing import Any

from .context import (
    current_session_id,
    current_span_id,
    current_trace_id,
    current_user_id,
    reset_current,
    set_current,
)
from .models import STATUS_ERROR, STATUS_OK, Event, new_id
from .transport import HttpSender, Transport

VERSION = "0.1.0"
DEFAULT_BASE_URL = "https://ingest-metrion.syncloud.my.id"


class _Node:
    """Shared behaviour for anything that starts, ends, and can nest.

    A context manager as well as an explicit `end()`, because `end()` is easy
    to skip on an exception path — and the exception path is when the trace
    matters most.
    """

    _type = "span"

    def __init__(
        self,
        client: "Metrion",
        *,
        trace_id: str,
        name: str,
        parent_span_id: str | None = None,
        model: str = "",
        provider: str = "",
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._client = client
        self.trace_id = trace_id
        self.span_id = new_id()
        self.parent_span_id = parent_span_id
        self.name = name
        self.model = model
        self.provider = provider
        self.input = input
        self.metadata = metadata
        # Inherit from the active trace so a nested generation() does not have
        # to repeat session_id / user_id. Explicit values still win.
        self.user_id = user_id or current_user_id()
        self.session_id = session_id or current_session_id()
        self.status = STATUS_OK
        self.error: str | None = None
        self.output: Any = None
        self.usage: dict[str, int] | None = None
        self.cost_usd: float | None = None
        self._started = time.monotonic()
        self._ended = False
        self._tokens = None

    # -- nesting ---------------------------------------------------------
    def span(self, name: str, **kw) -> "Span":
        return Span(self._client, trace_id=self.trace_id, name=name,
                    parent_span_id=self.span_id, **kw)

    def generation(self, name: str, **kw) -> "Generation":
        return Generation(self._client, trace_id=self.trace_id, name=name,
                          parent_span_id=self.span_id, **kw)

    # -- lifecycle -------------------------------------------------------
    def __enter__(self):
        self._tokens = set_current(
            self.trace_id,
            self.span_id,
            session_id=self.session_id,
            user_id=self.user_id,
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.status = STATUS_ERROR
            self.error = f"{exc_type.__name__}: {exc}"
        if self._tokens is not None:
            reset_current(self._tokens)
            self._tokens = None
        self.end()
        # Never swallow. The caller's exception is the caller's business.
        return False

    def end(self, output: Any = None, usage: dict[str, int] | None = None,
            cost_usd: float | None = None, **kw) -> None:
        if self._ended:
            # Ending twice would double-count. Common when a context manager
            # wraps code that also calls end() explicitly.
            return
        self._ended = True
        if output is not None:
            self.output = output
        if usage is not None:
            self.usage = usage
        if cost_usd is not None:
            self.cost_usd = cost_usd
        extra_metadata = kw.pop("metadata", None)
        if extra_metadata:
            self.metadata = {**(self.metadata or {}), **extra_metadata}
        for key, value in kw.items():
            setattr(self, key, value)
        self._client._emit(self._build())

    def _build(self) -> Event:
        latency_ms = int((time.monotonic() - self._started) * 1000)
        prompt = completion = total = None
        if self.usage:
            # Accept both spellings. OpenAI says prompt/completion, the plan's
            # examples say input/output, and a caller should not have to know
            # which one this SDK happens to prefer.
            prompt = self.usage.get("prompt_tokens", self.usage.get("input_tokens"))
            completion = self.usage.get("completion_tokens", self.usage.get("output_tokens"))
            total = self.usage.get("total_tokens")
            if total is None and prompt is not None and completion is not None:
                total = prompt + completion
            cached = self.usage.get("cached_tokens", self.usage.get("cached_prompt_tokens"))
            if cached is not None:
                self.metadata = {**(self.metadata or {}), "cached_tokens": cached}
            # Only when the caller did not say. A caller who knows the real
            # figure — a gateway that just billed it — is authoritative, and an
            # estimate must never overwrite a measurement.
            if self.cost_usd is None and self.model:
                from .pricing import estimate

                self.cost_usd = estimate(self.model, prompt, completion, cached)
        return Event(
            type=self._type,
            trace_id=self.trace_id,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            name=self.name,
            model=self.model,
            provider=self.provider,
            input=_stringify(self.input),
            output=_stringify(self.output),
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            latency_ms=latency_ms,
            cost_usd=self.cost_usd,
            status=self.status,
            error=self.error,
            user_id=self.user_id,
            session_id=self.session_id,
            metadata=self.metadata,
        )


class Span(_Node):
    _type = "span"


class Generation(_Node):
    _type = "generation"


class ToolCall(_Node):
    _type = "tool_call"


class Retrieval(_Node):
    _type = "retrieval"


class Trace(_Node):
    _type = "trace"

    def __init__(self, client: "Metrion", *, name: str, **kw) -> None:
        super().__init__(client, trace_id=new_id(), name=name, **kw)
        # A trace is its own root: children parent to it, and it parents to
        # nothing.
        self.span_id = self.trace_id
        self.parent_span_id = None


class Metrion:
    """The client. Construct once, reuse.

    Telemetry is disabled rather than raising when no key is configured: an
    application that forgot to set one should still run, and the SDK's whole
    contract is that it never breaks the caller.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        enabled: bool = True,
        debug: bool = False,
        transport: Transport | None = None,
        **transport_kw,
    ) -> None:
        self.api_key = api_key or os.environ.get("METRION_API_KEY", "")
        self.base_url = base_url or os.environ.get("METRION_BASE_URL", DEFAULT_BASE_URL)
        self.enabled = enabled and bool(self.api_key)
        self.debug = debug or os.environ.get("METRION_DEBUG") == "1"

        if transport is not None:
            self._transport: Transport | None = transport
        elif self.enabled:
            sender = HttpSender(self.base_url, self.api_key, f"metrion-python/{VERSION}")
            self._transport = Transport(sender, debug=self.debug, **transport_kw)
        else:
            self._transport = None

    # -- building --------------------------------------------------------
    def trace(self, name: str, **kw) -> Trace:
        return Trace(self, name=name, **kw)

    def span(self, name: str, **kw) -> Span:
        """A span under whatever is current, or a new trace if nothing is."""
        trace_id = current_trace_id() or new_id()
        return Span(self, trace_id=trace_id, name=name,
                    parent_span_id=current_span_id(), **kw)

    def generation(self, name: str, **kw) -> Generation:
        trace_id = current_trace_id() or new_id()
        return Generation(self, trace_id=trace_id, name=name,
                          parent_span_id=current_span_id(), **kw)

    # -- delivery --------------------------------------------------------
    def _emit(self, event: Event) -> None:
        if self._transport is None:
            return
        try:
            self._transport.enqueue(event)
        except Exception:  # noqa: BLE001 - the contract is that we never raise
            if self.debug:
                traceback.print_exc()

    def flush(self, timeout: float = 5.0) -> None:
        if self._transport is not None:
            self._transport.flush(timeout)

    def shutdown(self, timeout: float = 5.0) -> None:
        if self._transport is not None:
            self._transport.shutdown(timeout)

    @property
    def stats(self) -> dict[str, int]:
        return self._transport.stats.as_dict() if self._transport else {}


def _stringify(value: Any) -> str | None:
    """Payloads are String columns upstream, so anything structured is encoded.

    Truncated at 200KB because that is what the ingest was measured to accept;
    a larger body is refused outright, which would lose the whole event rather
    than the tail of one field.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        import json

        try:
            text = json.dumps(value, default=str)
        except Exception:  # noqa: BLE001
            text = str(value)
    limit = 200_000
    return text if len(text) <= limit else text[:limit]
