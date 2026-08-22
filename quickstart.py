"""Runs the manual API against the live deployment.

    METRION_API_KEY=mt_live_... python3 quickstart.py

Prints the trace id so the result can be checked in the platform, and the
delivery stats so a silent failure is visible rather than looking like success.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from metrion import Metrion

if not os.environ.get("METRION_API_KEY"):
    sys.exit("METRION_API_KEY is not set")

metrion = Metrion()
print(f"sending to {metrion.base_url}")

with metrion.trace(name="quickstart-agent", user_id="demo-user",
                   metadata={"source": "quickstart"}) as trace:
    with trace.span(name="retrieve-context") as span:
        span.end(output="3 documents")

    generation = trace.generation(name="answer-user", model="gpt-4o")
    generation.end(
        output="The answer is 4.",
        usage={"input_tokens": 120, "output_tokens": 8},
    )

metrion.shutdown()
print(f"trace_id: {trace.trace_id}")
print(f"stats:    {metrion.stats}")
