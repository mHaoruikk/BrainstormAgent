# Ingestion Agent

The Ingestion Agent is the entry point of the pipeline. It takes a reference to a paper (a URL, a PDF file, or a folder of PDFs), resolves it into a structured metadata record, and creates a row in the Notion Research Papers database. All downstream agents operate on records that this agent created.

---

## Inputs

The agent accepts exactly one of the following per call. From the CLI, use `python cli.py add-paper`.

### 1. ArXiv URL (`--url`)

Any URL pointing to an arXiv abstract or PDF page.

```
https://arxiv.org/abs/2310.01234
https://arxiv.org/abs/2310.01234v2
https://arxiv.org/pdf/2310.01234.pdf
```

The arXiv ID is extracted from the URL by regex. The `arxiv` Python library then fetches the full metadata (title, authors, abstract, published date, PDF link) directly from the arXiv API. No LLM call is made.

### 2. Direct PDF URL (`--url`, URL ending in `.pdf`)

Any publicly accessible URL that resolves directly to a PDF file.

```
https://proceedings.mlr.press/v202/paper.pdf
https://openreview.net/pdf?id=xxxxx
```

The PDF is downloaded to a temporary file, processed the same way as a local PDF (see below), and the temp file is deleted afterward. The original URL is stored in the `PDF URL` database column.

### 3. Local PDF file (`--pdf /path/to/paper.pdf`)

A path to a single PDF file on disk. The agent:

1. Extracts text from the first 3 pages using `pypdf`
2. Scans for an embedded arXiv ID (pattern `arXiv:XXXX.XXXXX` in headers/footers)
3. If found → fetches full metadata from arXiv (same as input type 1)
4. If not found → calls an LLM to extract title, authors, abstract, publication year
5. Searches arXiv by the extracted title to find a canonical arXiv ID
6. If arXiv match found → uses that metadata (authoritative)
7. If no match → uses LLM-extracted metadata with a locally generated ID

### 4. Local PDF directory (`--pdf /path/to/folder/`)

A path to a directory. The agent globs all `*.pdf` files in the directory (non-recursive, sorted alphabetically) and processes each one independently as in input type 3. Returns one result per file.

---

## Paper ID Resolution

The Paper ID is the deduplication key. The same paper uploaded twice via different inputs must resolve to the same ID. Resolution is attempted in priority order:

| Priority | Source | ID format | Example |
|---|---|---|---|
| 1 | ArXiv URL regex | `XXXX.XXXXX` | `2310.01234` |
| 2 | Embedded arXiv ID in PDF text | `XXXX.XXXXX` | `2310.01234` |
| 3 | arXiv title search (exact or substring match) | `XXXX.XXXXX` | `2310.01234` |
| 4 | Deterministic local hash | `local-{sha256[:12]}` | `local-a3f9c812b4e1` |

The local hash is computed as `sha256(normalized_title + "|" + first_author)[:12]`. Title normalization strips punctuation and lowercases. This means the same paper (same title and first author) will always generate the same local ID, even when uploaded from different file paths on different days.

Version suffixes (`v1`, `v2`) are always stripped — `2310.01234v2` is stored as `2310.01234`.

---

## Deduplication

Before writing to Notion, the agent queries the database for an existing record with the same Paper ID. If found:

- The new record is **not** created
- The CLI prints a yellow warning: `⚠ Paper already exists: 'Title' (id=XXXX.XXXXX)`
- The existing Notion URL is returned so you can navigate to it

This is safe to run repeatedly. Adding the same paper 10 times will create only 1 Notion record.

---

## Outputs

### Notion database record

A new row is created in the Research Papers database with the following columns populated:

| Column | Source |
|---|---|
| `Name` | Paper title |
| `Paper ID` | Resolved ID (arXiv or local hash) |
| `Authors` | Comma-separated author list |
| `Published Date` | From arXiv metadata or LLM extraction |
| `Processed Date` | Today's date (when the agent ran) |
| `Abstract` | Full abstract text |
| `ArXiv URL` | `https://arxiv.org/abs/{id}` (if known) |
| `PDF URL` | PDF download link or original local path |
| `Status` | `Unprocessed` (always; filter agent reads this) |

All other columns (`Pass Initial Filter`, `Filter Reasoning`, ratings, etc.) are left empty — they are filled by downstream agents.

### PDF attachment

After creating the database record, the agent attaches the original PDF to the Notion page so that downstream agents (filter, brainstorm) can access it from any machine — no reliance on local file paths.

| Source | Attachment method |
|---|---|
| ArXiv URL | External PDF block pointing to `https://arxiv.org/pdf/{id}.pdf` |
| Direct PDF URL | External PDF block pointing to the original URL |
| Local PDF (with arXiv match) | External PDF block pointing to the arXiv PDF URL |
| Local PDF (no arXiv match) | Uploaded via Notion File Uploads API; falls back to storing the local path as text if upload fails |

The PDF appears as an inline viewer on the Notion page. Downstream agents retrieve it via `NotionClient.get_pdf_url_from_page()`, which scans the page's child blocks for a PDF or file attachment and returns its URL. If no attachment is found, agents fall back to the `PDF URL` database property.

### Agent Log entry

A timestamped line is appended to the Notion Agent Logs page:
```
[2026-04-03 14:22 UTC] Ingested: 'Paper Title' (paper_id=2310.01234)
```

