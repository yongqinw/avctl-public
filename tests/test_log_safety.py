from __future__ import annotations

import logging

from api.main import _SensitiveAccessLogFilter


def test_access_log_filter_redacts_legacy_query_token():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("phone", "GET", "/?token=synthetic-secret&next=music", "1.1", 200),
        None,
    )

    assert _SensitiveAccessLogFilter().filter(record) is True

    rendered = record.getMessage()
    assert "synthetic-secret" not in rendered
    assert "token=[REDACTED]&next=music" in rendered
