# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure-logic tests of the four agents (no DB, no LLM, no real MCP).

We patch ``BaseAgent.call_json`` to feed canned MCP-tool responses so we can
exercise the diff/aggregation logic without spinning up servers.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from migration_validator.agents.functional_agent import FunctionalTestingAgent
from migration_validator.agents.inventory_agent import InventoryAgent
from migration_validator.agents.performance_agent import PerformanceAgent
from migration_validator.agents.row_count_agent import RowCountAgent
from migration_validator.config import ConnectionDetails, ValidatorConfig


def _cfg() -> ValidatorConfig:
    return ValidatorConfig(
        source=ConnectionDetails(
            engine="sqlserver",
            host="h",
            port=1433,
            database="d",
            username="u",
            password="p",
        ),
        target=ConnectionDetails(
            engine="postgresql",
            host="h2",
            port=5432,
            database="d",
            username="u",
            password="p",
        ),
    )


class _FakeSession:
    """Stand-in for ``MCPSession`` — only used as an identity by patched call_json."""

    def __init__(self, role: str) -> None:
        self.role = role


class _FakeManager:
    """Stand-in for ``MCPSessionManager``: returns fixed fake sessions per role."""

    def __init__(self) -> None:
        self.sources: dict[str, _FakeSession] = {
            "source": _FakeSession("source"),
            "target": _FakeSession("target"),
        }

    def get_or_create(self, _details: Any, role: str) -> _FakeSession:
        return self.sources[role]


def _patched_call(map_by_role: dict[str, dict[tuple[str, frozenset], Any]]):
    """Build a ``call_json`` substitute that dispatches by (session.role, tool, args)."""

    def fake(_self, session, tool, args=None):
        key = (tool, frozenset((args or {}).items()))
        return map_by_role[session.role].get(key)

    return fake


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
def test_inventory_diff_shapes_rows() -> None:
    cfg = _cfg()
    agent = InventoryAgent(cfg, _FakeManager())

    src_payload = [
        {"type_desc": "USER_TABLE", "name": "Customers"},
        {"type_desc": "USER_TABLE", "name": "Orders"},
        {"type_desc": "VIEW", "name": "v_active"},
        {"type_desc": "SQL_STORED_PROCEDURE", "name": "sp_recalc"},
    ]
    tgt_payload = [
        {"type_desc": "BASE TABLE", "name": "customers"},
        {"type_desc": "BASE TABLE", "name": "orders"},
        {"type_desc": "BASE TABLE", "name": "audit_log"},  # extra
        {"type_desc": "PROCEDURE", "name": "sp_recalc"},
    ]
    by_role = {
        "source": {("list_objects", frozenset({("schema", cfg.source.schema_name)})): src_payload},
        "target": {("list_objects", frozenset({("schema", cfg.target.schema_name)})): tgt_payload},
    }
    with patch.object(InventoryAgent, "call_json", _patched_call(by_role)):
        result = agent.run()

    by_type = {r["object_type"]: r for r in result.rows}
    assert by_type["TABLE"]["status"] == "mismatch"
    assert by_type["TABLE"]["_extra"] == ["audit_log"]
    assert by_type["VIEW"]["_missing"] == ["v_active"]
    assert "audit_log" in by_type["TABLE"]["details"]
    assert "v_active" in by_type["VIEW"]["details"]
    assert by_type["PROCEDURE"]["status"] == "match"
    assert result.status == "warn"


def test_inventory_all_match() -> None:
    cfg = _cfg()
    agent = InventoryAgent(cfg, _FakeManager())
    src = [{"type_desc": "USER_TABLE", "name": "A"}, {"type_desc": "USER_TABLE", "name": "B"}]
    tgt = [{"type_desc": "BASE TABLE", "name": "a"}, {"type_desc": "BASE TABLE", "name": "b"}]
    by_role = {
        "source": {("list_objects", frozenset({("schema", cfg.source.schema_name)})): src},
        "target": {("list_objects", frozenset({("schema", cfg.target.schema_name)})): tgt},
    }
    with patch.object(InventoryAgent, "call_json", _patched_call(by_role)):
        result = agent.run()
    assert result.status == "ok"
    assert "match" in result.summary.lower()


