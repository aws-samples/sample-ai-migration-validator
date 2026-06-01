"""Command-line entry point.

Usage examples:

  python -m migration_validator                 # interactive
  python -m migration_validator --mode auto
  python -m migration_validator --config ./config.yaml
  python -m migration_validator --source-secret migration-validator/source \\
                                --target-secret migration-validator/target

The orchestrator handles per-phase prompting in ``interactive`` mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .config import build_config
from .orchestrator import _print_banner
from .orchestrator import run as run_orchestrator
from .utils import logging as _log

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    config: Annotated[Path | None, typer.Option(help="YAML config file.")] = None,
    source_secret: Annotated[
        str | None, typer.Option("--source-secret", help="Secrets Manager id for source DB.")
    ] = None,
    target_secret: Annotated[
        str | None, typer.Option("--target-secret", help="Secrets Manager id for target DB.")
    ] = None,
    mode: Annotated[
        str | None, typer.Option(help="interactive | auto. If omitted, you will be prompted.")
    ] = None,
    region: Annotated[str, typer.Option(help="AWS region for Bedrock and Secrets Manager.")] = "us-east-1",
    bedrock_model: Annotated[
        str, typer.Option(help="Bedrock model id (or inference profile id).")
    ] = "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    bedrock_guardrail_id: Annotated[
        str | None,
        typer.Option(
            "--guardrail-id",
            help=(
                "Optional Bedrock Guardrail id. When set, every LLM call goes "
                "through this guardrail (PII, denied topics, content filters)."
            ),
        ),
    ] = None,
    bedrock_guardrail_version: Annotated[
        str, typer.Option("--guardrail-version", help="Guardrail version to apply.")
    ] = "DRAFT",
    report_dir: Annotated[Path, typer.Option(help="Directory for report output.")] = Path("./reports"),
    perf_threshold_ms: Annotated[float, typer.Option(help="Flag target if slower by this many ms.")] = 5.0,
    sample_size: Annotated[int, typer.Option(help="Rows to sample for functional tests.")] = 100,
    source_schema: Annotated[str | None, typer.Option(help="Override source schema.")] = None,
    target_schema: Annotated[str | None, typer.Option(help="Override target schema.")] = None,
    allow_insecure: Annotated[
        bool, typer.Option(help="Allow non-TLS connections (NOT recommended).")
    ] = False,
    redact_pii: Annotated[
        bool,
        typer.Option(
            "--redact-pii/--no-redact-pii",
            help=(
                "Redact common PII patterns (email, phone, SSN, IBAN, credit "
                "card, IP, private keys) from the HTML and JSON reports."
            ),
        ),
    ] = True,
    log_level: Annotated[str, typer.Option(help="DEBUG | INFO | WARNING | ERROR")] = "INFO",
) -> None:
    """Run the validator."""
    _log.configure(level=log_level)

    if mode is not None and mode not in ("interactive", "auto"):
        raise typer.BadParameter("mode must be 'interactive' or 'auto'")

    # Banner printed before any prompting so the user knows what tool is asking
    # for credentials.
    _print_banner()

    cfg = build_config(
        config_path=config,
        source_secret=source_secret,
        target_secret=target_secret,
        mode=mode,
        region=region,
        bedrock_model=bedrock_model,
        bedrock_guardrail_id=bedrock_guardrail_id,
        bedrock_guardrail_version=bedrock_guardrail_version,
        report_dir=report_dir,
        perf_threshold_ms=perf_threshold_ms,
        sample_size=sample_size,
        allow_insecure=allow_insecure,
        redact_pii=redact_pii,
        source_schema=source_schema,
        target_schema=target_schema,
    )
    run_orchestrator(cfg)


def cli() -> None:
    """Entry point for the ``migration-validator`` script."""
    app()


if __name__ == "__main__":
    cli()
