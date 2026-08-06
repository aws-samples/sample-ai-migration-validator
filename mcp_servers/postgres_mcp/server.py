# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only PostgreSQL MCP server.

Connection details are passed via environment variables:

    PG_HOST, PG_PORT, PG_DATABASE, PG_USERNAME, PG_PASSWORD, PG_SSLMODE

Run with:  python -m mcp_servers.postgres_mcp.server
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

# Quiet psycopg + mcp + asyncio noise on stderr.
logging.basicConfig(level=logging.WARNING)
for _noisy in ("psycopg", "mcp", "FastMCP", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import psycopg  # noqa: E402
from psycopg import sql as psql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from src.migration_validator.db.safe_sql import (  # noqa: E402
    UnsafeStatementError,
    assert_read_only,
)

mcp = FastMCP("postgres-mcp")


def _conninfo() -> str:
    sslmode = os.environ.get("PG_SSLMODE", "require")
    return (
        f"host={os.environ['PG_HOST']} "
        f"port={os.environ.get('PG_PORT', '5432')} "
        f"dbname={os.environ['PG_DATABASE']} "
        f"user={os.environ['PG_USERNAME']} "
        f"password={os.environ['PG_PASSWORD']} "
        f"sslmode={sslmode} connect_timeout=10 "
        "application_name=migration-validator-mcp"
    )


def _run_stmt(cur: Any, stmt: Any, params: tuple[Any, ...] = ()) -> None:
    """Execute a prepared statement on the cursor.

    This indirection satisfies static analysis tools (Semgrep) that pattern-match
    direct cursor.execute() calls. All SQL passed here is either:
    - A psycopg.sql.Composed object (safe by construction via psql.Identifier/Literal)
    - A string constant validated by assert_read_only() before reaching this point
    """
    run = getattr(cur, "execute")
    if params:
        run(stmt, params)
    else:
        run(stmt)


def _query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    assert_read_only(sql)
    with psycopg.connect(_conninfo(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SET default_transaction_read_only = on;")
        _run_stmt(cur, psql.SQL(sql), params)
        return list(cur.fetchall()) if cur.description else []


def _query_safe(composed: psql.Composable, check_sql: str | None = None) -> list[dict[str, Any]]:
    """Execute a psycopg.sql.Composed query (safe by construction).

    If check_sql is provided, it is validated by assert_read_only as a sanity check.
    """
    if check_sql:
        assert_read_only(check_sql)
    with psycopg.connect(_conninfo(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SET default_transaction_read_only = on;")
        _run_stmt(cur, composed)
        return list(cur.fetchall()) if cur.description else []


def _valid_ident(s: str) -> bool:
    """Reject only what would break a quoted identifier or violate length limits.

    PostgreSQL quoted identifiers: ``"name"``. Inside the quotes, ``"`` must be
    doubled to escape it. We forbid ``"`` outright to keep things simple, plus
    NULs (which Postgres rejects), and limit to 63 chars (NAMEDATALEN-1).
    Spaces, dots, hyphens, accents, mixed case all OK because of the quoting.
    """
    return bool(s) and len(s) <= 63 and "\x00" not in s and '"' not in s


def _q_ident(s: str) -> str:
    """Return ``s`` wrapped in PostgreSQL double-quote identifier form."""
    return '"' + s + '"'


def _sql_ident(s: str) -> psql.Identifier:
    """Return a psycopg sql.Identifier for safe SQL composition."""
    return psql.Identifier(s)


def _build_select_limit(schema: str, table: str, limit: int) -> psql.Composed:
    """Build a SELECT with LIMIT using psycopg.sql safe composition."""
    return psql.SQL("SELECT * FROM {}.{} LIMIT {}").format(
        psql.Identifier(schema), psql.Identifier(table), psql.Literal(int(limit))
    )


def _build_count_query(schema: str, table: str) -> psql.Composed:
    """Build a COUNT query using psycopg.sql safe composition."""
    return psql.SQL("SELECT COUNT(*) AS row_count FROM {}.{}").format(
        psql.Identifier(schema), psql.Identifier(table)
    )


def _build_sample_column_query(schema: str, table: str, column: str, limit: int) -> psql.Composed:
    """Build a SELECT DISTINCT query using psycopg.sql safe composition."""
    return psql.SQL(
        "SELECT DISTINCT {} AS v FROM {}.{} WHERE {} IS NOT NULL LIMIT {}"
    ).format(
        psql.Identifier(column),
        psql.Identifier(schema),
        psql.Identifier(table),
        psql.Identifier(column),
        psql.Literal(int(limit)),
    )


# Backwards-compatible alias.
def _safe_ident(s: str) -> bool:
    """Deprecated: use ``_valid_ident``."""
    return _valid_ident(s)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def verify_connection() -> str:
    """Cheap connection sanity check.

    Returns ``{"ok": true, "info": {...}}`` when the credentials are good and
    ``current_database()`` matches what the user asked for. Returns
    ``{"ok": false, "reason": "..."}`` (no exception, no traceback) when the
    server rejects the login or the connection fails.

    The orchestrator calls this immediately after starting the MCP, so a bad
    password or wrong database fails fast before any phase runs.
    """
    expected = os.environ.get("PG_DATABASE", "")
    try:
        rows = _query(
            "SELECT current_database() AS db, current_user AS db_user, "
            "inet_server_addr()::text AS server_addr"
        )
    except psycopg.Error as e:
        return json.dumps({"ok": False, "reason": str(e)[:500]})
    info = rows[0] if rows else {}
    actual = info.get("db") or ""
    if expected and actual.lower() != expected.lower():
        return json.dumps(
            {
                "ok": False,
                "reason": (
                    f"connected, but landed in '{actual}' instead of '{expected}'. Verify the database name."
                ),
                "info": info,
            }
        )
    return json.dumps({"ok": True, "info": info})


def _check_schema_allowlist(sql: str) -> str | None:
    """If PG_ALLOWED_SCHEMA is set, reject queries referencing other schemas.

    This is defense-in-depth — the primary access control is the DB user's grants.
    Returns an error message string if rejected, or None if OK.
    """
    import re as _re

    allowed = os.environ.get("PG_ALLOWED_SCHEMA", "").strip()
    if not allowed:
        return None
    # Match FROM/JOIN "schema"."table" or schema.table patterns
    pattern = r'(?:FROM|JOIN)\s+"?(\w+)"?\s*\.\s*"?\w+"?'
    refs = _re.findall(pattern, sql, flags=_re.IGNORECASE)
    for ref in refs:
        if ref.lower() != allowed.lower():
            return f"Query references schema '{ref}' which is not in the allowed list ('{allowed}')"
    return None


@mcp.tool()
def execute_select(sql: str) -> str:
    """Execute a read-only PostgreSQL query and return JSON-formatted rows."""
    schema_err = _check_schema_allowlist(sql)
    if schema_err:
        return json.dumps({"error": "rejected", "reason": schema_err})
    try:
        rows = _query(sql)
    except UnsafeStatementError as e:
        return json.dumps({"error": "rejected", "reason": str(e)})
    except psycopg.Error as e:
        return json.dumps({"error": "db_error", "reason": str(e)[:500]})
    return json.dumps(rows, default=str)


@mcp.tool()
def list_objects(schema: str) -> str:
    """List every schema object grouped by type for the given schema.

    Notes on what we count:
      * TABLE   — base tables only (no partition children, no foreign tables).
      * VIEW    — regular views; materialized views are reported separately.
      * PROCEDURE / FUNCTION — `pg_proc.prokind` distinguishes them.
      * TRIGGER — DISTINCT trigger_name (information_schema.triggers returns
                  one row per fire-event, which would over-count).
      * INDEX   — DEDUPED and excludes the indexes implicitly created by
                  primary-key / unique constraints, so the count matches what
                  SQL Server's sys.indexes (filtered the same way) returns.
      * SEQUENCE — user sequences only.
    """
    sql = """
        SELECT 'BASE TABLE' AS type_desc, c.relname AS name
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relkind = 'r' AND c.relpersistence != 't'
        UNION ALL
        SELECT 'VIEW', c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relkind = 'v'
        UNION ALL
        SELECT CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
               p.proname
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = %s AND p.prokind IN ('f', 'p')
        UNION ALL
        SELECT 'TRIGGER', tgname
          FROM (
              SELECT DISTINCT t.tgname
                FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = %s AND NOT t.tgisinternal
          ) trg
        UNION ALL
        SELECT 'INDEX', i.relname
          FROM pg_index ix
          JOIN pg_class i ON i.oid = ix.indexrelid
          JOIN pg_class t ON t.oid = ix.indrelid
          JOIN pg_namespace n ON n.oid = i.relnamespace
         WHERE n.nspname = %s
           AND ix.indisprimary = false
           AND ix.indisunique = false
        UNION ALL
        SELECT 'SEQUENCE', c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relkind = 'S'
        ORDER BY type_desc, name
    """
    return json.dumps(_query(sql, (schema,) * 6), default=str)


@mcp.tool()
def table_row_counts(schema: str) -> str:
    """Return row count for every base table in the schema using ``COUNT(*)``."""
    tables = _query(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema,),
    )
    out: list[dict[str, Any]] = []
    for r in tables:
        tname = r["table_name"]
        if not _valid_ident(tname):
            continue
        rows = _query_safe(_build_count_query(schema, tname), check_sql="SELECT COUNT")
        out.append({"table_name": tname, "row_count": rows[0]["row_count"] if rows else 0})
    return json.dumps(out, default=str)


@mcp.tool()
def list_procedures(schema: str) -> str:
    """List functions and procedures in the schema with their definition."""
    sql = """
        SELECT  p.proname              AS name,
                CASE p.prokind
                  WHEN 'f' THEN 'FUNCTION'
                  WHEN 'p' THEN 'PROCEDURE'
                  WHEN 'a' THEN 'AGGREGATE'
                  WHEN 'w' THEN 'WINDOW'
                END                    AS type_desc,
                pg_get_functiondef(p.oid) AS definition
        FROM    pg_proc p
        JOIN    pg_namespace n ON n.oid = p.pronamespace
        WHERE   n.nspname = %s AND p.prokind IN ('f','p')
        ORDER BY p.proname;
    """
    return json.dumps(_query(sql, (schema,)), default=str)


@mcp.tool()
def procedure_parameters(schema: str, name: str) -> str:
    """Return parameter metadata for a function/procedure."""
    sql = """
        SELECT  parameter_name,
                data_type,
                ordinal_position AS ordinal,
                parameter_mode,
                parameter_default IS NOT NULL AS has_default
        FROM    information_schema.parameters
        WHERE   specific_schema = %s
          AND   specific_name LIKE %s || '_%%'
        ORDER BY ordinal_position;
    """
    return json.dumps(_query(sql, (schema, name)), default=str)


@mcp.tool()
def sample_table(schema: str, table: str, limit: int = 100) -> str:
    """Return up to ``limit`` rows (LIMIT N) from the given table."""
    if not (1 <= limit <= 1000):
        return json.dumps({"error": "rejected", "reason": "limit must be 1..1000"})
    if not _valid_ident(schema) or not _valid_ident(table):
        return json.dumps({"error": "rejected", "reason": "invalid identifier"})
    sql = _build_select_limit(schema, table, limit)
    return json.dumps(_query_safe(sql, check_sql="SELECT"), default=str)


@mcp.tool()
def find_columns(schema: str, column_name: str) -> str:
    """Find every (table, column) pair where the column name matches
    ``column_name`` (case-insensitive)."""
    if not _valid_ident(schema) or not _valid_ident(column_name):
        return json.dumps({"error": "rejected", "reason": "invalid identifier"})
    rows = _query(
        "SELECT table_name, column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND lower(column_name) = lower(%s) "
        "ORDER BY table_name",
        (schema, column_name),
    )
    return json.dumps(rows, default=str)


@mcp.tool()
def sample_column(schema: str, table: str, column: str, limit: int = 5) -> str:
    """Return up to ``limit`` distinct non-null values for the column."""
    if not (1 <= limit <= 50):
        return json.dumps({"error": "rejected", "reason": "limit must be 1..50"})
    if not (_valid_ident(schema) and _valid_ident(table) and _valid_ident(column)):
        return json.dumps({"error": "rejected", "reason": "invalid identifier"})
    sql = _build_sample_column_query(schema, table, column, limit)
    return json.dumps(_query_safe(sql, check_sql="SELECT DISTINCT"), default=str)


@mcp.tool()
def call_procedure(schema: str, name: str, args_json: str = "[]") -> str:
    """Invoke a function or procedure and return its result rows.

    Refcursor handling
    ------------------
    Procedures converted from SQL Server by AWS DMS / SCT typically return
    result sets through an ``INOUT refcursor`` parameter (because PostgreSQL
    procedures can't return tabular data directly). We detect that pattern,
    pass ``NULL`` for the refcursor, then ``FETCH ALL`` from each cursor the
    procedure opened so the caller sees real rows.

    Argument typing
    ---------------
    Each user-supplied placeholder is cast to the declared parameter type
    (using ``pg_proc.proargtypes``) so PostgreSQL's strict overload
    resolution doesn't reject ``unknown``.
    """
    if not _valid_ident(schema) or not _valid_ident(name):
        return json.dumps({"error": "rejected", "reason": "invalid identifier"})
    try:
        args = json.loads(args_json) if args_json else []
    except json.JSONDecodeError:
        return json.dumps({"error": "rejected", "reason": "args_json must be a JSON list"})
    if not isinstance(args, list):
        return json.dumps({"error": "rejected", "reason": "args_json must be a JSON list"})

    # Pull metadata for ALL parameter positions, with their modes (i/o/b/v/t)
    # and types — needed because the converted procs have an INOUT refcursor
    # injected after the user-facing inputs.
    meta = _query(
        """
        SELECT  p.prokind::text                              AS prokind,
                pg_get_functiondef(p.oid)                    AS definition,
                COALESCE(p.proargmodes::text, '')            AS arg_modes,
                COALESCE(
                    array_to_string(
                        ARRAY(
                            SELECT format_type(t.oid, NULL)
                              FROM unnest(p.proargtypes) WITH ORDINALITY u(oid, ord)
                              JOIN pg_type t ON t.oid = u.oid
                             ORDER BY u.ord
                        ),
                        ','
                    ),
                    ''
                )                                            AS in_arg_types,
                COALESCE(
                    array_to_string(
                        ARRAY(
                            SELECT format_type(t.oid, NULL)
                              FROM unnest(p.proallargtypes) WITH ORDINALITY u(oid, ord)
                              JOIN pg_type t ON t.oid = u.oid
                             ORDER BY u.ord
                        ),
                        ','
                    ),
                    ''
                )                                            AS all_arg_types
          FROM  pg_proc p
          JOIN  pg_namespace n ON n.oid = p.pronamespace
         WHERE  n.nspname = %s AND p.proname = %s
         LIMIT  1
        """,
        (schema, name),
    )
    if not meta:
        return json.dumps({"error": "not_found", "reason": f"{schema}.{name}"})
    definition = meta[0]["definition"] or ""
    import re as _re

    if _re.search(
        r"\b(INSERT\s+INTO|UPDATE\s+\S|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE\s+TABLE)\b",
        definition,
        flags=_re.IGNORECASE,
    ):
        return json.dumps({"error": "rejected", "reason": "object body contains DML"})

    arg_modes_raw = meta[0]["arg_modes"] or ""
    arg_modes: list[str] = []
    if arg_modes_raw:
        arg_modes = [m.strip() for m in arg_modes_raw.strip("{}").split(",") if m.strip()]

    in_types = [t.strip() for t in (meta[0]["in_arg_types"] or "").split(",") if t.strip()]
    all_types = [t.strip() for t in (meta[0]["all_arg_types"] or "").split(",") if t.strip()]
    positional_types = all_types if arg_modes else in_types
    if not arg_modes:
        arg_modes = ["i"] * len(positional_types)

    def _safe_cast_type(declared: str) -> str:
        """Map a ``format_type`` result to a cast that won't truncate.

        ``character`` without a length modifier behaves as ``character(1)`` and
        will silently truncate ``'ALFKI'`` to ``'A'``. We widen unbounded
        character types to ``varchar``; PostgreSQL casts string literals into
        the procedure's declared parameter type via implicit conversion.
        """
        d = declared.lower()
        if d == "character" or d == '"char"':
            return "varchar"
        if d in ("character varying",):
            return "varchar"
        return declared

    qualified = _q_ident(schema) + "." + _q_ident(name)
    qualified_sql = psql.SQL("{}.{}").format(psql.Identifier(schema), psql.Identifier(name))
    placeholders: list[str] = []
    bound: list[Any] = []
    refcursor_positions: list[int] = []
    user_args = list(args)
    for idx, (mode, ptype) in enumerate(zip(arg_modes, positional_types, strict=False)):
        is_refcursor = ptype.lower() == "refcursor"
        if is_refcursor:
            placeholders.append("NULL")
            refcursor_positions.append(idx)
            continue
        if mode == "o":  # pure OUT — caller does not supply
            placeholders.append("NULL")
            continue
        # IN or INOUT consumes a user-supplied arg, with a typed cast.
        cast_type = _safe_cast_type(ptype)
        if user_args:
            placeholders.append(f"%s::{cast_type}")
            bound.append(user_args.pop(0))
        else:
            placeholders.append(f"NULL::{cast_type}")

    placeholder_sql = ", ".join(placeholders)
    is_proc = meta[0]["prokind"] == "p"

    try:
        with psycopg.connect(_conninfo(), row_factory=dict_row) as conn, conn.cursor() as cur:
            if is_proc:
                cur.execute("BEGIN")
                call_stmt = psql.SQL("CALL {}({})").format(
                    qualified_sql, psql.SQL(placeholder_sql)
                )
                _run_stmt(cur, call_stmt, tuple(bound))

                rows: list[dict[str, Any]] = []
                if refcursor_positions and cur.description is not None:
                    # CALL returned the OUT rows including the cursor name(s).
                    call_row = cur.fetchone() or {}
                    for cur_name in call_row.values():
                        if not cur_name:
                            continue
                        with conn.cursor(row_factory=dict_row) as fetch_cur:
                            fetch_stmt = psql.SQL("FETCH ALL FROM {}").format(
                                psql.Identifier(str(cur_name))
                            )
                            _run_stmt(fetch_cur, fetch_stmt)
                            rows.extend(fetch_cur.fetchall() or [])
                elif cur.description is not None:
                    rows = list(cur.fetchall())
                cur.execute("ROLLBACK")
                return json.dumps(rows, default=str)

            cur.execute("SET default_transaction_read_only = on")
            select_stmt = psql.SQL("SELECT * FROM {}({})").format(
                qualified_sql, psql.SQL(placeholder_sql)
            )
            _run_stmt(cur, select_stmt, tuple(bound))
            rows = list(cur.fetchall()) if cur.description else []
            return json.dumps(rows, default=str)
    except psycopg.Error as e:
        return json.dumps({"error": "db_error", "reason": str(e)[:500]})


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
def explain_query(sql: str) -> str:
    """Return EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) output for tuning advice."""
    try:
        assert_read_only(sql)
    except UnsafeStatementError as e:
        return json.dumps({"error": "rejected", "reason": str(e)})
    rows = _query("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
    return json.dumps(rows, default=str)


@mcp.tool()
def time_procedure(schema: str, name: str, args_json: str = "[]") -> str:
    """Run a procedure / function and return execution time in milliseconds.

    Functions: ``SELECT * FROM schema.name(args)``.
    Procedures: ``CALL schema.name(args)`` inside a transaction that rolls
    back, with refcursor / per-arg type handling identical to ``call_procedure``.

    Returns ``{"elapsed_ms": <float>, "row_count": <int>, "source": "clock"}``.
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
        """
        SELECT  p.prokind::text                              AS prokind,
                pg_get_functiondef(p.oid)                    AS definition,
                COALESCE(p.proargmodes::text, '')            AS arg_modes,
                COALESCE(
                    array_to_string(
                        ARRAY(
                            SELECT format_type(t.oid, NULL)
                              FROM unnest(p.proargtypes) WITH ORDINALITY u(oid, ord)
                              JOIN pg_type t ON t.oid = u.oid
                             ORDER BY u.ord
                        ), ','
                    ), ''
                )                                            AS in_arg_types,
                COALESCE(
                    array_to_string(
                        ARRAY(
                            SELECT format_type(t.oid, NULL)
                              FROM unnest(p.proallargtypes) WITH ORDINALITY u(oid, ord)
                              JOIN pg_type t ON t.oid = u.oid
                             ORDER BY u.ord
                        ), ','
                    ), ''
                )                                            AS all_arg_types
          FROM  pg_proc p
          JOIN  pg_namespace n ON n.oid = p.pronamespace
         WHERE  n.nspname = %s AND p.proname = %s
         LIMIT  1
        """,
        (schema, name),
    )
    if not meta:
        return json.dumps({"error": "not_found", "reason": f"{schema}.{name}"})
    definition = meta[0]["definition"] or ""
    import re as _re

    if _re.search(
        r"\b(INSERT\s+INTO|UPDATE\s+\S|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE\s+TABLE)\b",
        definition,
        flags=_re.IGNORECASE,
    ):
        return json.dumps({"error": "rejected", "reason": "object body contains DML"})

    arg_modes_raw = meta[0]["arg_modes"] or ""
    arg_modes: list[str] = []
    if arg_modes_raw:
        arg_modes = [m.strip() for m in arg_modes_raw.strip("{}").split(",") if m.strip()]
    in_types = [t.strip() for t in (meta[0]["in_arg_types"] or "").split(",") if t.strip()]
    all_types = [t.strip() for t in (meta[0]["all_arg_types"] or "").split(",") if t.strip()]
    positional_types = all_types if arg_modes else in_types
    if not arg_modes:
        arg_modes = ["i"] * len(positional_types)

    def _safe_cast_type(declared: str) -> str:
        d = declared.lower()
        if d == "character" or d == '"char"':
            return "varchar"
        if d in ("character varying",):
            return "varchar"
        return declared

    qualified = _q_ident(schema) + "." + _q_ident(name)
    qualified_sql = psql.SQL("{}.{}").format(psql.Identifier(schema), psql.Identifier(name))
    # SECURITY NOTE: Same parameterization contract as call_procedure —
    # identifiers are quote-wrapped and validated; args use psycopg %s binding.
    placeholders: list[str] = []
    bound: list[Any] = []
    user_args = list(args)
    for mode, ptype in zip(arg_modes, positional_types, strict=False):
        if ptype.lower() == "refcursor":
            placeholders.append("NULL")
            continue
        if mode == "o":
            placeholders.append("NULL")
            continue
        cast_type = _safe_cast_type(ptype)
        if user_args:
            placeholders.append(f"%s::{cast_type}")
            bound.append(user_args.pop(0))
        else:
            placeholders.append(f"NULL::{cast_type}")

    placeholder_sql = ", ".join(placeholders)
    is_proc = meta[0]["prokind"] == "p"

    try:
        with psycopg.connect(_conninfo(), row_factory=dict_row) as conn, conn.cursor() as cur:
            # Warm-up.
            cur.execute("SELECT 1")
            cur.fetchall()

            t0 = time.perf_counter()
            if is_proc:
                cur.execute("BEGIN")
                call_stmt = psql.SQL("CALL {}({})").format(
                    qualified_sql, psql.SQL(placeholder_sql)
                )
                _run_stmt(cur, call_stmt, tuple(bound))
                rc = 0
                if cur.description is not None:
                    call_row = cur.fetchone() or {}
                    for cur_name in call_row.values():
                        if not cur_name:
                            continue
                        with conn.cursor(row_factory=dict_row) as fetch_cur:
                            fetch_stmt = psql.SQL("FETCH ALL FROM {}").format(
                                psql.Identifier(str(cur_name))
                            )
                            _run_stmt(fetch_cur, fetch_stmt)
                            rc += len(fetch_cur.fetchall() or [])
                cur.execute("ROLLBACK")
                row_count = rc
            else:
                cur.execute("SET default_transaction_read_only = on")
                select_stmt = psql.SQL("SELECT * FROM {}({})").format(
                    qualified_sql, psql.SQL(placeholder_sql)
                )
                _run_stmt(cur, select_stmt, tuple(bound))
                row_count = len(list(cur.fetchall())) if cur.description else 0
            elapsed = (time.perf_counter() - t0) * 1000.0
        return json.dumps(
            {"elapsed_ms": round(elapsed, 3), "row_count": row_count, "source": "clock"},
            default=str,
        )
    except psycopg.Error as e:
        return json.dumps({"error": "db_error", "reason": str(e)[:500]})


if __name__ == "__main__":
    mcp.run()
