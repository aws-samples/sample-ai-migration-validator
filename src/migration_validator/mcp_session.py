# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MCP session manager.

Responsibilities:
* Given a ``ConnectionDetails``, return an active ``MCPClient`` connected to a
  read-only stdio MCP server for that database.
* If a session for the same connection fingerprint is already running, reuse it.
* Spawn a fresh server (`python -m mcp_servers.sqlserver_mcp.server` or
  `mcp_servers.postgres_mcp.server`) only when no session exists.
* Surface lifecycle messages to the console so users can see "creating MCP /
  reusing MCP".
* Cleanly shut every session down on ``close_all()``.

Lifecycle guarantees
--------------------
* **Per run**: at most ONE MCP server process per unique fingerprint
  (engine|host|port|db|user|schema). Subsequent ``get_or_create`` calls with
  the same fingerprint return the cached session and print ``↻ Reusing``.
* **On normal exit**: the ``with`` block's ``__exit__`` calls ``close_all()``,
  which calls ``client.stop()`` on every session, terminating the subprocess.
* **On Ctrl-C / SIGTERM**: the same ``close_all()`` is invoked from a signal
  handler, then the original signal is re-raised so the user gets the
  expected exit behaviour.
* **On Python interpreter shutdown**: ``atexit`` re-runs ``close_all()`` as a
  belt-and-braces guarantee against any unusual termination path.
* **Process discipline**: each MCP subprocess inherits stdin from the parent.
  When the parent exits and stdio closes, the MCP server's read loop returns
  EOF and the server exits voluntarily. The subprocess is therefore self-
  cleaning even in the unlikely event ``close_all()`` is never reached.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import signal
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import IO, Any

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from rich.console import Console
from strands.tools.mcp import MCPClient

from .config import ConnectionDetails
from .utils import logging as _log

log = _log.get(__name__)
_console = Console()


@dataclass(frozen=True)
class MCPFingerprint:
    """A stable identifier for an MCP target. Excludes the password by design."""

    engine: str
    host: str
    port: int
    database: str
    username: str
    schema_name: str

    @classmethod
    def of(cls, c: ConnectionDetails) -> MCPFingerprint:
        return cls(
            engine=c.engine,
            host=c.host,
            port=c.port,
            database=c.database,
            username=c.username,
            schema_name=c.schema_name,
        )

    def short(self) -> str:
        raw = f"{self.engine}|{self.host}|{self.port}|{self.database}|{self.username}".encode()
        return hashlib.sha256(raw).hexdigest()[:10]


@dataclass
class MCPSession:
    """A live MCP session bound to one database."""

    role: str  # 'source' or 'target'
    fingerprint: MCPFingerprint
    client: MCPClient
    tool_names: list[str]

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> str:
        """Invoke ``tool`` on the MCP server and return its text payload.

        The MCP servers in this project always return a single text content
        block (JSON), so we extract that for ergonomic call sites.

        Note: ``MCPToolResult`` is a ``TypedDict`` at runtime — content is
        accessed by key (``result["content"]``), not by attribute. The same
        applies to each content block.
        """
        result = self.client.call_tool_sync(
            tool_use_id=f"{self.role}-{tool}",
            name=tool,
            arguments=arguments or {},
        )
        # Defensive: handle both dict-style and object-style results.
        content = result["content"] if isinstance(result, dict) else getattr(result, "content", [])
        for block in content or []:
            if isinstance(block, dict):
                text = block.get("text")
                if text is not None:
                    return text
                # Some servers wrap JSON content in a 'json' key instead of 'text'.
                if "json" in block:
                    import json as _json

                    return _json.dumps(block["json"])
            else:
                text = getattr(block, "text", None)
                if text is not None:
                    return text
        return ""


