#!/usr/bin/env python3
"""
Heva Code Sentinel — AI-powered code review that posts to Google Chat.
Triggered on every push to dev branch.
"""

import os
import subprocess
import json
import requests
import anthropic
from datetime import datetime
from pathlib import Path

# ── Environment ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CHAT_WEBHOOK = os.environ["GOOGLE_CHAT_WEBHOOK"]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
DIFF_MAX_CHARS = int(os.environ.get("DIFF_MAX_CHARS", "12000"))
REPO_NAME = os.environ.get("REPO_NAME", "unknown/repo")
REPO_SHORT = os.environ.get("REPO_SHORT", "unknown")
COMMIT_SHA = os.environ.get("COMMIT_SHA", "unknown")[:8]
COMMIT_MSG = os.environ.get("COMMIT_MSG", "No message")
AUTHOR = os.environ.get("AUTHOR", "Unknown")
BRANCH = os.environ.get("BRANCH", "dev")
COMMIT_URL = os.environ.get("COMMIT_URL", "")
THREAD_KEY = os.environ.get("THREAD_KEY", f"sentinel-{os.environ.get('REPO_SHORT', 'unknown')}-{datetime.utcnow().strftime('%Y-%m-%d')}-1")

# ── Jira integration (OAuth 2.0 service account) ─────────────────────────────
JIRA_CLIENT_ID = os.environ.get("JIRA_CLIENT_ID", "")
JIRA_CLIENT_SECRET = os.environ.get("JIRA_CLIENT_SECRET", "")
JIRA_CLOUD_ID = os.environ.get("JIRA_CLOUD_ID", "")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "")
JIRA_BASE_URL = f"https://api.atlassian.com/ex/jira/{JIRA_CLOUD_ID}" if JIRA_CLOUD_ID else ""
JIRA_ENABLED = all([JIRA_CLIENT_ID, JIRA_CLIENT_SECRET, JIRA_CLOUD_ID, JIRA_PROJECT_KEY])
_jira_access_token = ""


