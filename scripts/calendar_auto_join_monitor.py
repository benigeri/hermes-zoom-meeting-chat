#!/usr/bin/env python3
"""Emit the one primary-calendar Zoom event Hio should join now.

The exact stdout is persisted by Hermes's cron monitor, so it intentionally
contains only a Calendar event ID and start/end times. Event titles and Zoom
URLs remain inside the admin tool and provider boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from calendar_auto_join import (  # noqa: E402
    fetch_events,
    load_calendar_service,
    parse_datetime,
    select_candidate,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookahead-seconds", type=int, default=90)
    parser.add_argument(
        "--now",
        help="ISO timestamp override for deterministic tests; defaults to current UTC time.",
    )
    args = parser.parse_args()
    if not 0 <= args.lookahead_seconds <= 600:
        raise SystemExit("--lookahead-seconds must be between 0 and 600")
    now_value = args.now or os.environ.get("HIO_AUTOJOIN_NOW")
    now = parse_datetime(now_value) if now_value else datetime.now(timezone.utc)
    try:
        events = fetch_events(load_calendar_service(), now, args.lookahead_seconds)
        payload: dict[str, Any] = {
            "calendar": "primary",
            "candidate": select_candidate(events, now, args.lookahead_seconds),
            "ok": True,
        }
    except Exception as exc:  # fail closed without leaking provider details
        payload = {
            "calendar": "primary",
            "error": type(exc).__name__,
            "ok": False,
        }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
