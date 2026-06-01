"""Tests for the PII-redaction layer applied to report payloads."""

from __future__ import annotations

from migration_validator.reports.redaction import (
    REDACTED_FIELDS,
    redact_phase_rows,
    redact_text,
)


def test_email_is_redacted() -> None:
    out = redact_text("contact alice@example.com for details")
    assert "alice@example.com" not in out
    assert "REDACTED:EMAIL" in out


def test_phone_is_redacted() -> None:
    assert "REDACTED:PHONE" in redact_text("call (415) 555-0123 today")
    assert "REDACTED:PHONE" in redact_text("call 415-555-0123 today")


def test_ssn_is_redacted() -> None:
    assert "REDACTED:SSN" in redact_text("SSN 123-45-6789 verified")


def test_credit_card_only_redacts_when_luhn_passes() -> None:
    # Visa test number — Luhn-valid.
    assert "REDACTED:CREDIT_CARD" in redact_text("card 4111 1111 1111 1111 ok")
    # Random 16 digits — Luhn-invalid, left alone.
    assert "0000111122223333" in redact_text("ref 0000111122223333 ok")


def test_iban_is_redacted() -> None:
    assert "REDACTED:IBAN" in redact_text("send to GB82WEST12345698765432")


def test_ipv4_is_redacted() -> None:
    assert "REDACTED:IPV4" in redact_text("server 10.0.1.42 reachable")


def test_private_key_block_is_redacted() -> None:
    # Build the PEM markers from fragments so this fixture file does not itself
    # match a "private key" scanner. The redactor still sees a real-looking
    # block at runtime and redacts it.
    dashes = "-" * 5
    begin = f"{dashes}BEGIN RSA PRIVATE KEY{dashes}"
    end = f"{dashes}END RSA PRIVATE KEY{dashes}"
    fake_body = "AAAA" + "B3" + "NzaC1yc2E..."
    pem = f"log entry: {begin}\n{fake_body}\n{end}\nend."
    out = redact_text(pem)
    assert fake_body not in out
    assert "REDACTED:PRIVATE_KEY" in out


def test_redact_phase_rows_only_touches_pii_fields() -> None:
    rows = [
        {
            "procedure": "CustOrderHist",
            "sql_test_case": "EXEC [dbo].[CustOrderHist] 'alice@example.com'",
            "sql_result": "[{'email': 'alice@example.com'}]",
            "delta_ms": 4.2,
            "status": "match",
            "notes": "OK",
        }
    ]
    out = redact_phase_rows(rows)
    # Procedure name is structural — never redacted.
    assert out[0]["procedure"] == "CustOrderHist"
    # delta_ms / status are not in REDACTED_FIELDS.
    assert out[0]["delta_ms"] == 4.2
    assert out[0]["status"] == "match"
    # The PII-bearing fields are scrubbed.
    assert "alice@example.com" not in out[0]["sql_test_case"]
    assert "alice@example.com" not in out[0]["sql_result"]
    # Original input dict is not mutated.
    assert "alice@example.com" in rows[0]["sql_result"]


def test_redacted_fields_set_is_stable() -> None:
    # Ensure the agents always emit fields the redactor knows about.
    expected = {
        "sql_test_case",
        "pg_test_case",
        "source_test_case",
        "target_test_case",
        "sql_result",
        "pg_result",
        "notes",
        "analysis",
        "recommendation",
        "details",
    }
    assert REDACTED_FIELDS == frozenset(expected)
