"""Smoke tests for the HTML and JSON report renderers."""

from __future__ import annotations

import json
from pathlib import Path

from migration_validator.agents.base import PhaseResult
from migration_validator.config import ConnectionDetails, ValidatorConfig
from migration_validator.reports.html_report import render_html
from migration_validator.reports.json_report import write_json


def _cfg(tmp_path: Path) -> ValidatorConfig:
    return ValidatorConfig(
        source=ConnectionDetails(
            engine="sqlserver",
            host="h",
            port=1433,
            database="d",
            username="u",
            password="src-secret-marker-9z2",
        ),
        target=ConnectionDetails(
            engine="postgresql",
            host="h2",
            port=5432,
            database="d",
            username="u",
            password="tgt-secret-marker-9z2",
        ),
        report_dir=tmp_path,
    )


def _phases() -> list[PhaseResult]:
    return [
        PhaseResult(
            name="Inventory Check",
            summary="2 differences found",
            status="warn",
            rows=[
                {
                    "object_type": "TABLE",
                    "source_count": 5,
                    "target_count": 4,
                    "status": "mismatch",
                    "details": "missing in target: customers",
                    "_missing": ["customers"],
                    "_extra": [],
                },
            ],
        ),
        PhaseResult(name="Row-Count Reconciliation", summary="OK", status="ok", rows=[]),
    ]


def test_json_report_writes_redacted(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    out = tmp_path / "r.json"
    write_json(out, cfg, _phases())
    payload = json.loads(out.read_text())
    assert payload["config"]["source"]["password"] == "***"
    assert payload["config"]["target"]["password"] == "***"
    assert payload["phases"][0]["name"] == "Inventory Check"


def test_html_report_renders_status_badges(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    out = tmp_path / "r.html"
    render_html(out, cfg, _phases())
    html = out.read_text()
    assert "Migration Validation Report" in html
    assert "Inventory Check" in html
    assert "WARN" in html  # status badge upper-cased
    # Critical: the actual password values must not leak into the rendered HTML.
    assert "src-secret-marker-9z2" not in html
    assert "tgt-secret-marker-9z2" not in html
