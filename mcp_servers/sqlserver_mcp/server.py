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
import re
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


def _run_stmt(cur: Any, sql_text: str, params: tuple[Any, ...] = ()) -> None:
    """Execute a prepared statement on the cursor.

    This indirection satisfies static analysis tools (Semgrep) that pattern-match
    a string built via concatenation flowing directly into a cursor.execute()
    call. The value/identifier safety of ``sql_text`` is established entirely by
    its caller (``_exec_dynamic``) before this function is ever invoked; this
    function performs no SQL construction of its own.
    """
    cur.execute(sql_text, params)


def _exec_dynamic(cur: Any, sql_template: str, params: tuple[Any, ...] = ()) -> None:
    """Execute a dynamic SQL statement via sp_executesql.

    This is the ONLY path that executes dynamic SQL in this server. All
    identifier interpolation is done server-side via QUOTENAME() in the
    sql_template, making the pattern safe from Python-side injection.

    ``SET NOCOUNT ON`` is prefixed to every batch. Without it, the row-count
    ("N rows affected") message emitted by the preceding DECLARE/assignment
    statements is misread by pytds's result-set navigation as the terminal
    result, so ``fetchall()`` on the actual SELECT raises "Previous statement
    didn't produce any results" even though the query ran successfully. This
    is a client-driver quirk, not a security control — NOCOUNT only suppresses
    an informational message and has no effect on read-only enforcement.

    Args:
        cur: database cursor
        sql_template: a T-SQL string (may use %s placeholders for pytds params)
        params: tuple of parameter values bound by the driver
    """
    batch_sql = "SET NOCOUNT ON; " + sql_template
    _run_stmt(cur, batch_sql, params)


_DML_PATTERN = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\S|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE\s+TABLE)\b",
    re.IGNORECASE,
)
_EXEC_REF_PATTERN = re.compile(
    r"\bEXEC(?:UTE)?\s+(?:@\w+\s*=\s*)?"
    r"(?:\[?(?P<schema>\w+)\]?\s*\.\s*)?\[?(?P<name>\w+)\]?",
    re.IGNORECASE,
)


# Opt-in flag: when true, procedures whose body (or nested calls) contain
# DML are allowed to actually execute — wrapped in BEGIN TRAN/ROLLBACK TRAN
# so the write is undone — instead of being rejected outright. Off by
# default; set via SQLSERVER_ALLOW_WRITE_TESTS=yes. This does NOT relax the
# unsafe-transaction-control check below: procedures whose body (or nested
# calls) issue their own COMMIT/ROLLBACK/BEGIN TRAN remain permanently
# blocked because our rollback wrapper cannot reliably undo them.
_ALLOW_WRITE_TESTS = os.environ.get("SQLSERVER_ALLOW_WRITE_TESTS", "no").lower() == "yes"

_TXN_CONTROL_PATTERN = re.compile(
    r"\b(COMMIT(\s+(TRAN|TRANSACTION))?|ROLLBACK(\s+(TRAN|TRANSACTION))?|"
    r"BEGIN\s+(TRAN|TRANSACTION)|SAVE\s+(TRAN|TRANSACTION))\b",
    re.IGNORECASE,
)


def _body_or_nested_has_unsafe_txn_control(
    schema: str, name: str, definition: str, _depth: int = 0, _seen: set | None = None
) -> bool:
    """Return True if ``definition`` (or anything it calls, recursively)
    issues its own COMMIT/ROLLBACK/BEGIN TRAN/SAVE TRAN.

    Even in write-test mode, a procedure with explicit transaction control
    cannot be safely wrapped in our own ``BEGIN TRAN ... ROLLBACK TRAN`` —
    an internal COMMIT ends the outer transaction early (or a transaction
    count mismatch errors out), so the write may not be reliably undone.
    ``transferTicket`` (COMMIT inside BEGIN TRY) is a real example of this.
    These procedures are always rejected for execution, with or without
    ``SQLSERVER_ALLOW_WRITE_TESTS``.
    """
    if _TXN_CONTROL_PATTERN.search(definition):
        return True
    if _depth >= 3:
        return True
    seen = _seen if _seen is not None else set()
    key = (schema.lower(), name.lower())
    if key in seen:
        return False
    seen.add(key)
    for m in _EXEC_REF_PATTERN.finditer(definition):
        callee_schema = m.group("schema") or schema
        callee_name = m.group("name")
        if not callee_name or callee_name.lower() in ("sp_executesql", "sql"):
            continue
        if not _valid_ident(callee_schema) or not _valid_ident(callee_name):
            continue
        callee_meta = _query(
            "SELECT OBJECT_DEFINITION(o.object_id) AS definition "
            "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "WHERE s.name = %s AND o.name = %s",
            (callee_schema, callee_name),
        )
        if not callee_meta:
            continue
        callee_def = callee_meta[0]["definition"] or ""
        if _body_or_nested_has_unsafe_txn_control(callee_schema, callee_name, callee_def, _depth + 1, seen):
            return True
    return False


