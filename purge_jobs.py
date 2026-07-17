from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import database
from settings import MAX_PURGE_BATCH_LIMIT, load_settings


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid purge arguments") from None


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def purge(cutoff: datetime, limit: int, dry_run: bool) -> tuple[int, int, datetime | None]:
    conn = None
    try:
        conn = database.get_connection()
        with conn.cursor() as cursor:
            if dry_run:
                cursor.execute(
                    """
                    SELECT COUNT(*), MIN(processed_at)
                    FROM (
                      SELECT id, processed_at
                      FROM budi95_jobs
                      WHERE status IN ('success', 'failed')
                        AND processed_at < %s
                      ORDER BY processed_at, id
                      LIMIT %s
                    ) selected
                    """,
                    (cutoff, limit),
                )
                selected, oldest = cursor.fetchone()
                conn.rollback()
                return int(selected), 0, oldest
            with conn:
                cursor.execute(
                    """
                    WITH selected AS (
                      SELECT id
                      FROM budi95_jobs
                      WHERE status IN ('success', 'failed')
                        AND processed_at < %s
                      ORDER BY processed_at, id
                      LIMIT %s
                      FOR UPDATE SKIP LOCKED
                    ), deleted AS (
                      DELETE FROM budi95_jobs jobs
                      USING selected
                      WHERE jobs.id = selected.id
                      RETURNING jobs.id
                    )
                    SELECT COUNT(*) FROM deleted
                    """,
                    (cutoff, limit),
                )
                deleted = int(cursor.fetchone()[0])
            return deleted, deleted, None
    finally:
        if conn is not None:
            conn.close()


def main() -> int:
    cutoff = None
    dry_run = False
    selected = deleted = 0
    oldest = None
    exit_status = 1
    try:
        parser = SafeArgumentParser(add_help=False)
        parser.add_argument("--help", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int)
        args = parser.parse_args()
        dry_run = args.dry_run
        if args.help:
            print(json.dumps({"event": "purge_help", "dry_run": False, "cutoff_utc": None, "selected_count": 0, "deleted_count": 0, "exit_status": 0, "help": "Options: --limit N, --dry-run"}))
            return 0
        settings = load_settings("purge")
        limit = settings.purge_batch_limit if args.limit is None else args.limit
        if not 1 <= limit <= MAX_PURGE_BATCH_LIMIT:
            raise ValueError("invalid purge arguments")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.job_retention_hours)
        selected, deleted, oldest = purge(cutoff, limit, dry_run)
        exit_status = 0
    except BaseException:
        pass
    record = {
        "event": "purge_complete" if exit_status == 0 else "purge_error",
        "dry_run": dry_run,
        "cutoff_utc": _utc(cutoff) if cutoff else None,
        "selected_count": selected,
        "deleted_count": deleted,
        "exit_status": exit_status,
    }
    if dry_run and oldest is not None:
        record["oldest_processed_at_utc"] = _utc(oldest)
    print(json.dumps(record))
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
