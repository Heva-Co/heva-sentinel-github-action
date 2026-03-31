# Heva Code Sentinel

Reusable GitHub Action that performs AI-powered code reviews using Claude and posts findings to Google Chat.

Triggered on every push to `dev`, it analyzes the commit diff and posts a summary card with a threaded detail reply.

## Usage

Add this workflow to any repo at `.github/workflows/code-sentinel.yml`:

```yaml
name: Heva Code Sentinel

on:
  push:
    branches: [dev]

jobs:
  sentinel-review:
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 2

      - uses: heva-care/heva-sentinel-actions@v1
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          google-chat-webhook: ${{ secrets.GOOGLE_CHAT_WEBHOOK }}
          repo-type: "Go backend"
          repo-stack: "Go, PostgreSQL, GORM, Gin, GCP"
          review-focus: "API contracts, DB transactions, error handling"
```

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `anthropic-api-key` | Yes | — | Anthropic API key |
| `google-chat-webhook` | Yes | — | Google Chat webhook URL |
| `repo-type` | Yes | — | Project type (e.g. `Go backend`, `React/Next.js app`) |
| `repo-stack` | Yes | — | Tech stack (e.g. `Go, PostgreSQL, Gin`) |
| `review-focus` | Yes | — | What the reviewer should focus on |
| `claude-model` | No | `claude-sonnet-4-6` | Claude model to use |
| `diff-max-chars` | No | `12000` | Max diff characters sent to Claude |

## How It Works

1. Gets the `git diff` between `HEAD~1` and `HEAD`
2. Sends the diff + repo context to Claude for review
3. Claude returns structured findings (security, reliability, architecture, performance, quality)
4. Posts a summary card to Google Chat with verdict: **LGTM**, **NEEDS_ATTENTION**, or **CRITICAL**
5. Posts detailed findings as a thread reply

### Known Issues Tracking

Repos can optionally maintain a `.github/sentinel/known_issues.json` file:

```json
[
  {
    "id": "IA-001",
    "severity": "high",
    "title": "Race condition in auth middleware",
    "file_hint": "middleware/auth.go"
  }
]
```

When Claude detects that a commit fixes a known issue, it automatically removes it from the file and commits the update.

## Secrets Setup

Configure these at the org level or per-repo:

- **`ANTHROPIC_API_KEY`** — Get one at [console.anthropic.com](https://console.anthropic.com)
- **`GOOGLE_CHAT_WEBHOOK`** — Create via Google Chat space > Apps & integrations > Webhooks

## Running Tests

```bash
pip install pytest anthropic requests
pytest tests/ -v
```

## Project Structure

```
heva-sentinel-actions/
├── action.yml              # Composite action definition
├── sentinel_review.py      # Main review script
├── requirements.txt        # Python dependencies
├── tests/
│   └── test_sentinel.py    # Unit tests (26 tests)
└── workflows/
    └── example-usage.yml   # Example workflow to copy into repos
```
