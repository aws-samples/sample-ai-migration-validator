"""Phase 3 — Functional Testing.

Algorithm
---------
1. List procedures and functions on both sides via the ``list_procedures`` MCP
   tool.
2. Diff names → report missing / extra.
3. For every procedure that exists in BOTH:
   a. Pull parameter metadata via ``procedure_parameters``.
   b. For each input parameter, sample a real value from a base table the
      source definition references via ``sample_table`` (this is "test case
      using actual data in the base table").
   c. Build read-only SQL invocations for both engines (functions only —
      procedures whose body contains DML are skipped under our read-only
      contract).
   d. Execute both via ``execute_select`` and diff normalised payloads.
   e. If they differ, ask the LLM for a 1-2 sentence root-cause explanation.
4. Returns one row per (procedure x test case).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..mcp_session import MCPSession
from .base import BaseAgent, PhaseResult

_DML_IN_DEF = re.compile(
    # Match real DML keywords only. Note: we deliberately do NOT match CREATE,
    # ALTER, DROP, GRANT, REVOKE, or EXEC because those appear in every
    # procedure header (``CREATE PROCEDURE …``) or as legitimate sub-call
    # patterns. Real writes are INSERT/UPDATE/DELETE/MERGE/TRUNCATE; if the
    # body has any of these we skip the procedure under the read-only contract.
    r"\b(INSERT\s+INTO|UPDATE\s+\S|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE\s+TABLE)\b",
    re.IGNORECASE,
)


def _strip_proc_header(definition: str) -> str:
    """Return the body of a CREATE PROC[EDURE]/FUNCTION block.

    The header (``CREATE PROCEDURE [s].[n] (...) AS``) sometimes contains
    keywords we do not want our DML sniffer to see. We split on the first
    ``\\bAS\\b`` outside parentheses and return whatever comes after it; if
    we can't find a header marker we return the full definition.
    """
    if not definition:
        return ""
    depth = 0
    i = 0
    while i < len(definition):
        c = definition[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and c.upper() == "A" and i + 1 < len(definition):
            # Look for a standalone " AS " token at depth 0.
            window = definition[i : i + 4].upper()
            before_ok = i == 0 or not definition[i - 1].isalnum()
            after_ok = i + 2 == len(definition) or not definition[i + 2].isalnum()
            if window.startswith("AS") and before_ok and after_ok:
                return definition[i + 2 :]
        i += 1
    return definition


@dataclass
class _ProcMeta:
    name: str
    type_desc: str
    parameters: list[dict[str, Any]]
    definition: str


class FunctionalTestingAgent(BaseAgent):
    """Diff procedures/functions and run sampled test cases on both sides."""

    name = "functional"

    def system_prompt(self) -> str:
        return (
            "You are the Functional Testing Agent. You compare results of equivalent "
            "stored procedures between SQL Server and PostgreSQL. When results differ "
            "you explain the most likely root cause in <=2 short sentences (data type "
            "differences, NULL ordering, collation, function semantics, etc.). Never "
            "fabricate; if the cause is unclear, say so."
        )

    # ------------------------------------------------------------------
    # Metadata via MCP
    # ------------------------------------------------------------------
    def _list_procs(self, session: MCPSession, schema: str) -> dict[str, _ProcMeta]:
        rows = self.call_json(session, "list_procedures", {"schema": schema}) or []
        out: dict[str, _ProcMeta] = {}
        for r in rows:
            name = r.get("name")
            if not name:
                continue
            params = self.call_json(session, "procedure_parameters", {"schema": schema, "name": name}) or []
            out[name.lower()] = _ProcMeta(
                name=name,
                type_desc=r.get("type_desc") or "",
                parameters=params,
                definition=r.get("definition") or "",
            )
        return out

    # ------------------------------------------------------------------
    # Test-case generation
    # ------------------------------------------------------------------
    @staticmethod
    def _referenced_tables(definition: str) -> list[str]:
        """Find table identifiers the procedure body references.

        Supports four forms:
            * unquoted:        ``FROM Customers``
            * sql-quoted:      ``FROM [Order Details]``
            * pg double-quote: ``FROM "Order Details"``
            * schema-qualified: ``FROM dbo.[Order Details]`` / ``"public"."Order Details"``

        Returns just the table portion (right-hand side of the dot) when
        qualified.
        """
        if not definition:
            return []
        # Match each RHS form separately and combine.
        patterns = [
            # [Bracket Quoted]
            r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(?:\[[^\]]+\]|\w+)\.\[([^\]]+)\]",
            r"\b(?:FROM|JOIN|UPDATE|INTO)\s+\[([^\]]+)\]",
            # "Double Quoted"
            r'\b(?:FROM|JOIN|UPDATE|INTO)\s+(?:"[^"]+"|\w+)\."([^"]+)"',
            r'\b(?:FROM|JOIN|UPDATE|INTO)\s+"([^"]+)"',
            # Unquoted (qualified or bare)
            r"\b(?:FROM|JOIN|UPDATE|INTO)\s+\w+\.(\w+)",
            r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(\w+)",
        ]
        names: list[str] = []
        for pat in patterns:
            names.extend(re.findall(pat, definition, flags=re.IGNORECASE))
        seen: set[str] = set()
        unique: list[str] = []
        for n in names:
            ln = n.lower()
            if ln not in seen:
                seen.add(ln)
                unique.append(n)
        return unique

    def _sample_value(self, param_name: str, declared_type: str, base_tables: list[str]) -> Any:
        """Find a real value for a procedure parameter.

        Strategy in order of preference:

        1. **Same-named column** — look in source for a base-table column whose
           name matches the parameter (e.g. ``@CustomerID`` -> ``CustomerID``)
           and pull a distinct non-null value. This is what gives realistic
           test cases against actual data.
        2. **Tables-the-proc-references** — if (1) finds nothing, fall back to
           sampling any compatible-typed column from the tables the procedure
           body mentions.
        3. **Type-based default** — last resort, return a typed dummy value.
        """
        clean = param_name.lstrip("@").strip()
        if clean:
            try:
                hits = (
                    self.call_json(
                        self.source_session,
                        "find_columns",
                        {"schema": self.config.source.schema_name, "column_name": clean},
                    )
                    or []
                )
            except RuntimeError:
                hits = []
            for hit in hits:
                table = hit.get("table_name")
                column = hit.get("column_name")
                if not table or not column:
                    continue
                try:
                    samples = (
                        self.call_json(
                            self.source_session,
                            "sample_column",
                            {
                                "schema": self.config.source.schema_name,
                                "table": table,
                                "column": column,
                                "limit": 5,
                            },
                        )
                        or []
                    )
                except RuntimeError:
                    continue
                for s in samples:
                    val = s.get("v")
                    if val is None:
                        continue
                    # SQL Server CHAR(N) pads with trailing spaces. Strip them
                    # so the value matches PostgreSQL's varchar/text without
                    # padding (otherwise CALL proc('ALFKI ') finds nothing).
                    if isinstance(val, str):
                        val = val.rstrip()
                    return val

        for table in base_tables:
            try:
                payload = self.call_json(
                    self.source_session,
                    "sample_table",
                    {
                        "schema": self.config.source.schema_name,
                        "table": table,
                        "limit": 1,
                    },
                )
            except RuntimeError:
                continue
            if not payload:
                continue
            row = payload[0] if isinstance(payload, list) else None
            if not row:
                continue
            for val in row.values():
                if val is None:
                    continue
                if self._dtype_compatible(declared_type, val):
                    return val
        return self._default_for(declared_type)

    @staticmethod
    def _dtype_compatible(declared: str, val: Any) -> bool:
        d = (declared or "").lower()
        if any(k in d for k in ("int", "numeric", "decimal", "float", "real", "money")):
            return isinstance(val, (int, float))
        if any(k in d for k in ("char", "text", "uuid")):
            return isinstance(val, str)
        if "date" in d or "time" in d:
            return True
        if "bool" in d or "bit" in d:
            return isinstance(val, (bool, int))
        return True

    @staticmethod
    def _default_for(declared: str) -> Any:
        d = (declared or "").lower()
        if any(k in d for k in ("int", "numeric", "decimal", "float", "real", "money")):
            return 1
        if "bool" in d or "bit" in d:
            return False
        return ""

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------
    @staticmethod
    def _quote_literal(val: Any) -> str:
        if val is None:
            return "NULL"
        if isinstance(val, bool):
            return "1" if val else "0"
        if isinstance(val, (int, float)):
            return str(val)
        return "'" + str(val).replace("'", "''") + "'"

    @staticmethod
    def _format_call_for_display(schema: str, name: str, args: list[Any], engine: str) -> str:
        """Produce a readable invocation string for the report."""

        def _fmt(v: Any) -> str:
            if v is None:
                return "NULL"
            if isinstance(v, bool):
                return "1" if v else "0"
            if isinstance(v, (int, float)):
                return str(v)
            return "'" + str(v).replace("'", "''") + "'"

        argsql = ", ".join(_fmt(a) for a in args)
        if engine == "sqlserver":
            return f"EXEC [{schema}].[{name}] {argsql}".rstrip()
        return f'CALL "{schema}"."{name}"({argsql})'

    def _build_source_sql(self, meta: _ProcMeta, args: list[Any]) -> str:
        # Kept for backward-compat with the performance agent which times raw
        # SELECTs. For functions we can still time a SELECT-style call; for
        # procedures the performance agent skips the case.
        if "FUNCTION" not in meta.type_desc.upper():
            return ""
        argsql = ", ".join(self._quote_literal(a) for a in args)
        return f"SELECT [{self.config.source.schema_name}].[{meta.name}]({argsql}) AS result"

    def _build_target_sql(self, meta: _ProcMeta, args: list[Any]) -> str:
        if "FUNCTION" not in meta.type_desc.upper():
            return ""
        argsql = ", ".join(self._quote_literal(a) for a in args)
        return f'SELECT "{self.config.target.schema_name}"."{meta.name}"({argsql}) AS result'

    @staticmethod
    def _normalise(rows: list[dict[str, Any]]) -> list[tuple]:
        """Return a canonical, comparable form of a result set.

        Differences we deliberately ignore:
            * Column-name case (SQL Server keeps mixed case, PostgreSQL's DMS
              conversion lower-cases identifiers).
            * Trailing whitespace in string values (CHAR(N) padding).
            * Numeric formatting (``6`` vs ``6.0``).
            * Row order — procedures often have no ORDER BY.
        """

        def _val(v: Any) -> str | None:
            if v is None:
                return None
            if isinstance(v, bool):
                return "1" if v else "0"
            if isinstance(v, (int, float)):
                # Coerce 6 and 6.0 to a single canonical representation.
                f = float(v)
                return str(int(f)) if f.is_integer() else f"{f:.6f}".rstrip("0").rstrip(".")
            return str(v).rstrip()

        out: list[tuple] = []
        for r in rows:
            sorted_items = sorted(r.items(), key=lambda kv: kv[0].lower())
            out.append(tuple((k.lower(), _val(v)) for k, v in sorted_items))
        out.sort()
        return out

    def _explain_diff(self, name: str, src_payload: Any, tgt_payload: Any) -> str:
        prompt = (
            f"Procedure: {name}\n"
            f"SQL Server result (truncated): {str(src_payload)[:600]}\n"
            f"PostgreSQL result (truncated): {str(tgt_payload)[:600]}\n"
            "Explain in <=2 sentences the most likely cause of the difference. "
            "If unclear, say so plainly."
        )
        return self.llm_analyze(
            prompt,
            fallback="Results differ; manual review required (LLM unavailable).",
        )

    # ------------------------------------------------------------------
    def run(self) -> PhaseResult:
        src = self._list_procs(self.source_session, self.config.source.schema_name)
        tgt = self._list_procs(self.target_session, self.config.target.schema_name)

        rows: list[dict[str, Any]] = []

        for n in sorted(set(src) - set(tgt)):
            rows.append(
                {
                    "procedure": src[n].name,
                    "category": "missing_in_target",
                    "sql_test_case": "—",
                    "sql_result": "—",
                    "pg_test_case": "—",
                    "pg_result": "—",
                    "match": False,
                    "analysis": "Object exists in source but not in target.",
                }
            )
        for n in sorted(set(tgt) - set(src)):
            rows.append(
                {
                    "procedure": tgt[n].name,
                    "category": "extra_in_target",
                    "sql_test_case": "—",
                    "sql_result": "—",
                    "pg_test_case": "—",
                    "pg_result": "—",
                    "match": False,
                    "analysis": "Object exists in target but not in source.",
                }
            )

        for n in sorted(set(src) & set(tgt)):
            s_meta, t_meta = src[n], tgt[n]
            body = _strip_proc_header(s_meta.definition)
            if _DML_IN_DEF.search(body):
                rows.append(
                    {
                        "procedure": s_meta.name,
                        "category": "skipped",
                        "sql_test_case": "—",
                        "sql_result": "—",
                        "pg_test_case": "—",
                        "pg_result": "—",
                        "match": None,
                        "analysis": "Body contains DML; skipped under read-only policy.",
                    }
                )
                continue

            base_tables = self._referenced_tables(s_meta.definition)
            input_params = [p for p in s_meta.parameters if not p.get("is_output")]
            args = [
                self._sample_value(
                    p.get("parameter_name", "") or "",
                    p.get("data_type", "") or "",
                    base_tables,
                )
                for p in input_params[:4]
            ]

            src_display = self._format_call_for_display(
                self.config.source.schema_name, s_meta.name, args, "sqlserver"
            )
            tgt_display = self._format_call_for_display(
                self.config.target.schema_name, t_meta.name, args, "postgresql"
            )

            try:
                src_payload: Any = self.call_json(
                    self.source_session,
                    "call_procedure",
                    {
                        "schema": self.config.source.schema_name,
                        "name": s_meta.name,
                        "args_json": json.dumps(args, default=str),
                    },
                )
            except RuntimeError as e:
                src_payload = {"error": str(e)[:300]}
            try:
                tgt_payload: Any = self.call_json(
                    self.target_session,
                    "call_procedure",
                    {
                        "schema": self.config.target.schema_name,
                        "name": t_meta.name,
                        "args_json": json.dumps(args, default=str),
                    },
                )
            except RuntimeError as e:
                tgt_payload = {"error": str(e)[:300]}

            match = (
                isinstance(src_payload, list)
                and isinstance(tgt_payload, list)
                and self._normalise(src_payload) == self._normalise(tgt_payload)
            )
            analysis = (
                "Results match." if match else self._explain_diff(s_meta.name, src_payload, tgt_payload)
            )

            rows.append(
                {
                    "procedure": s_meta.name,
                    "category": "executed",
                    "sql_test_case": src_display,
                    "sql_result": str(src_payload),
                    "pg_test_case": tgt_display,
                    "pg_result": str(tgt_payload),
                    "match": match,
                    "analysis": analysis,
                }
            )

        executed = [r for r in rows if r["category"] == "executed"]
        mismatches = [r for r in executed if r["match"] is False]
        structural = [r for r in rows if r["category"] in ("missing_in_target", "extra_in_target")]
        any_diff = bool(structural or mismatches)

        if not rows:
            summary = "No procedures or functions found in either schema."
            status = "ok"
        elif not any_diff:
            summary = f"All {len(executed)} executed procedures returned matching results."
            status = "ok"
        else:
            summary = (
                f"{len(mismatches)} mismatched and {len(rows) - len(executed)} structural diff(s) found."
            )
            status = "fail" if mismatches else "warn"

        return PhaseResult(
            name="Functional Testing",
            summary=summary,
            rows=rows,
            status=status,
            extras={"sample_size": self.config.sample_size},
        )
