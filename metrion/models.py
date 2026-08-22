"""The wire event, and nothing more.

Field names are flat and match the ingest contract exactly: `prompt_tokens`
and `cost_usd` sit at the top level, not nested under a `usage` object. That
distinction is not cosmetic. Ingest validates only `type` and `trace_id`, so a
nested shape is accepted with a 202 and the token counts are silently dropped
— the event lands with NULL usage and nobody is told. Getting these names
right is the whole job of this module.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# What the receiver counts as a failure is `status != "ok"`, so these two
# words are the only ones that mean anything to its dashboards.
STATUS_OK = "ok"
STATUS_ERROR = "error"


def new_id() -> str:
    """An id for a trace or span.

    A UUID rather than anything ordered: ids are correlation keys, and an
    ordered id would leak call volume to anyone who sees two of them.
    """
    return uuid.uuid4().hex


def now_iso() -> str:
    """UTC, to milliseconds, in the format the ingest parses."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


@dataclass
class Event:
    """One row as the backend stores it.

    Optional fields are omitted from the payload rather than sent as null:
    ClickHouse keeps NULL for "not reported", and sending an explicit null for
    a value we simply do not have would erase that distinction.
    """

    type: str
    trace_id: str
    name: str = ""
    span_id: str | None = None
    parent_span_id: str | None = None
    model: str = ""
    provider: str = ""
    input: str | None = None
    output: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    ttft_ms: int | None = None
    cost_usd: float | None = None
    status: str = STATUS_OK
    error: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    timestamp: str = field(default_factory=now_iso)
    metadata: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
            "status": self.status,
        }
        if self.name:
            payload["name"] = self.name
        if self.model:
            payload["model"] = self.model
        if self.provider:
            payload["provider"] = self.provider

        for key in (
            "span_id",
            "parent_span_id",
            "input",
            "output",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "latency_ms",
            "ttft_ms",
            "cost_usd",
            "error",
            "session_id",
            "user_id",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value

        # metadata is a String column upstream, so it is encoded here. Passing
        # a dict works by accident today and would stop working the moment the
        # receiver stops coercing.
        if self.metadata:
            import json

            payload["metadata"] = json.dumps(self.metadata)

        return payload
