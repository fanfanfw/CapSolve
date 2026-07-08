from __future__ import annotations

import argparse
from pathlib import Path


SQL_DIR = Path(__file__).with_name("sql")


def sql_files() -> list[Path]:
    return sorted(SQL_DIR.glob("*.sql"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SQL migrations.")
    parser.add_argument("--dry-run", action="store_true", help="Print SQL files without running them.")
    args = parser.parse_args()

    files = sql_files()
    if args.dry_run:
        for path in files:
            print(path.relative_to(Path(__file__).parent))
        return

    from database import get_connection

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cursor:
                for path in files:
                    cursor.execute(path.read_text())
    finally:
        conn.close()


if __name__ == "__main__":
    main()
