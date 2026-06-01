# Architecture

## High-level flow

```
CLI (typer) ─▶ build_config()  ─▶ ValidatorConfig (source + target)
                                       │
                                       ▼
                         MCPSessionManager (lazy, fingerprinted, reusable)
                                       │ stdio
                  ┌────────────────────┼────────────────────┐
                  ▼                                         ▼
        SQL Server MCP server                       PostgreSQL MCP server
        (FastMCP, read-only)                        (FastMCP, read-only)
                  │                                         │
                  ▼                                         ▼
           SQL Server (source)                     PostgreSQL (target)

                                       │
                                       ▼
                                  Orchestrator
                                       │  prompts: interactive | auto
                                       ▼
            ┌──────────────┬──────────────┬──────────────┬──────────────┐
            ▼              ▼              ▼              ▼              ▼
        Inventory      RowCount       Functional    Performance    Reports
        Agent          Agent          Testing       Testing        (HTML+JSON)
                                      Agent         Agent

   Every agent calls MCP tools — the agents do not open DB connections directly.

  Credentials: AWS Secrets Manager (preferred) | env vars | YAML | interactive prompt
  LLM:         Amazon Bedrock (Anthropic Claude family) — used by Functional and
               Performance agents for analysis & narration. The system never sends
               raw row data to the LLM; only summarised diff payloads.
```

## Run-time sequence (what the user sees)

1. `python -m migration_validator` is invoked.
2. CLI resolves connection details (CLI > YAML > Secrets Manager > env > interactive prompt). Passwords are masked at the prompt.
3. `MCPSessionManager.get_or_create()` is called for source and target.
   - It fingerprints the connection (`engine|host|port|db|user|schema`) so repeat calls in the same run reuse the existing process.
   - It launches `python -m mcp_servers.sqlserver_mcp.server` (or `mcp_servers.postgres_mcp.server`) as a stdio subprocess, passing connection details via environment variables — never via the command line.
4. The orchestrator prints the four capabilities and asks **interactive** vs **auto**.
5. Phases run in order. Each agent only calls MCP tools (`list_objects`, `table_row_counts`, `list_procedures`, `procedure_parameters`, `sample_table`, `execute_select`, `execute_with_timing`, `explain_query`) — there is no direct `pyodbc`/`psycopg` use in the agent code path.
6. After every phase the result is rendered as a Rich table. In interactive mode the orchestrator pauses and asks "Proceed to phase N?" before continuing.
7. After all phases, a self-contained HTML report (and matching JSON) is written to `./reports/` and the absolute paths are printed.

## Trust boundaries

| Boundary | Enforcement |
|---|---|
| User → CLI | Typer validates flag values; passwords prompted via `getpass`. |
| CLI → Secrets Manager | IAM role grants `GetSecretValue` on exactly two secret ARNs. |
| Validator → Bedrock | IAM role grants `InvokeModel` on exactly one model id. |
| Orchestrator → MCP server | stdio subprocess. Connection password is passed via env, never on argv. |
| MCP server → Database | `assert_read_only()` rejects any non-SELECT/SHOW/EXPLAIN statement and any multi-statement payload. PostgreSQL session sets `default_transaction_read_only=on`. SQL Server uses `ApplicationIntent=ReadOnly` on the connection plus `pyodbc.connect(readonly=True)`. |
| Agent → MCP | Single point of egress; identifiers in tool arguments are validated by `_safe_ident()` before any string-based SQL is constructed inside the server. |

## Why MCP instead of direct DB calls?

- One uniform tool surface for every agent — easier to audit and easier to extend.
- The MCP server is the trust boundary: even if an agent (or an LLM-driven tool call) tried to issue a write, the server-side guard would reject it.
- The same MCP servers can be plugged into Claude Desktop, Q Developer, or any other MCP-aware client without changing the validator code.
- Sessions are pooled by fingerprint, so two phases targeting the same database share a single stdio process.

## Phases

### 1. Inventory
- Calls `list_objects(schema)` on each side.
- Buckets objects to canonical types: TABLE, VIEW, PROCEDURE, FUNCTION, TRIGGER, INDEX, SEQUENCE.
- Reports counts, missing-in-target, extra-in-target.

### 2. Row counts
- Calls `table_row_counts(schema)` on each side.
- Source server uses `sys.dm_db_partition_stats` for fast counts.
- Target server uses exact `COUNT(*)` per table.

### 3. Functional Testing
- Calls `list_procedures(schema)` and `procedure_parameters(schema, name)` on each side.
- For each pair, calls `sample_table(schema, table, limit=1)` against the target to pull a real value for each input parameter.
- Builds parameterised function invocations and calls `execute_select(sql)` on each side.
- Compares normalised result sets; on diff, asks the LLM for a 1-2-sentence root-cause explanation. Procedures whose body contains DML keywords are skipped.

### 4. Performance Testing
- Reuses the functional agent's metadata + test-case builder so source and target see identical calls.
- Calls `execute_with_timing(sql)` `REPETITIONS` times per side; median is reported.
- Flag is raised when target median > source median + threshold (default 5 ms).
- For flagged rows, the LLM produces a short remediation suggestion (deterministic fallback when the LLM is unavailable).

## Reports

- HTML: self-contained, color-coded, includes a "Print / Save as PDF" button. Designed to be e-mailed.
- JSON: every phase row, suitable for programmatic comparison or storing in S3.

## Extending

To add a new phase:

1. Implement `MyAgent(BaseAgent)` returning a `PhaseResult`. Use `self.source_session` / `self.target_session` and `self.call_json(...)` to talk to MCP.
2. Add it to `PHASE_TITLES` in `orchestrator.py`.
3. Update tests under `tests/`.

To add a new MCP tool, edit the corresponding `mcp_servers/*/server.py` and re-run the validator. Existing agents will continue to work; new agents can opt into the new tool.
