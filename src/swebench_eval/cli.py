"""Typer CLI entry point for swebench-eval."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from swebench_eval.models import ModelEvaluation
from swebench_eval.orchestrator import EvaluationOrchestrator, Event, EventType
from swebench_eval.output import load_results, write_results
from swebench_eval.task_loader import load_tasks

app = typer.Typer(
    name="swebench-eval",
    help="Local SWE-bench Vetted evaluation tool.",
    no_args_is_help=True,
)

console = Console(stderr=True)

_STATUS_STYLE = {
    "pending": "dim",
    "building": "cyan",
    "agent_running": "yellow bold",
    "scoring": "magenta",
    "completed": "green",
    "failed": "red bold",
}


def _status_text(status: str) -> Text:
    style = _STATUS_STYLE.get(status, "")
    icons = {
        "pending": "  pending",
        "building": "  building",
        "agent_running": "  agent",
        "scoring": "  scoring",
        "completed": "  done",
        "failed": "  failed",
    }
    return Text(icons.get(status, status), style=style)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@app.command()
def run(
    input_path: Annotated[Path, typer.Argument(help="Task file or directory")],
    model: Annotated[str, typer.Option("-m", "--model", help="LiteLLM model string")] = "openai/gpt-4o",
    api_base: Annotated[Optional[str], typer.Option("--api-base", help="Custom API endpoint")] = None,
    api_key: Annotated[Optional[str], typer.Option("-k", "--api-key", help="API key (also reads env vars)")] = None,
    attempts: Annotated[int, typer.Option("-n", "--attempts", help="Attempts per task")] = 3,
    concurrent: Annotated[int, typer.Option("-j", "--concurrent", help="Max parallel tasks")] = 4,
    timeout: Annotated[int, typer.Option("--timeout", help="Agent timeout seconds")] = 1800,
    output: Annotated[Path, typer.Option("-o", "--output", help="Output directory")] = Path("./results"),
    force_rebuild: Annotated[bool, typer.Option("--force-rebuild", help="Rebuild Docker images even if cached")] = False,
    no_live: Annotated[bool, typer.Option("--no-live", help="Disable live display, plain log output")] = False,
    filter_prefix: Annotated[Optional[str], typer.Option("--filter", help="Filter tasks by ID prefix")] = None,
) -> None:
    """Run evaluation on tasks."""
    if api_key:
        os.environ.setdefault("OPENAI_API_KEY", api_key)
        os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
        os.environ.setdefault("GEMINI_API_KEY", api_key)

    tasks = load_tasks(input_path, filter_prefix=filter_prefix)
    if not tasks:
        console.print("[red]No tasks found[/red]")
        raise typer.Exit(1)

    console.print(f"Loaded [bold]{len(tasks)}[/bold] task(s), model [bold]{model}[/bold]")

    if no_live:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    # -- shared state --
    n_attempts = attempts
    start_time = time.monotonic()
    task_states: dict[str, dict[str, str]] = {
        t.metadata.id: {"status": "pending", "attempt": "-", "resolved": "-"}
        for t in tasks
    }

    def on_event(event: Event) -> None:
        tid = event.task_id
        if event.type == EventType.build_start:
            task_states[tid]["status"] = "building"
        elif event.type == EventType.build_done:
            if not event.data.get("success"):
                task_states[tid]["status"] = "failed"
        elif event.type == EventType.task_status:
            raw = event.data.get("status", "")
            task_states[tid]["status"] = raw.value if hasattr(raw, "value") else str(raw)
            a = event.data.get("attempt")
            if a:
                task_states[tid]["attempt"] = f"{a}/{n_attempts}"
        elif event.type == EventType.task_done:
            rc = event.data.get("resolved_count", 0)
            task_states[tid]["status"] = "completed"
            task_states[tid]["attempt"] = f"{n_attempts}/{n_attempts}"
            task_states[tid]["resolved"] = f"{rc}/{n_attempts}"

    def build_display() -> Panel:
        elapsed = time.monotonic() - start_time
        mm, ss = divmod(int(elapsed), 60)
        hh, mm = divmod(mm, 60)

        states = list(task_states.values())
        done = sum(1 for s in states if s["status"] in ("completed", "failed"))
        building = sum(1 for s in states if s["status"] == "building")
        running = sum(1 for s in states if s["status"] in ("agent_running", "scoring"))
        resolved = sum(
            1 for s in states
            if s["resolved"] != "-" and not s["resolved"].startswith("0/")
        )

        header = Text.assemble(
            ("Model: ", "bold"),
            (model, ""),
            ("  Tasks: ", "bold"),
            (f"{done}/{len(tasks)}", ""),
            ("  Building: ", "bold"),
            (str(building), "cyan"),
            ("  Running: ", "bold"),
            (str(running), "yellow"),
            ("  Resolved: ", "bold"),
            (f"{resolved}", "green"),
            ("  ", ""),
            (f"{hh:02d}:{mm:02d}:{ss:02d}", "dim"),
        )

        table = Table(box=None, pad_edge=False, show_header=True, header_style="bold dim")
        table.add_column("#", width=4, justify="right")
        table.add_column("Task ID", min_width=20, no_wrap=True)
        table.add_column("Status", width=12)
        table.add_column("Attempt", width=9, justify="center")
        table.add_column("Resolved", width=9, justify="center")

        for i, t in enumerate(tasks):
            s = task_states[t.metadata.id]
            table.add_row(
                str(i + 1),
                t.metadata.id,
                _status_text(s["status"]),
                s["attempt"],
                s["resolved"],
            )

        return Panel(
            Group(header, "", table),
            title="[bold]SWE-bench Eval[/bold]",
            border_style="blue",
            padding=(0, 1),
        )

    async def do_run() -> ModelEvaluation:
        orchestrator = EvaluationOrchestrator(
            tasks,
            model=model,
            api_base=api_base,
            n_attempts=n_attempts,
            max_concurrent=concurrent,
            timeout_seconds=timeout,
            force_rebuild=force_rebuild,
            on_event=on_event,
        )
        return await orchestrator.run()

    if no_live:
        result = asyncio.run(do_run())
    else:
        async def run_with_live() -> ModelEvaluation:
            with Live(build_display(), console=console, refresh_per_second=4) as live:
                eval_task = asyncio.create_task(do_run())
                while not eval_task.done():
                    live.update(build_display())
                    await asyncio.sleep(0.25)
                live.update(build_display())
                return eval_task.result()

        result = asyncio.run(run_with_live())

    run_dir = write_results(result, output)
    console.print()
    _print_summary(result)
    console.print(f"\nResults written to [bold]{run_dir}[/bold]")


def _print_summary(evaluation: ModelEvaluation) -> None:
    resolved = sum(1 for e in evaluation.evaluations if e.resolved_count > 0)
    total = len(evaluation.evaluations)

    table = Table(title="Results", box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_row("Model", evaluation.model)
    table.add_row("Tasks", str(total))
    table.add_row("Attempts/task", str(evaluation.n_attempts))
    table.add_row("Resolved", f"{resolved}/{total}")
    table.add_row("pass@1", f"{evaluation.pass_at_1:.3f}")
    table.add_row("pass@3", f"{evaluation.pass_at_3:.3f}")
    table.add_row("Resolved rate", f"{evaluation.resolved_rate:.3f}")
    console.print(table)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


@app.command()
def build(
    input_path: Annotated[Path, typer.Argument(help="Task file or directory")],
    force_rebuild: Annotated[bool, typer.Option("--force-rebuild", help="Rebuild even if cached")] = False,
    concurrent: Annotated[int, typer.Option("-j", "--concurrent", help="Max parallel builds")] = 4,
    filter_prefix: Annotated[Optional[str], typer.Option("--filter", help="Filter tasks by ID prefix")] = None,
) -> None:
    """Build Docker images only (no evaluation)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    tasks = load_tasks(input_path, filter_prefix=filter_prefix)
    if not tasks:
        console.print("[red]No tasks found[/red]")
        raise typer.Exit(1)

    console.print(f"Building images for {len(tasks)} tasks")

    from swebench_eval.docker_builder import build_image

    async def build_all() -> None:
        sem = asyncio.Semaphore(concurrent)

        async def build_one(task: object) -> None:
            async with sem:
                result = await build_image(task, force_rebuild=force_rebuild)  # type: ignore[arg-type]
                status = "[green]OK[/green]" if result.success else f"[red]FAIL: {result.error}[/red]"
                console.print(f"  {result.tag} — {status} ({result.build_time:.1f}s)")

        await asyncio.gather(*(build_one(t) for t in tasks))

    asyncio.run(build_all())
    console.print("[green]Done[/green]")


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@app.command()
def results(
    run_dir: Annotated[Path, typer.Argument(help="Run directory (results/run_*)")],
) -> None:
    """Display results summary from a previous run."""
    evaluation = load_results(run_dir)
    _print_summary(evaluation)

    console.print()
    table = Table(title="Per-task results")
    table.add_column("#", width=4, justify="right")
    table.add_column("Task ID", min_width=25)
    table.add_column("Resolved", width=10, justify="center")
    table.add_column("Count", width=8, justify="center")
    for i, te in enumerate(evaluation.evaluations):
        style = "green" if te.resolved_count > 0 else "red"
        table.add_row(
            str(i + 1),
            te.task_id,
            Text("Yes" if te.resolved_count > 0 else "No", style=style),
            f"{te.resolved_count}/{evaluation.n_attempts}",
        )
    console.print(table)
