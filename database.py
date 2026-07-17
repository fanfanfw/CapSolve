import os

import psycopg2

from settings import db_connect_timeout
from solver import load_dotenv


def get_connection(*, connect_timeout: int | None = None, statement_timeout: int | None = None):
    load_dotenv()
    db_name = os.environ.get("DB_NAME", "").strip()
    if not db_name:
        raise ValueError("DB_NAME environment variable is required for Postgres connection")

    kwargs = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": os.environ.get("DB_PORT", "5432"),
        "dbname": db_name,
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "connect_timeout": connect_timeout or db_connect_timeout(),
    }
    if statement_timeout is not None:
        kwargs["options"] = f"-c statement_timeout={statement_timeout * 1000}"
    return psycopg2.connect(**kwargs)


def is_ready(timeout: int) -> bool:
    conn = None
    try:
        conn = get_connection(connect_timeout=timeout, statement_timeout=timeout)
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone() == (1,)
    finally:
        if conn is not None:
            conn.close()
