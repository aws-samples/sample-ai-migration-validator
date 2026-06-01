"""Orchestrator.

Flow
----
1. Prompt for / load source + target connection details.
2. Use ``MCPSessionManager`` to spin up (or reuse) one MCP server per unique
   connection. Source and target therefore each get their own server process.
3. Display the four capabilities of the validator and ask the user whether to
   run in interactive or auto mode.
4. Run phases:
     interactive — show capabilities, run Inventory, display results, then ask
                   before each subsequent phase.
     auto        — run all four phases in sequence, displaying each result.
5. Generate the consolidated HTML + JSON report.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.spinner import Spinner
from rich.table import Table

from .agents import (
    FunctionalTestingAgent,
    InventoryAgent,
    PerformanceAgent,
    RowCountAgent,
)
from .agents.base import BaseAgent, PhaseResult
from .config import ValidatorConfig
from .mcp_session import MCPSessionManager
from .reports.html_report import render_html
from .reports.json_report import write_json
from .utils import logging as _log

log = _log.get(__name__)
console = Console()


PHASE_TITLES: list[tuple[str, type[BaseAgent], str, str]] = [
    (
        "Inventory Check",
        InventoryAgent,
        "Counts every object type (tables, views, procedures, functions, "
        "triggers, indexes, sequences) on both sides and flags anything missing "
        "or extra in the target.",
        "Comparing object inventories on source and target",
    ),
    (
        "Row-Count Reconciliation",
        RowCountAgent,
        "Compares COUNT(*) for every table between source and target and reports per-table deltas.",
        "Comparing row counts for every table",
    ),
    (
        "Functional Testing",
        FunctionalTestingAgent,
        "For procedures/functions present in both schemas, builds test cases "
        "from real base-table data, executes them on each side, and explains "
        "any result differences.",
        "Working on functional testing of stored procedures",
    ),
    (
        "Performance Testing",
        PerformanceAgent,
        "Re-runs the functional test cases and compares median execution time "
        "between source and target. Flags target slowness past the threshold "
        "and suggests tuning actions.",
        "Measuring source vs target execution time for each procedure",
    ),
]


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def _print_banner() -> None:
    """Show the tool banner. Called before any prompting so the user always
    sees what they are about to interact with."""
    banner = (
        "[bold cyan]   _    ___      __  __ _                  _   _                 [/]\n"
        "[bold cyan]  / \\  |_ _|    |  \\/  (_) __ _ _ __ __ _| |_(_) ___  _ __      [/]\n"
        "[bold cyan] / _ \\  | |     | |\\/| | |/ _` | '__/ _` | __| |/ _ \\| '_ \\     [/]\n"
        "[bold cyan]/ ___ \\ | |     | |  | | | (_| | | | (_| | |_| | (_) | | | |    [/]\n"
        "[bold cyan]\\_/   \\_\\___|   |_|  |_|_|\\__, |_|  \\__,_|\\__|_|\\___/|_| |_|   [/]\n"
        "[bold cyan]                          |___/                                  [/]\n"
        "[bold cyan]            __     __    _ _     _       _                       [/]\n"
        "[bold cyan]            \\ \\   / /_ _| (_) __| | __ _| |_ ___  _ __           [/]\n"
        "[bold cyan]             \\ \\ / / _` | | |/ _` |/ _` | __/ _ \\| '__|          [/]\n"
        "[bold cyan]              \\ V / (_| | | | (_| | (_| | || (_) | |             [/]\n"
        "[bold cyan]               \\_/ \\__,_|_|_|\\__,_|\\__,_|\\__\\___/|_|             [/]\n"
        "\n"
        "[bold]                    AI  Migration  Validator[/]\n"
        "[dim]      SQL Server -> PostgreSQL — agentic post-migration checks[/]"
    )
    console.print()
    console.print(banner)
    console.print()


def _print_connectivity_note(cfg: ValidatorConfig) -> None:
    """Reminder shown after credentials are collected, before we attempt
    to actually connect. Saves users from staring at obscure timeout errors
    when the real cause is firewall / Security Group / VPN."""
    note = (
        "[bold]Before we connect, please confirm network reachability:[/]\n\n"
        f"  • This machine must be able to reach the [bold]source[/] at "
        f"[cyan]{cfg.source.host}:{cfg.source.port}[/].\n"
        f"  • This machine must be able to reach the [bold]target[/] at "
        f"[cyan]{cfg.target.host}:{cfg.target.port}[/].\n\n"
        "[dim]On-prem databases:[/] open the firewall on the database host "
        "(or your corporate firewall) for inbound TCP from this machine's IP.\n"
        "[dim]Amazon RDS / Aurora:[/]  add this machine's IP to the database's "
        "[bold]Security Group[/] inbound rules. If you're connecting from a peered "
        "VPC or on-prem, confirm the route table and NAT/IGW path.\n"
        "[dim]Either side:[/]         a quick way to confirm is "
        f"[cyan]nc -vz {cfg.source.host} {cfg.source.port}[/] and "
        f"[cyan]nc -vz {cfg.target.host} {cfg.target.port}[/].\n"
    )
    console.print()
    console.print(Panel(note, title="Network connectivity", border_style="yellow"))


# ---------------------------------------------------------------------------
def _print_intro(cfg: ValidatorConfig) -> None:
    console.rule("[bold blue]Connection summary")
    console.print(
        Panel.fit(
            f"[bold]Source[/] {cfg.source.host}:{cfg.source.port}/{cfg.source.database} "
            f"(schema={cfg.source.schema_name})\n"
            f"[bold]Target[/] {cfg.target.host}:{cfg.target.port}/{cfg.target.database} "
            f"(schema={cfg.target.schema_name})\n"
            f"[bold]Model[/]  {cfg.bedrock_model} ({cfg.region})",
            title="What you provided",
        )
    )


def _print_capabilities(selected_mode: str) -> None:
    console.print()
    console.rule("[bold blue]Validator capabilities")
    cap_table = Table(show_header=True, header_style="bold", show_lines=False, box=None)
    cap_table.add_column("#", style="cyan", width=3)
    cap_table.add_column("Phase", style="bold")
    cap_table.add_column("What it does")
    for idx, (title, _agent, desc, _action) in enumerate(PHASE_TITLES, start=1):
        cap_table.add_row(str(idx), title, desc)
    console.print(cap_table)
    console.print(f"\n[bold]Selected mode:[/] {selected_mode}\n")


def _ask_mode(default: str) -> str:
    """Ask the user whether to run interactive or auto."""
    chosen = Prompt.ask(
        "How do you want to run the validator?",
        choices=["interactive", "auto"],
        default=default,
    )
    return chosen


def _render_phase(result: PhaseResult) -> None:
    from rich.markup import escape as _esc

    color = {"ok": "green", "warn": "yellow", "fail": "red"}.get(result.status, "white")
    console.rule(f"[bold {color}]{result.name}")
    console.print(f"[{color}]{result.summary}[/]\n")

    if not result.rows:
        return

    # Hide "private" keys that begin with underscore — those are passed through
    # to the JSON / HTML report but not displayed in the console table.
    keys = [k for k in result.rows[0].keys() if not k.startswith("_")]
    # Columns whose values are free text (potentially containing ``[`` etc.) and
    # therefore need Rich markup escaped to avoid e.g. ``[dbo]`` being eaten.
    free_text_keys = {
        "sql_test_case",
        "pg_test_case",
        "sql_result",
        "pg_result",
        "source_test_case",
        "target_test_case",
        "analysis",
        "recommendation",
        "notes",
    }
    table = Table(show_lines=False, header_style="bold")
    for k in keys:
        table.add_column(k.replace("_", " ").title(), overflow="fold")
    for r in result.rows:
        styled: list[str] = []
        for k in keys:
            val = r.get(k)
            text = "" if val is None else str(val)
            if k in free_text_keys:
                text = _esc(text)
            if k == "status":
                if val == "match":
                    text = f"[green]✓ {text}[/]"
                elif val == "flagged":
                    text = f"[red]⚑ {text}[/]"
                elif val == "error":
                    text = f"[red]✗ {text}[/]"
                elif val == "extra_in_target":
                    text = f"[yellow]{text}[/]"
                elif val in ("missing_in_target", "mismatch"):
                    text = f"[red]{text}[/]"
            elif k == "match":
                if val is True:
                    text = "[green]✓ match[/]"
                elif val is False:
                    text = "[red]✗ differ[/]"
                else:
                    text = "[dim]n/a[/]"
            elif k == "flagged":
                text = "[red]⚑ flagged[/]" if val else "[green]ok[/]"
            elif k == "sql_server_ms":
                text = f"[cyan]{text}[/]" if val is not None else "[dim]—[/]"
            elif k == "postgresql_ms":
                text = f"[magenta]{text}[/]" if val is not None else "[dim]—[/]"
            elif k == "delta_ms":
                if val is None:
                    text = "[dim]—[/]"
                elif isinstance(val, (int, float)) and val > 0:
                    text = f"[red]+{val}[/]"
                elif isinstance(val, (int, float)) and val < 0:
                    text = f"[green]{val}[/]"
                else:
                    text = f"[green]{val}[/]"
            elif k == "details":
                missing = r.get("_missing") or []
                extra = r.get("_extra") or []
                pieces: list[str] = []
                if missing:
                    pieces.append(f"[red]missing in target:[/] [red]{', '.join(missing)}[/]")
                if extra:
                    pieces.append(f"[yellow]extra in target:[/] [yellow]{', '.join(extra)}[/]")
                text = "  •  ".join(pieces) if pieces else "[dim]—[/]"
            elif k == "source_count":
                text = f"[cyan]{text}[/]"
            elif k == "target_count":
                text = f"[magenta]{text}[/]"
            styled.append(text)
        table.add_row(*styled)
    console.print(table)


# ---------------------------------------------------------------------------
# Pre-flight check
# ---------------------------------------------------------------------------
def _preflight_check(session, details, role: str) -> bool:
    """Call ``verify_connection`` on the MCP. Print result and return ok flag."""
    try:
        raw = session.call("verify_connection", {})
    except Exception as e:
        console.print(f"  [red]✗ {role.upper()} connection check failed[/]: could not reach MCP server ({e})")
        return False
    if not raw:
        console.print(
            f"  [red]✗ {role.upper()} connection check failed[/]: "
            "MCP returned no payload (server crashed during verify_connection)."
        )
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        console.print(f"  [red]✗ {role.upper()} connection check failed[/]: non-JSON response: {raw[:200]}")
        return False
    if not payload.get("ok"):
        reason = payload.get("reason", "unknown error")
        console.print(f"  [red]✗ {role.upper()} credentials/connection rejected[/]: {reason}")
        return False
    info = payload.get("info") or {}
    console.print(f"  [green]✓ {role.upper()} connected[/]  [dim]({details.host}:{details.port} → {info})[/]")
    return True


def _abort(
    html_path: Path,
    json_path: Path,
    log_path: Path,
    mcp_log_path: Path | None,
) -> Path:
    """Print an abort message and return the (uncreated) HTML path."""
    console.print()
    console.rule("[bold red]Aborting before phases run")
    console.print("Fix the connection details above and re-run. Logs that may help:")
    console.print(f"  Debug log: [dim]{log_path}[/]")
    if mcp_log_path is not None:
        console.print(f"  MCP log:   [dim]{mcp_log_path}[/]")
    _ = (html_path, json_path)  # No reports written for aborted runs.
    return html_path


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def run(cfg: ValidatorConfig, phase_filter: Sequence[str] | None = None) -> Path:
    """Run the validator end-to-end. Returns the path of the HTML report."""
    _print_intro(cfg)
    _print_connectivity_note(cfg)

    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    html_path = cfg.report_dir / f"validation_{timestamp}.html"
    json_path = cfg.report_dir / f"validation_{timestamp}.json"
    log_path = cfg.report_dir / f"validator-{timestamp}.log"
    _log.attach_file_handler(log_path)
    log.info("Run started; full DEBUG log: %s", log_path)

    # ------------------------------------------------------------------
    # MCP sessions: one per unique connection, reused thereafter.
    # ------------------------------------------------------------------
    # --------------------------------------------------------------
    # MCP sessions: one per unique connection, reused thereafter.
    # --------------------------------------------------------------
    console.print()
    console.rule("[bold blue]MCP setup")
    mcp_log_path: Path | None = None
    with MCPSessionManager(log_dir=cfg.report_dir) as sessions:
        mcp_log_path = sessions.errlog_path
        # Eagerly initialise both sessions so the user sees the lifecycle
        # messages before phases start, and so phase-1 timing isn't polluted
        # by server start-up.
        src_session = sessions.get_or_create(cfg.source, role="source")
        tgt_session = sessions.get_or_create(cfg.target, role="target")

        # --------------------------------------------------------------
        # Pre-flight: verify both connections work before running any phase.
        # --------------------------------------------------------------
        if not _preflight_check(src_session, cfg.source, "source"):
            return _abort(html_path, json_path, log_path, mcp_log_path)
        if not _preflight_check(tgt_session, cfg.target, "target"):
            return _abort(html_path, json_path, log_path, mcp_log_path)

        # --------------------------------------------------------------
        # Mode selection (only when not pre-set on CLI).
        # --------------------------------------------------------------
        chosen_mode = cfg.mode
        if cfg.mode_was_explicit is False:
            _print_capabilities(selected_mode="(not yet chosen)")
            chosen_mode = _ask_mode(default=cfg.mode)
        else:
            _print_capabilities(selected_mode=cfg.mode)

        # --------------------------------------------------------------
        # Phase loop.
        # --------------------------------------------------------------
        results: list[PhaseResult] = []
        for idx, (title, agent_cls, _desc, action) in enumerate(PHASE_TITLES, start=1):
            if phase_filter and title not in phase_filter:
                continue
            console.print(f"\n[bold cyan]▶ Phase {idx}: {title}[/]")
            agent = agent_cls(cfg, sessions)
            # Spinner runs while the agent does its work — gives the user
            # an obvious "I am working" signal even on slow phases.
            spinner = Spinner("earth", text=f"[dim]{action}…[/]")
            try:
                with Live(spinner, console=console, refresh_per_second=12, transient=True):
                    result = agent.run()
            except Exception as e:
                log.exception("Phase %s failed", title)
                result = PhaseResult(name=title, summary=f"Phase failed: {e}", status="fail")
            results.append(result)
            _render_phase(result)

            if chosen_mode == "interactive" and idx < len(PHASE_TITLES):
                if not Confirm.ask(f"Proceed to phase {idx + 1}?", default=True):
                    console.print("[yellow]Stopping at user request.[/]")
                    break

    # MCP sessions are now closed.
    write_json(json_path, cfg, results)
    render_html(html_path, cfg, results)
    console.rule("[bold green]Consolidated report ready")
    console.print(f"HTML report: [bold]{html_path}[/]")
    console.print(f"JSON report: [bold]{json_path}[/]")
    if mcp_log_path is not None:
        console.print(f"MCP log:     [dim]{mcp_log_path}[/]")
    console.print(f"Debug log:   [dim]{log_path}[/]")
    return html_path
