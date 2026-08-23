"""One real LLM call through Orvix, traced by Metrion.

    export ORVIX_API_KEY=orv-sk_live_...
    export METRION_API_KEY=mt_live_...
    python3 examples/orvix_end_to_end.py

This is the honest test: a genuine inference through the AI router, wrapped by
the SDK, landing as a trace. It exercises the whole chain at once — the
wrapper, the queue, the ingest, and the router's own billing — which is
something no amount of hand-written JSON can do.

The two products stay separate. Orvix serves the model; Metrion stores the
trace. They share nothing but this script, and each authenticates with its own
key. Neither knows the other exists.

Nothing here is required for either product to work: the SDK talks to any
OpenAI-compatible endpoint, and Metrion accepts events from anything that can
POST JSON.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from metrion import Metrion
from metrion.openai import OpenAI

ORVIX_BASE = os.environ.get("ORVIX_BASE_URL", "https://api.orvix.id/v1")
MODEL = os.environ.get("ORVIX_MODEL", "orvix/deepseek-v4-flash")

orvix_key = os.environ.get("ORVIX_API_KEY", "")
if not orvix_key:
    sys.exit("ORVIX_API_KEY is not set")
if not os.environ.get("METRION_API_KEY"):
    sys.exit("METRION_API_KEY is not set")

metrion = Metrion()
print(f"model     {MODEL}")
print(f"router    {ORVIX_BASE}")
print(f"telemetry {metrion.base_url}")

# The drop-in. Everything the official client accepts is forwarded, which is
# how it points at Orvix rather than OpenAI.
client = OpenAI(api_key=orvix_key, base_url=ORVIX_BASE, metrion=metrion)

with metrion.trace(name="orvix-end-to-end") as trace:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "Answer in one short sentence."},
            {"role": "user", "content": "What is a trace, in observability?"},
        ],
        max_tokens=60,
    )

answer = response.choices[0].message.content
usage = response.usage

print()
print(f"answer    {answer!r}")
print(f"tokens    {usage.prompt_tokens} in / {usage.completion_tokens} out")

# Bounded, so a broken telemetry backend cannot hang the script — the same
# promise the SDK makes to a real application.
metrion.shutdown(timeout=10)

print(f"trace_id  {trace.trace_id}")
print(f"stats     {metrion.stats}")
print()
print("Open the trace:")
print(f"  https://metrion.syncloud.my.id/traces/{trace.trace_id}")

# Delivery is fail-open by design, so "the call worked" and "the trace landed"
# are separate facts and the script says which is which.
if metrion.stats.get("sent", 0) == 0:
    print()
    print("WARNING: nothing was delivered to Metrion. The inference still")
    print("succeeded — that is the fail-open contract working, not a bug here.")
    sys.exit(1)