### `IngestResult` object (Python API)

When calling the agent programmatically, `agent.ingest(...)` returns a `list[IngestResult]`:

```python
class IngestResult(BaseModel):
    metadata: PaperMetadata      # all resolved metadata fields
    notion_page_id: str          # Notion page UUID
    notion_url: str              # direct link to the Notion page
    status: "created" | "duplicate" | "error"
    message: str                 # human-readable summary
```

---

## LLM Usage

The LLM is called **only** when processing a PDF that has no embedded arXiv ID and for which arXiv title search returns no match. This is the minority case (most academic PDFs on arXiv contain an embedded ID).

- **Model:** configured via `models.ingestion` in `config.yaml` (default: `openai:gpt-4o-mini`)
- **Input:** first ~3000 characters of PDF text
- **Output:** structured `PDFMetadataExtraction` (title, authors, abstract, year, venue)
- **Cost:** minimal — this is the cheapest model in the pipeline and only reads a few pages

---

## CLI Examples

```bash
# ArXiv URL
python cli.py add-paper --url "https://arxiv.org/abs/2404.16130"

# Direct PDF URL
python cli.py add-paper --url "https://proceedings.mlr.press/v202/some-paper.pdf"

# Local PDF
python cli.py add-paper --pdf "./papers/my_paper.pdf"

# Directory of PDFs
python cli.py add-paper --pdf "./papers/"

# Verify results
python cli.py list
python cli.py list --status "Unprocessed"
```

---

## Manually Adding a PDF File

If a paper already exists in the Notion database and you only want to attach or repair its PDF,
use `attach-pdf` instead of `add-paper`.

The command first finds the existing paper in Notion using:

1. `Paper ID` when the input is an arXiv ID or arXiv URL
2. exact match on the `ArXiv URL` column
3. exact match on the `PDF URL` column

### Attach from arXiv ID or URL

```bash
python cli.py attach-pdf 2404.16130
python cli.py attach-pdf "https://arxiv.org/abs/2404.16130"
python cli.py attach-pdf "https://example.com/paper.pdf"
```

For arXiv inputs, the command first tries `https://arxiv.org/pdf/<id>.pdf`. If that does not work,
it falls back to the existing `PDF URL` column on the matched Notion page when available.

### Attach a local PDF file to an existing paper

```bash
python cli.py attach-pdf 2404.16130 --pdf-file "./papers/my_copy.pdf"
python cli.py attach-pdf "https://arxiv.org/abs/2404.16130" --pdf-file "./papers/my_copy.pdf"
```

When `--pdf-file` is provided, the command tries to upload the local PDF to Notion first. If the
upload fails, it falls back to the derived arXiv PDF URL and then to the page's `PDF URL` property.

This command does not create a new paper row. It only attaches a PDF to an existing paper page.

---

## Error Handling

| Situation | Behavior |
|---|---|
| arXiv ID extracted but not found on arXiv | `status=error`, message explains the ID |
| PDF URL download fails (network error, 404) | `status=error`, message includes HTTP status |
| PDF text extraction fails (corrupt/scanned PDF) | Empty text returned; LLM receives empty input; likely produces poor metadata |
| Directory has no `.pdf` files | `status=error`, single result with message |
| Path does not exist | `status=error`, message includes the path |

Errors in one file within a directory batch do not abort the remaining files. Each file gets its own `IngestResult`.

---

## Future Features

These are not implemented. They are recorded here as design notes for later.

**Semantic deduplication**
The current deduplication is exact: same `paper_id` = same paper. A future improvement would embed paper abstracts and check cosine similarity against existing records. Papers with slightly different titles (e.g., workshop version vs. full version) or different arXiv IDs but essentially the same content would be flagged for human review rather than silently creating a duplicate.

**Semantic Scholar / OpenAlex as fallback**
When a paper has no arXiv ID and arXiv title search fails (e.g., a published NeurIPS or ACL paper not posted on arXiv), the agent could fall back to Semantic Scholar or OpenAlex APIs to retrieve canonical metadata and a DOI-based ID. This would reduce reliance on LLM extraction for non-arXiv papers.

**Batch ingestion from a BibTeX or `.ris` file**
Researchers often maintain a BibTeX file of papers they want to read. The agent could accept a `.bib` file and ingest all entries, resolving each to an arXiv ID where possible. This would allow importing an entire reading list in one command.

**Recursive directory scanning**
Currently the directory mode only scans the top-level folder (non-recursive). A `--recursive` flag could enable scanning nested subdirectories, useful for organized paper libraries.

**Automatic tag suggestion**
After ingestion, an LLM call could suggest multi-select tags (e.g., `RAG`, `RLHF`, `causal graph`, `alignment`) based on the abstract. These would be written to the `Tags` column as suggestions for the researcher to confirm or remove.

**Preprint version tracking**
ArXiv papers are versioned. The agent currently strips version suffixes and always stores the base ID. A future improvement could track the latest version number and notify the researcher when a paper they have in the database gets a new revision (e.g., after peer review).

**Google Scholar / ACM / IEEE ingestion**
Accept URLs from other academic sources (ACL Anthology, IEEE Xplore, ACM DL) and extract metadata from their HTML pages, falling back to PDF download if structured metadata is not available.
