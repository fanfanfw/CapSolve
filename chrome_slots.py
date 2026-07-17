from __future__ import annotations

from dataclasses import dataclass, field
import random

import database


# Host-wide Chrome slots occupy CHROME_SLOT_BASE_KEY through base + GLOBAL_CHROME_SLOTS - 1.
# This range is intentionally separate from job_repository.QUEUE_ADMISSION_LOCK_KEY.
CHROME_SLOT_BASE_KEY = 1_128_360_000


class ChromeSlotError(RuntimeError):
    pass


@dataclass
class ChromeSlot:
    connection: object
    index: int
    key: int
    _released: bool = field(default=False, init=False)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        error = False
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (self.key,))
                error = cursor.fetchone()[0] is not True
        except BaseException:
            error = True
        finally:
            try:
                self.connection.close()
            except BaseException:
                error = True
        if error:
            raise ChromeSlotError("Chrome slot release failed")


def try_acquire(slot_count: int) -> ChromeSlot | None:
    """Try each host-wide slot once on one dedicated autocommit PostgreSQL session."""
    connection = None
    acquired_key = None
    try:
        connection = database.get_connection()
        connection.autocommit = True
        start = random.SystemRandom().randrange(slot_count)
        with connection.cursor() as cursor:
            for offset in range(slot_count):
                index = (start + offset) % slot_count
                key = CHROME_SLOT_BASE_KEY + index
                cursor.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                if cursor.fetchone()[0]:
                    acquired_key = key
                    return ChromeSlot(connection, index, key)
    except BaseException:
        if connection is not None and acquired_key is not None:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", (acquired_key,))
                    cursor.fetchone()
            except BaseException:
                pass
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
        raise ChromeSlotError("Chrome slot acquisition failed") from None
    if connection is not None:
        try:
            connection.close()
        except BaseException:
            raise ChromeSlotError("Chrome slot acquisition failed") from None
    return None
