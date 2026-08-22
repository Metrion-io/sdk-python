"""A drop-in `OpenAI` that records what it sees.

    from metrion.openai import OpenAI
    client = OpenAI()

Wraps the official SDK, never reimplements it. Every attribute that is not
`chat.completions.create` is forwarded untouched, so a caller keeps the real
client's behaviour, return types, exceptions, and any method added after this
was written.

Transparency is the contract. Same return value, same exception type, same
streaming chunks. If instrumentation would change any of those, it is a bug in
this file — a wrapper that alters behaviour is worse than no wrapper, because
the difference only shows up in production.
"""

from __future__ import annotations

import time
from typing import Any

from ..client import Metrion
from ..context import current_span_id, current_trace_id
from ..models import STATUS_ERROR, new_id

try:  # pragma: no cover - exercised by absence, not by tests
    import openai as _openai
except ImportError:  # pragma: no cover
    _openai = None


def _require_openai():
    if _openai is None:
        raise ImportError(
            "The openai package is not installed. "
            "Install it with: pip install metrion[openai]"
        )
    return _openai


class _Completions:
    """Instruments `create`, forwards everything else."""

    def __init__(self, inner, metrion: Metrion) -> None:
        self._inner = inner
        self._metrion = metrion

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def create(self, *args, **kwargs):
        model = kwargs.get("model", "")
        streaming = bool(kwargs.get("stream"))
        started = time.monotonic()

        generation = self._metrion.generation(
            name="chat.completions",
            model=model,
            provider="openai",
            input=kwargs.get("messages"),
        )

        try:
            result = self._inner.create(*args, **kwargs)
        except Exception as error:
            # Record, then re-raise unchanged. The caller must see the
            # provider's own exception type, not ours.
            generation.status = STATUS_ERROR
            generation.error = f"{type(error).__name__}: {error}"
            generation.end()
            raise

        if streaming:
            # A stream cannot be measured until it is consumed, so the
            # generation ends when the iterator does — not here.
            return _StreamProxy(result, generation, started)

        generation.end(**_from_response(result))
        return result


class _StreamProxy:
    """Passes chunks through, then records once the stream finishes.

    Yields exactly what the underlying iterator yields. Anything else — a
    buffered copy, a transformed chunk — would break callers who parse deltas
    themselves, which is most of them.
    """

    def __init__(self, inner, generation, started: float) -> None:
        self._inner = inner
        self._generation = generation
        self._started = started
        self._ttft_ms: int | None = None
        self._text: list[str] = []
        self._usage: dict[str, int] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __iter__(self):
        try:
            for chunk in self._inner:
                if self._ttft_ms is None:
                    # Time to first token: the number a user actually feels,
                    # and one only the streaming path can measure.
                    self._ttft_ms = int((time.monotonic() - self._started) * 1000)
                self._absorb(chunk)
                yield chunk
        except Exception as error:
            self._generation.status = STATUS_ERROR
            self._generation.error = f"{type(error).__name__}: {error}"
            self._finish()
            raise
        self._finish()

    def __enter__(self):
        inner_enter = getattr(self._inner, "__enter__", None)
        if inner_enter is not None:
            inner_enter()
        return self

    def __exit__(self, *exc):
        inner_exit = getattr(self._inner, "__exit__", None)
        if inner_exit is not None:
            return inner_exit(*exc)
        return False

    def _absorb(self, chunk: Any) -> None:
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self._usage = _usage_dict(usage)
        for choice in getattr(chunk, "choices", None) or []:
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None) if delta else None
            if content:
                self._text.append(content)

    def _finish(self) -> None:
        self._generation.end(
            output="".join(self._text) or None,
            usage=self._usage,
            ttft_ms=self._ttft_ms,
        )


class _AsyncCompletions(_Completions):
    async def create(self, *args, **kwargs):  # type: ignore[override]
        model = kwargs.get("model", "")
        streaming = bool(kwargs.get("stream"))
        started = time.monotonic()

        generation = self._metrion.generation(
            name="chat.completions",
            model=model,
            provider="openai",
            input=kwargs.get("messages"),
        )

        try:
            result = await self._inner.create(*args, **kwargs)
        except Exception as error:
            generation.status = STATUS_ERROR
            generation.error = f"{type(error).__name__}: {error}"
            generation.end()
            raise

        if streaming:
            return _AsyncStreamProxy(result, generation, started)

        generation.end(**_from_response(result))
        return result


class _AsyncStreamProxy(_StreamProxy):
    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        try:
            async for chunk in self._inner:
                if self._ttft_ms is None:
                    self._ttft_ms = int((time.monotonic() - self._started) * 1000)
                self._absorb(chunk)
                yield chunk
        except Exception as error:
            self._generation.status = STATUS_ERROR
            self._generation.error = f"{type(error).__name__}: {error}"
            self._finish()
            raise
        self._finish()

    async def __aenter__(self):
        inner = getattr(self._inner, "__aenter__", None)
        if inner is not None:
            await inner()
        return self

    async def __aexit__(self, *exc):
        inner = getattr(self._inner, "__aexit__", None)
        if inner is not None:
            return await inner(*exc)
        return False


class _Chat:
    def __init__(self, inner, metrion: Metrion, completions_cls=_Completions) -> None:
        self._inner = inner
        self.completions = completions_cls(inner.completions, metrion)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ClientProxy:
    """Everything except `chat` is the real client, untouched."""

    _completions_cls = _Completions
    _client_cls_name = "OpenAI"

    def __init__(self, *args, metrion: Metrion | None = None, **kwargs) -> None:
        sdk = _require_openai()
        inner_cls = getattr(sdk, self._client_cls_name)
        self._inner = inner_cls(*args, **kwargs)
        self._metrion = metrion or Metrion()
        self.chat = _Chat(self._inner.chat, self._metrion, self._completions_cls)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class OpenAI(_ClientProxy):
    _completions_cls = _Completions
    _client_cls_name = "OpenAI"


class AsyncOpenAI(_ClientProxy):
    _completions_cls = _AsyncCompletions
    _client_cls_name = "AsyncOpenAI"


def _usage_dict(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    out = {}
    # Cached tokens live one level down in OpenAI's shape, and they matter:
    # they are priced at a fraction of fresh input.
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details else None
    if cached is not None:
        out["cached_tokens"] = cached
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if value is not None:
            out[key] = value
    return out or None


def _from_response(response: Any) -> dict[str, Any]:
    text = None
    choices = getattr(response, "choices", None) or []
    if choices:
        message = getattr(choices[0], "message", None)
        text = getattr(message, "content", None) if message else None
    return {"output": text, "usage": _usage_dict(getattr(response, "usage", None))}


__all__ = ["OpenAI", "AsyncOpenAI"]
