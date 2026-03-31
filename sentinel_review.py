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

# ── Repo context (passed via action inputs) ──────────────────────────────────
REPO_TYPE = os.environ.get("REPO_TYPE", "Unknown")
REPO_STACK = os.environ.get("REPO_STACK", "Unknown")
REVIEW_FOCUS = os.environ.get("REVIEW_FOCUS", "General code quality, security, reliability")


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


REQUIRED_FIELDS = {"summary", "security", "reliability", "architecture", "performance", "quality", "good_changes", "fixed_issues", "verdict"}
VALID_VERDICTS = {"LGTM", "NEEDS_ATTENTION", "CRITICAL"}


def review_with_claude(stat: str, diff: str, known_issues: list) -> dict:
    """Call Claude API to review the diff and return structured findings."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    known_issues_text = ""
    if known_issues:
        known_issues_text = "\n\nKNOWN EXISTING ISSUES TO CHECK IF FIXED:\n"
        for issue in known_issues:
            known_issues_text += f"- [{issue['id']}] ({issue['severity']}) {issue['title']} | hint: {issue.get('file_hint', '')}\n"

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
  "verdict": "LGTM" | "NEEDS_ATTENTION" | "CRITICAL"
}}

Rules:
- Only report REAL issues visible in the diff, no speculation
- Security issues in healthcare code are CRITICAL (PHI, auth, injection)
- Keep each finding under 15 words
- For fixed_issues: only include an ID if the diff clearly addresses that specific issue
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

    for field in ["security", "reliability", "architecture", "performance", "quality", "good_changes", "fixed_issues"]:
        if not isinstance(parsed.get(field), list):
            parsed[field] = []

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

    # Count issues per category
    counts = {
        "🔐": len(findings.get("security", [])),
        "⚡": len(findings.get("reliability", [])),
        "🏗️": len(findings.get("architecture", [])),
        "🚀": len(findings.get("performance", [])),
        "🧹": len(findings.get("quality", [])),
    }
    fixed_ids = findings.get("fixed_issues", [])
    issue_summary = "  ".join(f"{icon} {n}" for icon, n in counts.items() if n > 0) or "No issues"
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
                    "title": f"{emoji} {REPO_SHORT} — {verdict}",
                    "subtitle": f"Heva Code Sentinel  •  {timestamp}",
                    "imageUrl": "https://cdn-icons-png.flaticon.com/512/2092/2092663.png",
                    "imageType": "CIRCLE",
                },
                "sections": [{"widgets": [{"textParagraph": {"text": body}}]}],
            }
        }]
    }


def build_detail_thread(findings: dict, known_issues: list) -> str:
    """Build the detailed findings as plain text for the thread reply."""
    verdict = findings.get("verdict", "LGTM")
    emoji = verdict_emoji(verdict)
    lines = [
        f"📋 *Full Review — {REPO_SHORT} @ {COMMIT_SHA}*",
        f"*Verdict:* {emoji} {verdict}",
        f"*Summary:* {findings.get('summary', 'N/A')}\n",
    ]

    # Fixed issues
    fixed_ids = findings.get("fixed_issues", [])
    if fixed_ids and known_issues:
        fixed_map = {i["id"]: i for i in known_issues}
        fixed_lines = [f"  ✅ [{fid}] {fixed_map[fid]['title']}" for fid in fixed_ids if fid in fixed_map]
        if fixed_lines:
            lines.append("🎉 *Fixed in This Commit*")
            lines.extend(fixed_lines)
            lines.append("")

    categories = [
        ("🔐 Security", findings.get("security", [])),
        ("⚡ Reliability", findings.get("reliability", [])),
        ("🏗️ Architecture", findings.get("architecture", [])),
        ("🚀 Performance", findings.get("performance", [])),
        ("🧹 Code Quality", findings.get("quality", [])),
        ("👍 Good Changes", findings.get("good_changes", [])),
    ]

    for label, items in categories:
        if items:
            lines.append(f"*{label}*")
            lines.extend(f"  • {item}" for item in items)
            lines.append("")

    if not any(items for _, items in categories) and not fixed_ids:
        lines.append("No issues found. Clean commit. ✨")

    return "\n".join(lines)


def remove_fixed_issues(fixed_ids: list, known_issues: list) -> bool:
    """Remove fixed issues from known_issues.json. Returns True if file was updated."""
    if not fixed_ids:
        return False
    issues_path = Path(".github/sentinel/known_issues.json")
    if not issues_path.exists():
        return False
    remaining = [i for i in known_issues if i["id"] not in fixed_ids]
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
            ["git", "commit", "-m", "chore: sentinel removed fixed known issues"],
            check=True
        )
        subprocess.run(["git", "push"], check=True)
        print("Updated known_issues.json committed and pushed.")
    except subprocess.CalledProcessError as e:
        print(f"Warning: could not commit known_issues update: {e}")


def post_to_google_chat(message: dict, thread_key: str = None, reply: bool = False) -> str:
    """Post a message. Returns the thread name for replies."""
    url = GOOGLE_CHAT_WEBHOOK
    if thread_key:
        url += f"&threadKey={thread_key}"
        if reply:
            url += "&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

    response = requests.post(url, json=message, headers={"Content-Type": "application/json"})
    if response.status_code != 200:
        print(f"Failed to post to Google Chat: {response.status_code} {response.text}")
        raise Exception("Google Chat post failed")
    print("Posted to Google Chat successfully.")
    return response.json().get("thread", {}).get("name", "")


def main():
    # Validate secrets are present without printing their values
    missing = [k for k in ["ANTHROPIC_API_KEY", "GOOGLE_CHAT_WEBHOOK"] if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing required secrets: {', '.join(missing)}")

    print(f"Running Heva Code Sentinel for {REPO_SHORT} @ {COMMIT_SHA}")
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
        # Raise without printing env vars or secrets
        raise SystemExit(f"Review failed: {e}")

    print(f"Verdict: {findings.get('verdict')}")
    fixed_ids = findings.get("fixed_issues", [])
    print(f"Fixed issues: {fixed_ids}")

    thread_key = f"sentinel-{REPO_SHORT}-{COMMIT_SHA}"

    # Post compact summary card
    summary = build_summary_card(findings)
    post_to_google_chat(summary, thread_key=thread_key)

    # Post full details as thread reply
    detail_text = build_detail_thread(findings, known_issues)
    post_to_google_chat({"text": detail_text}, thread_key=thread_key, reply=True)

    # Remove fixed issues from known_issues.json so they never repeat
    if remove_fixed_issues(fixed_ids, known_issues):
        commit_and_push_known_issues()


if __name__ == "__main__":
    main()
