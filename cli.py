"""
BrainstormAgent CLI

Entry point for all pipeline commands.
Each command is a thin wrapper that loads config/context and delegates to an agent.

Usage:
    python cli.py --help
    python cli.py status
    python cli.py add-paper --url "https://arxiv.org/abs/2310.01234"
    python cli.py run-pipeline
"""

from __future__ import annotations

import re
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="brainstorm",
    help="Multi-agent research ideation framework.",
    add_completion=False,
)
console = Console()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command()
def status() -> None:
    """Check Notion connection and show paper counts per pipeline stage."""
    from config import settings, config
    from notion.client import NotionClient
    from notion.schema import Status

    console.print("\n[bold]BrainstormAgent — Connection Check[/bold]\n")

    # 1. Notion connection
    try:
        with NotionClient() as notion:
            db_info = notion.test_connection()
            db_title = db_info.get("title", [{}])[0].get("plain_text", "?")
            console.print(f"[green]✓[/green] Notion connection OK  (database: [cyan]{db_title}[/cyan])")

            # 2. Research Directions page
            try:
                directions = notion.get_directions_text()
                preview = directions[:80].replace("\n", " ") if directions else "(empty)"
                console.print(f"[green]✓[/green] Research Directions page OK  → [dim]{preview}…[/dim]")
            except Exception as exc:
                console.print(f"[yellow]⚠[/yellow]  Research Directions page: {exc}")

            # 3. Paper counts
            counts = notion.count_papers_by_status()

    except Exception as exc:
        console.print(f"[red]✗[/red] Notion connection FAILED: {exc}")
        raise typer.Exit(code=1)

    # 4. Config summary
    console.print(f"[green]✓[/green] Config loaded")
    console.print(f"    Filter model:   [cyan]{config.models.filter}[/cyan]")
    console.print(f"    Brainstorm:     [cyan]{_format_model_list(config.models.brainstorm)}[/cyan]")
    console.print(f"    Critic model:   [cyan]{_format_model_list(config.models.critic)}[/cyan]")
    console.print(f"    Proposal model: [cyan]{config.models.proposal_writer}[/cyan]")

    # 5. Pipeline status table
    console.print()
    table = Table(title="Pipeline Status", show_header=True, header_style="bold magenta")
    table.add_column("Stage", style="dim")
    table.add_column("Count", justify="right")

    for stage in [
        ("Unprocessed", counts.get("Unprocessed", 0)),
        ("Needs Review", counts.get("Needs Review", 0)),
        ("Filter:Pass", counts.get("Filter:Pass", 0)),
        ("Filter:Reject", counts.get("Filter:Reject", 0)),
        ("Brainstorming", counts.get("Brainstorming", 0)),
        ("Critiqued", counts.get("Critiqued", 0)),
        ("Proposal:Drafted", counts.get("Proposal:Drafted", 0)),
        ("Proposal:Rejected", counts.get("Proposal:Rejected", 0)),
        ("Archived", counts.get("Archived", 0)),
    ]:
        table.add_row(stage[0], str(stage[1]))

    console.print(table)
    console.print()


# ---------------------------------------------------------------------------
# add-paper  (stub — full implementation comes with Ingestion Agent)
# ---------------------------------------------------------------------------

@app.command("add-paper")
def add_paper(
    url: str | None = typer.Option(None, "--url", "-u", help="ArXiv URL or direct PDF URL"),
    pdf: str | None = typer.Option(None, "--pdf", "-p", help="Path to a local PDF file or directory"),
) -> None:
    """Add a paper (or directory of PDFs) to the Notion database."""
    if not any([url, pdf]):
        console.print("[red]Provide one of --url or --pdf.[/red]")
        raise typer.Exit(code=1)

    from agents.ingestion import PaperIngestionAgent
    from notion.client import NotionClient

    with NotionClient() as notion:
        agent = PaperIngestionAgent(notion)
        results = agent.ingest(url=url, pdf_path=pdf)

    for result in results:
        if result.status == "created":
            console.print(f"[green]✓[/green] {result.message}")
            if result.notion_url:
                console.print(f"    Notion: {result.notion_url}")
        elif result.status == "duplicate":
            console.print(f"[yellow]⚠[/yellow]  {result.message}")
        else:
            console.print(f"[red]✗[/red] {result.message}")


