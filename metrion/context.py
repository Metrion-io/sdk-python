"""The current trace and span, across async and thread boundaries.

`contextvars` rather than a thread local: an async application runs many
requests on one thread, and a thread local would attribute all of their spans
to whichever happened to set it last. contextvars follow the task.
"""

from __future__ import annotations

from contextvars import ContextVar

_current_trace: ContextVar[str | None] = ContextVar("metrion_trace", default=None)
_current_span: ContextVar[str | None] = ContextVar("metrion_span", default=None)
_current_session: ContextVar[str | None] = ContextVar("metrion_session", default=None)
_current_user: ContextVar[str | None] = ContextVar("metrion_user", default=None)


def current_trace_id() -> str | None:
    return _current_trace.get()


def current_span_id() -> str | None:
    return _current_span.get()


def current_session_id() -> str | None:
    return _current_session.get()


def current_user_id() -> str | None:
    return _current_user.get()


def set_current(
    trace_id: str | None,
    span_id: str | None,
    session_id: str | None = None,
    user_id: str | None = None,
):
    """Returns the tokens needed to restore what was there before.

    Restoring rather than clearing, because spans nest: ending a child must
    return the parent to being current, not leave the context empty.
    """
    return (
        _current_trace.set(trace_id),
        _current_span.set(span_id),
        _current_session.set(session_id),
        _current_user.set(user_id),
    )


def reset_current(tokens) -> None:
    trace_token, span_token, session_token, user_token = tokens
    _current_trace.reset(trace_token)
    _current_span.reset(span_token)
    _current_session.reset(session_token)
    _current_user.reset(user_token)
