# Changelog

All notable changes to this project will be documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-27

### Added
- Initial release of the SQL Server → PostgreSQL Migration Validator.
- Four-phase agentic workflow: Inventory, Row Counts, Functional Testing, Performance Testing.
- Strands Agents + Amazon Bedrock orchestration.
- FastMCP servers for SQL Server and PostgreSQL (read-only).
- Interactive and Auto run modes.
- HTML and JSON report generation.
- CloudFormation template for least-privilege IAM and Secrets Manager.
- Dockerfile for containerized execution.
- GitHub Actions CI with `ruff`, `bandit`, `pip-audit`, and `pytest`.