# ---------------------------------------------------------------------------
# run-filter  (stub)
# ---------------------------------------------------------------------------

@app.command("run-filter")
def run_filter(
    paper_id: str | None = typer.Option(None, "--paper-id", help="Process a specific paper only"),
) -> None:
    """Run the Initial Filter Agent on all Unprocessed papers."""
    from agents.filter_agent import InitialFilterAgent
    from notion.client import NotionClient

    with NotionClient() as notion:
        agent = InitialFilterAgent(notion)
        results = agent.run(paper_id=paper_id)

    if not results:
        console.print("[dim]No Unprocessed papers found.[/dim]")
        return

    for r in results:
        if r.status == "ok":
            icon = {"pass": "[green]✓[/green]", "reject": "[red]✗[/red]", "uncertain": "[yellow]?[/yellow]"}.get(
                r.decision, "[green]✓[/green]"
            )
            console.print(f"{icon} {r.message}")
        else:
            console.print(f"[red]✗[/red] {r.message}")


# ---------------------------------------------------------------------------
# run-brainstorm  (stub)
# ---------------------------------------------------------------------------

@app.command("run-brainstorm")
def run_brainstorm(
    paper_id: str | None = typer.Option(None, "--paper-id", help="Process a specific paper only"),
    rerun: bool = typer.Option(False, "--rerun", help="Re-run on already-Critiqued papers, injecting prior rounds as compressed context"),
) -> None:
    """Run the Brainstorm Pipeline on Filter:Pass or Brainstorming papers."""
    from agents.brainstorm_agent import BrainstormPipeline
    from notion.client import NotionClient

    with NotionClient() as notion:
        pipeline = BrainstormPipeline(notion)
        results = pipeline.run(paper_id=paper_id, rerun=rerun)

    if not results:
        console.print("[dim]No Filter:Pass or Brainstorming papers found.[/dim]")
        return

    for r in results:
        if r.status == "ok":
            icon = {
                "pursue": "[green]✓[/green]",
                "refine": "[yellow]~[/yellow]",
                "drop": "[red]✗[/red]",
            }.get(r.final_recommendation, "[green]✓[/green]")
            console.print(f"{icon} {r.message}")
        elif r.status == "skipped":
            console.print(f"[dim]⊘ {r.message}[/dim]")
        else:
            console.print(f"[red]✗[/red] {r.message}")


# ---------------------------------------------------------------------------
# run-proposal  (stub)
# ---------------------------------------------------------------------------

@app.command("run-proposal")
def run_proposal(
    paper_id: str | None = typer.Option(None, "--paper-id"),
) -> None:
    """Run the Proposal Writer on all Critiqued papers above threshold."""
    console.print("[yellow]Proposal writer not yet implemented.[/yellow]")


# ---------------------------------------------------------------------------
# attach-pdf
# ---------------------------------------------------------------------------

_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?$")
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)


@app.command("attach-pdf")
def attach_pdf(
    source: str = typer.Argument(..., help="ArXiv ID or URL used to locate the paper and PDF"),
    pdf_file: str | None = typer.Option(
        None,
        "--pdf-file",
        help="Optional path to a local PDF file to upload to the existing paper page",
    ),
) -> None:
    """Attach a PDF block to an existing paper page in Notion."""
    from notion.client import NotionClient
    from notion.schema import Props

    arxiv_id = _extract_arxiv_id(source)
    pdf_url = _pdf_url_from_source(source, arxiv_id)
    pdf_path = _validate_local_pdf(pdf_file) if pdf_file else None

    with NotionClient() as notion:
        page = _find_existing_paper(notion, source, arxiv_id)
        if page is None:
            console.print(f"[red]✗[/red] No existing paper matched: {source}")
            raise typer.Exit(code=1)

        props = page["properties"]
        title = _get_title(props)
        page_id = page["id"]

        attached = False
        attempted_url = pdf_url
        attempted_path = pdf_path

        if attempted_path:
            attached = notion.attach_pdf_to_page(page_id, pdf_path=attempted_path)

        if not attached and attempted_url:
            attached = notion.attach_pdf_to_page(page_id, pdf_url=attempted_url)

        if not attached:
            fallback_pdf_url = props.get(Props.PDF_URL, {}).get("url")
            if fallback_pdf_url and fallback_pdf_url != attempted_url:
                attached = notion.attach_pdf_to_page(page_id, pdf_url=fallback_pdf_url)
                if attached:
                    attempted_url = fallback_pdf_url

        if not attached:
            console.print(
                f"[red]✗[/red] Failed to attach PDF for '{title}'. "
                "Tried the local PDF file, the derived arXiv PDF URL, and the page's PDF URL property when available."
            )
            raise typer.Exit(code=1)

    console.print(f"[green]✓[/green] Attached PDF to '{title}'")
    if attempted_path:
        console.print(f"    Local PDF: {attempted_path}")
    if attempted_url:
        console.print(f"    PDF: {attempted_url}")


