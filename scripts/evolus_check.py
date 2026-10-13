"""Smoke test for Evolus agent chat: list the agents this API key can reach, then say hello to ours.

Usage: python scripts/evolus_check.py
Reads EVOLUS_API_KEY, EVOLUS_AGENT_ID ("<applicationId>:<agentId>", optional) and EVOLUS_BASE_URL (optional).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a plain script from the repo
from agent.evolus_client import evolus_client  # noqa: E402


def main() -> int:
    client = evolus_client()
    agents = [m.id for m in client.models.list().data]
    print("Agents (use one as EVOLUS_AGENT_ID):")
    for agent_id in agents:
        print(f"  {agent_id}")
    agent_id = os.environ.get("EVOLUS_AGENT_ID")
    if not agent_id:
        print("Set EVOLUS_AGENT_ID to one of these, then run this again.")
        return 1
    if agent_id not in agents:
        print(f"FAIL: {agent_id} is not in the list.")
        return 1
    start = time.time()
    reply = client.chat.completions.create(
        model=agent_id, messages=[{"role": "user", "content": "Which tools can you use? Answer in one line."}]
    )
    print(f"Answered in {time.time() - start:.1f}s: {reply.choices[0].message.content!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
