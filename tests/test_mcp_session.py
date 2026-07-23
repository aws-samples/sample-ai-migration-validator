# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for ``MCPSessionManager`` reuse semantics and lifecycle.

We don't actually start a server subprocess here — that requires real DBs.
Instead we patch ``MCPClient`` so we can observe how many times the manager
constructs / starts / stops a session.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from migration_validator.config import ConnectionDetails
from migration_validator.mcp_session import MCPFingerprint, MCPSessionManager


def _details(role: str, host: str = "h", database: str = "d") -> ConnectionDetails:
    engine = "sqlserver" if role == "source" else "postgresql"
    port = 1433 if engine == "sqlserver" else 5432
    return ConnectionDetails(
        engine=engine,
        host=host,
        port=port,
        database=database,
        username="u",
        password="p",
        schema_name="dbo" if engine == "sqlserver" else "public",
    )


def _make_fake_client_factory():
    """Return (factory, instances). Each call to factory() creates a fresh mock."""
    instances: list[MagicMock] = []

    def factory(*_args, **_kwargs):
        m = MagicMock(name=f"MCPClient-{len(instances)}")
        # Realistic behaviour: list_tools_sync returns objects with .tool_name.
        m.list_tools_sync.return_value = [MagicMock(tool_name="execute_select")]
        instances.append(m)
        return m

    return factory, instances


def test_same_fingerprint_reuses_one_subprocess() -> None:
    factory, instances = _make_fake_client_factory()
    with patch("migration_validator.mcp_session.MCPClient", side_effect=factory):
        with MCPSessionManager() as mgr:
            d = _details("source")
            s1 = mgr.get_or_create(d, role="source")
            s2 = mgr.get_or_create(d, role="source")  # same fingerprint
            s3 = mgr.get_or_create(d, role="source")
    # Only ONE underlying MCPClient was constructed.
    assert len(instances) == 1
    # All three returns are the same session object.
    assert s1 is s2 is s3


def test_different_databases_get_their_own_subprocess() -> None:
    factory, instances = _make_fake_client_factory()
    with patch("migration_validator.mcp_session.MCPClient", side_effect=factory):
        with MCPSessionManager() as mgr:
            mgr.get_or_create(_details("source", database="db1"), role="source")
            mgr.get_or_create(_details("target", database="db2"), role="target")
    assert len(instances) == 2


def test_close_all_is_idempotent() -> None:
    factory, instances = _make_fake_client_factory()
    with patch("migration_validator.mcp_session.MCPClient", side_effect=factory):
        mgr = MCPSessionManager()
        mgr.get_or_create(_details("source"), role="source")
        mgr.close_all()
        mgr.close_all()
        mgr.close_all()
    # ``stop`` was called exactly once per session despite three close_all() calls.
    assert instances[0].stop.call_count == 1


def test_context_manager_stops_clients_on_exit() -> None:
    factory, instances = _make_fake_client_factory()
    with patch("migration_validator.mcp_session.MCPClient", side_effect=factory):
        with MCPSessionManager() as mgr:
            mgr.get_or_create(_details("source"), role="source")
            mgr.get_or_create(_details("target"), role="target")
    assert all(inst.stop.call_count == 1 for inst in instances)


def test_fingerprint_excludes_password() -> None:
    a = ConnectionDetails(
        engine="sqlserver",
        host="h",
        port=1433,
        database="d",
        username="u",
        password="one",
    )
    b = ConnectionDetails(
        engine="sqlserver",
        host="h",
        port=1433,
        database="d",
        username="u",
        password="two",  # different password, same target
    )
    # Two passwords, same connection target → same fingerprint, same MCP.
    assert MCPFingerprint.of(a) == MCPFingerprint.of(b)
