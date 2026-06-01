"""Phase 2 — Row-Count Reconciliation.

Pulls per-table row counts from each side using the ``table_row_counts`` MCP
tool and reports per-table deltas.
"""

from __future__ import annotations

from typing import Any

from ..utils import logging as _log
from .base import BaseAgent, PhaseResult

log = _log.get(__name__)


class RowCountAgent(BaseAgent):
    """Compare per-table COUNT(*) between source and target."""

    name = "row_counts"

    def system_prompt(self) -> str:
        return (
            "You are the Row-Count Reconciliation Agent. Compare per-table counts "
            "between SQL Server and PostgreSQL and report deltas. Never speculate."
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _to_dict(rows: list[dict[str, Any]] | None) -> dict[str, int]:
        """Build a name -> row_count map preserving case sensitivity.

        We deliberately do NOT lower-case keys: PostgreSQL treats
        ``"Order Details"`` and ``"order details"`` as two different tables,
        and a tool that silently merges them would mask data-migration bugs.
        Compare uses case-insensitive matching at the row level instead.
        """
        out: dict[str, int] = {}
        for r in rows or []:
            name = r.get("table_name")
            if not name:
                continue
            out[name] = int(r.get("row_count") or 0)
        return out

    # ------------------------------------------------------------------
    def run(self) -> PhaseResult:
        src_raw = self.call_json(
            self.source_session,
            "table_row_counts",
            {"schema": self.config.source.schema_name},
        )
        tgt_raw = self.call_json(
            self.target_session,
            "table_row_counts",
            {"schema": self.config.target.schema_name},
        )
        log.info(
            "row_counts: source schema=%s tables=%d target schema=%s tables=%d",
            self.config.source.schema_name,
            len(src_raw or []),
            self.config.target.schema_name,
            len(tgt_raw or []),
        )
        log.debug("row_counts.source.raw=%s", src_raw)
        log.debug("row_counts.target.raw=%s", tgt_raw)

        src = self._to_dict(src_raw)
        tgt = self._to_dict(tgt_raw)

        # Case-insensitive matching: PostgreSQL distinguishes ``"Order Details"``
        # from ``"order details"``, so we keep both as separate rows but pair
        # them with whichever case-equivalent name exists on the other side.
        # If a side has duplicates that collapse on lower-case (rare but does
        # happen after AWS DMS conversion), every variant is shown explicitly.

        def _by_lower(d: dict[str, int]) -> dict[str, list[tuple[str, int]]]:
            out: dict[str, list[tuple[str, int]]] = {}
            for k, v in d.items():
                out.setdefault(k.lower(), []).append((k, v))
            return out

        src_lc = _by_lower(src)
        tgt_lc = _by_lower(tgt)
        all_lc = sorted(set(src_lc) | set(tgt_lc))

        rows: list[dict[str, Any]] = []
        mismatches = 0
        for lc in all_lc:
            src_variants = src_lc.get(lc, [])
            tgt_variants = tgt_lc.get(lc, [])
            # Pair them up by exact case first; leftovers pair by position.
            paired: list[tuple[tuple[str, int] | None, tuple[str, int] | None]] = []
            src_remaining = list(src_variants)
            tgt_remaining = list(tgt_variants)
            for sv in list(src_remaining):
                for tv in list(tgt_remaining):
                    if sv[0] == tv[0]:
                        paired.append((sv, tv))
                        src_remaining.remove(sv)
                        tgt_remaining.remove(tv)
                        break
            for sv in src_remaining:
                tv = tgt_remaining.pop(0) if tgt_remaining else None
                paired.append((sv, tv))
            for tv in tgt_remaining:
                paired.append((None, tv))

            for sv, tv in paired:
                s = sv[1] if sv else None
                g = tv[1] if tv else None
                src_name = sv[0] if sv else None
                tgt_name = tv[0] if tv else None
                if s is None:
                    status, delta = "extra_in_target", g
                elif g is None:
                    status, delta = "missing_in_target", -(s or 0)
                elif s == g:
                    status, delta = "match", 0
                else:
                    status, delta = "mismatch", (g - s)
                if status != "match":
                    mismatches += 1

                # When source and target names differ in case (or one side has
                # duplicate variants), surface the actual names so the user can
                # see what we matched.
                display_name = src_name or tgt_name or lc
                if src_name and tgt_name and src_name != tgt_name:
                    display_name = f"{src_name} -> {tgt_name}"
                elif src_name and tgt_name is None:
                    display_name = src_name
                elif tgt_name and src_name is None:
                    display_name = tgt_name

                rows.append(
                    {
                        "table_name": display_name,
                        "source_count": s if s is not None else 0,
                        "target_count": g if g is not None else 0,
                        "delta": delta,
                        "status": status,
                    }
                )

        if not rows:
            summary = (
                "No tables found in source or target schema. Check that the schema names "
                f"are correct (source='{self.config.source.schema_name}', "
                f"target='{self.config.target.schema_name}')."
            )
            status = "warn"
        elif mismatches == 0:
            summary = f"All {len(rows)} table row counts match between source and target."
            status = "ok"
        else:
            summary = f"{mismatches} of {len(rows)} table(s) have row-count differences."
            status = "fail"

        return PhaseResult(
            name="Row-Count Reconciliation",
            summary=summary,
            rows=rows,
            status=status,
        )
