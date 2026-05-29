"""
Paper Ingestion Agent.

Accepts three input types:
  - ArXiv URL (https://arxiv.org/abs/XXXX.XXXXX)
  - Direct PDF URL (any URL ending in .pdf) — downloaded then processed as PDF
  - Local PDF file path or directory of PDFs

Paper ID resolution (in priority order):
  1. ArXiv ID extracted from URL
  2. ArXiv ID embedded in PDF text (arXiv:XXXX.XXXXX pattern)
  3. ArXiv search by extracted title — if confident match, use that ID
  4. Deterministic local ID: local-{sha256(normalized_title+first_author)[:12]}

Deduplication: before creating a Notion record, checks if the resolved
paper_id already exists. If so, skips and reports as duplicate.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import arxiv
import httpx
from pypdf import PdfReader
from pydantic import BaseModel

from agents.base import BaseAgent
from llm_clients import make_client
from models.paper import PaperMetadata
from notion.client import NotionClient
from notion.schema import Props, Status, prop_title, prop_text, prop_date, prop_url, prop_select


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches arXiv IDs in URLs: /abs/2310.01234  /pdf/2310.01234v2
_ARXIV_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE
)

# Matches embedded arXiv IDs in PDF text: arXiv:2310.01234 or arXiv:2310.01234v2
_ARXIV_EMBEDDED_RE = re.compile(
    r"(?:arXiv|arxiv)\s*:\s*(\d{4}\.\d{4,5}(?:v\d+)?)"
)

# Strip version suffix from arXiv IDs
_VERSION_RE = re.compile(r"v\d+$")

# URLs that serve a PDF directly but don't end with ".pdf"
# e.g. https://openreview.net/pdf?id=Zy4uFzMviZ
#      https://openreview.net/pdf/Zy4uFzMviZ
_PDF_URL_RE = re.compile(
    r"openreview\.net/pdf",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# LLM output model (used only when PDF has no arXiv ID and arXiv search fails)
# ---------------------------------------------------------------------------

class PDFMetadataExtraction(BaseModel):
    """Structured metadata extracted from PDF text by an LLM."""
    title: str
    authors: list[str]
    abstract: str
    published_year: int | None = None
    venue: str | None = None


# ---------------------------------------------------------------------------
# Ingestion result
# ---------------------------------------------------------------------------

class IngestResult(BaseModel):
    metadata: PaperMetadata
    notion_page_id: str
    notion_url: str
    status: Literal["created", "duplicate", "error"]
    message: str


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class PaperIngestionAgent(BaseAgent):

    def __init__(self, notion: NotionClient) -> None:
        super().__init__(instruction_file=None)  # No per-agent instruction file needed
        self.notion = notion
        self._arxiv_client = arxiv.Client()
        # LLM client for PDF metadata extraction — only called as last resort.
        # Backend (API vs Claude Code / Codex CLI) is driven by config.clients.ingestion.
        self._llm_agent = make_client(
            agent_key="ingestion",
            output_type=PDFMetadataExtraction,
            system_prompt=(
                "You are a precise academic metadata extractor. "
                "Given the first few pages of an academic paper, extract the title, "
                "authors, abstract, publication year, and venue. "
                "Be exact — do not paraphrase the title or abstract. "
                "If a field is not present, omit it."
            ),
        )

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def ingest(
        self,
        url: str | None = None,
        pdf_path: str | None = None,
    ) -> list[IngestResult]:
        """
        Dispatch to the correct handler based on input type.
        Returns a list (directory input may yield multiple results).
        """
        if url:
            if _ARXIV_URL_RE.search(url):
                return [self._ingest_arxiv_url(url)]
            elif url.lower().endswith(".pdf") or _PDF_URL_RE.search(url):
                return [self._ingest_pdf_url(url)]
            else:
                # Try arXiv anyway (handles bare IDs or other arXiv URL formats)
                return [self._ingest_arxiv_url(url)]

        if pdf_path:
            path = Path(pdf_path)
            if path.is_dir():
                return self._ingest_directory(path)
            elif path.is_file():
                return [self._ingest_local_pdf(path)]
            else:
                return [IngestResult(
                    metadata=PaperMetadata(paper_id="", title="", authors=[], abstract=""),
                    notion_page_id="",
                    notion_url="",
                    status="error",
                    message=f"Path does not exist: {pdf_path}",
                )]

        return [IngestResult(
            metadata=PaperMetadata(paper_id="", title="", authors=[], abstract=""),
            notion_page_id="",
            notion_url="",
            status="error",
            message="No input provided. Use --url or --pdf.",
        )]

    # ------------------------------------------------------------------
    # ArXiv URL
    # ------------------------------------------------------------------

    def _ingest_arxiv_url(self, url: str) -> IngestResult:
        arxiv_id = self._extract_arxiv_id_from_url(url)
        if not arxiv_id:
            return IngestResult(
                metadata=PaperMetadata(paper_id="", title="", authors=[], abstract=""),
                notion_page_id="",
                notion_url="",
                status="error",
                message=f"Could not extract arXiv ID from URL: {url}",
            )
        metadata = self._fetch_arxiv_by_id(arxiv_id)
        if metadata is None:
            return IngestResult(
                metadata=PaperMetadata(paper_id=arxiv_id, title="", authors=[], abstract=""),
                notion_page_id="",
                notion_url="",
                status="error",
                message=f"arXiv ID {arxiv_id} not found on arxiv.org",
            )
        return self._save_to_notion(metadata)

    # ------------------------------------------------------------------
    # PDF URL (download then process as local PDF)
    # ------------------------------------------------------------------

    def _ingest_pdf_url(self, url: str) -> IngestResult:
        tmp_path = self._download_pdf(url)
        if tmp_path is None:
            return IngestResult(
                metadata=PaperMetadata(paper_id="", title="", authors=[], abstract=""),
                notion_page_id="",
                notion_url="",
                status="error",
                message=f"Failed to download PDF from {url}",
            )
        result = self._ingest_local_pdf(tmp_path, source_url=url)
        tmp_path.unlink(missing_ok=True)
        return result

    # Helper: resolve a PDF URL to ensure it points directly to the PDF file.
    # ArXiv abstract URLs are converted to their PDF counterparts.
    @staticmethod
    def _resolve_pdf_url(metadata: PaperMetadata) -> str | None:
        """Return a direct PDF download URL if one can be derived."""
        if metadata.pdf_url:
            return metadata.pdf_url
        if metadata.arxiv_url and metadata.paper_id:
            return f"https://arxiv.org/pdf/{metadata.paper_id}.pdf"
        return None

    # ------------------------------------------------------------------
    # Local PDF (single file)
    # ------------------------------------------------------------------

    def _ingest_local_pdf(self, path: Path, source_url: str | None = None) -> IngestResult:
        # 1. Extract text: page 1 only for ID detection, 3 pages for LLM metadata
        page1_text = self._extract_pdf_text(path, max_pages=1)
        full_text = self._extract_pdf_text(path, max_pages=3)

        # 2. Try to find an embedded arXiv ID — only in page 1 header/footer
        #    to avoid matching arXiv IDs cited in references on later pages.
        arxiv_id = self._extract_arxiv_id_from_page1(page1_text)
        if arxiv_id:
            metadata = self._fetch_arxiv_by_id(arxiv_id)
            if metadata:
                if source_url:
                    metadata = metadata.model_copy(update={"pdf_url": source_url})
                return self._save_to_notion(metadata, pdf_path=path)

        # 3. Use LLM to extract metadata from PDF text
        llm_meta = self._extract_metadata_with_llm(full_text)

        # 4. Try arXiv search by title to get a canonical ID
        arxiv_meta = self._search_arxiv_by_title(llm_meta.title, llm_meta.authors)
        if arxiv_meta:
            if source_url:
                arxiv_meta = arxiv_meta.model_copy(update={"pdf_url": source_url})
            return self._save_to_notion(arxiv_meta, pdf_path=path)

        # 5. Fallback: build metadata from LLM extraction with a local ID
        local_id = self._make_local_id(llm_meta.title, llm_meta.authors)
        pub_date: date | None = None
        if llm_meta.published_year:
            pub_date = date(llm_meta.published_year, 1, 1)

        metadata = PaperMetadata(
            paper_id=local_id,
            title=llm_meta.title,
            authors=llm_meta.authors,
            published_date=pub_date,
            abstract=llm_meta.abstract,
            arxiv_url=None,
            pdf_url=source_url or str(path.resolve()),
        )
        return self._save_to_notion(metadata, pdf_path=path)

    # ------------------------------------------------------------------
    # Directory
    # ------------------------------------------------------------------

    def _ingest_directory(self, path: Path) -> list[IngestResult]:
        pdf_files = sorted(path.glob("*.pdf"))
        if not pdf_files:
            return [IngestResult(
                metadata=PaperMetadata(paper_id="", title="", authors=[], abstract=""),
                notion_page_id="",
                notion_url="",
                status="error",
                message=f"No PDF files found in directory: {path}",
            )]
        return [self._ingest_local_pdf(f) for f in pdf_files]

    # ------------------------------------------------------------------
    # arXiv helpers
    # ------------------------------------------------------------------

    def _fetch_arxiv_by_id(self, arxiv_id: str) -> PaperMetadata | None:
        search = arxiv.Search(id_list=[arxiv_id])
        results = list(self._arxiv_client.results(search))
        if not results:
            return None
        return self._arxiv_result_to_metadata(results[0])

    def _search_arxiv_by_title(
        self, title: str, authors: list[str]
    ) -> PaperMetadata | None:
        """
        Search arXiv by title. Returns a match only when the top result's
        title is highly similar to the extracted title (case-insensitive,
        ignoring punctuation) to avoid false positives.
        """
        query = f'ti:"{title}"'
        search = arxiv.Search(query=query, max_results=5)
        results = list(self._arxiv_client.results(search))

        normalized_query = _normalize_title(title)
        for result in results:
            if _normalize_title(result.title) == normalized_query:
                return self._arxiv_result_to_metadata(result)

        # Looser match: query title is a substring of arXiv title or vice versa
        for result in results:
            norm = _normalize_title(result.title)
            if normalized_query in norm or norm in normalized_query:
                return self._arxiv_result_to_metadata(result)

        return None

    @staticmethod
    def _arxiv_result_to_metadata(result: arxiv.Result) -> PaperMetadata:
        # entry_id: "http://arxiv.org/abs/2310.01234v2"
        raw_id = result.entry_id.split("/")[-1]
        clean_id = _VERSION_RE.sub("", raw_id)
        return PaperMetadata(
            paper_id=clean_id,
            title=result.title,
            authors=[a.name for a in result.authors],
            published_date=result.published.date() if result.published else None,
            abstract=result.summary.replace("\n", " "),
            arxiv_url=f"https://arxiv.org/abs/{clean_id}",
            pdf_url=result.pdf_url,
        )

    # ------------------------------------------------------------------
    # LLM metadata extraction
    # ------------------------------------------------------------------

    def _extract_metadata_with_llm(self, text: str) -> PDFMetadataExtraction:
        # Limit text to ~3000 chars — enough for title/authors/abstract
        truncated = text[:3000]
        result = self._llm_agent.run_sync(
            f"Extract metadata from this paper text:\n\n{truncated}"
        )
        return result.output

    # ------------------------------------------------------------------
    # PDF text extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _download_pdf(url: str) -> Path | None:
        """
        Download a PDF from any URL to a named temp file.
        Returns the Path on success, None on failure (non-fatal).
        The caller is responsible for deleting the file after use.
        """
        try:
            with httpx.Client(
                timeout=60.0,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/pdf,*/*",
                },
            ) as http:
                resp = http.get(url)
                resp.raise_for_status()
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(resp.content)
                return Path(tmp.name)
        except Exception:
            return None

    @staticmethod
    def _extract_pdf_text(path: Path, max_pages: int = 3) -> str:
        try:
            reader = PdfReader(str(path))
            pages = reader.pages[:max_pages]
            return "\n".join(page.extract_text() or "" for page in pages)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # ID extraction and generation
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_arxiv_id_from_url(url: str) -> str | None:
        match = _ARXIV_URL_RE.search(url)
        if match:
            return _VERSION_RE.sub("", match.group(1))
        return None

    @staticmethod
    def _extract_arxiv_id_from_page1(page1_text: str) -> str | None:
        """
        Search for an embedded arXiv ID only in the header and footer regions
        of page 1. The arXiv watermark stamp (e.g. 'arXiv:2310.01234v2 [cs.LG]')
        always appears at the very top or bottom of page 1. Restricting the
        search to these regions avoids false matches against cited arXiv IDs
        that may also appear on page 1 in an early related-work mention.
        """
        header = page1_text[:300]
        footer = page1_text[-300:] if len(page1_text) > 300 else ""
        for region in (header, footer):
            match = _ARXIV_EMBEDDED_RE.search(region)
            if match:
                return _VERSION_RE.sub("", match.group(1))
        return None

    @staticmethod
    def _extract_arxiv_id_from_text(text: str) -> str | None:
        match = _ARXIV_EMBEDDED_RE.search(text)
        if match:
            return _VERSION_RE.sub("", match.group(1))
        return None

    @staticmethod
    def _make_local_id(title: str, authors: list[str]) -> str:
        """Deterministic ID for non-arXiv papers based on title + first author."""
        first_author = authors[0] if authors else ""
        key = _normalize_title(title) + "|" + first_author.lower().strip()
        digest = hashlib.sha256(key.encode()).hexdigest()[:12]
        return f"local-{digest}"

    # ------------------------------------------------------------------
    # Notion write (with deduplication)
    # ------------------------------------------------------------------

    def _save_to_notion(self, metadata: PaperMetadata, pdf_path: Path | None = None) -> IngestResult:
        # Deduplication check
        existing = self.notion.get_paper_by_paper_id(metadata.paper_id)
        if existing:
            return IngestResult(
                metadata=metadata,
                notion_page_id=existing["id"],
                notion_url=existing.get("url", ""),
                status="duplicate",
                message=f"Paper already exists: '{metadata.title}' (id={metadata.paper_id})",
            )

        properties = {
            Props.NAME: prop_title(metadata.title),
            Props.PAPER_ID: prop_text(metadata.paper_id),
            Props.AUTHORS: prop_text(", ".join(metadata.authors)),
            Props.PROCESSED_DATE: prop_date(date.today()),
            Props.ABSTRACT: prop_text(metadata.abstract),
            Props.STATUS: prop_select(Status.UNPROCESSED),
        }
        if metadata.published_date:
            properties[Props.PUBLISHED_DATE] = prop_date(metadata.published_date)
        if metadata.arxiv_url:
            properties[Props.ARXIV_URL] = prop_url(metadata.arxiv_url)
        if metadata.pdf_url:
            properties[Props.PDF_URL] = prop_url(metadata.pdf_url)

        page = self.notion.create_paper(properties)
        page_id = page["id"]
        metadata = metadata.model_copy(update={"notion_page_id": page_id})

        # Attach the PDF to the page so it's accessible from any machine.
        # Priority: external URL block (permanent) > file upload (local PDFs).
        resolved_pdf_url = self._resolve_pdf_url(metadata)
        self.notion.attach_pdf_to_page(
            page_id,
            pdf_url=resolved_pdf_url,
            pdf_path=pdf_path if not resolved_pdf_url else None,
        )

        self.notion.append_to_log(
            f"Ingested: '{metadata.title}' (paper_id={metadata.paper_id})"
        )

        return IngestResult(
            metadata=metadata,
            notion_page_id=page_id,
            notion_url=page.get("url", ""),
            status="created",
            message=f"Created: '{metadata.title}' (id={metadata.paper_id})",
        )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and extra whitespace for fuzzy matching."""
    t = title.lower()
    t = re.sub(r"[^\w\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()
