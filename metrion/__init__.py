"""Metrion — traces for LLM applications.

    from metrion import Metrion

    metrion = Metrion()                      # reads METRION_API_KEY
    with metrion.trace(name="agent") as t:
        with t.span(name="retrieve"):
            ...
        gen = t.generation(name="answer", model="gpt-5")
        gen.end(output="...", usage={"input_tokens": 1200, "output_tokens": 300})

Pass `session_id` and `user_id` on `trace()` to group related executions.
Nested spans inherit them. Sessions in the dashboard only appear when
`session_id` was actually sent — Metrion does not invent one.

Telemetry never sits in the inference path: delivery is buffered, bounded, and
fails open. If Metrion is unreachable the SDK drops events and counts them; it
does not raise into the application.
"""

from .client import (
    DEFAULT_BASE_URL,
    VERSION,
    Generation,
    Metrion,
    Retrieval,
    Span,
    ToolCall,
    Trace,
)
from .models import STATUS_ERROR, STATUS_OK, Event

__version__ = VERSION
__all__ = [
    "Metrion", "Trace", "Span", "Generation", "ToolCall", "Retrieval",
    "Event", "STATUS_OK", "STATUS_ERROR", "DEFAULT_BASE_URL", "VERSION",
]
