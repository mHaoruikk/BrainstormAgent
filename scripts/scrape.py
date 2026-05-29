from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_PAGE_URL = (
    "https://papercopilot.com/paper-list/iclr-paper-list/iclr-2025-paper-list/"
)
ACCEPTED_STATUSES = {"oral", "spotlight", "poster"}
TITLE_PATTERN = re.compile(
    r"(?P<venue>[A-Za-z0-9]+)\s+(?P<year>\d{4})\s+Accepted Paper List",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(
    r"/(?P<venue>[a-z0-9]+)-paper-list/(?P=venue)-(?P<year>\d{4})-paper-list/?$",
    re.IGNORECASE,
)


class PaperCopilotPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_h1 = False
        self._current_href: str | None = None
        self._current_link_text: list[str] = []
        self.h1_chunks: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "h1":
            self._in_h1 = True
        elif tag == "a":
            self._current_href = attrs_dict.get("href")
            self._current_link_text = []

    def handle_data(self, data: str) -> None:
        if self._in_h1:
            self.h1_chunks.append(data)
        if self._current_href is not None:
            self._current_link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self._in_h1 = False
        elif tag == "a" and self._current_href is not None:
            text = " ".join(chunk.strip() for chunk in self._current_link_text).strip()
            self.links.append((text, self._current_href))
            self._current_href = None
            self._current_link_text = []

    @property
    def title(self) -> str:
        return " ".join(chunk.strip() for chunk in self.h1_chunks).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch accepted ICLR 2025 papers from Paper Copilot and emit a JSON list "
            "containing title, PDF URL, average rating, and status."
        )
    )
    parser.add_argument("--url", default=DEFAULT_PAGE_URL, help="Paper Copilot page URL.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional output file path. If omitted, JSON is written to stdout.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for each request.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def fetch_text(url: str, description: str, timeout: float) -> str:
    logging.info("Fetching %s: %s", description, url)
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    logging.info("Fetched %s (%s bytes)", description, len(body))
    return body


def parse_page_metadata(page_url: str, html: str) -> tuple[str, str, str]:
    parser = PaperCopilotPageParser()
    parser.feed(html)

    venue, year = extract_venue_and_year(parser.title, page_url)
    repo_url = find_source_repo_url(parser.links)

    logging.info(
        "Parsed page metadata: venue=%s year=%s repo=%s",
        venue,
        year,
        repo_url,
    )
    return venue, year, repo_url


def extract_venue_and_year(title: str, page_url: str) -> tuple[str, str]:
    title_match = TITLE_PATTERN.search(title)
    if title_match:
        return title_match.group("venue").lower(), title_match.group("year")

    url_match = URL_PATTERN.search(urlparse(page_url).path)
    if url_match:
        return url_match.group("venue").lower(), url_match.group("year")

    raise ValueError(
        f"Could not determine venue/year from page title {title!r} or URL {page_url!r}."
    )


def find_source_repo_url(links: list[tuple[str, str]]) -> str:
    for _text, href in links:
        if "github.com/papercopilot/paperlists" in href:
            return href.rstrip("/")

    raise ValueError("Could not find the Paper Copilot source-data GitHub repository link.")


def build_dataset_url(repo_url: str, venue: str, year: str) -> str:
    parsed = urlparse(repo_url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"Unexpected GitHub repository URL: {repo_url}")

    owner, repo = parts[0], parts[1]
    return (
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/"
        f"{venue}/{venue}{year}.json"
    )


def load_dataset(dataset_url: str, timeout: float) -> list[dict[str, Any]]:
    raw_json = fetch_text(dataset_url, "conference dataset", timeout)
    papers = json.loads(raw_json)
    if not isinstance(papers, list):
        raise ValueError("Expected dataset JSON to be a list of paper objects.")
    logging.info("Loaded %s total paper records from the dataset", len(papers))
    return papers


def extract_average_rating(paper: dict[str, Any]) -> float | None:
    rating_avg = paper.get("rating_avg")
    if isinstance(rating_avg, list) and rating_avg:
        first_value = rating_avg[0]
        if isinstance(first_value, (int, float)):
            return float(first_value)

    if isinstance(rating_avg, (int, float)):
        return float(rating_avg)

    rating = paper.get("rating")
    if isinstance(rating, str):
        values = [float(value) for value in rating.split(";") if value.strip()]
        if values:
            return sum(values) / len(values)

    return None


def extract_pdf_url(paper: dict[str, Any]) -> str | None:
    pdf_url = paper.get("pdf")
    if isinstance(pdf_url, str) and pdf_url.strip():
        return pdf_url.strip()

    paper_id = paper.get("id")
    if isinstance(paper_id, str) and paper_id.strip():
        return f"https://openreview.net/pdf?id={paper_id.strip()}"

    return None


def extract_accepted_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    total = len(papers)

    for index, paper in enumerate(papers, start=1):
        if index == 1 or index % 1000 == 0 or index == total:
            logging.info("Processed %s/%s source records", index, total)

        status = str(paper.get("status", "")).strip().lower()
        if status not in ACCEPTED_STATUSES:
            continue

        accepted.append(
            {
                "title": paper.get("title"),
                "pdf_url": extract_pdf_url(paper),
                "average_rating": extract_average_rating(paper),
                "status": status,
            }
        )

    logging.info("Collected %s accepted papers", len(accepted))
    return accepted


def write_output(records: list[dict[str, Any]], output_path: Path | None) -> None:
    payload = json.dumps(records, indent=2, ensure_ascii=False)

    if output_path is None:
        sys.stdout.write(payload)
        sys.stdout.write("\n")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(payload + "\n", encoding="utf-8")
    logging.info("Wrote JSON output to %s", output_path)


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)

    try:
        page_html = fetch_text(args.url, "Paper Copilot page", args.timeout)
        venue, year, repo_url = parse_page_metadata(args.url, page_html)
        dataset_url = build_dataset_url(repo_url, venue, year)
        logging.info("Derived raw dataset URL: %s", dataset_url)
        papers = load_dataset(dataset_url, args.timeout)
        accepted_papers = extract_accepted_papers(papers)
        write_output(accepted_papers, args.output)
        logging.info("Done. Total accepted papers found: %s", len(accepted_papers))
    except Exception as exc:
        logging.exception("Scrape failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
