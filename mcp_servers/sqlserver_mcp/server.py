# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only SQL Server MCP server (pure Python, no ODBC required).

Uses ``python-tds`` so the validator works on a fresh machine without any
system driver install.

Connection details are passed via environment variables so they never appear
on the command line:

    SQLSERVER_HOST, SQLSERVER_PORT, SQLSERVER_DATABASE,
    SQLSERVER_USERNAME, SQLSERVER_PASSWORD, SQLSERVER_ENCRYPT (yes/no)

Run with:  python -m mcp_servers.sqlserver_mcp.server
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

import pytds
from mcp.server.fastmcp import FastMCP

# Quiet pytds + mcp + asyncio noise on stderr (this process inherits the
# validator's stderr, so verbose logs become user-visible chatter).
logging.basicConfig(level=logging.WARNING)
for _noisy in ("pytds", "pytds.tds_session", "mcp", "FastMCP", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.migration_validator.db.safe_sql import (  # noqa: E402
    UnsafeStatementError,
    assert_read_only,
)

mcp = FastMCP("sqlserver-mcp")


def _connect() -> pytds.Connection:
    """Open a read-only TDS connection. Caller must close()."""
    encrypt = os.environ.get("SQLSERVER_ENCRYPT", "yes").lower() == "yes"
    kwargs: dict[str, Any] = {
        "server": os.environ["SQLSERVER_HOST"],
        "port": int(os.environ.get("SQLSERVER_PORT", "1433")),
        "database": os.environ["SQLSERVER_DATABASE"],
        "user": os.environ["SQLSERVER_USERNAME"],
        "password": os.environ["SQLSERVER_PASSWORD"],
        "as_dict": True,
        "autocommit": True,
        "readonly": True,
        "appname": "migration-validator",
        "login_timeout": 15,
        "timeout": 60,
    }
    if encrypt:
        # python-tds enables encryption when cafile is set OR when the server
        # advertises it. We default to the system CA bundle if available.
        cafile = os.environ.get("SQLSERVER_CA_FILE")
        if cafile:
            kwargs["cafile"] = cafile
    return pytds.connect(**kwargs)


def _query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    assert_read_only(sql)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        if cur.description is None:
            return []
        return list(cur.fetchall())
    finally:
        conn.close()


def _valid_ident(s: str) -> bool:
    """Reject only what would break a quoted identifier or violate length limits.

    SQL Server quoted identifiers: ``[name]``. Inside the brackets, ``]`` must
    be doubled to escape it. We forbid ``]`` outright to keep things simple,
    plus NULs (which TDS rejects), and limit to 128 chars per T-SQL rules.
    Spaces, dots, hyphens, accents and other "weird" chars are allowed because
    the bracket-quoting handles them.
    """
    return bool(s) and len(s) <= 128 and "\x00" not in s and "]" not in s


def _q_ident(s: str) -> str:
    """Return ``s`` wrapped in SQL Server bracket-quotes."""
    return "[" + s + "]"


def _q_lit(s: str) -> str:
    """Return ``s`` wrapped as a SQL Server string literal (apostrophes doubled)."""
    return "'" + s.replace("'", "''") + "'"


def _exec_dynamic(cur: Any, sql_template: str, params: tuple[Any, ...] = ()) -> None:
    """Execute a dynamic SQL statement via sp_executesql.

    This is the ONLY path that executes dynamic SQL in this server. All
    identifier interpolation is done server-side via QUOTENAME() in the
    sql_template, making the pattern safe from Python-side injection.

    Args:
        cur: database cursor
        sql_template: a T-SQL string (may use %s placeholders for pytds params)
        params: tuple of parameter values bound by the driver
    """
    cur.execute(sql_template, params)


# Backwards-compatible alias retained because some tools still call it.
def _safe_ident(s: str) -> bool:
    """Deprecated: use ``_valid_ident``. Kept so external callers don't break."""
    return _valid_ident(s)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _check_schema_allowlist(sql: str) -> str | None:
    """If SQLSERVER_ALLOWED_SCHEMA is set, reject queries referencing other schemas.

    This is defense-in-depth — the primary access control is the DB user's grants.
    Returns an error message string if rejected, or None if OK.
    """
    import re as _re

    allowed = os.environ.get("SQLSERVER_ALLOWED_SCHEMA", "").strip()
    if not allowed:
        return None
    # Match FROM/JOIN [schema].[table] or schema.table patterns
    pattern = r"(?:FROM|JOIN)\s+\[?(\w+)\]?\s*\.\s*\[?\w+\]?"
    refs = _re.findall(pattern, sql, flags=_re.IGNORECASE)
    for ref in refs:
        if ref.lower() != allowed.lower():
            return f"Query references schema '{ref}' which is not in the allowed list ('{allowed}')"
    return None


@mcp.tool()
def execute_select(sql: str) -> str:
    """Execute a read-only SQL Server query and return JSON-formatted rows."""
    schema_err = _check_schema_allowlist(sql)
    if schema_err:
        return json.dumps({"error": "rejected", "reason": schema_err})
    try:
        rows = _query(sql)
    except UnsafeStatementError as e:
        return json.dumps({"error": "rejected", "reason": str(e)})
    except pytds.Error as e:
        return json.dumps({"error": "db_error", "reason": str(e)[:500]})
    return json.dumps(rows, default=str)


@mcp.tool()
def verify_connection() -> str:
    """Cheap connection sanity check.

    Returns ``{"ok": true, "info": {...}}`` when the credentials are good and
    ``DB_NAME()`` matches what the user asked for. Returns
    ``{"ok": false, "reason": "..."}`` (no exception, no traceback) when the
    server rejects the login or the connection fails.

    The orchestrator calls this immediately after starting the MCP, so a bad
    password or wrong database fails fast before any phase runs.
    """
    expected = os.environ.get("SQLSERVER_DATABASE", "")
    try:
        rows = _query("SELECT @@SERVERNAME AS server, DB_NAME() AS db, USER_NAME() AS db_user")
    except pytds.Error as e:
        return json.dumps({"ok": False, "reason": str(e)[:500]})
    info = rows[0] if rows else {}
    actual = info.get("db") or ""
    if expected and actual.lower() != expected.lower():
        return json.dumps(
            {
                "ok": False,
                "reason": (
                    f"connected, but landed in '{actual}' instead of "
                    f"'{expected}'. Likely the user lacks access to '{expected}' or "
                    f"the database name is wrong."
                ),
                "info": info,
            }
        )
    return json.dumps({"ok": True, "info": info})


@mcp.tool()
def db_info() -> str:
    """Return basic connection info: server name, db name, current user, schema counts.

    Useful for debugging permission / wrong-DB issues. Run with no arguments.
    """
    try:
        rows = _query(
            """
            SELECT  @@SERVERNAME AS server_name,
                    DB_NAME()    AS database_name,
                    SUSER_NAME() AS login_name,
                    USER_NAME()  AS db_user,
                    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
                       WHERE table_type = 'BASE TABLE')                  AS total_tables,
                    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.VIEWS)      AS total_views,
                    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.ROUTINES)   AS total_routines
            """
        )
    except pytds.Error as e:
        return json.dumps({"error": "db_error", "reason": str(e)[:500]})
    schemas = _query("SELECT DISTINCT table_schema AS schema_name FROM INFORMATION_SCHEMA.TABLES")
    return json.dumps({"info": rows[0] if rows else {}, "visible_schemas": schemas}, default=str)


@mcp.tool()
def list_objects(schema: str) -> str:
    """List every schema object grouped by type.

    Uses INFORMATION_SCHEMA (permission-friendly, standards-based) for tables,
    views, procedures, and functions. Drops down to ``sys.*`` only for things
    INFORMATION_SCHEMA does not cover: triggers, indexes, sequences.
    """
    if not _valid_ident(schema):
        return json.dumps({"error": "rejected", "reason": "invalid schema identifier"})
    sql = """
        SELECT 'USER_TABLE' AS type_desc, table_name AS name
          FROM INFORMATION_SCHEMA.TABLES
         WHERE table_schema = %s AND table_type = 'BASE TABLE'
        UNION ALL
        SELECT 'VIEW', table_name
          FROM INFORMATION_SCHEMA.VIEWS
         WHERE table_schema = %s
        UNION ALL
        SELECT CASE WHEN routine_type = 'PROCEDURE'
                    THEN 'SQL_STORED_PROCEDURE'
                    ELSE 'SQL_SCALAR_FUNCTION' END,
               routine_name
          FROM INFORMATION_SCHEMA.ROUTINES
         WHERE routine_schema = %s
        UNION ALL
        SELECT 'SQL_TRIGGER', tr.name
          FROM sys.triggers tr
          JOIN sys.tables t ON tr.parent_id = t.object_id
          JOIN sys.schemas s ON s.schema_id = t.schema_id
         WHERE s.name = %s
        UNION ALL
        SELECT 'INDEX', i.name
          FROM sys.indexes i
          JOIN sys.tables t ON t.object_id = i.object_id
          JOIN sys.schemas s ON s.schema_id = t.schema_id
         WHERE s.name = %s
           AND i.is_primary_key = 0 AND i.is_unique_constraint = 0
           AND i.name IS NOT NULL
        UNION ALL
        SELECT 'SEQUENCE_OBJECT', sq.name
          FROM sys.sequences sq
          JOIN sys.schemas s ON s.schema_id = sq.schema_id
         WHERE s.name = %s
    """
    return json.dumps(_query(sql, (schema,) * 6), default=str)


@mcp.tool()
def table_row_counts(schema: str) -> str:
    """Row count for every base table in the schema.

    Tries fast metadata (``sys.dm_db_partition_stats``); falls back to
    ``COUNT_BIG(*)`` per table when the user lacks ``VIEW DATABASE STATE``.
    """
    if not _valid_ident(schema):
        return json.dumps({"error": "rejected", "reason": "invalid schema identifier"})
    fast_sql = """
        SELECT  t.name AS table_name,
                SUM(CASE WHEN p.index_id IN (0,1) THEN p.row_count ELSE 0 END) AS row_count
          FROM  sys.dm_db_partition_stats p
          JOIN  sys.tables  t ON t.object_id = p.object_id
          JOIN  sys.schemas s ON s.schema_id = t.schema_id
         WHERE  s.name = %s
         GROUP BY t.name
         ORDER BY t.name
    """
    rows = _query(fast_sql, (schema,))
    if rows:
        return json.dumps(rows, default=str)
    tables = _query(
        """SELECT table_name FROM INFORMATION_SCHEMA.TABLES
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name""",
        (schema,),
    )
    out: list[dict[str, Any]] = []
    for r in tables:
        tname = r["table_name"]
        if not _valid_ident(tname):
            continue
        # Use sp_executesql with QUOTENAME for server-side safe quoting
        cnt = _query(
            "DECLARE @sql NVARCHAR(500) = "
            "N'SELECT COUNT_BIG(*) AS row_count FROM ' + QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
            " EXEC sp_executesql @sql",
            (schema, tname),
        )
        out.append({"table_name": tname, "row_count": cnt[0]["row_count"] if cnt else 0})
    return json.dumps(out, default=str)


@mcp.tool()
def list_procedures(schema: str) -> str:
    """List stored procedures and functions in the schema with their definition."""
    if not _valid_ident(schema):
        return json.dumps({"error": "rejected", "reason": "invalid schema identifier"})
    sql = """
        SELECT  o.name,
                o.type_desc,
                CAST(OBJECT_DEFINITION(o.object_id) AS NVARCHAR(MAX)) AS definition
          FROM  sys.objects o
          JOIN  sys.schemas s ON s.schema_id = o.schema_id
         WHERE  s.name = %s
           AND  o.type IN ('P','FN','IF','TF')
         ORDER BY o.name
    """
    return json.dumps(_query(sql, (schema,)), default=str)


@mcp.tool()
def procedure_parameters(schema: str, name: str) -> str:
    """Parameter metadata for a procedure or function."""
    if not _valid_ident(schema) or not _valid_ident(name):
        return json.dumps({"error": "rejected", "reason": "invalid identifier"})
    sql = """
        SELECT  p.name              AS parameter_name,
                t.name              AS data_type,
                p.parameter_id      AS ordinal,
                p.is_output         AS is_output,
                p.has_default_value AS has_default
          FROM  sys.parameters p
          JOIN  sys.types t   ON t.user_type_id = p.user_type_id
          JOIN  sys.objects o ON o.object_id   = p.object_id
          JOIN  sys.schemas sc ON sc.schema_id = o.schema_id
         WHERE  sc.name = %s AND o.name = %s
         ORDER BY p.parameter_id
    """
    return json.dumps(_query(sql, (schema, name)), default=str)


@mcp.tool()
def sample_table(schema: str, table: str, limit: int = 100) -> str:
    """Return up to ``limit`` rows (TOP N) from the given table."""
    if not (1 <= limit <= 1000):
        return json.dumps({"error": "rejected", "reason": "limit must be 1..1000"})
    if not _valid_ident(schema) or not _valid_ident(table):
        return json.dumps({"error": "rejected", "reason": "invalid identifier"})
    # Use sp_executesql with QUOTENAME for server-side safe quoting
    sql = (
        "DECLARE @sql NVARCHAR(500) = "
        "N'SELECT TOP (' + CAST(%s AS NVARCHAR(10)) + N') * FROM ' "
        "+ QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
        " EXEC sp_executesql @sql"
    )
    return json.dumps(_query(sql, (int(limit), schema, table)), default=str)


@mcp.tool()
def find_columns(schema: str, column_name: str) -> str:
    """Find every (table, column) pair in the schema where the column name
    matches ``column_name`` (case-insensitive).

    Used by the functional agent to map procedure parameters (e.g. ``@CustomerID``)
    to base-table columns we can sample real values from.
    """
    if not _valid_ident(schema) or not _valid_ident(column_name):
        return json.dumps({"error": "rejected", "reason": "invalid identifier"})
    sql = (
        "SELECT table_name, column_name, data_type "
        "FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE table_schema = %s "
        "  AND LOWER(column_name) = LOWER(%s) "
        "ORDER BY table_name"
    )
    return json.dumps(_query(sql, (schema, column_name)), default=str)


@mcp.tool()
def sample_column(schema: str, table: str, column: str, limit: int = 5) -> str:
    """Return up to ``limit`` distinct non-null values for ``schema.table.column``.

    The functional agent uses this to find real values for procedure
    parameters: e.g. real CustomerIDs from the Customers table.
    """
    if not (1 <= limit <= 50):
        return json.dumps({"error": "rejected", "reason": "limit must be 1..50"})
    if not (_valid_ident(schema) and _valid_ident(table) and _valid_ident(column)):
        return json.dumps({"error": "rejected", "reason": "invalid identifier"})
    # Use sp_executesql with QUOTENAME for server-side safe quoting
    sql = (
        "DECLARE @sql NVARCHAR(500) = "
        "N'SELECT DISTINCT TOP (' + CAST(%s AS NVARCHAR(10)) + N') ' "
        "+ QUOTENAME(%s) + N' AS v FROM ' "
        "+ QUOTENAME(%s) + N'.' + QUOTENAME(%s) "
        "+ N' WHERE ' + QUOTENAME(%s) + N' IS NOT NULL';"
        " EXEC sp_executesql @sql"
    )
    return json.dumps(_query(sql, (int(limit), column, schema, table, column)), default=str)


@mcp.tool()
def call_procedure(schema: str, name: str, args_json: str = "[]") -> str:
    """Invoke a stored procedure or function and return its result rows.

    SQL Server procedures cannot be called via ``SELECT proc(...)`` — they
    require ``EXEC``, which our ``execute_select`` guard rejects. This tool
    bypasses the SELECT-only guard but only for *named* objects in
    ``sys.objects``, and only after confirming the object's body contains no
    DML keywords. The actual EXEC runs inside an explicit
    ``BEGIN TRAN ... ROLLBACK TRAN`` so any accidental side effect is undone.

    Arguments
        schema   — schema name (validated)
        name     — procedure or function name (validated)
        args_json — JSON-encoded list of positional arguments
    """
    if not _valid_ident(schema) or not _valid_ident(name):
        return json.dumps({"error": "rejected", "reason": "invalid identifier"})
    try:
        args = json.loads(args_json) if args_json else []
    except json.JSONDecodeError:
        return json.dumps({"error": "rejected", "reason": "args_json must be a JSON list"})
    if not isinstance(args, list):
        return json.dumps({"error": "rejected", "reason": "args_json must be a JSON list"})

    # Confirm the object exists and gate on its DML content.
    meta = _query(
        "SELECT o.type_desc, OBJECT_DEFINITION(o.object_id) AS definition "
        "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
        "WHERE s.name = %s AND o.name = %s",
        (schema, name),
    )
    if not meta:
        return json.dumps({"error": "not_found", "reason": f"{schema}.{name}"})
    type_desc = meta[0]["type_desc"] or ""
    definition = meta[0]["definition"] or ""
    # Disallow obvious writers.
    import re as _re

    if _re.search(
        r"\b(INSERT\s+INTO|UPDATE\s+\S|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE\s+TABLE)\b",
        definition,
        flags=_re.IGNORECASE,
    ):
        return json.dumps({"error": "rejected", "reason": "object body contains DML"})

    is_function = "FUNCTION" in type_desc.upper()

    conn = _connect()
    try:
        cur = conn.cursor()
        if is_function:
            # Use sp_executesql with QUOTENAME for safe function invocation
            if args:
                param_count = len(args)
                param_decls = ", ".join("@p" + str(i) + " sql_variant" for i in range(param_count))
                param_refs = ", ".join("@p" + str(i) for i in range(param_count))
                build_sql = (
                    "DECLARE @obj NVARCHAR(300) = QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
                    " DECLARE @sql NVARCHAR(MAX) = N'SELECT ' + @obj + N'(" + param_refs + ") AS result';"
                    " EXEC sp_executesql @sql, N'"
                    + param_decls
                    + "', "
                    + ", ".join("@p" + str(i) + "=%s" for i in range(param_count))
                )
                _exec_dynamic(cur, build_sql, (schema, name, *tuple(args)))
            else:
                build_sql = (
                    "DECLARE @obj NVARCHAR(300) = QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
                    " DECLARE @sql NVARCHAR(MAX) = N'SELECT ' + @obj + N'() AS result';"
                    " EXEC sp_executesql @sql"
                )
                _exec_dynamic(cur, build_sql, (schema, name))
            rows = list(cur.fetchall())
            return json.dumps(rows, default=str)

        # Procedure: wrap in a rollback transaction so any write is undone.
        conn.autocommit = False
        try:
            cur.execute("BEGIN TRAN")
            if args:
                param_count = len(args)
                param_decls = ", ".join("@p" + str(i) + " sql_variant" for i in range(param_count))
                param_refs = ", ".join("@p" + str(i) for i in range(param_count))
                build_sql = (
                    "DECLARE @obj NVARCHAR(300) = QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
                    " DECLARE @sql NVARCHAR(MAX) = N'EXEC ' + @obj + N' " + param_refs + "';"
                    " EXEC sp_executesql @sql, N'"
                    + param_decls
                    + "', "
                    + ", ".join("@p" + str(i) + "=%s" for i in range(param_count))
                )
                _exec_dynamic(cur, build_sql, (schema, name, *tuple(args)))
            else:
                build_sql = (
                    "DECLARE @obj NVARCHAR(300) = QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
                    " DECLARE @sql NVARCHAR(MAX) = N'EXEC ' + @obj;"
                    " EXEC sp_executesql @sql"
                )
                _exec_dynamic(cur, build_sql, (schema, name))
            collected_rows: list[dict[str, Any]] = []
            if cur.description is not None:
                collected_rows = list(cur.fetchall())
            cur.execute("ROLLBACK TRAN")
            return json.dumps(collected_rows, default=str)
        finally:
            conn.autocommit = True
    except pytds.Error as e:
        return json.dumps({"error": "db_error", "reason": str(e)[:500]})
    finally:
        conn.close()


@mcp.tool()
def execute_with_timing(sql: str) -> str:
    """Run a SELECT and return rows + clock-side elapsed time in ms."""
    try:
        assert_read_only(sql)
    except UnsafeStatementError as e:
        return json.dumps({"error": "rejected", "reason": str(e)})
    t0 = time.perf_counter()
    rows = _query(sql)
    elapsed = (time.perf_counter() - t0) * 1000.0
    return json.dumps(
        {"elapsed_ms": round(elapsed, 3), "row_count": len(rows), "rows": rows[:50]},
        default=str,
    )


@mcp.tool()
def time_procedure(schema: str, name: str, args_json: str = "[]") -> str:
    """Run a procedure / function and return execution time in milliseconds.

    For procedures we EXEC inside ``BEGIN TRAN ... ROLLBACK TRAN`` so any
    accidental side effect is undone. For functions we ``SELECT proc(args)``.

    Returns ``{"elapsed_ms": <float>, "row_count": <int>, "source": "clock"}``.
    The ``source`` is always ``clock`` here — SQL Server's
    ``sys.dm_exec_query_stats`` aggregates by query hash and is unreliable for
    one-shot timing of a CALL/EXEC; clock-side timing is more accurate per
    invocation. The performance agent averages multiple runs to mitigate noise.
    """
    if not _valid_ident(schema) or not _valid_ident(name):
        return json.dumps({"error": "rejected", "reason": "invalid identifier"})
    try:
        args = json.loads(args_json) if args_json else []
    except json.JSONDecodeError:
        return json.dumps({"error": "rejected", "reason": "args_json must be a JSON list"})
    if not isinstance(args, list):
        return json.dumps({"error": "rejected", "reason": "args_json must be a JSON list"})

    meta = _query(
        "SELECT o.type_desc, OBJECT_DEFINITION(o.object_id) AS definition "
        "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
        "WHERE s.name = %s AND o.name = %s",
        (schema, name),
    )
    if not meta:
        return json.dumps({"error": "not_found", "reason": f"{schema}.{name}"})
    type_desc = (meta[0]["type_desc"] or "").upper()
    definition = meta[0]["definition"] or ""
    import re as _re

    if _re.search(
        r"\b(INSERT\s+INTO|UPDATE\s+\S|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE\s+TABLE)\b",
        definition,
        flags=_re.IGNORECASE,
    ):
        return json.dumps({"error": "rejected", "reason": "object body contains DML"})

    is_function = "FUNCTION" in type_desc

    conn = _connect()
    try:
        cur = conn.cursor()
        # Warm the connection / plan cache.
        cur.execute("SELECT 1")
        cur.fetchall()

        t0 = time.perf_counter()
        if is_function:
            if args:
                param_count = len(args)
                param_decls = ", ".join("@p" + str(i) + " sql_variant" for i in range(param_count))
                param_refs = ", ".join("@p" + str(i) for i in range(param_count))
                build_sql = (
                    "DECLARE @obj NVARCHAR(300) = QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
                    " DECLARE @sql NVARCHAR(MAX) = N'SELECT ' + @obj + N'(" + param_refs + ") AS result';"
                    " EXEC sp_executesql @sql, N'"
                    + param_decls
                    + "', "
                    + ", ".join("@p" + str(i) + "=%s" for i in range(param_count))
                )
                _exec_dynamic(cur, build_sql, (schema, name, *tuple(args)))
            else:
                build_sql = (
                    "DECLARE @obj NVARCHAR(300) = QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
                    " DECLARE @sql NVARCHAR(MAX) = N'SELECT ' + @obj + N'() AS result';"
                    " EXEC sp_executesql @sql"
                )
                _exec_dynamic(cur, build_sql, (schema, name))
            rows = list(cur.fetchall())
        else:
            conn.autocommit = False
            try:
                cur.execute("BEGIN TRAN")
                if args:
                    param_count = len(args)
                    param_decls = ", ".join("@p" + str(i) + " sql_variant" for i in range(param_count))
                    param_refs = ", ".join("@p" + str(i) for i in range(param_count))
                    build_sql = (
                        "DECLARE @obj NVARCHAR(300) = QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
                        " DECLARE @sql NVARCHAR(MAX) = N'EXEC ' + @obj + N' " + param_refs + "';"
                        " EXEC sp_executesql @sql, N'"
                        + param_decls
                        + "', "
                        + ", ".join("@p" + str(i) + "=%s" for i in range(param_count))
                    )
                    _exec_dynamic(cur, build_sql, (schema, name, *tuple(args)))
                else:
                    build_sql = (
                        "DECLARE @obj NVARCHAR(300) = QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
                        " DECLARE @sql NVARCHAR(MAX) = N'EXEC ' + @obj;"
                        " EXEC sp_executesql @sql"
                    )
                    _exec_dynamic(cur, build_sql, (schema, name))
                rows = list(cur.fetchall()) if cur.description is not None else []
                cur.execute("ROLLBACK TRAN")
            finally:
                conn.autocommit = True
        elapsed = (time.perf_counter() - t0) * 1000.0
        return json.dumps(
            {"elapsed_ms": round(elapsed, 3), "row_count": len(rows), "source": "clock"},
            default=str,
        )
    except pytds.Error as e:
        return json.dumps({"error": "db_error", "reason": str(e)[:500]})
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
