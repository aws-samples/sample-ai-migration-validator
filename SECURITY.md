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
