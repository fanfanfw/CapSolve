from __future__ import annotations

import os
import urllib.error

import config_resolver
import job_repository
import service
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

    js_fixture = 'var e="https://be.budirakyat.gov.my/api/",t="https://www.budirakyat.gov.my/",s="v1",a={CLOUDFLARE_TURNSTILE_SITEID:"0x4AAAAAABldcyHGJWZTEqRB",apiEndpoints:{portalsvc:{pubGetCountInfo:`${e}portalsvc/${s}/pub_getcountinfo`}}}'
    config = config_resolver.parse_config_from_js(js_fixture, "https://www.budirakyat.gov.my/eligibility-check")
    assert config.local_post_url == "https://be.budirakyat.gov.my/api/portalsvc/v1/pub_getcountinfo"
    assert config.turnstile_siteurl == "https://www.budirakyat.gov.my/eligibility-check?type=individual"
    assert config.turnstile_sitekey == "0x4AAAAAABldcyHGJWZTEqRB"

    old_fetch = config_resolver._fetch_text
    old_url = os.environ.get("BUDI95_CONFIG_URL")
    try:
        os.environ["BUDI95_CONFIG_URL"] = "https://www.budirakyat.gov.my/eligibility-check"
        pages = {
            "https://www.budirakyat.gov.my/eligibility-check": '<script src="/main.js"></script>',
            "https://www.budirakyat.gov.my/main.js": 'import x from "./chunk-FPBHLHBE.js";',
            "https://www.budirakyat.gov.my/chunk-FPBHLHBE.js": js_fixture,
        }
        config_resolver._fetch_text = lambda url, timeout: pages[url]
        chunk_config = config_resolver.resolve_from_website()
        assert chunk_config.local_post_url == "https://be.budirakyat.gov.my/api/portalsvc/v1/pub_getcountinfo"
        assert chunk_config.turnstile_sitekey == "0x4AAAAAABldcyHGJWZTEqRB"
    finally:
        config_resolver._fetch_text = old_fetch
        if old_url is None:
            os.environ.pop("BUDI95_CONFIG_URL", None)
        else:
            os.environ["BUDI95_CONFIG_URL"] = old_url

    old_resolve = service.resolve_budi95_config
    old_solve = service.solve
    old_post = service.post_local_result
    try:
        calls = []
        config1 = config_resolver.Budi95Config("url1", "site1", "key1", "test")
        config2 = config_resolver.Budi95Config("url2", "site2", "key2", "test")
        service.resolve_budi95_config = lambda force_refresh=False: config2 if force_refresh else config1
        service.solve = lambda key, url, timeout: calls.append(("solve", key, url)) or f"token-{key}"

        def fake_post(url, nric, token, timeout):
            calls.append(("post", url, token))
            if url == "url1":
                raise urllib.error.URLError("connection refused")
            return {"ok": True}

        service.post_local_result = fake_post
        assert service._solve_and_post("S1234567A", 5, 6) == {"ok": True}
        assert calls == [
            ("solve", "key1", "site1"),
            ("post", "url1", "token-key1"),
            ("solve", "key2", "site2"),
            ("post", "url2", "token-key2"),
        ]
    finally:
        service.resolve_budi95_config = old_resolve
        service.solve = old_solve
        service.post_local_result = old_post

    openapi = app.openapi()
    paths = set(openapi["paths"])
    assert "/api/budi95" in paths
    assert "/api/budi95/result" not in paths
    assert "/api/budi95/result/{ulid}" in paths
    assert "requestBody" not in openapi["paths"]["/api/budi95/result/{ulid}"]["get"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