def jira_get_access_token() -> str:
    """Fetch a fresh OAuth 2.0 access token using client credentials grant."""
    global _jira_access_token
    if _jira_access_token:
        return _jira_access_token

    resp = requests.post(
        "https://auth.atlassian.com/oauth/token",
        json={
            "grant_type": "client_credentials",
            "client_id": JIRA_CLIENT_ID,
            "client_secret": JIRA_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code != 200:
        print(f"Jira OAuth token fetch failed: {resp.status_code} {resp.text[:200]}")
        return ""
    _jira_access_token = resp.json().get("access_token", "")
    return _jira_access_token


def jira_headers() -> dict:
    """Return auth headers for Jira API calls."""
    token = jira_get_access_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ── Repo context (passed via action inputs) ──────────────────────────────────
REPO_TYPE = os.environ.get("REPO_TYPE", "Unknown")
REPO_STACK = os.environ.get("REPO_STACK", "Unknown")
REVIEW_FOCUS = os.environ.get("REVIEW_FOCUS", "General code quality, security, reliability")

# ── Model pricing (USD per 1M tokens) ────────────────────────────────────────
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}


def load_known_issues() -> list:
    """Load known issues from .github/sentinel/known_issues.json if it exists."""
    issues_path = Path(".github/sentinel/known_issues.json")
    if issues_path.exists():
        with open(issues_path) as f:
            return json.load(f)
    return []


def get_diff():
    """Get the git diff for the current commit."""
    try:
        stat = subprocess.check_output(
            ["git", "diff", "HEAD~1", "HEAD", "--stat"], text=True
        )
        diff = subprocess.check_output(
            ["git", "diff", "HEAD~1", "HEAD", "--unified=3"], text=True
        )
        if len(diff) > DIFF_MAX_CHARS:
            diff = diff[:DIFF_MAX_CHARS] + f"\n\n... [diff truncated, showing first {DIFF_MAX_CHARS} chars]"
        return stat.strip(), diff.strip()
    except subprocess.CalledProcessError:
        return "Could not get diff", ""


REQUIRED_FIELDS = {"summary", "security", "reliability", "architecture", "performance", "quality", "good_changes", "fixed_issues", "critical_points", "verdict"}
VALID_VERDICTS = {"LGTM", "NEEDS_ATTENTION", "CRITICAL"}


def review_with_claude(stat: str, diff: str, known_issues: list) -> dict:
    """Call Claude API to review the diff and return structured findings."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Separate suppressed (business context) from open issues
    open_issues = [i for i in known_issues if not i.get("suppressed")]
    suppressed_issues = [i for i in known_issues if i.get("suppressed")]

    known_issues_text = ""
    if open_issues:
        known_issues_text = "\n\nKNOWN EXISTING ISSUES TO CHECK IF FIXED:\n"
        for issue in open_issues:
            known_issues_text += f"- [{issue['id']}] ({issue['severity']}) {issue['title']} | hint: {issue.get('file_hint', '')}\n"

    if suppressed_issues:
        known_issues_text += "\n\nSUPPRESSED — INTENTIONAL BUSINESS DECISIONS (DO NOT FLAG):\n"
        for issue in suppressed_issues:
            known_issues_text += f"- {issue['title']}: {issue.get('detail', '')}\n"

    prompt = f"""You are Heva Code Sentinel — a senior engineer reviewing code changes for the Heva healthcare platform.

Repo: {REPO_NAME}
Type: {REPO_TYPE}
Stack: {REPO_STACK}
Review focus: {REVIEW_FOCUS}

Commit: {COMMIT_SHA} by {AUTHOR}
Message: {COMMIT_MSG}

Files changed:
{stat}

Diff:
{diff}
{known_issues_text}

Review this diff and return a JSON object with ONLY these fields:
{{
  "summary": "1-2 sentence summary of what this commit does",
  "security": ["list of security issues found, empty if none"],
  "reliability": ["list of reliability/error handling issues, empty if none"],
  "architecture": ["list of architecture/design concerns, empty if none"],
  "performance": ["list of performance issues, empty if none"],
  "quality": ["list of code quality issues, empty if none"],
  "good_changes": ["list of positive things worth noting, max 2"],
  "fixed_issues": ["list of known issue IDs (e.g. IA-001) that this diff clearly fixes, empty if none"],
  "critical_points": ["list of exact finding strings that caused CRITICAL verdict, copied verbatim from their category, empty if verdict is not CRITICAL"],
  "verdict": "LGTM" | "NEEDS_ATTENTION" | "CRITICAL"
}}

Rules:
- Only report REAL issues visible in the diff, no speculation
- Security issues in healthcare code are CRITICAL (PHI, auth, injection)
- Keep each finding under 15 words
- For fixed_issues: only include an ID if the diff clearly addresses that specific issue
- For critical_points: copy the exact finding string(s) from security/reliability/etc that triggered CRITICAL
- verdict = CRITICAL if any security or data integrity risk
- verdict = NEEDS_ATTENTION if reliability/arch concerns
- verdict = LGTM if clean commit
- Return valid JSON only, no markdown, no explanation
"""

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    # Cost tracking
    pricing = MODEL_PRICING.get(CLAUDE_MODEL, {"input": 3.00, "output": 15.00})
    input_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens
    total_cost = (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing["output"]

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned invalid JSON: {e}\nRaw (truncated): {raw[:200]}")

    missing = REQUIRED_FIELDS - parsed.keys()
    if missing:
        raise ValueError(f"Claude response missing fields: {missing}")

    if parsed["verdict"] not in VALID_VERDICTS:
        parsed["verdict"] = "NEEDS_ATTENTION"

    for field in ["security", "reliability", "architecture", "performance", "quality", "good_changes", "fixed_issues", "critical_points"]:
        if not isinstance(parsed.get(field), list):
            parsed[field] = []

    parsed["_cost"] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_usd": round(total_cost, 6),
        "model": CLAUDE_MODEL,
    }

    return parsed


def verdict_emoji(verdict: str) -> str:
    return {
        "LGTM": "✅",
        "NEEDS_ATTENTION": "⚠️",
        "CRITICAL": "🚨",
    }.get(verdict, "ℹ️")


def build_summary_card(findings: dict) -> dict:
    """Build a compact summary card — one glance view."""
    verdict = findings.get("verdict", "LGTM")
    emoji = verdict_emoji(verdict)
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    counts = {
        ("🔐", "Security"): len(findings.get("security", [])),
        ("⚡", "Reliability"): len(findings.get("reliability", [])),
        ("🏗️", "Architecture"): len(findings.get("architecture", [])),
        ("🚀", "Performance"): len(findings.get("performance", [])),
        ("🧹", "Code Quality"): len(findings.get("quality", [])),
    }
    fixed_ids = findings.get("fixed_issues", [])
    issue_summary = "  ".join(f"{icon} {label}: {n}" for (icon, label), n in counts.items() if n > 0) or "No issues"
    fixed_text = f"  🎉 Fixed: {len(fixed_ids)}" if fixed_ids else ""

    body = (
        f"*Author:* {AUTHOR}  |  *Commit:* <{COMMIT_URL}|{COMMIT_SHA}>\n"
        f"*Message:* {COMMIT_MSG}\n"
        f"*Summary:* {findings.get('summary', 'N/A')}\n\n"
        f"{issue_summary}{fixed_text}\n\n"
        f"_See thread for full details_ 👇"
    )

    return {
        "cardsV2": [{
            "cardId": f"sentinel-{COMMIT_SHA}",
            "card": {
                "header": {
                    "title": f"{emoji} {REPO_SHORT}",
                    "subtitle": f"{verdict}  •  {timestamp}",
                    "imageUrl": "https://cdn-icons-png.flaticon.com/512/2092/2092663.png",
                    "imageType": "CIRCLE",
                },
                "sections": [{"widgets": [{"textParagraph": {"text": body}}]}],
            }
        }]
    }


def build_detail_thread(findings: dict, known_issues: list, jira_links: dict = None) -> str:
    """Build the detailed findings as plain text for the thread reply."""
    verdict = findings.get("verdict", "LGTM")
    emoji = verdict_emoji(verdict)
    lines = [
        f"📋 *Full Review — {REPO_SHORT} @ {COMMIT_SHA}*",
        f"*Verdict:* {emoji} {verdict}",
        f"*Summary:* {findings.get('summary', 'N/A')}\n",
    ]

    severity_emoji = {"CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "🔶", "LOW": "🔵"}

    # Fixed issues — shown only when fixed, then removed from known_issues.json
    fixed_ids = findings.get("fixed_issues", [])
    if fixed_ids and known_issues:
        fixed_map = {i["id"]: i for i in known_issues}
        fixed_lines = []
        for fid in fixed_ids:
            if fid in fixed_map:
                issue = fixed_map[fid]
                sev = severity_emoji.get(issue["severity"], "•")
                fixed_lines.append(f"  {sev} [{fid}] {issue['title']}")
        if fixed_lines:
            lines.append("─────────────────────")
            lines.append("🎉 *Fixed in This Commit* _(won't appear again)_")
            lines.extend(fixed_lines)
            lines.append("")

    # New issues found in this diff
    categories = [
        ("🔐 Security", findings.get("security", [])),
        ("⚡ Reliability", findings.get("reliability", [])),
        ("🏗️ Architecture", findings.get("architecture", [])),
        ("🚀 Performance", findings.get("performance", [])),
        ("🧹 Code Quality", findings.get("quality", [])),
        ("👍 Good Changes", findings.get("good_changes", [])),
    ]

    critical_points = set(findings.get("critical_points", []))
    for label, items in categories:
        if items:
            lines.append(f"*{label}*")
            for item in items:
                prefix = "🚨" if item in critical_points else "  •"
                lines.append(f"{prefix} {item}")
            lines.append("")

    if not any(items for _, items in categories) and not fixed_ids:
        lines.append("No issues found. Clean commit. ✨\n")

    # Still open known issues — always show so team stays aware
    still_open = [i for i in known_issues if i["id"] not in fixed_ids and not i.get("suppressed")]
    if still_open:
        jira_map = jira_links or {}
        lines.append("─────────────────────")
        lines.append("📌 *Still Open — Known Issues*")
        for issue in still_open:
            sev = severity_emoji.get(issue["severity"], "•")
            ticket_keys = jira_map.get(issue["id"], [])
            if ticket_keys:
                jira_text = "  ".join(
                    f"<https://heva-co.atlassian.net/browse/{t}|🎫 {t}>" for t in ticket_keys
                )
                lines.append(f"  {sev} [{issue['id']}] {issue['title']}  →  {jira_text}")
            else:
                lines.append(f"  {sev} [{issue['id']}] {issue['title']}")
        lines.append("")

    # Suppressed issues — intentional business decisions, shown for audit trail
    suppressed = [i for i in known_issues if i.get("suppressed")]
    if suppressed:
        lines.append("─────────────────────")
        lines.append("🔕 *Suppressed — Intentional Business Decisions*")
        for issue in suppressed:
            lines.append(f"  • [{issue['id']}] {issue['title']}")
        lines.append("")

    # Cost footer
    cost = findings.get("_cost", {})
    if cost:
        model_short = cost.get("model", CLAUDE_MODEL).replace("claude-", "").replace("-20251001", "")
        lines.append("─────────────────────")
        lines.append(f"💰 *Review cost:* ${cost['total_usd']:.4f} | {cost['input_tokens']:,} in / {cost['output_tokens']:,} out tokens _({model_short})_")

    return "\n".join(lines)


def auto_persist_critical_issues(findings: dict, known_issues: list) -> bool:
    """Auto-add new CRITICAL findings to known_issues.json so they persist until fixed or suppressed."""
    if findings.get("verdict") != "CRITICAL":
        return False
    critical_points = findings.get("critical_points", [])
    if not critical_points:
        return False

    issues_path = Path(".github/sentinel/known_issues.json")
    existing_titles = {i["title"].lower() for i in known_issues}

    # Determine next auto ID
    repo_prefix = REPO_SHORT.replace("heva-", "").replace("-backend", "").replace("-frontend", "").upper()[:3]
    existing_ids = [i["id"] for i in known_issues if i["id"].startswith(f"{repo_prefix}-AUTO-")]
    next_num = len(existing_ids) + 1

    new_entries = []
    for point in critical_points:
        if point.lower() not in existing_titles:
            new_entries.append({
                "id": f"{repo_prefix}-AUTO-{next_num:03d}",
                "severity": "CRITICAL",
                "category": "security",
                "title": point,
                "file_hint": "",
                "auto_added": True,
                "commit": COMMIT_SHA
            })
            next_num += 1

    if not new_entries:
        return False

    updated = known_issues + new_entries
    issues_path.parent.mkdir(parents=True, exist_ok=True)
    with open(issues_path, "w") as f:
        json.dump(updated, f, indent=2)
    print(f"Auto-persisted {len(new_entries)} CRITICAL finding(s) to known_issues.json")
    return True


def remove_fixed_issues(fixed_ids: list, known_issues: list) -> bool:
    """Remove fixed issues from known_issues.json. Returns True if file was updated."""
    if not fixed_ids:
        return False
    issues_path = Path(".github/sentinel/known_issues.json")
    if not issues_path.exists():
        return False
    remaining = [i for i in known_issues if i["id"] not in fixed_ids or i.get("suppressed")]
    if len(remaining) == len(known_issues):
        return False
    with open(issues_path, "w") as f:
        json.dump(remaining, f, indent=2)
    return True


def commit_and_push_known_issues():
    """Commit the updated known_issues.json back to the repo."""
    try:
        subprocess.run(["git", "config", "user.email", "sentinel@heva.co"], check=True)
        subprocess.run(["git", "config", "user.name", "Heva Code Sentinel"], check=True)
        subprocess.run(["git", "add", ".github/sentinel/known_issues.json"], check=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: sentinel updated known_issues.json"],
            check=True
        )
        subprocess.run(["git", "push"], check=True)
        print("Updated known_issues.json committed and pushed.")
    except subprocess.CalledProcessError as e:
        print(f"Warning: could not commit known_issues update: {e}")


def post_to_google_chat(message: dict, thread_key: str = None, reply: bool = False) -> str:
    """Post a message with retry on 429. Returns the thread name for replies."""
    import time
    url = GOOGLE_CHAT_WEBHOOK
    if thread_key:
        url += f"&threadKey={thread_key}"
        if reply:
            url += "&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

    for attempt in range(4):
        response = requests.post(url, json=message, headers={"Content-Type": "application/json"})
        if response.status_code == 200:
            print("Posted to Google Chat successfully.")
            return response.json().get("thread", {}).get("name", "")
        if response.status_code == 429:
            wait = 2 ** attempt  # 1s, 2s, 4s, 8s
            print(f"Rate limited (429), retrying in {wait}s... (attempt {attempt + 1}/4)")
            time.sleep(wait)
            continue
        print(f"Failed to post to Google Chat: {response.status_code} {response.text}")
        raise Exception("Google Chat post failed")

    print(f"Failed to post to Google Chat after retries: {response.status_code} {response.text}")
    raise Exception("Google Chat post failed after 4 attempts")


def jira_search_recent_bugs(days: int = 7) -> list:
    """Search Jira for recent open bugs in the project."""
    if not JIRA_ENABLED:
        return []

    jql = (
        f"project = {JIRA_PROJECT_KEY} AND issuetype = Bug "
        f"AND status NOT IN (Done, Closed) "
        f"AND created >= -{days}d "
        f"ORDER BY created DESC"
    )
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    payload = {"jql": jql, "maxResults": 20, "fields": ["summary", "description", "status", "key"]}

    try:
        resp = requests.post(url, headers=jira_headers(), json=payload)
        if resp.status_code != 200:
            print(f"Jira search failed: {resp.status_code} {resp.text[:200]}")
            return []
        return resp.json().get("issues", [])
    except Exception as e:
        print(f"Jira search error: {e}")
        return []


def jira_match_bugs_to_findings(bugs: list, findings: dict) -> list:
    """Use Claude to match Jira bugs to CRITICAL findings from this commit."""
    if not bugs or findings.get("verdict") != "CRITICAL":
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    bug_list = "\n".join(
        f"- {b['key']}: {b['fields']['summary']}"
        for b in bugs
    )
    critical_points = findings.get("critical_points", [])
    all_findings = []
    for cat in ["security", "reliability", "architecture", "performance", "quality"]:
        all_findings.extend(findings.get(cat, []))

    findings_text = "\n".join(f"- {f}" for f in all_findings)

    prompt = f"""You are analyzing whether any of these Jira bug tickets could be caused by or related to a recent code commit.

Commit: {COMMIT_SHA} by {AUTHOR}
Message: {COMMIT_MSG}
Repo: {REPO_SHORT}

CRITICAL findings from this commit:
{findings_text}

Open Jira bugs (last 7 days):
{bug_list}

Return a JSON array of matched ticket keys that could plausibly be caused by or related to this commit's changes. Only include tickets where there is a reasonable connection — not speculative.

Return format: {{"matched_tickets": ["TT-779", "TT-771"], "reasoning": {{"TT-779": "one line reason", "TT-771": "one line reason"}}}}

If no matches, return: {{"matched_tickets": [], "reasoning": {{}}}}
Return valid JSON only, no markdown."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        print(f"Jira matching failed: {e}")
        return {"matched_tickets": [], "reasoning": {}}


def jira_comment_on_tickets(match_result: dict, findings: dict):
    """Post a comment on matched Jira tickets with root cause context."""
    if not JIRA_ENABLED or not match_result.get("matched_tickets"):
        return

    headers = jira_headers()

    critical_points = findings.get("critical_points", [])
    findings_text = "\n".join(f"• {p}" for p in critical_points) if critical_points else "See commit diff for details."

    for ticket_key in match_result["matched_tickets"]:
        reason = match_result.get("reasoning", {}).get(ticket_key, "Related to commit changes")

        comment_body = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "🔍 Possible root cause identified by ", "marks": [{"type": "strong"}]},
                            {"type": "text", "text": "Heva Code Sentinel", "marks": [{"type": "strong"}]}
                        ]
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": f"Commit: "},
                            {"type": "text", "text": COMMIT_SHA, "marks": [{"type": "code"}]},
                            {"type": "text", "text": f" by {AUTHOR}"}
                        ]
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": f"Repo: {REPO_SHORT}\nMessage: {COMMIT_MSG}"}
                        ]
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": f"Why this may be related: ", "marks": [{"type": "strong"}]},
                            {"type": "text", "text": reason}
                        ]
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": f"CRITICAL findings:\n{findings_text}"}
                        ]
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": f"Full review: ", "marks": [{"type": "em"}]},
                            {
                                "type": "text",
                                "text": COMMIT_URL,
                                "marks": [{"type": "link", "attrs": {"href": COMMIT_URL}}]
                            }
                        ]
                    }
                ]
            }
        }

        url = f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_key}/comment"
        try:
            resp = requests.post(url, headers=headers, json=comment_body)
            if resp.status_code in (200, 201):
                print(f"Posted comment on {ticket_key}")
            else:
                print(f"Failed to comment on {ticket_key}: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"Error commenting on {ticket_key}: {e}")


def jira_match_known_issues_to_bugs(known_issues: list) -> dict:
    """Match existing known CRITICAL/HIGH issues to open Jira bugs. Returns {issue_id: [ticket_keys]}."""
    if not JIRA_ENABLED:
        return {}

    critical_high = [i for i in known_issues if i.get("severity") in ("CRITICAL", "HIGH") and not i.get("suppressed")]
    if not critical_high:
        return {}

    # Search broader — all open bugs, not just last 7 days
    jql = (
        f"project = {JIRA_PROJECT_KEY} AND issuetype = Bug "
        f"AND status NOT IN (Done, Closed) "
        f"ORDER BY created DESC"
    )
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    payload = {"jql": jql, "maxResults": 50, "fields": ["summary", "status", "key"]}

    try:
        resp = requests.post(url, headers=jira_headers(), json=payload)
        if resp.status_code != 200:
            print(f"Jira known-issue search failed: {resp.status_code}")
            return {}
        bugs = resp.json().get("issues", [])
    except Exception as e:
        print(f"Jira known-issue search error: {e}")
        return {}

    if not bugs:
        return {}

    # Use Claude Sonnet to match known issues to Jira bugs (better precision than Haiku)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    issues_text = "\n".join(
        f"- {i['id']} ({i['severity']}): {i['title']}" + (f" | {i.get('detail', '')}" if i.get('detail') else "")
        for i in critical_high
    )
    bugs_text = "\n".join(f"- {b['key']}: {b['fields']['summary']}" for b in bugs)

    prompt = f"""You are matching known CODE-LEVEL issues (bugs found in source code review) to USER-FACING Jira bug tickets.

A match means: the code issue DIRECTLY causes or could directly cause the user-facing bug described in the Jira ticket.

STRICT RULES:
- Only match when the code issue is a DIRECT root cause of the Jira bug symptom
- Do NOT match based on vague thematic similarity (e.g. "both involve validation" or "both involve failures")
- Do NOT match an issue to a ticket just because they're in the same domain area
- A code issue about nudges should NOT match escalation tickets
- A code issue about input sanitization should NOT match unrelated validation errors
- When in doubt, do NOT include the match

Known code issues:
{issues_text}

Open Jira bugs:
{bugs_text}

Return a JSON object mapping issue IDs to arrays of matched Jira ticket keys.

Return format: {{"IA-009": ["TT-783", "TT-787"]}}
If no confident matches for an issue, omit it entirely. Return valid JSON only, no markdown."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="You are a JSON-only API. Respond with ONLY a valid JSON object. No text before or after. No markdown. No explanation. No reasoning.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Extract JSON robustly in case model wraps it
        if raw.startswith("```"):
            import re
            match = re.search(r'```(?:json)?\s*(.*?)```', raw, re.DOTALL)
            if match:
                raw = match.group(1).strip()
        elif not raw.startswith("{"):
            import re
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                raw = match.group(0)
        result = json.loads(raw.strip())
        print(f"Matched known issues to Jira: {result}")
        return result
    except Exception as e:
        print(f"Jira known-issue matching failed: {e}")
        return {}


def build_jira_section(match_result: dict) -> str:
    """Build the Jira matched tickets section for Google Chat thread."""
    matched = match_result.get("matched_tickets", [])
    if not matched:
        return ""
    reasoning = match_result.get("reasoning", {})
    lines = [
        "─────────────────────",
        "🎫 *Potentially Affected Jira Tickets*",
    ]
    for ticket in matched:
        reason = reasoning.get(ticket, "")
        ticket_url = f"https://heva-co.atlassian.net/browse/{ticket}"
        reason_text = f" — {reason}" if reason else ""
        lines.append(f"  • <{ticket_url}|{ticket}>{reason_text}")
    lines.append("")
    return "\n".join(lines)


def main():
    missing = [k for k in ["ANTHROPIC_API_KEY", "GOOGLE_CHAT_WEBHOOK"] if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing required secrets: {', '.join(missing)}")

    print(f"Running Heva Code Sentinel for {REPO_SHORT} @ {COMMIT_SHA} using {CLAUDE_MODEL}")
    known_issues = load_known_issues()
    print(f"Loaded {len(known_issues)} known issues.")

    stat, diff = get_diff()
    if not diff:
        print("No diff found, skipping review.")
        return

    print("Reviewing with Claude...")
    try:
        findings = review_with_claude(stat, diff, known_issues)
    except (ValueError, KeyError) as e:
        raise SystemExit(f"Review failed: {e}")

    print(f"Verdict: {findings.get('verdict')} | Cost: ${findings.get('_cost', {}).get('total_usd', 0):.4f}")
    fixed_ids = findings.get("fixed_issues", [])

    summary = build_summary_card(findings)
    post_to_google_chat(summary, thread_key=THREAD_KEY, reply=True)

    # Jira integration — match CRITICAL findings to open bugs (new commit)
    jira_section = ""
    if findings.get("verdict") == "CRITICAL" and JIRA_ENABLED:
        print("Searching Jira for related bugs (new findings)...")
        bugs = jira_search_recent_bugs(days=7)
        print(f"Found {len(bugs)} recent open bugs.")
        if bugs:
            match_result = jira_match_bugs_to_findings(bugs, findings)
            matched = match_result.get("matched_tickets", [])
            print(f"Matched {len(matched)} tickets: {matched}")
            if matched:
                jira_comment_on_tickets(match_result, findings)
                jira_section = build_jira_section(match_result)

    # Jira integration — match existing known issues to Jira bugs (always runs)
    jira_links = {}
    if JIRA_ENABLED and known_issues:
        print("Matching known issues to Jira tickets...")
        jira_links = jira_match_known_issues_to_bugs(known_issues)

    detail_text = build_detail_thread(findings, known_issues, jira_links=jira_links)
    if jira_section:
        detail_text += "\n" + jira_section
    post_to_google_chat({"text": detail_text}, thread_key=THREAD_KEY, reply=True)

    persisted = auto_persist_critical_issues(findings, known_issues)
    # Reload after potential auto-persist before removing fixed
    known_issues = load_known_issues()
    fixed = remove_fixed_issues(fixed_ids, known_issues)
    if persisted or fixed:
        commit_and_push_known_issues()


if __name__ == "__main__":
    main()
