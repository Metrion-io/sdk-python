# Metrion SDK for Python

The reference SDK for [Metrion](https://github.com/Metrion-io) — an AI & data
engineering platform. It instruments an application so its LLM calls show up as
traces, and is the first of the planned SDKs (Python, then TypeScript, Go, Rust).

> **Status: empty repo.** Nothing here is implemented. This README is the
> design contract the implementation is expected to meet — it describes intent,
> not behaviour you can rely on today.

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

```env
METRION_API_KEY=mt_...
METRION_BASE_URL=https://telemetry.metrion.io
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