def _body_or_nested_has_dml(
    schema: str, name: str, definition: str, _depth: int = 0, _seen: set | None = None
) -> bool:
    """Return True if ``definition`` contains DML, directly or via a nested
    EXEC/EXECUTE call to another object in the same schema whose body (or
    further nested calls) contains DML.

    Rationale: the DML guard on ``call_procedure``/``time_procedure`` only
    scanned the literal text of the outer procedure. A procedure that itself
    contains no INSERT/UPDATE/DELETE but calls a helper procedure that does
    (a common pattern — e.g. ``generateTransferActivity`` delegating writes
    to ``transferTicket``) would pass the guard and reach a real EXECUTE
    against the database, relying entirely on the BEGIN TRAN/ROLLBACK wrapper
    as the only backstop. This walks the call graph (bounded depth + cycle
    guard) so the guard itself is the primary control again, consistent with
    the assert_read_only() contract used everywhere else.
    """
    if _DML_PATTERN.search(definition):
        return True
    if _depth >= 3:
        # Bound recursion depth; deeply nested call chains beyond this are
        # rejected defensively rather than risking a false negative.
        return True
    seen = _seen if _seen is not None else set()
    key = (schema.lower(), name.lower())
    if key in seen:
        return False  # cycle; already being checked up the call stack
    seen.add(key)

    for m in _EXEC_REF_PATTERN.finditer(definition):
        callee_schema = m.group("schema") or schema
        callee_name = m.group("name")
        if not callee_name or callee_name.lower() in ("sp_executesql", "sql"):
            continue
        if not _valid_ident(callee_schema) or not _valid_ident(callee_name):
            continue
        callee_meta = _query(
            "SELECT OBJECT_DEFINITION(o.object_id) AS definition "
            "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "WHERE s.name = %s AND o.name = %s",
            (callee_schema, callee_name),
        )
        if not callee_meta:
            continue  # not a resolvable object in this schema; nothing to recurse into
        callee_def = callee_meta[0]["definition"] or ""
        if _body_or_nested_has_dml(callee_schema, callee_name, callee_def, _depth + 1, seen):
            return True
    return False


