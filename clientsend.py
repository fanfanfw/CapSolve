"""Deprecated client retained only to reject the obsolete token-returning workflow."""


def request_token(*args, **kwargs):
    raise RuntimeError("Legacy token client is disabled")


def main() -> int:
    print("Legacy token client is disabled; use the authenticated API.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
