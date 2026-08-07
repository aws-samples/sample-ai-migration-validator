# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Configuration loading and validation.

Resolution order: CLI > YAML config > Secrets Manager > env vars > interactive prompt.
Passwords are never logged; ``__repr__`` masks them.
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Literal

import boto3
import yaml
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, SecretStr, field_validator


class ConnectionDetails(BaseModel):
    """A single database connection."""

    engine: Literal["sqlserver", "postgresql"]
    host: str
    port: int
    database: str
    username: str
    password: SecretStr
    schema_name: str = Field(default="dbo")
    encrypt: bool = True

    @field_validator("port")
    @classmethod
    def _port_range(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return v

    def safe_dict(self) -> dict:
        """Return a copy with the password redacted, safe to log."""
        d = self.model_dump()
        d["password"] = "***"  # noqa: S105  # nosec B105 - redaction placeholder, not a real password
        return d


class ValidatorConfig(BaseModel):
    """Top-level configuration."""

    source: ConnectionDetails
    target: ConnectionDetails
    mode: Literal["interactive", "auto"] = "interactive"
    mode_was_explicit: bool = False
    bedrock_model: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    bedrock_guardrail_id: str | None = None
    bedrock_guardrail_version: str = "DRAFT"
    region: str = "us-east-1"
    report_dir: Path = Field(default=Path("./reports"))
    perf_threshold_ms: float = 5.0
    sample_size: int = 100
    allow_insecure: bool = False
    redact_pii: bool = True


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _expand_env(value: object) -> object:
    """Recursively expand ``${VAR}`` placeholders in YAML values."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_from_yaml(path: Path) -> dict:
    """Load a YAML file and expand env vars. Raises if the file is missing."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Top-level YAML must be a mapping in {path}")
    return _expand_env(raw)  # type: ignore[return-value]


def load_from_secrets_manager(secret_id: str, region: str) -> dict:
    """Load a JSON secret from AWS Secrets Manager.

    The secret value must be JSON with keys: host, port, database, username, password.
    """
    client = boto3.client("secretsmanager", region_name=region)
    try:
        resp = client.get_secret_value(SecretId=secret_id)
    except ClientError as e:
        raise RuntimeError(f"Could not retrieve secret '{secret_id}': {e}") from e
    payload = resp.get("SecretString")
    if not payload:
        raise RuntimeError(f"Secret '{secret_id}' has no SecretString")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Secret '{secret_id}' is not valid JSON") from e


def load_from_env(prefix: str) -> dict:
    """Build a connection dict from env vars with the given prefix (e.g. SOURCE_)."""
    keys = ("HOST", "PORT", "DATABASE", "USERNAME", "PASSWORD", "SCHEMA")
    out: dict = {}
    for k in keys:
        val = os.environ.get(f"{prefix}{k}")
        if val is not None:
            field = "schema_name" if k == "SCHEMA" else k.lower()
            out[field] = int(val) if k == "PORT" else val
    return out


def prompt_interactively(role: Literal["source", "target"]) -> dict:
    """Ask the user for connection details. Passwords are never echoed."""
    engine = "sqlserver" if role == "source" else "postgresql"
    default_port = 1433 if engine == "sqlserver" else 5432
    default_schema = "dbo" if engine == "sqlserver" else "public"

    print(f"\n--- {role.upper()} ({engine}) connection ---")
    host = input("Host: ").strip()
    port_in = input(f"Port [{default_port}]: ").strip()
    port = int(port_in) if port_in else default_port
    database = input("Database: ").strip()
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")
    schema_in = input(f"Schema [{default_schema}]: ").strip()
    schema = schema_in if schema_in else default_schema

    return {
        "engine": engine,
        "host": host,
        "port": port,
        "database": database,
        "username": username,
        "password": password,
        "schema_name": schema,
    }


def build_config(
    *,
    config_path: Path | None,
    source_secret: str | None,
    target_secret: str | None,
    mode: str | None,
    region: str,
    bedrock_model: str,
    bedrock_guardrail_id: str | None,
    bedrock_guardrail_version: str,
    report_dir: Path,
    perf_threshold_ms: float,
    sample_size: int,
    allow_insecure: bool,
    redact_pii: bool,
    source_schema: str | None,
    target_schema: str | None,
    interactive_fallback: bool = True,
) -> ValidatorConfig:
    """Build a ``ValidatorConfig`` by merging all configuration sources.

    ``mode`` is ``None`` when the user did not pass ``--mode`` on the CLI; in
    that case the orchestrator will prompt for it after MCP setup.
    """
    yaml_data: dict = {}
    if config_path is not None:
        yaml_data = load_from_yaml(config_path)

    def resolve(role: Literal["source", "target"], secret_id: str | None) -> dict:
        # 1) YAML overrides everything except CLI
        if role in yaml_data:
            return yaml_data[role]
        # 2) Secrets Manager
        if secret_id:
            data = load_from_secrets_manager(secret_id, region)
            data.setdefault("engine", "sqlserver" if role == "source" else "postgresql")
            return data
        # 3) Env vars
        env_data = load_from_env(prefix=f"{role.upper()}_")
        if env_data and {"host", "database", "username", "password"} <= env_data.keys():
            env_data.setdefault("engine", "sqlserver" if role == "source" else "postgresql")
            return env_data
        # 4) Interactive
        if not interactive_fallback:
            raise RuntimeError(f"No connection details provided for {role}")
        return prompt_interactively(role)

    src = resolve("source", source_secret)
    tgt = resolve("target", target_secret)

    if source_schema:
        src["schema_name"] = source_schema
    if target_schema:
        tgt["schema_name"] = target_schema

    src.setdefault("encrypt", not allow_insecure)
    tgt.setdefault("encrypt", not allow_insecure)

    return ValidatorConfig(
        source=ConnectionDetails(**src),
        target=ConnectionDetails(**tgt),
        mode=mode or "interactive",
        mode_was_explicit=mode is not None,
        bedrock_model=bedrock_model,
        bedrock_guardrail_id=bedrock_guardrail_id,
        bedrock_guardrail_version=bedrock_guardrail_version,
        region=region,
        report_dir=report_dir,
        perf_threshold_ms=perf_threshold_ms,
        sample_size=sample_size,
        allow_insecure=allow_insecure,
        redact_pii=redact_pii,
    )
