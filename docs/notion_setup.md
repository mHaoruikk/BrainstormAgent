# Notion Setup Guide

## Overview

This guide walks you through setting up the Notion workspace that BrainstormAgent uses. You need to complete this once before running any agent commands.

---

## Step 1: Create a Notion Integration

An integration is a server-side API token that gives BrainstormAgent permission to read and write to your Notion workspace.

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click **"New integration"**
3. Fill in:
   - **Name:** `BrainstormAgent`
   - **Associated workspace:** select your workspace
   - **Type:** Internal
4. Under **Capabilities**, enable:
   - Read content
   - Update content
   - Insert content
5. Click **Save**
6. Copy the **Internal Integration Token** (starts with `secret_...`) — you will need this for your `.env` file

---

## Step 2: Set Up the Workspace Structure

Create the following three pages in your Notion workspace. They must be **top-level pages** (not nested inside anything else), and later you must **share each one with your integration** (Step 3).

### 2a. Research Papers (Database)

1. In Notion, click **"New page"** in the left sidebar
2. Give it the title: `Research Papers`
3. Choose **Table** as the page type
4. You now have a blank database. Add the following properties by clicking **"+ New property"** for each:

| Property Name | Type | Notes |
|---|---|---|
| `Name` | Title | Already exists by default — rename if needed |
| `Paper ID` | Text | ArXiv ID or local UUID |
| `Authors` | Text | Comma-separated |
| `Published Date` | Date | |
| `Processed Date` | Date | |
| `Abstract` | Text | |
| `ArXiv URL` | URL | |
| `PDF URL` | URL | |
| `Status` | Select | Add options: `Unprocessed`, `Filter:Pass`, `Filter:Reject`, `Needs Review`, `Brainstorming`, `Critiqued`, `Proposal:Drafted`, `Proposal:Rejected`, `Archived` |
| `Pass Initial Filter` | Select | Add options: `Yes`, `No`, `Uncertain` |
| `Filter Reasoning` | Text | |
| `Engineering Complexity` | Select | Add options: `Low`, `Medium`, `High` |
| `Causal Relevance` | Select | Add options: `High`, `Medium`, `Low`, `None` |
| `Novelty Rating` | Number | Format: Number |
| `Viability Rating` | Number | Format: Number |
| `Critique Summary` | Text | |
| `Tags` | Multi-select | Leave empty for now; agent will populate |
| `Notes` | Text | For your personal annotations |

> **Tip:** The order of properties doesn't matter for the agents, but for your own viewing comfort, keep `Name`, `Status`, `Pass Initial Filter`, `Novelty Rating`, and `Viability Rating` as the first visible columns.

### 2b. Research Directions (Plain Page)

1. Click **"New page"** in the left sidebar
2. Title: `Research Directions`
3. Leave it empty for now — the system will read this page, and you can write your research inclination here in plain text (or copy from `instructions/research_direction.yaml`)

### 2c. Agent Logs (Plain Page)

1. Click **"New page"** in the left sidebar
2. Title: `Agent Logs`
3. Leave it empty — agents will append run summaries here automatically

---

## Step 3: Share Pages with the Integration

Notion requires you to explicitly grant each page access to your integration.

For **each of the three pages** created above:

1. Open the page
2. Click **"..."** (three dots) in the top-right corner
3. Click **"Connections"** (or **"Add connections"** depending on your Notion version)
4. Search for `BrainstormAgent` and click to add it

> If you don't do this step, the API will return 404 errors even with a valid token.

---

## Step 4: Collect Page and Database IDs

You need the IDs of the three pages for your `.env` file.

### How to find a Notion page ID

**Method A (from the URL):**
1. Open the page in Notion
2. Look at the browser URL: `https://www.notion.so/Your-Page-Title-<32-char-hex-id>`
3. The ID is the 32-character string at the end, optionally with hyphens: `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

**Method B (Share link):**
1. Click **Share** → **Copy link**
2. The link looks like: `https://www.notion.so/xxxxx?pvs=4`
3. The ID is the hex string before the `?`

Collect three IDs:

| Variable | Page |
|---|---|
| `NOTION_DATABASE_ID` | Research Papers (the table) |
| `NOTION_DIRECTIONS_PAGE_ID` | Research Directions |
| `NOTION_LOG_PAGE_ID` | Agent Logs |

---

## Step 5: Configure Your `.env` File

In the project root, copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Then fill in your values:

```bash
# Notion
NOTION_API_TOKEN=YOUR_NOTION_INTEGRATION_TOKEN
NOTION_DATABASE_ID=YOUR_NOTION_DATABASE_ID
NOTION_DIRECTIONS_PAGE_ID=YOUR_NOTION_DIRECTIONS_PAGE_ID
NOTION_LOG_PAGE_ID=YOUR_NOTION_LOG_PAGE_ID

# LLM providers — add only the ones you use
OPENAI_API_KEY=YOUR_OPENAI_API_KEY
ANTHROPIC_API_KEY=YOUR_ANTHROPIC_API_KEY
GOOGLE_API_KEY=YOUR_GOOGLE_API_KEY
```

> **Never commit `.env` to git.** It is already in `.gitignore`.

---

## Step 6: Verify the Setup

Once you have installed the Python dependencies (`pip install -r requirements.txt`), run:

```bash
python cli.py status
```

This command:
- Connects to Notion using your token
- Reads the Research Papers database
- Prints a pipeline status summary (counts per status)

A successful run looks like:

```
Notion connection: OK
Research Papers database: OK (0 papers)
Research Directions page: OK
Agent Logs page: OK

Pipeline status:
  Unprocessed:        0
  Filter:Pass:        0
  ...
```

If you see an error like `APIResponseError: Could not find database`, double-check Step 3 (sharing the page with the integration).

---

## Optional: Create Database Views

Notion lets you create filtered views of the same database. These are useful for reviewing the pipeline at different stages. To create a view:

1. Open the **Research Papers** database
2. Click **"+ Add a view"**
3. Choose **Table**, give it a name, and add a filter

Recommended views:

| View name | Filter |
|---|---|
| `Needs Review` | Status = `Needs Review` |
| `Ready to Brainstorm` | Status = `Filter:Pass` |
| `Ready to Critique` | Status = `Brainstorming` |
| `Final Proposals` | Status = `Proposal:Drafted` |
| `All` | No filter (default) |

---

## Troubleshooting

**`APIResponseError: Could not find database with ID ...`**
→ Check Step 3: the integration must be added as a connection to each page.

**`401 Unauthorized`**
→ Your `NOTION_API_TOKEN` is wrong or expired. Re-copy from [notion.so/my-integrations](https://www.notion.so/my-integrations).

**Properties not found / wrong column name**
→ Property names in Notion are case-sensitive and must match exactly what is in `notion/schema.py`. Re-check Step 2a.

**`Object with ID ... does not exist`**
→ The page ID in `.env` is incorrect. Re-copy from the URL (Step 4).
