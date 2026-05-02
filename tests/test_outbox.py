"""Outbox durability + tag pinning.

The outbox is the durability story: turns persist to disk before any
emit attempt, so a Langfuse outage of any duration cannot lose them.
Tags are captured at write time so a session draining another session's
outbox cannot overwrite them with its own env.
"""
import json
from pathlib import Path

import pytest

import hook


@pytest.fixture
def outbox_dir(tmp_path, monkeypatch):
    """Redirect the module-level OUTBOX_DIR to a tmp path for isolation."""
    d = tmp_path / "outbox"
    monkeypatch.setattr(hook, "OUTBOX_DIR", d)
    return d


@pytest.fixture
def sample_turn():
    return hook.Turn(
        user_msg={"type": "user", "message": {"role": "user", "content": "hi"}},
        assistant_msgs=[{
            "type": "assistant",
            "message": {
                "id": "m1", "role": "assistant", "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "hello"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }],
        tool_results_by_id={},
    )


def test_outbox_write_creates_file_with_payload(outbox_dir, sample_turn):
    p = hook.outbox_write("sess1", 1, sample_turn, Path("/tmp/x.jsonl"))
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["session_id"] == "sess1"
    assert data["turn_num"] == 1
    assert data["schema"] == 2


def test_outbox_round_trip_reconstructs_turn(outbox_dir, sample_turn):
    p = hook.outbox_write("sess1", 1, sample_turn, Path("/tmp/x.jsonl"))
    rec = hook._load_outbox_file(p)
    assert rec is not None
    sid, tn, t, tp, tags = rec
    assert sid == "sess1"
    assert tn == 1
    assert t.user_msg == sample_turn.user_msg
    assert t.assistant_msgs == sample_turn.assistant_msgs
    assert tp == Path("/tmp/x.jsonl")


def test_outbox_pins_tags_at_write_time(outbox_dir, sample_turn, monkeypatch):
    """Tags computed at write time persist in payload, surviving env changes."""
    monkeypatch.setenv("PERSONA", "session_A_persona")
    monkeypatch.setenv("CC_LANGFUSE_PROJECT_TAG", "project_A")
    p = hook.outbox_write("sess1", 1, sample_turn, Path("/tmp/x.jsonl"))

    # Simulate the draining session having different env
    monkeypatch.delenv("PERSONA")
    monkeypatch.setenv("CC_LANGFUSE_PROJECT_TAG", "project_B")

    rec = hook._load_outbox_file(p)
    _sid, _tn, _t, _tp, tags = rec
    assert tags == ["claude-code", "persona:session_A_persona", "project:project_A"]


def test_outbox_explicit_tags_override_env(outbox_dir, sample_turn, monkeypatch):
    """Caller can pass tags= explicitly (used by emit_turn fallthrough)."""
    monkeypatch.setenv("PERSONA", "shouldnt_appear")
    explicit = ["claude-code", "custom:tag"]
    p = hook.outbox_write("sess1", 1, sample_turn, Path("/tmp/x.jsonl"), tags=explicit)
    rec = hook._load_outbox_file(p)
    _sid, _tn, _t, _tp, tags = rec
    assert tags == explicit


def test_load_outbox_file_returns_none_for_unparseable(outbox_dir):
    bad = outbox_dir / "garbage" / "turn-000001.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{ not json")
    assert hook._load_outbox_file(bad) is None


def test_load_schema_1_payload_returns_tags_none(outbox_dir, sample_turn):
    """Backwards-compat: outbox files written before tag-pinning load with tags=None,
    triggering env fallback in emit_turn."""
    legacy_dir = outbox_dir / "legacy_session"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "turn-000001.json"
    legacy_file.write_text(json.dumps({
        "schema": 1,
        "session_id": "legacy_session",
        "turn_num": 1,
        "transcript_path": "/tmp/x.jsonl",
        "user_msg": sample_turn.user_msg,
        "assistant_msgs": sample_turn.assistant_msgs,
        "tool_results_by_id": sample_turn.tool_results_by_id,
        "written": "2025-12-31T00:00:00Z",
    }))
    rec = hook._load_outbox_file(legacy_file)
    assert rec is not None
    _sid, _tn, _t, _tp, tags = rec
    assert tags is None


def test_compute_run_tags_emits_claude_code_always():
    tags = hook._compute_run_tags(Path("/some/path"))
    assert "claude-code" in tags


def test_compute_run_tags_persona_when_set(monkeypatch):
    monkeypatch.setenv("PERSONA", "alpha")
    tags = hook._compute_run_tags(Path("/some/path"))
    assert "persona:alpha" in tags


def test_compute_run_tags_project_from_env_overrides_path_fallback(monkeypatch):
    monkeypatch.setenv("CC_LANGFUSE_PROJECT_TAG", "explicit")
    tags = hook._compute_run_tags(Path("/.claude/projects/-Users-x-Code-derived/sess.jsonl"))
    project_tags = [t for t in tags if t.startswith("project:")]
    assert project_tags == ["project:explicit"]


def test_compute_run_tags_project_from_path_when_env_unset(monkeypatch):
    monkeypatch.delenv("CC_LANGFUSE_PROJECT_TAG", raising=False)
    tags = hook._compute_run_tags(Path("/.claude/projects/-Users-x-Code-derived/sess.jsonl"))
    project_tags = [t for t in tags if t.startswith("project:")]
    assert project_tags == ["project:derived"]


def test_compute_run_tags_backfill(monkeypatch):
    monkeypatch.setenv("CC_LANGFUSE_BACKFILL", "true")
    tags = hook._compute_run_tags(Path("/some/path"))
    assert "backfill" in tags
