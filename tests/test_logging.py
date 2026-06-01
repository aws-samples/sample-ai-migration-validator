"""The logging redaction filter is a defense-in-depth layer for secrets."""

from __future__ import annotations

import logging

from migration_validator.utils.logging import SecretsRedactingFilter, configure


def test_filter_redacts_password_in_message(caplog) -> None:
    f = SecretsRedactingFilter()
    rec = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname="",
        lineno=0,
        args=None,
        exc_info=None,
        msg="connecting with password=hunter2 and token=abc",
    )
    f.filter(rec)
    assert "hunter2" not in rec.getMessage()
    assert "abc" not in rec.getMessage()
    assert "***" in rec.getMessage()


def test_filter_redacts_in_args() -> None:
    f = SecretsRedactingFilter()
    rec = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname="",
        lineno=0,
        exc_info=None,
        msg="dsn: %s",
        args=("Server=x;Password=zzz;",),
    )
    f.filter(rec)
    assert "zzz" not in rec.getMessage()


def test_configure_is_idempotent() -> None:
    configure()
    configure()
    root = logging.getLogger()
    assert getattr(root, "_validator_configured", False) is True
