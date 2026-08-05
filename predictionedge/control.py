"""Runtime trading arm switch - toggled from the dashboard button, read by the runner.

This is the operational on/off the user controls live. It is necessary but NOT
sufficient for real orders: the runner still requires the master capability
(``PE_LIVE_TRADING=1`` + credentials), an unengaged kill-switch, and all the caps.
Stored as a file so the dashboard process and the scheduled runner process agree.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def armed(cfg) -> bool:
    p = Path(cfg.control_path)
    if not p.exists():
        return False
    try:
        return bool(json.loads(p.read_text(encoding="utf-8")).get("armed", False))
    except Exception:  # noqa: BLE001
        return False


def set_armed(cfg, value: bool) -> dict:
    p = Path(cfg.control_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"armed": bool(value), "updated": datetime.now(timezone.utc).isoformat()}
    p.write_text(json.dumps(payload), encoding="utf-8")
    return payload
