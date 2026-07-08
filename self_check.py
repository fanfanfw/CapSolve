from __future__ import annotations

import job_repository
from service import app


def main() -> int:
    first = job_repository.new_ulid()
    second = job_repository.new_ulid()
    assert isinstance(first, str) and first
    assert isinstance(second, str) and second
    assert first != second

    submit = job_repository.public_submit_response({"nric": "S1234567A", "ulid": first})
    assert set(submit) == {"nric", "ulid"}
    assert submit == {"nric": "S1234567A", "ulid": first}

    failed = job_repository.public_result_response({"status": "failed", "error": "boom"})
    assert set(failed) == {"status", "data"}
    assert failed["status"] is False
    assert failed["data"]["error"] == "boom"

    success = job_repository.public_result_response({"status": "success", "response_body": {"ok": True}})
    assert set(success) == {"status", "data"}

    paths = set(app.openapi()["paths"])
    assert "/api/budi95" in paths
    assert "/api/budi95/result" in paths
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
