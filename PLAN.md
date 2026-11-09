# Metrion Python SDK — Implementation Plan

**Status: alpha, implemented.** Transport, manual API, OpenAI drop-in, and
pricing are built; 43 tests pass offline. Not yet published to PyPI.

The two surfaces below are what the SDK delivers, and they are not
alternatives — the drop-in exists so a customer can start in one line, and the
manual API exists because agents and RAG pipelines are not a single model call.

---

## The rule everything is subordinate to

> Telemetry must never sit in the inference critical path.

A customer's LLM call cannot fail, slow down, or throw because Metrion is
unavailable. Every design choice below follows from that, and any change that
weakens it is a change to the product, not to the SDK.

Concretely: delivery **fails open** — buffer, then drop. The SDK never raises
into the calling application, and inference traffic goes straight to the
provider. Metrion is never a proxy.

---

## Surface 1 — the drop-in wrapper

```python
from metrion.openai import OpenAI

client = OpenAI(api_key="sk-...")

response = client.chat.completions.create(
    model="gpt-5",
    messages=[{"role": "user", "content": "Hello"}],
)
```

One import changed, nothing else. The call signature, the return value and the
exceptions are the official SDK's, because they *are* the official SDK's.

```
User application
      │
      ▼
metrion.openai.OpenAI          ← thin wrapper, not a reimplementation
      │
      ├── open trace + generation span
      │
      ▼
Official OpenAI Python SDK     ← the real client, untouched
      │
      ▼
OpenAI API
      │
      ▼
response
      │
      ├── capture usage, latency, model, status/error, input/output, cost
      └── close span ─────────┐
      │                       │
      ▼                       ▼
returned to caller     bounded queue → background batcher
                              │
                              ▼
                    POST /v1/events/batch  (ingest-metrion.syncloud.my.id)
                              ▼
                        telemetry (Actix)
                              ▼
                          ClickHouse
```

**Never reimplement the provider SDK.** Wrap or instrument it. A
reimplementation has to chase every parameter, every streaming change and
every new endpoint, and it will be wrong the week after it ships.

### What the wrapper must handle

| Case | Requirement |
|---|---|
| Streaming | span closes on the **last** chunk, not the first; TTFT recorded at the first |
| Exceptions | the provider's exception propagates **unchanged**; the span records the error and closes |
| Async | `AsyncOpenAI` wrapped the same way, sharing one queue |
| Cost | computed from usage against a price table, `None` when the model is unknown — a guessed cost is worse than none |
| Nested calls | a call inside an active trace becomes a child span, not a new trace |

---

## Surface 2 — the manual API

For anything that is not one model call: agents, RAG, tool loops.

```python
from metrion import Metrion

metrion = Metrion(public_key="mt_pk_xxx", secret_key="mt_sk_xxx")

trace = metrion.trace(
    name="customer-support-agent",
    user_id="user_123",
    metadata={"tenant": "acme"},
)

span = trace.span(name="retrieve-context")
# ...
span.end()

generation = trace.generation(name="answer-user", model="gpt-5")
# ...
generation.end(output="...", usage={"input_tokens": 1200, "output_tokens": 300})
```

Also as context managers, because `end()` is easy to skip on an exception path:

```python
with metrion.trace(name="agent") as trace:
    with trace.span(name="retrieve"):
        ...
```

`__exit__` closes the span and records the exception if one is in flight.

### Objects

| Object | Is | Emits |
|---|---|---|
| `Trace` | one logical request | `type: "trace"` |
| `Span` | one operation inside it | `type: "span"` |
| `Generation` | a model inference | `type: "generation"` |
| `ToolCall` / `Retrieval` | typed spans | `type: "tool_call"` / `"retrieval"` |

Ids are ULIDs generated client-side, so a caller can reference a trace before
the server has seen it.

Nesting uses `contextvars`, not a global — a global breaks under asyncio and
under threads, and the failure is silent cross-talk between concurrent
requests, which is worse than an error.

---

## Package layout

```
metrion/
├── __init__.py        Metrion, Trace, Span, Generation
├── client.py          config, auth, the public entry point
├── transport.py       bounded queue, batcher, background sender, retry
├── context.py         contextvars: the current trace and span
├── models.py          the wire event, matching the ingest contract
├── pricing.py         model → cost table, best-effort
└── openai/
    └── __init__.py    the drop-in OpenAI / AsyncOpenAI
```

Dependencies: `httpx` and nothing else required. `openai` is an **extra** —
`pip install metrion[openai]` — so a customer using only the manual API does
not inherit a provider SDK they never call.

---

## Transport, and how it fails