# ---------------------------------------------------------------------------
# Row counts
# ---------------------------------------------------------------------------
def test_row_count_diff_detection() -> None:
    cfg = _cfg()
    agent = RowCountAgent(cfg, _FakeManager())

    src_payload = [
        {"table_name": "a", "row_count": 100},
        {"table_name": "b", "row_count": 50},
    ]
    tgt_payload = [
        {"table_name": "a", "row_count": 100},
        {"table_name": "b", "row_count": 49},  # mismatch
        {"table_name": "c", "row_count": 1},  # extra
    ]
    by_role = {
        "source": {("table_row_counts", frozenset({("schema", cfg.source.schema_name)})): src_payload},
        "target": {("table_row_counts", frozenset({("schema", cfg.target.schema_name)})): tgt_payload},
    }
    with patch.object(RowCountAgent, "call_json", _patched_call(by_role)):
        result = agent.run()

    by_table = {r["table_name"]: r for r in result.rows}
    assert by_table["a"]["status"] == "match"
    assert by_table["b"]["status"] == "mismatch"
    assert by_table["b"]["delta"] == -1
    assert by_table["c"]["status"] == "extra_in_target"
    assert result.status == "fail"


# ---------------------------------------------------------------------------
# Functional & performance — smoke tests on internal helpers
# ---------------------------------------------------------------------------
def test_functional_referenced_tables_extracts_identifiers() -> None:
    sql = """
        CREATE FUNCTION dbo.f() RETURNS INT AS BEGIN
            SELECT TOP 1 c.id FROM dbo.Customers c
            INNER JOIN [dbo].[Orders] o ON o.cid = c.id
        END
    """
    out = FunctionalTestingAgent._referenced_tables(sql)
    assert "Customers" in out
    assert "Orders" in out


def test_functional_referenced_tables_handles_quoted_names_with_spaces() -> None:
    """Northwind has tables like ``[Order Details]`` that the old regex missed."""
    sql = """
        CREATE PROC dbo.X AS
        SELECT TOP 5 * FROM [dbo].[Order Details]
        UNION ALL
        SELECT TOP 5 * FROM "public"."Order Details"
    """
    out = FunctionalTestingAgent._referenced_tables(sql)
    assert "Order Details" in out


def test_functional_quote_literal_handles_types() -> None:
    f = FunctionalTestingAgent
    assert f._quote_literal(None) == "NULL"
    assert f._quote_literal(True) == "1"
    assert f._quote_literal(42) == "42"
    assert f._quote_literal("O'Brien") == "'O''Brien'"


def test_performance_agent_threshold_logic() -> None:
    cfg = _cfg()
    cfg = cfg.model_copy(update={"perf_threshold_ms": 5.0})
    agent = PerformanceAgent(cfg, _FakeManager())

    fake_meta = MagicMock()
    fake_meta.name = "do_thing"
    fake_meta.type_desc = "FUNCTION"
    fake_meta.parameters = []  # no input params -> single empty-args test case
    fake_meta.definition = ""

    # Two _list_procs calls (source then target). 3 test cases x 2 timings each
    # plus 1 warm-up per side = 4 source + 4 target timing returns.
    src_timings = [(0.5, ""), (1.0, ""), (1.0, ""), (1.0, "")]
    tgt_timings = [(0.5, ""), (10.0, ""), (10.0, ""), (10.0, "")]

    with (
        patch.object(
            FunctionalTestingAgent,
            "_list_procs",
            side_effect=[
                {"do_thing": fake_meta},
                {"do_thing": fake_meta},
            ],
        ),
        patch.object(
            PerformanceAgent,
            "_time_one",
            side_effect=lambda session, *_: (
                src_timings.pop(0) if session.role == "source" else tgt_timings.pop(0)
            ),
        ),
    ):
        result = agent.run()

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["status"] == "flagged"
    assert row["sql_server_ms"] == 1.0
    assert row["postgresql_ms"] == 10.0
    assert row["delta_ms"] == 9.0
    assert row.get("notes")
    assert result.status == "warn"
