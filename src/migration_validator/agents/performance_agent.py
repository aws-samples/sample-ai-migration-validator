# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Phase 4 — Performance Testing.

Algorithm
---------
For every procedure that exists in BOTH source and target (and isn't DML):

1. Build **3 test cases** by sampling real values from source base tables —
   one set of arguments per test case. Same arg values are used for source
   AND target so we measure the same logical workload.
2. Execute each test case via the ``time_procedure`` MCP tool on each side.
   That tool returns clock-side elapsed milliseconds for the actual EXEC /
   CALL (procedures roll back any side effects).
3. Aggregate per procedure: median of the 3 timings on each side.
4. Compare. If ``target_ms - source_ms > perf_threshold_ms`` (default 5 ms)
   the row is flagged.
5. For flagged rows, compose a Notes column with:
     * a short rule-based diagnosis ("target slower by X ms (Yx source)")
     * tuning recommendations (LLM-generated when Bedrock is available,
       deterministic fallback otherwise).

Output columns: procedure | sql_server_ms | postgresql_ms | delta_ms |
status | notes
"""

from __future__ import annotations

import json
import statistics
from typing import Any

from ..utils import logging as _log
from .base import BaseAgent, PhaseResult
from .functional_agent import FunctionalTestingAgent

log = _log.get(__name__)


class PerformanceAgent(BaseAgent):
    """Compare median execution times for equivalent procedure calls."""

    name = "performance"
    TEST_CASES_PER_PROC = 3
    TIMING_WARMUP_RUNS = 1  # one warm-up so plan-cache effects don't dominate

    def system_prompt(self) -> str:
        return (
            "You are the Performance Tuning Agent. When the PostgreSQL target is slower "
            "than the SQL Server source, give 1-2 short concrete suggestions: indexes "
            "on join/filter columns, ANALYZE for statistics, increase work_mem for "
            "sort/hash-heavy plans, parallel_workers, or rewriting the SQL. Do not "
            "invent identifiers. Plain text, no markdown."
        )

    # ------------------------------------------------------------------
    def _time_one(self, session, schema: str, name: str, args: list[Any]) -> tuple[float, str]:
        """Call ``time_procedure`` once. Return (ms, error_or_empty)."""
        try:
            payload = self.call_json(
                session,
                "time_procedure",
                {
                    "schema": schema,
                    "name": name,
                    "args_json": json.dumps(args, default=str),
                },
            )
        except RuntimeError as e:
            return -1.0, str(e)[:200]
        if not isinstance(payload, dict) or "elapsed_ms" not in payload:
            return -1.0, f"unexpected response: {payload!r}"
        try:
            return float(payload["elapsed_ms"]), ""
        except (TypeError, ValueError):
            return -1.0, "non-numeric elapsed_ms"

    # ------------------------------------------------------------------
    def _build_test_cases(self, ft: FunctionalTestingAgent, s_meta) -> list[list[Any]]:
        """Return up to TEST_CASES_PER_PROC distinct argument lists.

        The functional agent's ``_sample_value`` already maps a parameter name
        to a real value via ``find_columns`` + ``sample_column``. We call it
        TEST_CASES_PER_PROC times: ``sample_column`` returns up to 5 distinct
        values per column so the agent will rotate through them naturally if
        we keep calling.

        For procedures with no input parameters we still produce a single
        empty-args test case.
        """
        base_tables = ft._referenced_tables(s_meta.definition)
        input_params = [p for p in s_meta.parameters if not p.get("is_output")]
        if not input_params:
            return [[]]

        cases: list[list[Any]] = []
        seen: set[tuple] = set()
        for _ in range(self.TEST_CASES_PER_PROC * 3):  # try a few times to get distinct values
            args = [
                ft._sample_value(
                    p.get("parameter_name", "") or "",
                    p.get("data_type", "") or "",
                    base_tables,
                )
                for p in input_params[:4]
            ]
            key = tuple(args)
            if key in seen:
                continue
            seen.add(key)
            cases.append(args)
            if len(cases) >= self.TEST_CASES_PER_PROC:
                break

        # If we couldn't get N distinct values (e.g. small sample column) we
        # still pad to N by repeating the first one — better than skipping.
        while cases and len(cases) < self.TEST_CASES_PER_PROC:
            cases.append(list(cases[0]))
        return cases or [[]]

    # ------------------------------------------------------------------
    def _diagnosis(self, name: str, src_ms: float, tgt_ms: float) -> str:
        """Short factual statement of WHAT the difference is."""
        delta = tgt_ms - src_ms
        if src_ms <= 0:
            return f"Target took {tgt_ms:.2f} ms; source timing unavailable."
        ratio = tgt_ms / src_ms if src_ms else 0.0
        return (
            f"Target slower by {delta:+.2f} ms ({ratio:.2f}x source) over "
            f"{self.TEST_CASES_PER_PROC} test cases."
        )

    def _recommendation(self, name: str, src_ms: float, tgt_ms: float) -> str:
        """Tuning suggestion. Uses LLM when available; rule-based otherwise."""
        prompt = (
            f"Procedure {name}: SQL Server median {src_ms:.2f} ms, "
            f"PostgreSQL median {tgt_ms:.2f} ms over {self.TEST_CASES_PER_PROC} test cases. "
            "Recommend at most 2 concrete PostgreSQL tuning steps. Plain text, no markdown."
        )
        fallback = (
            "Run EXPLAIN (ANALYZE, BUFFERS) for the procedure body. Check that "
            "indexes exist on join and filter columns referenced by the proc. "
            "Run ANALYZE on the affected tables to refresh statistics. For "
            "sort or hash-heavy plans, consider raising work_mem at the session "
            "or role level."
        )
        return self.llm_analyze(prompt, fallback=fallback)

    # ------------------------------------------------------------------
    def run(self) -> PhaseResult:
        ft = FunctionalTestingAgent(self.config, self._sessions)
        src_procs = ft._list_procs(self.source_session, self.config.source.schema_name)
        tgt_procs = ft._list_procs(self.target_session, self.config.target.schema_name)
        common = sorted(set(src_procs) & set(tgt_procs))

        threshold = self.config.perf_threshold_ms
        rows: list[dict[str, Any]] = []
        flagged = 0

        for proc_key in common:
            s_meta = src_procs[proc_key]
            t_meta = tgt_procs[proc_key]

            cases = self._build_test_cases(ft, s_meta)
            if not cases:
                continue

            src_timings: list[float] = []
            tgt_timings: list[float] = []
            errors: list[str] = []

            # Warm-up run on each side, discarded.
            for _ in range(self.TIMING_WARMUP_RUNS):
                self._time_one(self.source_session, self.config.source.schema_name, s_meta.name, cases[0])
                self._time_one(self.target_session, self.config.target.schema_name, t_meta.name, cases[0])

            for args in cases:
                s_ms, s_err = self._time_one(
                    self.source_session, self.config.source.schema_name, s_meta.name, args
                )
                t_ms, t_err = self._time_one(
                    self.target_session, self.config.target.schema_name, t_meta.name, args
                )
                if s_ms >= 0:
                    src_timings.append(s_ms)
                if t_ms >= 0:
                    tgt_timings.append(t_ms)
                if s_err:
                    errors.append(f"source: {s_err}")
                if t_err:
                    errors.append(f"target: {t_err}")

            if not src_timings and not tgt_timings:
                rows.append(
                    {
                        "procedure": s_meta.name,
                        "sql_server_ms": None,
                        "postgresql_ms": None,
                        "delta_ms": None,
                        "status": "error",
                        "notes": "; ".join(errors) or "no timing produced",
                    }
                )
                flagged += 1
                continue

            src_med = round(statistics.median(src_timings), 3) if src_timings else -1.0
            tgt_med = round(statistics.median(tgt_timings), 3) if tgt_timings else -1.0

            if src_med < 0 or tgt_med < 0:
                status = "error"
                notes = "; ".join(errors) or "partial timings only"
                flagged += 1
            else:
                delta = round(tgt_med - src_med, 3)
                if delta > threshold:
                    status = "flagged"
                    flagged += 1
                    diagnosis = self._diagnosis(s_meta.name, src_med, tgt_med)
                    rec = self._recommendation(s_meta.name, src_med, tgt_med)
                    notes = f"{diagnosis} {rec}"
                else:
                    status = "match"
                    notes = (
                        f"Within threshold (delta {delta:+.2f} ms ≤ {threshold} ms). "
                        f"{len(src_timings)} timed runs each side."
                    )

            rows.append(
                {
                    "procedure": s_meta.name,
                    "sql_server_ms": src_med if src_med >= 0 else None,
                    "postgresql_ms": tgt_med if tgt_med >= 0 else None,
                    "delta_ms": (None if (src_med < 0 or tgt_med < 0) else round(tgt_med - src_med, 3)),
                    "status": status,
                    "notes": notes,
                }
            )

        if not rows:
            summary = "No comparable procedures executed for performance testing."
            phase_status = "ok"
        elif flagged == 0:
            summary = f"All {len(rows)} procedures performed within {threshold} ms threshold."
            phase_status = "ok"
        else:
            summary = (
                f"{flagged} of {len(rows)} procedure(s) exceeded the {threshold} ms threshold "
                f"on PostgreSQL or failed to time."
            )
            phase_status = "warn"

        return PhaseResult(
            name="Performance Testing",
            summary=summary,
            rows=rows,
            status=phase_status,
            extras={
                "threshold_ms": threshold,
                "test_cases_per_proc": self.TEST_CASES_PER_PROC,
            },
        )
