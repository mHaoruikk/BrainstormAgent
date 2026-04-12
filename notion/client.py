"""
Thin synchronous wrapper around the Notion REST API.

All Notion I/O in the project goes through this class.
We use httpx directly (no notion-client SDK) for minimal dependencies.

Key design decisions:
- All methods raise httpx.HTTPStatusError on API errors (let callers handle).
- Pagination is handled transparently in query_papers().
- Child-page markdown is sent directly to Notion's markdown API.
- The client is a context manager (use `with NotionClient() as n:`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx

from config import settings
from notion.schema import (
    Props,
    Status,
    block_paragraph,
    block_pdf_external,
)

_BASE_URL = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"          # standard API (database, page properties)
_MARKDOWN_VERSION = "2026-03-11"        # markdown API (POST /pages with markdown field, GET /pages/:id/markdown)


class NotionClient:
    def __init__(self) -> None:
        self._http = httpx.Client(
            base_url=_BASE_URL,
            headers={
                "Authorization": f"Bearer {settings.notion_api_token}",
                "Notion-Version": _NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # Connection / metadata
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        """Retrieve database metadata. Raises on auth or not-found errors."""
        resp = self._http.get(f"/databases/{settings.notion_database_id}")
        resp.raise_for_status()
        return resp.json()

    def get_directions_text(self) -> str:
        """Read the Research Directions page and return its plain text."""
        return self._read_page_text(settings.notion_directions_page_id)

    # ------------------------------------------------------------------
    # Database queries
    # ------------------------------------------------------------------

    def query_papers(self, status: str | None = None) -> list[dict]:
        """
        Return all paper pages from the database.
        If status is given, filter to only that pipeline stage.
        Handles Notion pagination automatically.
        """
        payload: dict = {}
        if status:
            payload["filter"] = {
                "property": Props.STATUS,
                "select": {"equals": status},
            }

        results: list[dict] = []
        cursor: str | None = None

        while True:
            if cursor:
                payload["start_cursor"] = cursor
            resp = self._http.post(
                f"/databases/{settings.notion_database_id}/query",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            results.extend(data["results"])
            if not data.get("has_more"):
                break
            cursor = data["next_cursor"]

        return results

    def query_papers_multi_status(self, statuses: list[str]) -> list[dict]:
        """Return papers matching any of the given status values."""
        payload = {
            "filter": {
                "or": [
                    {"property": Props.STATUS, "select": {"equals": s}}
                    for s in statuses
                ]
            }
        }
        results: list[dict] = []
        cursor: str | None = None
        while True:
            if cursor:
                payload["start_cursor"] = cursor
            resp = self._http.post(
                f"/databases/{settings.notion_database_id}/query",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            results.extend(data["results"])
            if not data.get("has_more"):
                break
            cursor = data["next_cursor"]
        return results

    def get_paper_by_paper_id(self, paper_id: str) -> dict | None:
        """Find a paper record by its Paper ID property. Returns None if not found."""
        resp = self._http.post(
            f"/databases/{settings.notion_database_id}/query",
            json={
                "filter": {
                    "property": Props.PAPER_ID,
                    "rich_text": {"equals": paper_id},
                }
            },
        )
        if not resp.is_success:
            raise RuntimeError(
                f"Notion query failed ({resp.status_code}): {resp.text}"
            )
        results = resp.json()["results"]
        return results[0] if results else None

    def get_paper_by_url(self, property_name: str, url: str) -> dict | None:
        """Find a paper record by a URL property such as PDF URL or ArXiv URL."""
        resp = self._http.post(
            f"/databases/{settings.notion_database_id}/query",
            json={
                "filter": {
                    "property": property_name,
                    "url": {"equals": url},
                }
            },
        )
        if not resp.is_success:
            raise RuntimeError(
                f"Notion query failed ({resp.status_code}): {resp.text}"
            )
        results = resp.json()["results"]
        return results[0] if results else None

    def count_papers_by_status(self) -> dict[str, int]:
        """Return a {status: count} dict for all papers in the database."""
        all_papers = self.query_papers()
        counts: dict[str, int] = {s: 0 for s in Status.ALL}
        for page in all_papers:
            sel = page["properties"].get(Props.STATUS, {}).get("select")
            status = sel["name"] if sel else Status.UNPROCESSED
            counts[status] = counts.get(status, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Database writes
    # ------------------------------------------------------------------

    def create_paper(self, properties: dict) -> dict:
        """Create a new row (page) in the Research Papers database."""
        resp = self._http.post(
            "/pages",
            json={
                "parent": {"database_id": settings.notion_database_id},
                "properties": properties,
            },
        )
        if resp.is_error:
            raise RuntimeError(
                f"Notion {resp.status_code} creating page.\n"
                f"Response body: {resp.text}\n"
                f"Properties sent: {list(properties.keys())}"
            )
        resp.raise_for_status()
        return resp.json()

    def update_paper(self, page_id: str, properties: dict) -> dict:
        """Update one or more properties on an existing paper page."""
        resp = self._http.patch(
            f"/pages/{page_id}",
            json={"properties": properties},
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Child pages (sub-documents under each paper)
    # ------------------------------------------------------------------

    def create_child_page(
        self,
        parent_page_id: str,
        title: str,
        markdown: str,
    ) -> dict:
        """
        Create a sub-page under a paper page with the given markdown content.

        Uses Notion-Version 2026-03-11 which is required for the `markdown`
        body parameter on POST /v1/pages.
        """
        payload: dict = {
            "parent": {"page_id": parent_page_id},
            "properties": {
                "title": {"title": [{"text": {"content": title}}]}
            },
        }
        if markdown.strip():
            payload["markdown"] = markdown

        resp = self._http.post(
            "/pages",
            json=payload,
            headers={"Notion-Version": _MARKDOWN_VERSION},
        )
        resp.raise_for_status()
        return resp.json()

    def find_child_page(self, parent_page_id: str, title: str) -> dict | None:
        """
        Find a direct child page by title. Returns the block object or None.
        Only searches the first page of children (100 items); sufficient for
        our use case where each paper has at most ~5 child pages.
        """
        resp = self._http.get(f"/blocks/{parent_page_id}/children")
        resp.raise_for_status()
        for block in resp.json().get("results", []):
            if block.get("type") == "child_page":
                if block["child_page"]["title"] == title:
                    return block
        return None

    def get_child_page_text(self, page_id: str) -> str:
        """Return the plain-text content of a page (first 100 blocks)."""
        return self._read_page_text(page_id)

    def get_child_page_text_by_title(
        self, parent_page_id: str, title: str
    ) -> str | None:
        """
        Convenience: find a child page by title and return its text.
        Returns None if the child page doesn't exist yet.
        """
        child = self.find_child_page(parent_page_id, title)
        if child is None:
            return None
        return self._read_page_text(child["id"])

    # ------------------------------------------------------------------
    # PDF attachment
    # ------------------------------------------------------------------

    def attach_pdf_to_page(
        self,
        page_id: str,
        pdf_url: str | None = None,
        pdf_path: Path | None = None,
    ) -> bool:
        """
        Attach a PDF to a Notion page.

        Strategy (in priority order):
        1. If pdf_url is given → embed as an external PDF block (inline viewer).
        2. If pdf_path is given → upload via the Notion File Uploads API and
           attach as a file block. Falls back to an external PDF block if the
           file also has a resolvable URL, or stores the path as plain text.

        Returns True if a block was written, False otherwise (non-fatal).
        """
        if pdf_url:
            try:
                resp = self._http.patch(
                    f"/blocks/{page_id}/children",
                    json={"children": [block_pdf_external(pdf_url)]},
                )
                resp.raise_for_status()
                return True
            except Exception:
                return False

        if pdf_path:
            # Try Notion File Uploads API first
            if self._upload_file_to_page(page_id, pdf_path):
                return True
            # Fallback: store path as a note so the location is not lost
            try:
                self._http.patch(
                    f"/blocks/{page_id}/children",
                    json={"children": [block_paragraph(f"PDF location: {pdf_path}")]},
                )
                return True
            except Exception:
                return False

        return False

    def _upload_file_to_page(self, page_id: str, file_path: Path) -> bool:
        """
        Upload a local file to Notion via the File Uploads API and attach it
        as a file block on the given page.

        Returns True on success, False on any failure (non-fatal).
        """
        try:
            file_bytes = file_path.read_bytes()
            # File upload requires multipart/form-data — use a separate
            # httpx client to avoid the Content-Type: application/json header
            # on self._http.
            upload_headers = {
                "Authorization": f"Bearer {settings.notion_api_token}",
                "Notion-Version": _NOTION_VERSION,
            }
            with httpx.Client(timeout=60.0) as upload_client:
                resp = upload_client.post(
                    f"{_BASE_URL}/file-uploads",
                    headers=upload_headers,
                    files={"file": ("original.pdf", file_bytes, "application/pdf")},
                )
                resp.raise_for_status()
                file_upload_id = resp.json()["id"]

            # Attach uploaded file as a block on the page
            resp = self._http.patch(
                f"/blocks/{page_id}/children",
                json={"children": [{
                    "type": "file",
                    "file": {
                        "type": "file_upload",
                        "file_upload": {"id": file_upload_id},
                        "name": "original.pdf",
                    },
                }]},
            )
            resp.raise_for_status()
            return True
        except Exception:
            return False

    def get_pdf_url_from_page(self, page_id: str) -> str | None:
        """
        Scan a page's child blocks for a PDF or file attachment and return
        its URL.  Returns None if no PDF block is found.

        Handles both external PDF blocks and Notion-hosted file blocks.
        Note: Notion-hosted file URLs expire after ~1 hour.
        """
        try:
            resp = self._http.get(f"/blocks/{page_id}/children")
            resp.raise_for_status()
            for block in resp.json().get("results", []):
                btype = block.get("type", "")
                if btype == "pdf":
                    inner = block["pdf"]
                    if inner["type"] == "external":
                        return inner["external"]["url"]
                    if inner["type"] == "file":
                        return inner["file"]["url"]
                if btype == "file":
                    inner = block["file"]
                    if inner["type"] == "external":
                        return inner["external"]["url"]
                    if inner["type"] == "file":
                        return inner["file"]["url"]
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def append_to_log(self, message: str) -> None:
        """Append a timestamped entry to the Agent Logs page."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"[{ts}] {message}"
        resp = self._http.patch(
            f"/blocks/{settings.notion_log_page_id}/children",
            json={"children": [block_paragraph(entry)]},
        )
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "NotionClient":
        return self

    def __exit__(self, *_: object) -> None:
        self._http.close()

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_page_text(self, page_id: str) -> str:
        resp = self._http.get(
            f"/pages/{page_id}/markdown",
            headers={"Notion-Version": _MARKDOWN_VERSION},
        )
        resp.raise_for_status()
        return resp.json().get("markdown", "")
