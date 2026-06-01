"""Phase 1 — Inventory Check.

Compares object inventories (tables, views, procedures, functions, triggers,
indexes, sequences) between source and target via the ``list_objects`` MCP
tool on each side.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..utils import logging as _log
from .base import BaseAgent, PhaseResult

log = _log.get(__name__)

# SQL Server type_desc -> canonical bucket
_SQLSERVER_TYPE_MAP: dict[str, str] = {
    "USER_TABLE": "TABLE",
    "VIEW": "VIEW",
    "SQL_STORED_PROCEDURE": "PROCEDURE",
    "SQL_SCALAR_FUNCTION": "FUNCTION",
    "SQL_INLINE_TABLE_VALUED_FUNCTION": "FUNCTION",
    "SQL_TABLE_VALUED_FUNCTION": "FUNCTION",
    "SQL_TRIGGER": "TRIGGER",
    "SEQUENCE_OBJECT": "SEQUENCE",
    "INDEX": "INDEX",
}

_PG_TYPE_MAP: dict[str, str] = {
    "BASE TABLE": "TABLE",
    "VIEW": "VIEW",
    "PROCEDURE": "PROCEDURE",
    "FUNCTION": "FUNCTION",
    "TRIGGER": "TRIGGER",
    "SEQUENCE": "SEQUENCE",
    "INDEX": "INDEX",
}


class InventoryAgent(BaseAgent):
    """Compare object inventories between source and target."""

    name = "inventory"

    def system_prompt(self) -> str:
        return (
            "You are the Inventory Validation Agent. Compare object inventories "
            "between SQL Server (source) and PostgreSQL (target). Report only the "
            "truth: matches, missing in target, or extra in target."
        )

    # ------------------------------------------------------------------
    def _bucket(self, rows: list[dict[str, Any]], type_map: dict[str, str]) -> dict[str, set[str]]:
        """Group object names by canonical bucket, preserving original case.

        Case is preserved so a target with both ``"Order Details"`` and
        ``"order details"`` shows up as two distinct objects (which they are
        in PostgreSQL). Cross-side comparison happens case-insensitively
        downstream.
        """
        out: dict[str, set[str]] = defaultdict(set)
        for r in rows or []:
            bucket = type_map.get(r.get("type_desc", ""))
            name = r.get("name")
            if bucket and name:
                out[bucket].add(name)
        return out

    # ------------------------------------------------------------------
    def run(self) -> PhaseResult:
        try:
            src_rows = self.call_json(
                self.source_session, "list_objects", {"schema": self.config.source.schema_name}
            )
        except RuntimeError as e:
            src_rows = []
            src_error = str(e)
        else:
            src_error = ""

        try:
            tgt_rows = self.call_json(
                self.target_session, "list_objects", {"schema": self.config.target.schema_name}
            )
        except RuntimeError as e:
            tgt_rows = []
            tgt_error = str(e)
        else:
            tgt_error = ""

        # Verbose record of what the MCP servers actually returned. Goes to
        # the structured logger (and ends up in reports/mcp-<ts>.log when the
        # user runs the validator end-to-end) so debugging is concrete.
        log.info(
            "inventory: source schema=%s rows=%d target schema=%s rows=%d",
            self.config.source.schema_name,
            len(src_rows or []),
            self.config.target.schema_name,
            len(tgt_rows or []),
        )
        log.debug("inventory.source.raw=%s", src_rows)
        log.debug("inventory.target.raw=%s", tgt_rows)

        src = self._bucket(src_rows or [], _SQLSERVER_TYPE_MAP)
        tgt = self._bucket(tgt_rows or [], _PG_TYPE_MAP)

        rows: list[dict[str, Any]] = []
        any_mismatch = False
        # Stable presentation order. We always render all canonical types so the
        # table looks the same shape whether a schema is empty or full.
        all_types = ["TABLE", "VIEW", "PROCEDURE", "FUNCTION", "TRIGGER", "INDEX", "SEQUENCE"]
        # Anything unexpected we discovered also gets appended at the end.
        seen = set(src) | set(tgt)
        all_types += sorted(seen - set(all_types))

        for t in all_types:
            s_set, g_set = src.get(t, set()), tgt.get(t, set())
            # Case-insensitive matching: compare names lowercased, but show
            # original case in details so case-sensitive duplicates are visible.
            s_by_lc: dict[str, list[str]] = {}
            for n in s_set:
                s_by_lc.setdefault(n.lower(), []).append(n)
            g_by_lc: dict[str, list[str]] = {}
            for n in g_set:
                g_by_lc.setdefault(n.lower(), []).append(n)

            missing_lc = sorted(set(s_by_lc) - set(g_by_lc))
            extra_lc = sorted(set(g_by_lc) - set(s_by_lc))
            common_lc = set(s_by_lc) & set(g_by_lc)

            # Within a common bucket, if either side has multiple
            # case-variants (e.g. PG having both ``Order Details`` and
            # ``order details``), call the extras out individually.
            duplicate_notes: list[str] = []
            for lc in common_lc:
                src_variants = sorted(s_by_lc[lc])
                tgt_variants = sorted(g_by_lc[lc])
                if len(tgt_variants) > 1:
                    duplicate_notes.append(
                        f"target has {len(tgt_variants)} case-variants of '{lc}': "
                        f"{', '.join(repr(v) for v in tgt_variants)}"
                    )
                if len(src_variants) > 1:
                    duplicate_notes.append(
                        f"source has {len(src_variants)} case-variants of '{lc}': "
                        f"{', '.join(repr(v) for v in src_variants)}"
                    )

            missing = sorted({orig for lc in missing_lc for orig in s_by_lc[lc]})
            extra = sorted({orig for lc in extra_lc for orig in g_by_lc[lc]})

            if not missing and not extra and not duplicate_notes and len(s_set) == len(g_set):
                status = "match"
                details = ""
            else:
                any_mismatch = True
                status = "mismatch"
                parts: list[str] = []
                if missing:
                    parts.append(f"missing: {', '.join(missing)}")
                if extra:
                    parts.append(f"extra: {', '.join(extra)}")
                if duplicate_notes:
                    parts.append("; ".join(duplicate_notes))
                details = " | ".join(parts)
            rows.append(
                {
                    "object_type": t,
                    "source_count": len(s_set),
                    "target_count": len(g_set),
                    "status": status,
                    "details": details,
                    "_missing": missing,
                    "_extra": extra,
                }
            )

        # If a side returned nothing at all, that's almost certainly a wrong
        # schema name or a permission issue, not a clean migration. Surface it.
        warnings: list[str] = []
        if not src:
            warnings.append(
                f"Source returned no objects for schema '{self.config.source.schema_name}'. "
                "Verify the schema name and that the user has SELECT on INFORMATION_SCHEMA / sys.tables."
                + (f" (server error: {src_error})" if src_error else "")
            )
        if not tgt:
            warnings.append(
                f"Target returned no objects for schema '{self.config.target.schema_name}'. "
                "Verify the schema name and that the user has USAGE on the schema."
                + (f" (server error: {tgt_error})" if tgt_error else "")
            )

        if warnings:
            summary = " | ".join(warnings)
            status = "fail"
        elif any_mismatch:
            summary = "Inventory differences detected. See the Details column."
            status = "warn"
        else:
            summary = "All object inventories match."
            status = "ok"

        return PhaseResult(
            name="Inventory Check",
            summary=summary,
            rows=rows,
            status=status,
            extras={
                "source_schema": self.config.source.schema_name,
                "target_schema": self.config.target.schema_name,
            },
        )
