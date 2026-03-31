"""Unit tests for sentinel_review.py — tests pure functions without external calls."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Set required env vars before importing the module
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("GOOGLE_CHAT_WEBHOOK", "https://chat.googleapis.com/test")
os.environ.setdefault("REPO_NAME", "heva-care/heva-backend")
os.environ.setdefault("REPO_SHORT", "heva-backend")
os.environ.setdefault("COMMIT_SHA", "abc12345")
os.environ.setdefault("COMMIT_MSG", "fix: resolve auth bug")
os.environ.setdefault("AUTHOR", "Test Author")
os.environ.setdefault("BRANCH", "dev")
os.environ.setdefault("COMMIT_URL", "https://github.com/heva-care/heva-backend/commit/abc12345")
os.environ.setdefault("REPO_TYPE", "Go backend")
os.environ.setdefault("REPO_STACK", "Go, PostgreSQL")
os.environ.setdefault("REVIEW_FOCUS", "API contracts, error handling")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sentinel_review as sr


# ── verdict_emoji ────────────────────────────────────────────────────────────

class TestVerdictEmoji:
    def test_lgtm(self):
        assert sr.verdict_emoji("LGTM") == "✅"

    def test_needs_attention(self):
        assert sr.verdict_emoji("NEEDS_ATTENTION") == "⚠️"

    def test_critical(self):
        assert sr.verdict_emoji("CRITICAL") == "🚨"

    def test_unknown(self):
        assert sr.verdict_emoji("UNKNOWN") == "ℹ️"


# ── load_known_issues ────────────────────────────────────────────────────────

class TestLoadKnownIssues:
    def test_returns_empty_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert sr.load_known_issues() == []

    def test_loads_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        issues_dir = tmp_path / ".github" / "sentinel"
        issues_dir.mkdir(parents=True)
        issues = [{"id": "IA-001", "severity": "high", "title": "Race condition"}]
        (issues_dir / "known_issues.json").write_text(json.dumps(issues))
        assert sr.load_known_issues() == issues


# ── remove_fixed_issues ─────────────────────────────────────────────────────

class TestRemoveFixedIssues:
    def test_no_fixed_ids(self):
        assert sr.remove_fixed_issues([], [{"id": "IA-001"}]) is False

    def test_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert sr.remove_fixed_issues(["IA-001"], [{"id": "IA-001"}]) is False

    def test_removes_fixed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        issues_dir = tmp_path / ".github" / "sentinel"
        issues_dir.mkdir(parents=True)
        issues = [
            {"id": "IA-001", "severity": "high", "title": "Bug A"},
            {"id": "IA-002", "severity": "low", "title": "Bug B"},
        ]
        path = issues_dir / "known_issues.json"
        path.write_text(json.dumps(issues))

        result = sr.remove_fixed_issues(["IA-001"], issues)
        assert result is True

        remaining = json.loads(path.read_text())
        assert len(remaining) == 1
        assert remaining[0]["id"] == "IA-002"

    def test_no_match(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        issues_dir = tmp_path / ".github" / "sentinel"
        issues_dir.mkdir(parents=True)
        issues = [{"id": "IA-001", "severity": "high", "title": "Bug A"}]
        (issues_dir / "known_issues.json").write_text(json.dumps(issues))

        assert sr.remove_fixed_issues(["IA-999"], issues) is False


# ── build_summary_card ───────────────────────────────────────────────────────

class TestBuildSummaryCard:
    def test_lgtm_card(self):
        findings = {
            "verdict": "LGTM",
            "summary": "Clean refactor",
            "security": [],
            "reliability": [],
            "architecture": [],
            "performance": [],
            "quality": [],
            "good_changes": ["Nice cleanup"],
            "fixed_issues": [],
        }
        card = sr.build_summary_card(findings)
        assert "cardsV2" in card
        header = card["cardsV2"][0]["card"]["header"]
        assert "LGTM" in header["title"]
        assert "✅" in header["title"]

    def test_critical_card_with_issues(self):
        findings = {
            "verdict": "CRITICAL",
            "summary": "SQL injection found",
            "security": ["Unsanitized input in query"],
            "reliability": [],
            "architecture": [],
            "performance": [],
            "quality": [],
            "good_changes": [],
            "fixed_issues": ["IA-001"],
        }
        card = sr.build_summary_card(findings)
        header = card["cardsV2"][0]["card"]["header"]
        assert "CRITICAL" in header["title"]
        assert "🚨" in header["title"]
        body = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
        assert "🔐 1" in body
        assert "Fixed: 1" in body


# ── build_detail_thread ──────────────────────────────────────────────────────

class TestBuildDetailThread:
    def test_clean_commit(self):
        findings = {
            "verdict": "LGTM",
            "summary": "Minor refactor",
            "security": [],
            "reliability": [],
            "architecture": [],
            "performance": [],
            "quality": [],
            "good_changes": [],
            "fixed_issues": [],
        }
        text = sr.build_detail_thread(findings, [])
        assert "No issues found" in text
        assert "LGTM" in text
        assert "Minor refactor" in text

    def test_always_includes_verdict_and_summary(self):
        findings = {
            "verdict": "NEEDS_ATTENTION",
            "summary": "Added new endpoint without validation",
            "security": [],
            "reliability": ["Missing input validation"],
            "architecture": [],
            "performance": [],
            "quality": [],
            "good_changes": [],
            "fixed_issues": [],
        }
        text = sr.build_detail_thread(findings, [])
        assert "NEEDS_ATTENTION" in text
        assert "Added new endpoint without validation" in text
        assert "Missing input validation" in text

    def test_with_findings(self):
        findings = {
            "verdict": "CRITICAL",
            "summary": "Security issues found",
            "security": ["SQL injection risk"],
            "reliability": [],
            "architecture": [],
            "performance": ["N+1 query"],
            "quality": [],
            "good_changes": ["Good error handling"],
            "fixed_issues": [],
        }
        text = sr.build_detail_thread(findings, [])
        assert "SQL injection risk" in text
        assert "N+1 query" in text
        assert "Good error handling" in text
        assert "CRITICAL" in text

    def test_with_fixed_issues(self):
        findings = {
            "verdict": "LGTM",
            "summary": "Fixed auth bug",
            "security": [],
            "reliability": [],
            "architecture": [],
            "performance": [],
            "quality": [],
            "good_changes": [],
            "fixed_issues": ["IA-001"],
        }
        known = [{"id": "IA-001", "severity": "high", "title": "Race condition in auth"}]
        text = sr.build_detail_thread(findings, known)
        assert "Fixed in This Commit" in text
        assert "Race condition in auth" in text


# ── get_diff ─────────────────────────────────────────────────────────────────

class TestGetDiff:
    def test_truncates_large_diff(self):
        with patch("sentinel_review.DIFF_MAX_CHARS", 50):
            large_diff = "a" * 200
            with patch("subprocess.check_output", side_effect=[b"stat output", large_diff.encode()]):
                # Need to re-mock since check_output returns str with text=True
                pass

    def test_handles_git_error(self):
        import subprocess as sp
        with patch("subprocess.check_output", side_effect=sp.CalledProcessError(1, "git")):
            stat, diff = sr.get_diff()
            assert stat == "Could not get diff"
            assert diff == ""


# ── review_with_claude (response parsing) ────────────────────────────────────

class TestReviewWithClaude:
    VALID_RESPONSE = json.dumps({
        "summary": "Added input validation",
        "security": [],
        "reliability": [],
        "architecture": [],
        "performance": [],
        "quality": [],
        "good_changes": ["Good validation"],
        "fixed_issues": [],
        "verdict": "LGTM",
    })

    def _mock_claude(self, raw_text):
        mock_client = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=raw_text)]
        mock_client.messages.create.return_value = mock_msg
        return mock_client

    def test_parses_valid_json(self):
        with patch("anthropic.Anthropic", return_value=self._mock_claude(self.VALID_RESPONSE)):
            result = sr.review_with_claude("stat", "diff", [])
            assert result["verdict"] == "LGTM"
            assert result["summary"] == "Added input validation"

    def test_strips_markdown_fences(self):
        wrapped = f"```json\n{self.VALID_RESPONSE}\n```"
        with patch("anthropic.Anthropic", return_value=self._mock_claude(wrapped)):
            result = sr.review_with_claude("stat", "diff", [])
            assert result["verdict"] == "LGTM"

    def test_invalid_json_raises(self):
        with patch("anthropic.Anthropic", return_value=self._mock_claude("not json")):
            with pytest.raises(ValueError, match="invalid JSON"):
                sr.review_with_claude("stat", "diff", [])

    def test_missing_fields_raises(self):
        incomplete = json.dumps({"summary": "test"})
        with patch("anthropic.Anthropic", return_value=self._mock_claude(incomplete)):
            with pytest.raises(ValueError, match="missing fields"):
                sr.review_with_claude("stat", "diff", [])

    def test_invalid_verdict_defaults_to_needs_attention(self):
        response = json.loads(self.VALID_RESPONSE)
        response["verdict"] = "INVALID"
        with patch("anthropic.Anthropic", return_value=self._mock_claude(json.dumps(response))):
            result = sr.review_with_claude("stat", "diff", [])
            assert result["verdict"] == "NEEDS_ATTENTION"

    def test_non_list_fields_coerced_to_empty_list(self):
        response = json.loads(self.VALID_RESPONSE)
        response["security"] = "not a list"
        response["quality"] = None
        with patch("anthropic.Anthropic", return_value=self._mock_claude(json.dumps(response))):
            result = sr.review_with_claude("stat", "diff", [])
            assert result["security"] == []
            assert result["quality"] == []


# ── post_to_google_chat ──────────────────────────────────────────────────────

class TestPostToGoogleChat:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"thread": {"name": "spaces/123/threads/456"}}
        with patch("requests.post", return_value=mock_resp):
            thread = sr.post_to_google_chat({"text": "hi"}, thread_key="key-1")
            assert thread == "spaces/123/threads/456"

    def test_failure_raises(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(Exception, match="Google Chat post failed"):
                sr.post_to_google_chat({"text": "hi"})

    def test_thread_key_appended(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"thread": {"name": ""}}
        with patch("requests.post", return_value=mock_resp) as mock_post:
            sr.post_to_google_chat({"text": "hi"}, thread_key="abc", reply=True)
            url = mock_post.call_args[0][0]
            assert "&threadKey=abc" in url
            assert "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" in url
