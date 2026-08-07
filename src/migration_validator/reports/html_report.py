# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Self-contained HTML report renderer (Jinja2 template)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..agents.base import PhaseResult
from ..config import ValidatorConfig
from .redaction import redact_phase_rows

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def render_html(path: Path, cfg: ValidatorConfig, results: Sequence[PhaseResult]) -> None:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("report.html.j2")
    json_filename = path.with_suffix(".json").name

    phases: list[dict] = []
    for r in results:
        d = asdict(r)
        if cfg.redact_pii:
            d["rows"] = redact_phase_rows(d.get("rows") or [])
        phases.append(d)

    html = template.render(
        generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        json_filename=json_filename,
        redaction_applied=cfg.redact_pii,
        config={
            "bedrock_model": cfg.bedrock_model,
            "bedrock_guardrail_id": cfg.bedrock_guardrail_id,
            "source": cfg.source.safe_dict(),
            "target": cfg.target.safe_dict(),
        },
        phases=phases,
    )
    path.write_text(html, encoding="utf-8")
