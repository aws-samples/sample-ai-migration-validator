# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""End-to-end probe: launch the validator's SQL Server MCP server and ask it
for database info, list_objects, and table_row_counts.

Reads SQLSERVER_* and SQLSERVER_SCHEMA from the environment so passwords never
touch argv. Useful for confirming the MCP transport works against a real
server independently of the orchestrator and agents.

Failures are surfaced loudly. If the MCP server crashes during startup or a
tool call, you will see the subprocess stderr printed at the end.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from strands.tools.mcp import MCPClient


def _missing(name: str) -> bool:
    return not os.environ.get(name)


def _dump_block(text: str) -> None:
    """Pretty-print a JSON text block; pass-through if not JSON."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        print(text[:4000])
        return
    print(json.dumps(parsed, indent=2, default=str)[:8000])


def main() -> int:
    required = ["SQLSERVER_HOST", "SQLSERVER_DATABASE", "SQLSERVER_USERNAME", "SQLSERVER_PASSWORD"]
    if any(_missing(n) for n in required):
        print(
            "Set SQLSERVER_HOST, SQLSERVER_DATABASE, SQLSERVER_USERNAME, "
            "SQLSERVER_PASSWORD (and optionally SQLSERVER_PORT, SQLSERVER_SCHEMA) before running.",
            file=sys.stderr,
        )
        return 2

    schema = os.environ.get("SQLSERVER_SCHEMA", "dbo")

    # Capture MCP server stderr so we can show it on failure.
    errlog_path = Path(tempfile.gettempdir()) / "probe-mcp.log"
    errlog = errlog_path.open("w", encoding="utf-8")

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_servers.sqlserver_mcp.server"],
        env={**os.environ},
    )

    print(
        f"Spawning MCP: {sys.executable} -m mcp_servers.sqlserver_mcp.server\n"
        f"Server stderr -> {errlog_path}\n"
        f"DB target: host={os.environ['SQLSERVER_HOST']} "
        f"port={os.environ.get('SQLSERVER_PORT', '1433')} "
        f"db={os.environ['SQLSERVER_DATABASE']} user={os.environ['SQLSERVER_USERNAME']} "
        f"schema={schema}\n"
    )

    client = MCPClient(lambda: stdio_client(params, errlog=errlog))
    try:
        client.start()
    except Exception as e:
        errlog.flush()
        errlog.close()
        print(f"\n[ERROR] MCP failed to start: {e}\n--- subprocess stderr ---")
        print(errlog_path.read_text(errors="replace"))
        return 3

    try:
        tools = client.list_tools_sync()
        print(f"Tools advertised by server: {[t.tool_name for t in tools]}\n")

        for tool in ("db_info", "list_objects", "table_row_counts"):
            kwargs: dict = {} if tool == "db_info" else {"schema": schema}
            print(f"\n==== {tool}({kwargs}) ====")
            try:
                result = client.call_tool_sync(tool_use_id=f"probe-{tool}", name=tool, arguments=kwargs)
            except Exception as e:
                print(f"[ERROR] tool call raised: {e}")
                continue

            content = result["content"] if isinstance(result, dict) else getattr(result, "content", [])
            is_error = (
                result.get("isError", False)
                if isinstance(result, dict)
                else getattr(result, "isError", False)
            )
            if is_error:
                print("[!] MCP returned isError=True")
            if not content:
                print("[!] MCP returned no content blocks at all")
                print(f"    raw result: {result!r}")
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if text is None and "json" in block:
                        text = json.dumps(block["json"])
                else:
                    text = getattr(block, "text", None)
                if text is None:
                    print(f"[block of type {type(block).__name__} has no text]: {block!r}")
                    continue
                _dump_block(text)
    finally:
        try:
            client.stop(None, None, None)
        except Exception:  # noqa: S110 - shutdown best-effort
            pass
        errlog.flush()
        errlog.close()

    print(f"\n--- MCP server stderr ({errlog_path}) ---")
    print(errlog_path.read_text(errors="replace") or "(empty)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
