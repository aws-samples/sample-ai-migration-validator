"""Defense-in-depth statement guard.

The MCP servers each apply this same check. We re-apply it on the client side
so a buggy/compromised MCP cannot induce a write.
"""

from __future__ import annotations

import re

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|EXEC(UTE)?|"
    r"CALL|DBCC|BACKUP|RESTORE|SHUTDOWN|KILL|BULK|OPENROWSET|COPY)\b",
    re.IGNORECASE,
)
_ALLOWED_PREFIX = re.compile(r"^\s*(SELECT|WITH|SHOW|EXPLAIN|VALUES)\b", re.IGNORECASE)


class UnsafeStatementError(ValueError):
    """Raised when a SQL statement is rejected by the read-only guard."""


def assert_read_only(sql: str) -> None:
    """Raise ``UnsafeStatementError`` if the statement is not read-only.

    Multiple statements (separated by ``;``) are rejected so that a smuggled
    write cannot ride along after an allowed SELECT.
    """
    if sql is None:
        raise UnsafeStatementError("empty statement")
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise UnsafeStatementError("empty statement")
    # No multi-statement payloads.
    # We allow ';' inside string literals only conservatively: reject any non-trailing ';'.
    if ";" in stripped:
        raise UnsafeStatementError("multi-statement payloads are not allowed")
    if _FORBIDDEN.search(stripped):
        raise UnsafeStatementError("DDL/DML keyword detected; only read queries are permitted")
    if not _ALLOWED_PREFIX.match(stripped):
        raise UnsafeStatementError("statement must start with SELECT, WITH, SHOW, EXPLAIN or VALUES")
