# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PII redaction applied to report payloads.

The validator's reports include raw procedure result rows and test-case
argument values, which can contain real customer data (emails, phone numbers,
SSN-like patterns, credit card-like patterns, IBANs, IP addresses, names of
real customers, etc.). This module runs a defence-in-depth scrub over the
fields that surface to humans before the HTML / JSON report is written.

It is *not* a substitute for proper data classification — a customer may have
columns we cannot detect (e.g. internal account numbers). For that reason the
HTML report carries a banner explaining what was redacted and what the user
should still review manually.

Patterns are intentionally conservative:
    * email           — RFC-ish addresses
    * phone           — North American + international forms
    * ssn             — 3-2-4 numeric pattern (not bare numbers)
    * credit card     — 13-19 digits passing Luhn
    * iban            — country code + 2 check digits + up to 30 chars
    * ipv4            — dotted quads
    * private key     — ``-----BEGIN ... PRIVATE KEY-----`` blocks

False-positives are far less harmful than false-negatives in this context.
"""

from __future__ import annotations

import re
from typing import Any

# Field names we redact. Anything else is left untouched so structural columns
# (counts, deltas, status) keep their original values.
REDACTED_FIELDS: frozenset[str] = frozenset(
    {
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
)

# (pattern, label) — order matters: longer / more specific first.
_PRIV_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_PRIV_KEY_RE, "PRIVATE_KEY"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "EMAIL"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "SSN"),
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"), "IBAN"),
    (re.compile(r"\b(?:\+?\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b"), "PHONE"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "POSSIBLE_CARD"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "IPV4"),
]


def _luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if len(nums) < 13 or len(nums) > 19:
        return False
    total = 0
    parity = len(nums) % 2
    for i, n in enumerate(nums):
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _redact_card(match: re.Match[str]) -> str:
    raw = match.group(0)
    digits_only = re.sub(r"[ -]", "", raw)
    if _luhn_ok(digits_only):
        return "[REDACTED:CREDIT_CARD]"
    return raw


def redact_text(text: str) -> str:
    """Return ``text`` with PII-looking substrings replaced by labels."""
    if not text:
        return text
    out = text
    for pattern, label in _PATTERNS:
        if label == "POSSIBLE_CARD":
            out = pattern.sub(_redact_card, out)
        else:
            out = pattern.sub(f"[REDACTED:{label}]", out)
    return out


def redact_value(value: Any) -> Any:
    """Recursively redact strings in any JSON-ish structure."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    return value


def redact_phase_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply ``redact_text`` to every value of a known-PII-bearing column."""
    if not rows:
        return rows
    redacted: list[dict[str, Any]] = []
    for row in rows:
        new_row = dict(row)
        for k in list(new_row.keys()):
            if k in REDACTED_FIELDS and isinstance(new_row[k], str):
                new_row[k] = redact_text(new_row[k])
        redacted.append(new_row)
    return redacted
