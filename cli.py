from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from logscribe.analyzer import OpenAIAnalyzer
from logscribe.memory import ErrorMemory
from logscribe.processor import ErrorProcessor
from logscribe.sampler import LogSampler

load_dotenv()

app = typer.Typer(help="Tail-based log monitoring with RAG-powered error analysis.")
console = Console()


@app.command()
def watch(
    file: Path = typer.Option(..., "--file", "-f", exists=True, help="Log file to tail."),
    buffer_size: int = typer.Option(50, help="Number of context lines to capture per error."),
    top_k: int = typer.Option(5, help="Number of similar past errors to retrieve for analysis."),
    from_start: bool = typer.Option(
        False, help="Tail from the beginning of the file instead of the end."
    ),
) -> None:
    """Tail a log file, capture errors, and auto-analyze each one against past history."""
    sampler = LogSampler(file, buffer_size=buffer_size)
    processor = ErrorProcessor()
    memory = ErrorMemory()
    analyzer = OpenAIAnalyzer()

    console.print(f"[bold green]Watching[/bold green] {file} (buffer={buffer_size} lines)...")

    for chunk in sampler.tail(from_start=from_start):
        event = processor.process(chunk)
        similar = memory.query_similar(event.to_document(), top_k=top_k)

        console.rule(f"[bold red]{event.error_type}[/bold red] @ {event.timestamp}")
        console.print(event.message)

        analysis = analyzer.analyze(event, similar)
        console.print("\n[bold cyan]Root-cause analysis:[/bold cyan]")
        console.print(analysis)

        memory.add(event)


@app.command()
def query(
    description: str = typer.Argument(
        ..., help="Natural-language description of the error to search for."
    ),
    top_k: int = typer.Option(5, help="Number of results to return."),
) -> None:
    """Semantic search over previously captured errors."""
    memory = ErrorMemory()
    results = memory.query_similar(description, top_k=top_k)

    if not results:
        console.print("[yellow]No matching errors found.[/yellow]")
        raise typer.Exit()

    table = Table(title="Similar past errors")
    table.add_column("Timestamp")
    table.add_column("Type")
    table.add_column("Message")
    table.add_column("Distance", justify="right")

    for item in results:
        meta = item["metadata"]
        table.add_row(
            meta.get("timestamp", ""),
            meta.get("error_type", ""),
            meta.get("message", "")[:80],
            f"{item['distance']:.4f}",
        )

    console.print(table)


@app.command()
def history(
    limit: int = typer.Option(20, help="Number of recent errors to list."),
) -> None:
    """List recently captured errors."""
    memory = ErrorMemory()
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


if __name__ == "__main__":
    app()
