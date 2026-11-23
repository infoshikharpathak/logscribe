from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from logscribe import seeds
from logscribe.analyzer import build_analyzer
from logscribe.config import Source, WatchConfig, load_config
from logscribe.incident import OnCallCurator
from logscribe.memory import ErrorMemory
from logscribe.processor import ErrorProcessor
from logscribe.sampler import LogSampler, MultiFileSampler

load_dotenv()

app = typer.Typer(help="Tail-based log monitoring with RAG-powered error analysis.")
console = Console()


def _memory(config: Path | None) -> ErrorMemory:
    if config is None:
        return ErrorMemory()
    cfg = load_config(config)
    return ErrorMemory(persist_dir=cfg.persist_dir, collection_name=cfg.collection)


@app.command()
def watch(
    file: Optional[list[Path]] = typer.Option(
        None, "--file", "-f", help="Log file to tail. Repeat for several files."
    ),
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", exists=True, help="TOML watch config: files, service names, history."
    ),
    buffer_size: Optional[int] = typer.Option(None, help="Number of context lines to capture per error."),
    top_k: Optional[int] = typer.Option(None, help="Number of similar past errors to retrieve for analysis."),
    from_start: bool = typer.Option(
        False, help="Tail from the beginning of the file instead of the end."
    ),
    report: Optional[Path] = typer.Option(
        None, help="Also append each analysis (error, matches with scores, root cause) to this JSONL file."
    ),
) -> None:
    """Tail log files, capture errors, and auto-analyze each one against past history."""
    if bool(file) == bool(config):
        raise typer.BadParameter("pass --file (one or more) or --config, not both")
    cfg = load_config(config) if config else WatchConfig(sources=[Source(path=f) for f in file])
    buffer_size = buffer_size or cfg.buffer_size
    top_k = top_k or cfg.top_k

    sampler = MultiFileSampler([
        LogSampler(src.path, buffer_size=buffer_size, service_name=src.service) for src in cfg.sources
    ])
    processor = ErrorProcessor()
    memory = ErrorMemory(persist_dir=cfg.persist_dir, collection_name=cfg.collection)
    analyzer = build_analyzer(repo_path=cfg.repo_path)
    curator = OnCallCurator()

    for src in cfg.sources:
        label = f" ({src.service})" if src.service else ""
        console.print(f"[bold green]Watching[/bold green] {src.path}{label}")
    console.print(f"buffer={buffer_size} lines, history={cfg.collection}, analyzer={type(analyzer).__name__}")
    if getattr(analyzer, "repo_path", None):
        console.print(f"git correlation: {analyzer.repo_path}")

    for chunk in sampler.tail(from_start=from_start):
        event = processor.process(chunk)
        similar = memory.query_similar(event.embedding_text(), top_k=top_k, min_similarity=cfg.min_similarity)

        where = f"{event.service_name} " if event.service_name else ""
        console.rule(f"[bold red]{where}{event.error_type}[/bold red] @ {event.timestamp}")
        console.print(event.message)
        if similar:
            table = Table(title="Matched past errors")
            table.add_column("Similarity", justify="right")
            table.add_column("Type")
            table.add_column("When")
            table.add_column("Message")
            for item in similar:
                meta = item["metadata"]
                score = item.get("similarity")
                table.add_row(f"{score:.3f}" if score is not None else f"d={item['distance']:.3f}",
                              meta.get("error_type", ""),
                              "seeded example" if meta.get("seeded") else meta.get("timestamp", ""),
                              meta.get("message", "")[:70])
            console.print(table)

        try:
            analysis = analyzer.analyze(event, similar)
        except Exception as exc:  # one failed analysis must not stop the watcher
            analysis = f"(analysis failed: {type(exc).__name__}: {exc})"
        console.print("\n[bold cyan]Root-cause analysis:[/bold cyan]")
        console.print(analysis)

        extra_metadata = {}
        card = None
        if not memory.has_error_type(event.error_type):
            card = curator.curate(event)
            extra_metadata = card.to_metadata()
            console.print(Panel(card.summary, title="🆕 New error type — on-call summary", border_style="yellow"))

        memory.add(event, extra_metadata=extra_metadata)
        if report:
            _append_report(report, event, similar, analysis, card, type(analyzer).__name__)