# ---------------------------------------------------------------------------
# run-pipeline
# ---------------------------------------------------------------------------

@app.command("run-pipeline")
def run_pipeline() -> None:
    """Run all pipeline stages sequentially on eligible papers."""
    console.print("[yellow]Full pipeline not yet implemented.[/yellow]")
    console.print("Run stages individually: run-filter → run-brainstorm → run-critique → run-proposal")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@app.command("list")
def list_papers(
    status: str | None = typer.Option(None, "--status", "-s", help="Filter by pipeline status"),
) -> None:
    """List papers in the database, optionally filtered by status."""
    from notion.client import NotionClient
    from notion.schema import Props

    with NotionClient() as notion:
        papers = notion.query_papers(status=status)

    if not papers:
        console.print("[dim]No papers found.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Paper ID", style="dim", width=14)
    table.add_column("Title")
    table.add_column("Status", width=18)

    for page in papers:
        props = page["properties"]
        paper_id = _get_text(props, Props.PAPER_ID)
        title = _get_title(props)
        sel = props.get(Props.STATUS, {}).get("select")
        status_val = sel["name"] if sel else "—"
        table.add_row(paper_id or "—", title or "—", status_val)

    console.print(table)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_title(props: dict) -> str:
    rich = props.get("Name", {}).get("title", [])
    return "".join(r.get("plain_text", "") for r in rich)


def _get_text(props: dict, key: str) -> str:
    rich = props.get(key, {}).get("rich_text", [])
    return "".join(r.get("plain_text", "") for r in rich)


def _format_model_list(models: list[object]) -> str:
    return ", ".join(
        f"{getattr(model, 'label', '?')}={getattr(model, 'model', '?')}"
        for model in models
    )


def _extract_arxiv_id(value: str) -> str | None:
    match = _ARXIV_URL_RE.search(value)
    if match:
        return match.group(1)
    match = _ARXIV_ID_RE.fullmatch(value.strip())
    if match:
        return match.group(1)
    return None


def _pdf_url_from_source(source: str, arxiv_id: str | None) -> str | None:
    if arxiv_id:
        return f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    if source.lower().endswith(".pdf"):
        return source
    return None


def _validate_local_pdf(pdf_file: str) -> Path:
    path = Path(pdf_file).expanduser()
    if not path.exists():
        raise typer.BadParameter(f"Local PDF does not exist: {pdf_file}")
    if not path.is_file():
        raise typer.BadParameter(f"Local PDF path is not a file: {pdf_file}")
    if path.suffix.lower() != ".pdf":
        raise typer.BadParameter(f"Local PDF path must end with .pdf: {pdf_file}")
    return path.resolve()


def _find_existing_paper(notion: object, source: str, arxiv_id: str | None) -> dict | None:
    from notion.schema import Props

    if arxiv_id:
        page = notion.get_paper_by_paper_id(arxiv_id)
        if page is not None:
            return page
        arxiv_abs_url = f"https://arxiv.org/abs/{arxiv_id}"
        page = notion.get_paper_by_url(Props.ARXIV_URL, arxiv_abs_url)
        if page is not None:
            return page
        arxiv_pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        page = notion.get_paper_by_url(Props.PDF_URL, arxiv_pdf_url)
        if page is not None:
            return page

    page = notion.get_paper_by_url(Props.PDF_URL, source)
    if page is not None:
        return page
    return notion.get_paper_by_url(Props.ARXIV_URL, source)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
