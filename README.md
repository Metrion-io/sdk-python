# Metrion SDK for Python

The reference SDK for [Metrion](https://github.com/Metrion-io) — an AI & data
engineering platform. It instruments an application so its LLM calls show up as
traces, and is the first of the planned SDKs (Python, then TypeScript, Go, Rust).

> **Status: alpha.** The transport, the manual API and the OpenAI drop-in are
> implemented and verified against the live deployment. Not yet published to
> PyPI, and `pricing.py` (best-effort cost) is still to come.

## The rule this SDK exists to honour

> Telemetry must never sit in the inference critical path.

A customer's LLM call cannot fail, slow down, or throw because Metrion is
unavailable. Delivery **fails open**: on any error the SDK buffers, then drops.
It never raises into the calling application.

Inference traffic goes straight to the provider. Telemetry leaves out-of-band:

```
Application
    ↓
Metrion instrumentation ──▶ local queue ──▶ background batch ──▶ telemetry.metrion.io
    ↓
Official provider SDK ──▶ OpenAI
```

## Intended usage

Transparent OpenAI instrumentation is the first DX target — wrap the official
SDK, never reimplement it:

```python
from metrion.openai import OpenAI

client = OpenAI()

response = client.chat.completions.create(
    model="...",
    messages=[...],
)
```

Group related calls into a conversation by passing `session_id` (and optionally
`user_id`) on the trace. Nested generations inherit both; the Sessions page
only lists traces that actually sent a `session_id`.

```python
from metrion import Metrion

metrion = Metrion()
with metrion.trace(name="support", session_id="chat-42", user_id="user-9"):
    client.chat.completions.create(...)
```

```env
METRION_API_KEY=mt_live_...
METRION_BASE_URL=https://ingest-metrion.syncloud.my.id
```

Server-side, one key. The `mt_pk_` / `mt_sk_` split in the plan is deliberately
deferred: a public key with no rate limit or origin control is a way to have
someone else's traffic billed to a customer's project.

## Install

Not on PyPI. Install from git:

```bash
pip install "git+https://github.com/Metrion-io/sdk-python.git"
uv pip install "git+https://github.com/Metrion-io/sdk-python.git"
```

With the OpenAI drop-in:

```bash
pip install "metrion[openai] @ git+https://github.com/Metrion-io/sdk-python.git"
```

Pin a commit for anything that matters — a branch moves under you:

```bash
pip install "git+https://github.com/Metrion-io/sdk-python.git@<sha>"
```

The manual API has **no dependencies at all**: the transport is `urllib`, so
nothing is pulled into the caller's environment. `openai` is an extra, not a
requirement.

## Verify it yourself

```bash
python3 tests/run_all.py                       # 30 tests, no network
METRION_API_KEY=mt_live_... python3 quickstart.py
```

## What the SDK owns

| Concern | Responsibility |
|---|---|
| Context propagation | carry `trace_id` / `span_id` across async and task boundaries (`contextvars`) |
| Instrumentation | auto-instrument known libraries; manual API otherwise |
| Buffering | bounded in-memory queue, batcher, background sender |
| Fail-open | never throw into the app — buffer, then drop |
| Config | base URL, API key, flush / queue / retry knobs |

The SDK is a **convenience layer, not the architecture**. Anything it does must
be reachable with raw HTTP to `/v1/events/batch`, and no Metrion service may
assume a customer is using it.

## Related

| Repo | Role |
|---|---|
| [`telemetry`](https://github.com/Metrion-io/telemetry) | Where this SDK sends events |
| [`gateway`](https://github.com/Metrion-io/gateway) | Issues the API keys it authenticates with |
| [`platform`](https://github.com/Metrion-io/platform) | Where the resulting traces are displayed |
| [`ops`](https://github.com/Metrion-io/ops) | Internal admin |
| [`clickhouse`](https://github.com/Metrion-io/clickhouse) | Where those events come to rest |
