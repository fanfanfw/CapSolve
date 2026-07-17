from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Iterable


WORKER_EVENTS = {"worker_complete", "worker_failure", "worker_error", "worker_busy"}
MAX_AGE_SECONDS = 315_360_000


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid arguments") from None


def _utc_timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() == timedelta(0) else None


def _record(line: str) -> dict | None:
    try:
        value = json.loads(line)
        if isinstance(value, dict) and "MESSAGE" in value:
            value = json.loads(value["MESSAGE"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("event") not in WORKER_EVENTS:
        return None
    invoked = _utc_timestamp(value.get("invoked_at"))
    completed = _utc_timestamp(value.get("completed_at"))
    if invoked is None or completed is None or invoked > completed:
        return None
    if type(value.get("exit_status")) is not int:
        return None
    return {**value, "_completed": completed}


def check(lines: Iterable[str], max_age_seconds: int, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    records = [
        (record["_completed"], index, record)
        for index, line in enumerate(lines)
        if (record := _record(line)) is not None
    ]
    if not records:
        return False
    _, _, latest = max(records, key=lambda item: (item[0], item[1]))
    age = now - latest["_completed"]
    return timedelta(0) <= age <= timedelta(seconds=max_age_seconds) and latest["exit_status"] == 0


def main() -> int:
    try:
        parser = SafeArgumentParser(description="Check CapSolve worker freshness from journal JSON.", add_help=False)
        parser.add_argument("--max-age-seconds", type=int, default=900)
        args = parser.parse_args()
        if not 1 <= args.max_age_seconds <= MAX_AGE_SECONDS:
            parser.error("invalid")
        healthy = check(sys.stdin, args.max_age_seconds)
    except (ValueError, SystemExit):
        healthy = False
    print(json.dumps({"worker_fresh": healthy}))
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
