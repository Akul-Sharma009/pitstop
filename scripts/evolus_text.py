"""Print text to paste into Evolus.

  python scripts/evolus_text.py prompt   the agent's instructions (EVOLUS_SYSTEM_PROMPT)
  python scripts/evolus_text.py alert    a test alert for the Evolus chat: detected now, failure in 48 h,
                                         exactly the message the Evolus backend sends. Reset the mocks first:
                                         curl -X POST 'localhost:8000/admin/reset?scenario=out_of_stock'
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a plain script from the repo
from agent.evolus_client import evolus_message  # noqa: E402
from agent.prompts import EVOLUS_SYSTEM_PROMPT  # noqa: E402
from devtools.fakes import sample_alert  # noqa: E402


def test_alert():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return sample_alert().model_copy(update={"detected_at": now, "predicted_failure_at": now + timedelta(hours=48)})


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "prompt":
        print(EVOLUS_SYSTEM_PROMPT)
    elif what == "alert":
        print(evolus_message(test_alert()))
    else:
        print(__doc__)
        sys.exit(1)