class MCPSessionManager:
    """Spins up and reuses MCP sessions for source / target connections."""

    def __init__(self, log_dir: Path | None = None) -> None:
        self._sessions: dict[MCPFingerprint, MCPSession] = {}
        self._closed = False
        # Persist the MCP server stderr alongside the validation reports so
        # the user can inspect what the servers actually said after the run
        # completes. Each run gets its own timestamped file.
        log_dir = log_dir or Path("./reports")
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self._errlog_path = log_dir / f"mcp-{ts}.log"
        self._errlog: IO[str] = self._errlog_path.open("w", encoding="utf-8")
        # Belt-and-braces: ensure children are cleaned up even on hard exit.
        atexit.register(self.close_all)
        self._install_signal_handlers()

    @property
    def errlog_path(self) -> Path:
        return self._errlog_path

    # ------------------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        """Make Ctrl-C / SIGTERM tear down child MCP processes before exiting."""

        def handler(signum: int, _frame: FrameType | None) -> None:
            try:
                self.close_all()
            finally:
                # Restore default behaviour and re-raise so the shell sees the
                # right exit status.
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Not the main thread, or platform doesn't allow it. We still
                # have ``atexit`` and the ``with`` context as backstops.
                pass

    # ------------------------------------------------------------------
    def get_or_create(self, details: ConnectionDetails, role: str) -> MCPSession:
        """Return the MCP session for ``details``, creating one if necessary.

        Reuse semantics: sessions are pooled inside a single validator run.
        Two phases (or two roles) that share the same fingerprint share one
        MCP server process. Across separate ``./run`` invocations, sessions
        are NOT reused; each run starts fresh server processes and shuts them
        down on exit. This is by design — the validator is a CLI tool, not a
        long-running daemon.

        Lifecycle messages: 'creating' is shown to the user (they may wait a
        few seconds for the subprocess to spawn). 'reusing' is logged at
        DEBUG level only, so phases that fan out many tool calls don't spam
        the console.
        """
        fp = MCPFingerprint.of(details)
        existing = self._sessions.get(fp)
        if existing is not None:
            log.debug(
                "Reusing MCP session [%s] for %s (%s:%s/%s)",
                fp.short(),
                role,
                details.host,
                details.port,
                details.database,
            )
            return existing

        _console.print(
            f"  [cyan]+ Creating MCP session [{fp.short()}] for {role} "
            f"({details.engine} {details.host}:{details.port}/{details.database})[/]"
        )
        client = self._build_client(details, self._errlog)
        client.start()
        try:
            tool_listing = client.list_tools_sync()
            tool_names = [t.tool_name for t in tool_listing]
        except Exception as e:
            client.stop(None, None, None)
            raise RuntimeError(f"MCP server for {role} failed to start: {e}") from e

        session = MCPSession(role=role, fingerprint=fp, client=client, tool_names=tool_names)
        self._sessions[fp] = session
        _console.print(f"    [dim]tools available: {', '.join(tool_names)}[/]")
        return session

    # ------------------------------------------------------------------
    @staticmethod
    def _build_client(details: ConnectionDetails, errlog: IO[str]) -> MCPClient:
        """Return an unstarted ``MCPClient`` configured for the right engine."""
        if details.engine == "sqlserver":
            module = "mcp_servers.sqlserver_mcp.server"
            env = {
                "SQLSERVER_HOST": details.host,
                "SQLSERVER_PORT": str(details.port),
                "SQLSERVER_DATABASE": details.database,
                "SQLSERVER_USERNAME": details.username,
                "SQLSERVER_PASSWORD": details.password.get_secret_value(),
                "SQLSERVER_ENCRYPT": "yes" if details.encrypt else "no",
                "SQLSERVER_ALLOWED_SCHEMA": details.schema_name,
            }
        elif details.engine == "postgresql":
            module = "mcp_servers.postgres_mcp.server"
            env = {
                "PG_HOST": details.host,
                "PG_PORT": str(details.port),
                "PG_DATABASE": details.database,
                "PG_USERNAME": details.username,
                "PG_PASSWORD": details.password.get_secret_value(),
                "PG_SSLMODE": "require" if details.encrypt else "prefer",
                "PG_ALLOWED_SCHEMA": details.schema_name,
            }
        else:
            raise ValueError(f"Unsupported engine: {details.engine}")

        # Inherit PATH / PYTHONPATH but layer connection env vars on top so
        # they never appear on the command line.
        merged_env = {**os.environ, **env}
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", module],
            env=merged_env,
        )
        return MCPClient(lambda: stdio_client(params, errlog=errlog))

    # ------------------------------------------------------------------
    def close_all(self) -> None:
        """Shut down every active session. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        for session in list(self._sessions.values()):
            try:
                session.client.stop(None, None, None)
            except Exception as e:
                log.warning("error closing MCP session %s: %s", session.fingerprint.short(), e)
        self._sessions.clear()
        try:
            self._errlog.close()
        except Exception:  # noqa: S110 - close failure during shutdown is non-fatal
            pass

    # ------------------------------------------------------------------
    def __enter__(self) -> MCPSessionManager:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close_all()
