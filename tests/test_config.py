# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Configuration loading tests (no AWS / no DB required)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from migration_validator.config import (
    ConnectionDetails,
    build_config,
    load_from_env,
    load_from_yaml,
)


def test_safe_dict_redacts_password() -> None:
    c = ConnectionDetails(
        engine="sqlserver",
        host="h",
        port=1433,
        database="d",
        username="u",
        password="super-secret",
    )
    assert c.safe_dict()["password"] == "***"
    # And the secret is still retrievable when needed.
    assert c.password.get_secret_value() == "super-secret"


def test_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_HOST", "h")
    monkeypatch.setenv("SOURCE_PORT", "1433")
    monkeypatch.setenv("SOURCE_DATABASE", "d")
    monkeypatch.setenv("SOURCE_USERNAME", "u")
    monkeypatch.setenv("SOURCE_PASSWORD", "p")
    data = load_from_env("SOURCE_")
    assert data["port"] == 1433
    assert data["host"] == "h"
    assert data["password"] == "p"


def test_load_yaml_expands_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO_HOST", "from-env")
    p = tmp_path / "c.yaml"
    p.write_text("source:\n  host: ${FOO_HOST}\n")
    out = load_from_yaml(p)
    assert out["source"]["host"] == "from-env"


def test_build_config_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        """
mode: auto
source:
  engine: sqlserver
  host: a
  port: 1433
  database: d
  username: u
  password: p
target:
  engine: postgresql
  host: b
  port: 5432
  database: d
  username: u
  password: p
"""
    )
    cfg = build_config(
        config_path=p,
        source_secret=None,
        target_secret=None,
        mode="auto",
        region="us-east-1",
        bedrock_model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        bedrock_guardrail_id=None,
        bedrock_guardrail_version="DRAFT",
        report_dir=tmp_path / "reports",
        perf_threshold_ms=5.0,
        sample_size=100,
        allow_insecure=False,
        redact_pii=True,
        source_schema=None,
        target_schema=None,
        interactive_fallback=False,
    )
    assert cfg.mode == "auto"
    assert cfg.source.host == "a"
    assert cfg.target.engine == "postgresql"
    assert cfg.target.encrypt is True


def test_invalid_port_raises() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConnectionDetails(
            engine="sqlserver",
            host="h",
            port=99999,
            database="d",
            username="u",
            password="p",
        )


def test_safe_dict_is_json_serialisable() -> None:
    distinctive_password = "x9z2q-secret-marker"
    c = ConnectionDetails(
        engine="postgresql",
        host="h",
        port=5432,
        database="d",
        username="u",
        password=distinctive_password,
    )
    # Important: the redacted dict goes into HTML/JSON reports.
    rendered = json.dumps(c.safe_dict())
    assert "***" in rendered
    assert distinctive_password not in rendered