def _append_report(path: Path, event, similar, analysis: str, card, analyzer: str) -> None:
    entry = {
        "event_id": event.id,
        "service_name": event.service_name,
        "error_type": event.error_type,
        "message": event.message,
        "timestamp": event.timestamp,
        "request_id": event.key_variables.get("request_id"),
        "matches": [
            {"id": item["id"], "error_type": item["metadata"].get("error_type"),
             "message": item["metadata"].get("message"), "seeded": bool(item["metadata"].get("seeded")),
             "when": item["metadata"].get("timestamp"), "similarity": item.get("similarity"),
             "distance": item.get("distance")}
            for item in similar
        ],
        "analyzer": analyzer,
        "analysis": analysis,
        "on_call_summary": card.summary if card else None,
    }
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


@app.command()
def seed(
    file: Optional[Path] = typer.Argument(None, help="Seed incidents JSON (default: the config's seeds file)."),
    config: Optional[Path] = typer.Option(None, "--config", "-c", exists=True, help="TOML watch config."),
) -> None:
    """Load resolved example incidents (symptom, cause, fix) into the error history."""
    cfg = load_config(config) if config else None
    path = file or (cfg.seeds if cfg else None)
    if path is None:
        raise typer.BadParameter("give a seeds file, or a --config that names one")
    memory = _memory(config)
    ids = seeds.load_into(memory, path)
    console.print(f"Loaded {len(ids)} seeded incidents into {cfg.collection if cfg else 'error_events'}:")
    for seed_id in ids:
        console.print(f"  {seed_id}")


@app.command()
def query(
    description: str = typer.Argument(
        ..., help="Natural-language description of the error to search for."
    ),
    top_k: int = typer.Option(5, help="Number of results to return."),
    config: Optional[Path] = typer.Option(None, "--config", "-c", exists=True, help="TOML watch config."),
) -> None:
    """Semantic search over previously captured errors."""
    memory = _memory(config)
    results = memory.query_similar(description, top_k=top_k)

    if not results:
        console.print("[yellow]No matching errors found.[/yellow]")
        raise typer.Exit()

    table = Table(title="Similar past errors")
    table.add_column("Timestamp")
    table.add_column("Type")
    table.add_column("Message")
    table.add_column("Similarity", justify="right")

    for item in results:
        meta = item["metadata"]
        score = item.get("similarity")
        table.add_row(
            "seeded example" if meta.get("seeded") else meta.get("timestamp", ""),
            meta.get("error_type", ""),
            meta.get("message", "")[:80],
            f"{score:.4f}" if score is not None else f"d={item['distance']:.4f}",
        )

    console.print(table)


@app.command()
def history(
    limit: int = typer.Option(20, help="Number of recent errors to list."),
    config: Optional[Path] = typer.Option(None, "--config", "-c", exists=True, help="TOML watch config."),
) -> None:
    """List recently captured errors."""
    memory = _memory(config)
    items = memory.recent(limit=limit)

    if not items:
        console.print("[yellow]No errors captured yet.[/yellow]")
        raise typer.Exit()

    table = Table(title=f"Last {len(items)} captured errors")
    table.add_column("Timestamp")
    table.add_column("Type")
    table.add_column("Message")
    table.add_column("Source")

    for item in items:
        meta = item["metadata"]
        table.add_row(
            meta.get("timestamp", ""),
            meta.get("error_type", ""),
            meta.get("message", "")[:80],
            f"{meta.get('source_file', '')}:{meta.get('line_number', '')}",
        )

    console.print(table)


@app.command()
def incidents(
    limit: int = typer.Option(20, help="Number of recent incidents to list."),
    config: Optional[Path] = typer.Option(None, "--config", "-c", exists=True, help="TOML watch config."),
) -> None:
    """List curated on-call summaries for newly-seen error types."""
    memory = _memory(config)
    items = memory.list_incidents(limit=limit)

    if not items:
        console.print("[yellow]No incidents recorded yet.[/yellow]")
        raise typer.Exit()

    for item in items:
        meta = item["metadata"]
        console.print(
            Panel(
                meta.get("incident_summary", ""),
                title=f"{meta.get('error_type', '')} — {meta.get('timestamp', '')}",
                border_style="yellow",
            )
        )


if __name__ == "__main__":
    app()