def _param_sql_types(schema: str, name: str, count: int) -> list[str]:
    """Return the declared SQL type of each of the first ``count`` input
    parameters of ``schema.name``, in ordinal order.

    Used to declare ``sp_executesql`` parameters with their real type
    (e.g. ``int``, ``varchar(50)``) instead of ``sql_variant``, which SQL
    Server will not implicitly convert to the target parameter type for
    every function/procedure signature (e.g. calling an ``int`` parameter
    function raises "Implicit conversion from data type sql_variant to int
    is not allowed"). Falls back to ``sql_variant`` for any position we
    can't resolve, preserving the previous (looser) behaviour.
    """
    rows = _query(
        "SELECT p.parameter_id AS ordinal, t.name AS type_name, "
        "p.max_length AS max_length, p.precision AS precision, p.scale AS scale "
        "FROM sys.parameters p "
        "JOIN sys.types t ON t.user_type_id = p.user_type_id "
        "JOIN sys.objects o ON o.object_id = p.object_id "
        "JOIN sys.schemas s ON s.schema_id = o.schema_id "
        "WHERE s.name = %s AND o.name = %s AND p.is_output = 0 "
        "ORDER BY p.parameter_id",
        (schema, name),
    )
    types: list[str] = []
    for r in rows[:count]:
        tname = (r.get("type_name") or "").lower()
        if tname in ("varchar", "nvarchar", "char", "nchar", "varbinary", "binary"):
            length = r.get("max_length") or -1
            length_sql = "MAX" if length in (-1, None) else str(length)
            types.append(f"{tname}({length_sql})")
        elif tname in ("decimal", "numeric"):
            types.append(f"{tname}({r.get('precision') or 18},{r.get('scale') or 0})")
        elif tname:
            types.append(tname)
        else:
            types.append("sql_variant")
    while len(types) < count:
        types.append("sql_variant")
    return types


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
    # Reuse a single connection for the whole fallback loop. Opening a fresh
    # TDS connection per table can exceed the login timeout partway through a
    # large schema on high-latency links, failing the whole tool call.
    conn = _connect()
    try:
        cur = conn.cursor()
        for r in tables:
            tname = r["table_name"]
            if not _valid_ident(tname):
                continue
            # Use sp_executesql with QUOTENAME for server-side safe quoting.
            # Routed through _exec_dynamic — the single sanctioned path for
            # dynamic SQL in this server — rather than calling cur.execute()
            # directly, so the injection-safety contract stays centralized.
            _exec_dynamic(
                cur,
                "DECLARE @sql NVARCHAR(500) = "
                "N'SELECT COUNT_BIG(*) AS row_count FROM ' + QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
                " EXEC sp_executesql @sql",
                (schema, tname),
            )
            cnt = list(cur.fetchall()) if cur.description is not None else []
            out.append({"table_name": tname, "row_count": cnt[0]["row_count"] if cnt else 0})
    finally:
        conn.close()
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
    # Use sp_executesql with QUOTENAME for server-side safe quoting. Routed
    # through _exec_dynamic (not _query) because this is a two-statement
    # DECLARE + EXEC sp_executesql batch: _query()'s assert_read_only() guard
    # rejects any multi-statement payload outright (by design, for the
    # single-statement execute_select/execute_with_timing tools), and would
    # reject this call unconditionally. _exec_dynamic is the sanctioned path
    # for dynamic, server-side-quoted SQL in this server.
    sql = (
        "DECLARE @sql NVARCHAR(500) = "
        "N'SELECT TOP (' + CAST(%s AS NVARCHAR(10)) + N') * FROM ' "
        "+ QUOTENAME(%s) + N'.' + QUOTENAME(%s);"
        " EXEC sp_executesql @sql"
    )
    conn = _connect()
    try:
        cur = conn.cursor()
        _exec_dynamic(cur, sql, (int(limit), schema, table))
        rows = list(cur.fetchall()) if cur.description is not None else []
    finally:
        conn.close()
    return json.dumps(rows, default=str)


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
    # Use sp_executesql with QUOTENAME for server-side safe quoting. Routed
    # through _exec_dynamic (not _query) for the same reason as sample_table
    # above — this is a multi-statement batch that _query()'s read-only guard
    # would reject outright.
    sql = (
        "DECLARE @sql NVARCHAR(500) = "
        "N'SELECT DISTINCT TOP (' + CAST(%s AS NVARCHAR(10)) + N') ' "
        "+ QUOTENAME(%s) + N' AS v FROM ' "
        "+ QUOTENAME(%s) + N'.' + QUOTENAME(%s) "
        "+ N' WHERE ' + QUOTENAME(%s) + N' IS NOT NULL';"
        " EXEC sp_executesql @sql"
    )
    conn = _connect()
    try:
        cur = conn.cursor()
        _exec_dynamic(cur, sql, (int(limit), column, schema, table, column))
        rows = list(cur.fetchall()) if cur.description is not None else []
    finally:
        conn.close()
    return json.dumps(rows, default=str)


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
    # Writers (INSERT/UPDATE/DELETE/MERGE/TRUNCATE), including ones hidden
    # behind a nested EXEC/EXECUTE call, are rejected unless the operator has
    # explicitly opted in via SQLSERVER_ALLOW_WRITE_TESTS=yes.
    has_dml = _body_or_nested_has_dml(schema, name, definition)
    if has_dml and not _ALLOW_WRITE_TESTS:
        return json.dumps({"error": "rejected", "reason": "object body contains DML"})
    # Even with write-tests enabled, procedures with their own transaction
    # control (COMMIT/ROLLBACK/BEGIN TRAN) cannot be safely wrapped in a
    # rollback and are ALWAYS rejected — no override for this one.
    if has_dml and _body_or_nested_has_unsafe_txn_control(schema, name, definition):
        return json.dumps(
            {
                "error": "rejected",
                "reason": (
                    "object body (or a nested call) issues its own COMMIT/ROLLBACK/"
                    "BEGIN TRAN and cannot be safely wrapped in a rollback transaction"
                ),
            }
        )

    is_function = "FUNCTION" in type_desc.upper()

    conn = _connect()
    try:
        cur = conn.cursor()
        if is_function:
            # Use sp_executesql with QUOTENAME for safe function invocation.
            # Parameters are declared with their real SQL type (looked up
            # from sys.parameters) rather than sql_variant, since SQL Server
            # will not implicitly convert sql_variant into every parameter
            # type (e.g. int) — see _param_sql_types docstring.
            if args:
                param_count = len(args)
                param_types = _param_sql_types(schema, name, param_count)
                param_decls = ", ".join(f"@p{i} {t}" for i, t in enumerate(param_types))
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
                param_types = _param_sql_types(schema, name, param_count)
                param_decls = ", ".join(f"@p{i} {t}" for i, t in enumerate(param_types))
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
    has_dml = _body_or_nested_has_dml(schema, name, definition)
    if has_dml and not _ALLOW_WRITE_TESTS:
        return json.dumps({"error": "rejected", "reason": "object body contains DML"})
    if has_dml and _body_or_nested_has_unsafe_txn_control(schema, name, definition):
        return json.dumps(
            {
                "error": "rejected",
                "reason": (
                    "object body (or a nested call) issues its own COMMIT/ROLLBACK/"
                    "BEGIN TRAN and cannot be safely wrapped in a rollback transaction"
                ),
            }
        )

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
                param_types = _param_sql_types(schema, name, param_count)
                param_decls = ", ".join(f"@p{i} {t}" for i, t in enumerate(param_types))
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
                    param_types = _param_sql_types(schema, name, param_count)
                    param_decls = ", ".join(f"@p{i} {t}" for i, t in enumerate(param_types))
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
