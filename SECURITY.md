# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, we ask that you notify AWS Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/) or directly via email to aws-security@amazon.com. **Please do not create a public GitHub issue.**

## Security Posture of this Sample

- Database connections require TLS by default.
- The bundled MCP servers are **read-only**: they reject any statement that is not `SELECT`, `SHOW`, `EXPLAIN`, or a metadata query against `INFORMATION_SCHEMA` / `pg_catalog` / `sys.*`.
- Credentials are loaded from AWS Secrets Manager, environment variables, or interactive prompt — never logged.
- The included CloudFormation template provisions least-privilege IAM (read-only Secrets Manager access for two named secrets and `bedrock:InvokeModel` only).
- Dependencies are pinned and scanned with `pip-audit` and `bandit` in CI.

## Responsible use

This is a **sample**. Before running it against production data:

1. Review the code.
2. Use credentials with **read-only** privileges on both source and target.
3. Run from a host inside your VPC (or via VPN / Direct Connect), not over the public internet.
4. Treat all generated reports as containing potentially sensitive schema metadata.

## Query Security

The MCP servers construct SQL queries using two distinct patterns, depending on the position in the query:

### Value positions — parameterized queries

All value comparisons in `WHERE` clauses use the database driver's native parameterized query mechanism (`%s` placeholders with a params tuple). This prevents SQL injection regardless of user-supplied input:

```python
sql = "SELECT ... WHERE s.name = %s AND o.name = %s"
rows = _query(sql, (schema, name))
```

### Identifier positions — validated + quoted interpolation

SQL does not support parameterized identifiers (table names, schema names in `FROM` / `JOIN` / `EXEC`). These positions use f-string interpolation but are guarded by:

1. **`_valid_ident(s)`** — rejects empty strings, NUL characters, bracket/quote characters that could escape quoting, and names exceeding the database's length limit (128 for SQL Server, 63 for PostgreSQL).
2. **`_q_ident(s)`** — wraps the validated identifier in the engine's quoting mechanism (`[name]` for SQL Server, `"name"` for PostgreSQL).

Together, these ensure that identifier positions cannot be exploited for injection.

### Schema allow-list (`execute_select`)

When the validator spawns an MCP server, it passes the configured schema name via `SQLSERVER_ALLOWED_SCHEMA` (SQL Server) or `PG_ALLOWED_SCHEMA` (PostgreSQL). If set, the `execute_select` tool parses the incoming SQL for `FROM` / `JOIN` table references and rejects any query that references a schema outside the allowed one.

This is **defense-in-depth** — not the primary access control. The primary control is the database user's grants:

- **SQL Server**: grant only `db_datareader` (or `SELECT ON SCHEMA::your_schema`).
- **PostgreSQL**: grant only `SELECT ON ALL TABLES IN SCHEMA your_schema`.

When the MCP server is used standalone (without the validator), the env var is unset and the check is skipped.
