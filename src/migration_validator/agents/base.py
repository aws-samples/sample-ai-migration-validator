"""Shared base for all specialist agents.

Every agent talks to the source and target databases **only through MCP**. The
``MCPSessionManager`` is created once by the orchestrator and shared across
phases so we don't spawn new server processes per agent.

A small ``llm_analyze()`` helper wraps Strands + Bedrock for narrative analysis
and degrades gracefully to a deterministic fallback when Bedrock is unavailable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..config import ValidatorConfig
from ..mcp_session import MCPSession, MCPSessionManager
from ..utils import logging as _log

log = _log.get(__name__)


@dataclass
class PhaseResult:
    """Common shape returned by every phase."""

    name: str
    summary: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # 'ok' | 'warn' | 'fail'


class BaseAgent:
    """Common helpers shared by every specialist agent.

    Agents receive the orchestrator's MCP session manager and resolve their
    own source/target sessions on first use. Sessions are keyed on
    fingerprint, so two agents asking for the same target reuse the same
    server process.
    """

    name: str = "base"

    def __init__(self, config: ValidatorConfig, sessions: MCPSessionManager) -> None:
        self.config = config
        self._sessions = sessions
        self._strands_agent = None
        self._llm_unavailable = False
        self._llm_unavailable_reason: str | None = None

    # ------------------------------------------------------------------
    @property
    def source_session(self) -> MCPSession:
        return self._sessions.get_or_create(self.config.source, role="source")

    @property
    def target_session(self) -> MCPSession:
        return self._sessions.get_or_create(self.config.target, role="target")

    # ------------------------------------------------------------------
    @staticmethod
    def call_json(session: MCPSession, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call an MCP tool and parse its JSON payload.

        The bundled MCP servers always return JSON in a single text block. If
        the server reports a structured ``{"error": ...}`` payload we raise
        ``RuntimeError`` so the calling agent can surface it cleanly.
        """
        raw = session.call(tool, arguments)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"MCP tool '{tool}' returned non-JSON: {raw[:200]}") from e
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"MCP tool '{tool}' rejected: {payload.get('reason', payload['error'])}")
        return payload

    # ------------------------------------------------------------------
    def _get_llm(self):
        """Return a Strands ``Agent`` bound to Bedrock, or ``None`` if unavailable.

        Captures the underlying exception in ``self._llm_unavailable_reason`` so
        callers can include the cause in their analysis output (e.g. "AWS
        credentials not found", "AccessDeniedException for model …").
        """
        if self._llm_unavailable:
            return None
        if self._strands_agent is not None:
            return self._strands_agent
        try:
            from strands import Agent
            from strands.models import BedrockModel

            model_kwargs: dict[str, Any] = {
                "model_id": self.config.bedrock_model,
                "region_name": self.config.region,
            }
            # When a Guardrail is configured, every model invocation flows
            # through it. The Bedrock runtime applies content / PII filters
            # both pre-invoke (input) and post-invoke (output).
            if self.config.bedrock_guardrail_id:
                model_kwargs["guardrail_id"] = self.config.bedrock_guardrail_id
                model_kwargs["guardrail_version"] = self.config.bedrock_guardrail_version
            model = BedrockModel(**model_kwargs)
            self._strands_agent = Agent(model=model, system_prompt=self.system_prompt())
            return self._strands_agent
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            log.warning("LLM unavailable for %s (%s); using deterministic-only path", self.name, reason)
            self._llm_unavailable = True
            self._llm_unavailable_reason = reason
            return None

    def system_prompt(self) -> str:
        return (
            "You are a database migration validator. Be precise, terse, and only "
            "describe what the data shows. Never fabricate."
        )

    def llm_analyze(self, prompt: str, fallback: str = "") -> str:
        agent = self._get_llm()
        if agent is None:
            reason = getattr(self, "_llm_unavailable_reason", None)
            if reason and fallback:
                return f"{fallback}  [LLM unavailable: {reason}]"
            return fallback
        try:
            result = agent(prompt)
            return str(result).strip()
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            log.warning("LLM call failed (%s); using fallback", reason)
            self._llm_unavailable = True
            self._llm_unavailable_reason = reason
            if fallback:
                return f"{fallback}  [LLM call failed: {reason}]"
            return fallback

    # ------------------------------------------------------------------
    def run(self) -> PhaseResult:  # pragma: no cover - abstract
        raise NotImplementedError

    # ------------------------------------------------------------------
    @classmethod
    def capability(cls) -> str:
        """One-line description shown in the capabilities banner."""
        return cls.__doc__.splitlines()[0].strip() if cls.__doc__ else cls.name