```
span.end() → queue.put_nowait(event)   ← never blocks the caller
                    │
        bounded queue (default 10_000)
                    │
     background thread / task, batches by size or age
                    │
            POST /v1/events/batch
                    │
     ┌──────────────┴──────────────┐
   2xx                          4xx / 5xx / timeout
   done            retry with backoff, then DROP and count
```

| Decision | Why |
|---|---|
| **Bounded** queue | an unbounded one turns a Metrion outage into the customer's OOM |
| `put_nowait`, never block | blocking on a full queue would put Metrion back in the critical path |
| Drop **oldest** when full | the newest events describe what is happening now |
| Batch on size **or** age | a low-traffic app must not hold its first trace for an hour |
| Retry only 5xx and timeouts | a 4xx will fail identically on retry; retrying it is just load |
| **Never raise** | not into the app, not from the background worker |
| `atexit` flush, bounded | a short script must not lose its only trace, and must not hang on exit |
| Count drops, expose the count | silent data loss is indistinguishable from "nothing happened" |

`MTERION_DEBUG=1` logs delivery failures to stderr. Off by default: an SDK that
prints during an outage becomes the noisiest thing in the customer's logs.

---

## The backend is ready — verify it yourself first

```bash
METRION_API_KEY=mt_live_... python3 verify_backend.py
```

12 checks against the live deployment: the nested trace/span/generation shape,
a 500-event batch, a 200KB payload, the 1000-event cap, auth rejection, and a
read-back that confirms usage and nesting survived. All passing as of this
commit.

**Base URL:** `https://ingest-metrion.syncloud.my.id`

| Endpoint | Use |
|---|---|
| `POST /v1/events` | one event |
| `POST /v1/events/batch` | up to 1000 |
| `GET /v1/traces` | list, for verifying delivery |
| `GET /v1/traces/{id}` | one trace with every span |

Measured, not assumed: a 500-event batch round-trips in **~250 ms**, and
payloads up to 200KB are accepted.

### ⚠ Set a User-Agent, or Cloudflare answers 403

The host sits behind Cloudflare, which rejects requests with **no** User-Agent
header — `error code: 1010`, a plain 403 with no JSON body. Python's `urllib`
sends none by default, so the first thing that hits this is a hand-rolled
transport during development.

`httpx` sets one, so the SDK as planned is unaffected. Set an explicit
`metrion-python/<version>` anyway: it makes the SDK's traffic identifiable in
logs, and it means a future WAF rule cannot silently break every customer.

### Status codes the transport must handle

| Code | Meaning | What the SDK does |
|---|---|---|
| `202` | stored | drop from the queue |
| `400` | malformed batch | **do not retry** — it fails identically |
| `401` | key unknown, revoked, or wrong scope | stop retrying; log once |
| `413` | over 1000 events | split the batch |
| `403` | Cloudflare, not the API | check the User-Agent before blaming auth |
| `503` | write failed downstream | **retry** with backoff |

`503` matters: telemetry answers it rather than a false `202` when the
ClickHouse write fails, precisely so a buffering SDK retries instead of
dropping events it was told had landed.

## Configuration

```env
METRION_PUBLIC_KEY=mt_pk_...
METRION_SECRET_KEY=mt_sk_...
METRION_BASE_URL=https://ingest-metrion.syncloud.my.id
```

Explicit arguments beat environment. `METRION_ENABLED=false` disables the SDK
entirely — instrumentation stays in the code, nothing is sent, so a customer
can switch it off without a deploy that touches their call sites.

---

## ⚠ Blocked on a decision — the two-key split

The examples use **`mt_pk_` (public) and `mt_sk_` (secret)**. Today's platform
issues **one** key, `mt_live_`, scoped by `telemetry:write` / `telemetry:read`.

This is not a naming difference. A publishable key is one that may be exposed
— shipped in a browser bundle or a mobile app — and that changes what
ingestion must assume about the caller:

| | one key (today) | two keys (the examples) |
|---|---|---|
| Server-side use | works | works |
| Browser / mobile | credential is readable by anyone | intended |
| Ingestion trust | caller is authenticated | **a public key can be used by anyone** |
| Needed | — | per-key rate limits, origin allowlists, spend caps, and events marked lower-trust |

Building the SDK against `mt_pk_` / `mt_sk_` means committing the platform to
issuing them and to hardening ingestion for anonymous traffic. That is a
product decision, not an SDK one.

**Three options:**

1. **Ship server-side only, one key.** `Metrion(api_key="mt_live_...")`.
   Fastest, honest about today's platform. Browser support later.
2. **Add the split now.** Gateway issues both, ingestion treats `pk` traffic as
   untrusted. Correct long-term, and the larger piece of work is on the
   platform side, not the SDK.
3. **Accept both.** `Metrion(secret_key=...)` and `Metrion(api_key=...)` as an
   alias. Ships now, keeps the door open — at the cost of two auth paths to
   maintain.

