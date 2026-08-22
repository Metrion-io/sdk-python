"""The current trace and span, across async and thread boundaries.

`contextvars` rather than a thread local: an async application runs many
requests on one thread, and a thread local would attribute all of their spans
to whichever happened to set it last. contextvars follow the task.
"""

from __future__ import annotations

from contextvars import ContextVar

_current_trace: ContextVar[str | None] = ContextVar("metrion_trace", default=None)
_current_span: ContextVar[str | None] = ContextVar("metrion_span", default=None)


def current_trace_id() -> str | None:
    return _current_trace.get()


def current_span_id() -> str | None:
    return _current_span.get()


def set_current(trace_id: str | None, span_id: str | None):
    """Returns the tokens needed to restore what was there before.

    Restoring rather than clearing, because spans nest: ending a child must
    return the parent to being current, not leave the context empty.
    """
    return _current_trace.set(trace_id), _current_span.set(span_id)


def reset_current(tokens) -> None:
    trace_token, span_token = tokens
    _current_trace.reset(trace_token)
    _current_span.reset(span_token)
