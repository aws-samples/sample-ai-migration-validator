"""Centralised logging that scrubs secrets out of every record.

Anything that looks like a password / connection-string fragment is replaced
with ``***`` before the record is emitted.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(password\s*[=:]\s*)([^\s;,'\"]+)"),
    re.compile(r"(?i)(pwd\s*[=:]\s*)([^\s;,'\"]+)"),
    re.compile(r"(?i)(secret\s*[=:]\s*)([^\s;,'\"]+)"),
    re.compile(r"(?i)(token\s*[=:]\s*)([^\s;,'\"]+)"),
]


def _scrub(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(r"\1***", text)
    return text


class SecretsRedactingFilter(logging.Filter):
    """Filter that scrubs the formatted message and any string args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _scrub(record.msg)
        if record.args:
            new_args: tuple[Any, ...] | dict[str, Any]
            if isinstance(record.args, dict):
                new_args = {k: _scrub(v) if isinstance(v, str) else v for k, v in record.args.items()}
            else:
                new_args = tuple(_scrub(a) if isinstance(a, str) else a for a in record.args)
            record.args = new_args
        return True


def configure(level: str = "INFO", file_path: Path | None = None) -> None:
    """Configure root logging once. Idempotent.

    Always installs a stderr handler. Optionally also installs a file handler
    so DEBUG-level traces land in a per-run log file alongside the report.
    """
    root = logging.getLogger()
    if getattr(root, "_validator_configured", False):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    redact = SecretsRedactingFilter()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    stderr_handler.addFilter(redact)
    stderr_handler.setLevel(level.upper())
    root.addHandler(stderr_handler)

    if file_path is not None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(file_path, mode="a", encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.addFilter(redact)
        file_handler.setLevel(logging.DEBUG)  # always capture full detail in file
        root.addHandler(file_handler)

    root.setLevel(logging.DEBUG)
    # Quiet noisy libraries.
    for noisy in (
        "botocore",
        "urllib3",
        "boto3",
        "pyodbc",
        "pytds",
        "pytds.tds_session",
        "mcp",
        "mcp.server",
        "mcp.server.lowlevel",
        "mcp.server.lowlevel.server",
        "FastMCP",
        "asyncio",
        "httpx",
        "strands",
        "strands.agent",
        "strands.event_loop",
        "strands.models",
        "strands.tools",
        "strands.telemetry",
        "strands.telemetry.metrics",
        "opentelemetry",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._validator_configured = True  # type: ignore[attr-defined]


def attach_file_handler(file_path: Path) -> None:
    """Attach a DEBUG-level file handler to root after initial configuration.

    Called by the orchestrator once the report directory (and therefore the
    file path) is known.
    """
    root = logging.getLogger()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    handler = logging.FileHandler(file_path, mode="a", encoding="utf-8")
    handler.setFormatter(fmt)
    handler.addFilter(SecretsRedactingFilter())
    handler.setLevel(logging.DEBUG)
    root.addHandler(handler)
    # Make sure the root level lets DEBUG through to the new handler.
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
