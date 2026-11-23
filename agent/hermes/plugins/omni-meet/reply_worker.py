"""One reply decision, out of process: stdin {"prompt", "system", "schema"} → stdout JSON.

Run by the conductor with the REPO's interpreter, so it uses omni_llm exactly as every other text-only
model call in this repo does: no tools, empty MCP config, no settings files, an empty working
directory and an allowlisted, secret-free environment (scripts/omni_llm.py). The model routes are the
``chat.meeting`` job in config/models.yaml. The meeting transcript is other people's words — it only
ever reaches a model that cannot act.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))


def main() -> int:
    req = json.loads(sys.stdin.read())
    import omni_llm
    try:
        data = omni_llm.structured(req["prompt"], job="chat.meeting", system=req["system"],
                                   json_schema=req["schema"], timeout=req.get("timeout", 45))
    except Exception as exc:  # noqa: BLE001 — the conductor treats any failure as silence
        print(json.dumps({"speak": False, "text": "", "reason": "model unavailable: %s" % type(exc).__name__}))
        return 0
    print(json.dumps(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
