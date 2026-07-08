import os

import psycopg2

from solver import load_dotenv


def get_connection():
    load_dotenv()
    db_name = os.environ.get("DB_NAME", "").strip()
    if not db_name:
        raise ValueError("DB_NAME environment variable is required for Postgres connection")

    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=db_name,
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )
