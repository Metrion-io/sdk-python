#!/usr/bin/env python3
"""Check the Metrion ingestion backend before writing any SDK code.

Sends exactly the shapes the SDK will send — a nested trace/span/generation,
a batch, an error event — and reads them back. If this passes, the backend is
not what is blocking you.

    METRION_API_KEY=mt_live_... python3 verify_backend.py

The key needs `telemetry:write` and `telemetry:read`. Mint one at
https://gateway-metrion.syncloud.my.id (register, create a project, create a
key) or ask for one.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("METRION_BASE_URL", "https://ingest-metrion.syncloud.my.id")
KEY = os.environ.get("METRION_API_KEY", "")

passed = failed = 0


def call(method: str, path: str, body=None, key: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    request.add_header("Authorization", f"Bearer {key if key is not None else KEY}")
    # Cloudflare fronts this host and answers 403 (error 1010) to a request
    # with no User-Agent — which is exactly what urllib sends by default.
    # Every SDK must set one; httpx does, urllib does not.
    request.add_header("User-Agent", "metrion-verify/1.0")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return error.code, {"raw": raw[:200].decode(errors="replace")}


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  \033[32mPASS\033[0m  {label}")
    else:
        failed += 1
        print(f"  \033[31mFAIL\033[0m  {label}  {detail}")


run = str(int(time.time()))
print(f"\nMetrion backend check — {BASE}\n")

if not KEY:
    print("  METRION_API_KEY is not set.\n")
    sys.exit(2)

# ─── auth ────────────────────────────────────────────────────────────────
status, _ = call("POST", "/v1/events", {"type": "generation", "trace_id": "x"}, key="mt_live_bogus")
check("a bogus key is refused (401)", status == 401, f"got {status}")

status, _ = call("GET", "/v1/traces")
check("the supplied key can read", status == 200, f"got {status}")

# ─── the shape the SDK sends ─────────────────────────────────────────────
trace_id = f"tr-verify-{run}"
status, body = call("POST", "/v1/events/batch", [
    {
        "type": "trace", "trace_id": trace_id, "span_id": "root",
        "name": "customer-support-agent", "user_id": "user_123",
        "metadata": {"tenant": "acme"},
    },
    {
        "type": "span", "trace_id": trace_id, "span_id": "s1",
        "parent_span_id": "root", "name": "retrieve-context", "latency_ms": 45,
    },
    {
        "type": "generation", "trace_id": trace_id, "span_id": "g1",
        "parent_span_id": "root", "name": "answer-user", "model": "gpt-5",
        "provider": "openai",
        "input": [{"role": "user", "content": "Hello"}],
        "output": "Hi there",
        "prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500,
        "latency_ms": 890, "ttft_ms": 210, "cost_usd": 0.0045,
    },
])
check("nested trace + span + generation accepted (202)",
      status == 202 and body.get("data", {}).get("accepted") == 3, f"got {status} {body}")

status, _ = call("POST", "/v1/events", {
    "type": "generation", "trace_id": f"tr-err-{run}", "name": "failed-call",
    "model": "gpt-5", "status": "error", "error": "upstream 429", "latency_ms": 30000,
})
check("an error event is accepted (202)", status == 202, f"got {status}")

# ─── what the transport layer will hit ───────────────────────────────────
status, body = call("POST", "/v1/events/batch", [
    {"type": "span", "trace_id": f"tr-bulk-{run}", "name": "bulk", "latency_ms": 5}
    for _ in range(500)
])
check("a 500-event batch is accepted", status == 202, f"got {status}")

status, _ = call("POST", "/v1/events/batch", [
    {"type": "span", "trace_id": "tr-over", "name": "x"} for _ in range(1001)
])
# Refused rather than truncated: a silent truncation would leave the SDK
# believing it had delivered.
check("a batch over 1000 is refused (413)", status == 413, f"got {status}")

status, _ = call("POST", "/v1/events", {
    "type": "generation", "trace_id": f"tr-big-{run}", "output": "ok", "input": "x" * 200_000,
})
check("a 200KB payload is accepted", status == 202, f"got {status}")

status, _ = call("POST", "/v1/events", {"type": "generation"})
check("a missing trace_id is rejected (400)", status == 400, f"got {status}")

# ─── read back ───────────────────────────────────────────────────────────
time.sleep(1.5)
status, body = call("GET", f"/v1/traces/{trace_id}")
events = body.get("data") or []
check("the trace reads back with all three events",
      status == 200 and len(events) == 3, f"got {status}, {len(events)} events")

by_type = {e["type"]: e for e in events}
generation = by_type.get("generation", {})
check("usage survived the round trip",
      generation.get("total_tokens") == 1500 and generation.get("ttft_ms") == 210,
      f"tokens={generation.get('total_tokens')} ttft={generation.get('ttft_ms')}")
check("nesting survived (parent_span_id kept)",
      by_type.get("span", {}).get("parent_span_id") == "root",
      f"got {by_type.get('span', {}).get('parent_span_id')!r}")

status, body = call("GET", "/v1/metrics/summary")
check("metrics summary answers", status == 200 and "traces" in (body.get("data") or {}),
      f"got {status}")

print(f"\n  {passed} passed, {failed} failed\n")
sys.exit(1 if failed else 0)
