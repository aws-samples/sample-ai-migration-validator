# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Machine-readable JSON dump of every phase."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ..agents.base import PhaseResult
from ..config import ValidatorConfig
from .redaction import redact_phase_rows


def write_json(path: Path, cfg: ValidatorConfig, results: Sequence[PhaseResult]) -> None:
    phases: list[dict] = []
    for r in results:
        d = asdict(r)
        if cfg.redact_pii:
            d["rows"] = redact_phase_rows(d.get("rows") or [])
        phases.append(d)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "redaction_applied": cfg.redact_pii,
        "config": {
            "source": cfg.source.safe_dict(),
            "target": cfg.target.safe_dict(),
            "mode": cfg.mode,
            "bedrock_model": cfg.bedrock_model,
            "bedrock_guardrail_id": cfg.bedrock_guardrail_id,
            "region": cfg.region,
            "perf_threshold_ms": cfg.perf_threshold_ms,
            "sample_size": cfg.sample_size,
            "allow_write_tests": cfg.allow_write_tests,
        },
        "phases": phases,
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