**Recommendation: 1 now, 2 when a browser SDK is actually on the roadmap.**
Every example in this plan is server-side, and a public key with no rate
limiting or origin control is a way to have someone else's traffic billed to a
customer's project.

*Also open, and smaller:* the platform currently has no per-key rate limit or
spend cap. Not needed for a server-side SDK; required before any public key
exists.

---

## Order of work

All steps below are **implemented** (alpha). PyPI publish and a polished
quickstart doc are the remaining follow-ups.

1. ~~`transport.py` + `models.py`~~ — fail-open queue, tested against a stub
2. ~~`context.py` + trace/span/generation~~ — the manual API
3. ~~`metrion.openai`~~ — the drop-in, sync then async, streaming
4. ~~`pricing.py`~~ — best-effort cost
5. Quickstart against `ingest-metrion.syncloud.my.id` — `verify_backend.py` + `examples/orvix_e2e.py`

---

## How it gets tested

- **No network in the test suite.** A stub sender records batches; the E2E
  script talks to the real deployment, as `intelligence/e2e.sh` already does.
- **Failure is the interesting case.** Ingest down, ingest slow, ingest
  returning 500, queue full — the assertion in each is that the *caller* is
  unaffected.
- **The wrapper must be transparent.** Same return value, same exception type,
  same streaming chunks as the unwrapped official SDK. A test that asserts
  equality against the real client, with telemetry disabled, is the honest
  version of that claim.

---

# Next — work packages for parallel workers (2026-11-09)

The alpha above is done. What follows is written for AI workers running in
parallel worktrees. The contracts this SDK must obey are owned elsewhere:
**C1/C2/C9/C10/C11** in `telemetry/PLAN.md`, **C3** in `gateway/PLAN.md`.
Behaviour must stay identical to `sdk-typescript` (same field names, same
fail-open rules, same status-code handling).

## Worker protocol

1. One WP = one branch `wp/<WP-ID>-<slug>` = one worktree = one PR.
   `git worktree add ../.worktrees/sdk-python-<WP-ID> -b wp/<WP-ID>-<slug> origin/main`
2. `Depends on` is hard. 3. Touch only `Owns`; hotspot `metrion/__init__.py`
   (exports) gets a minimal append, rebased before PR.
4. Contracts are law. Public API names below are binding — they mirror the
   TypeScript SDK so docs stay one page.
5. DoD: offline tests added; verify passes; status cell updated; PR pastes real output.
6. No network in tests. Never publish, never push to `main`.

**Verify**

```bash
python -m venv .venv && .venv/bin/pip install -e . -q && .venv/bin/python tests/run_all.py
```

## Work packages

| ID | Title | Depends on | Owns | Status |
|---|---|---|---|---|
| PYS-01 | Contract conformance tests | TEL-01 | `tests/contract/**` (vendored fixtures), `tests/test_contract.py` | todo |
| PYS-02 | `score()` API (C9) | TEL-02 merged for E2E; code can start now | `metrion/client.py` (`score` method only), `tests/test_score.py` | todo |
| PYS-03 | `mask` hook | — | `metrion/transport.py` (enqueue path), `tests/test_mask.py` | todo |
| PYS-04 | Anthropic drop-in | — | `metrion/anthropic/**`, `tests/test_anthropic_wrapper.py` | todo |
| PYS-05 | PyPI release workflow | **blocked (D4)** | `.github/workflows/release.yml` | blocked (D4) |
| PYS-06 | C10 metadata on `ToolCall` / `Retrieval` | — | `metrion/client.py` (those two classes), `tests/test_typed_spans.py` | todo |

PYS-02 and PYS-06 both edit `client.py` in disjoint classes; second to merge rebases.

### PYS-01
Copy `telemetry/contracts/` into `tests/contract/` (record the source commit
in `tests/contract/SOURCE`). For every valid fixture, building the event via
the public API and calling `to_payload()` yields the same JSON (key order free).
### PYS-02
`metrion.score(trace_id, name, value, *, span_id=None, comment=None, source="sdk")`
and `trace.score(name, value, comment=None)`. Emits C9. Never raises.
### PYS-03
`Metrion(mask=callable)` — called on every event payload before enqueue;
exception in `mask` → event dropped and counted in `stats()["masked_errors"]`,
never forwarded unmasked.
### PYS-04
`from metrion.anthropic import Anthropic, AsyncAnthropic` — wraps the official
SDK for `messages.create` incl. streaming; same rules as the OpenAI table above;
`provider="anthropic"`; usage from `input_tokens`/`output_tokens`.
### PYS-06
`trace.tool_call(name, arguments=…)` → `metadata["tool.name"]`, `["tool.arguments"]`;
`trace.retrieval(query=…, k=…)` + `end(documents=[…])` → C10 keys.
