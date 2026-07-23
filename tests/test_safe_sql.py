# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The read-only guard is the single most important safety control. Test thoroughly."""

from __future__ import annotations

import pytest

from migration_validator.db.safe_sql import UnsafeStatementError, assert_read_only


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from sys.tables",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "EXPLAIN SELECT * FROM t",
        "SHOW search_path",
        "VALUES (1), (2)",
    ],
)
def test_read_only_allowed(sql: str) -> None:
    assert_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN x int",
        "TRUNCATE t",
        "MERGE INTO t USING s ON 1=1",
        "EXEC sp_who",
        "EXECUTE sp_who",
        "CALL my_proc()",
        "GRANT SELECT ON t TO public",
        "REVOKE ALL ON t FROM public",
        "DBCC CHECKDB",
        "BACKUP DATABASE foo TO DISK = '/tmp/x'",
        "COPY t FROM '/tmp/x'",
        "SELECT 1; DROP TABLE t",
        "SELECT 1; SELECT 2",
        "",
        "   ",
        ";",
    ],
)
def test_read_only_rejected(sql: str) -> None:
    with pytest.raises(UnsafeStatementError):
        assert_read_only(sql)


def test_trailing_semicolon_allowed() -> None:
    """A single trailing ';' is fine; only embedded ';' is rejected."""
    assert_read_only("SELECT 1;")


def test_case_insensitive() -> None:
    with pytest.raises(UnsafeStatementError):
        assert_read_only("dRoP TaBlE x")
