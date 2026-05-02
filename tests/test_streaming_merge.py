"""Streaming-merge correctness.

Claude Code's transcript JSONL emits progressive updates of the same
assistant message.id, each carrying one new tool_use block. The naive
last-wins approach drops earlier tool_uses on multi-tool turns. These
tests pin the merge behaviour.
"""
import hook


def _asst(mid, *, text=None, tool_uses=None, ts="2026-01-01T00:00:00Z"):
    """Build an assistant entry shaped like Claude Code's JSONL."""
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for tu in tool_uses or []:
        content.append({"type": "tool_use", **tu})
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": mid,
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": content,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }


def _user(content, ts="2026-01-01T00:00:00Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": content}}


def _tool_result(tool_use_id, content, ts="2026-01-01T00:00:00Z"):
    return _user([{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}], ts=ts)


def test_single_entry_passes_through_unchanged():
    e = _asst("m1", text="hi")
    assert hook._merge_streaming_entries([e]) is e


def test_two_updates_one_tool_each_unions_tool_uses():
    e1 = _asst("m1", text="thinking", tool_uses=[{"id": "t1", "name": "Bash", "input": {}}])
    e2 = _asst("m1", tool_uses=[{"id": "t2", "name": "Read", "input": {}}])
    merged = hook._merge_streaming_entries([e1, e2])
    tool_ids = [tu["id"] for tu in hook.iter_tool_uses(hook.get_content(merged))]
    assert tool_ids == ["t1", "t2"]


def test_dedup_by_tool_use_id():
    """Same tool_use.id appearing in two updates is kept once (first occurrence)."""
    e1 = _asst("m1", tool_uses=[{"id": "t1", "name": "Bash", "input": {"v": 1}}])
    e2 = _asst("m1", tool_uses=[{"id": "t1", "name": "Bash", "input": {"v": 2}}])
    merged = hook._merge_streaming_entries([e1, e2])
    tools = hook.iter_tool_uses(hook.get_content(merged))
    assert len(tools) == 1
    assert tools[0]["input"] == {"v": 1}


def test_text_retained_from_latest_non_empty_update():
    """When the final update has only a tool_use (no text), retain text from earlier update."""
    e1 = _asst("m1", text="here is the answer", tool_uses=[{"id": "t1", "name": "Bash", "input": {}}])
    e2 = _asst("m1", tool_uses=[{"id": "t2", "name": "Read", "input": {}}])
    merged = hook._merge_streaming_entries([e1, e2])
    text = hook.extract_text(hook.get_content(merged))
    assert text == "here is the answer"


def test_text_retained_from_final_when_present():
    e1 = _asst("m1", text="thinking", tool_uses=[{"id": "t1", "name": "Bash", "input": {}}])
    e2 = _asst("m1", text="final answer")
    merged = hook._merge_streaming_entries([e1, e2])
    text = hook.extract_text(hook.get_content(merged))
    assert text == "final answer"


def test_build_turns_preserves_all_streaming_tool_uses():
    """Full integration: a 1-turn session with 3 streaming updates, 3 tool_uses across them.
    Old latest-wins logic would drop 2 of 3.
    """
    msgs = [
        _user("question"),
        _asst("m1", text="planning", tool_uses=[{"id": "t1", "name": "Bash", "input": {"cmd": "ls"}}]),
        _asst("m1", tool_uses=[{"id": "t2", "name": "Read", "input": {"path": "/x"}}]),
        _asst("m1", text="done", tool_uses=[{"id": "t3", "name": "Edit", "input": {}}]),
        _tool_result("t1", "out1"),
        _tool_result("t2", "out2"),
        _tool_result("t3", "out3"),
    ]
    turns = hook.build_turns(msgs)
    assert len(turns) == 1
    t = turns[0]
    tool_ids = [tu["id"] for am in t.assistant_msgs for tu in hook.iter_tool_uses(hook.get_content(am))]
    assert sorted(tool_ids) == ["t1", "t2", "t3"]
    assert sorted(t.tool_results_by_id.keys()) == ["t1", "t2", "t3"]


def test_build_turns_separates_consecutive_user_messages_into_turns():
    msgs = [
        _user("first"),
        _asst("m1", text="r1"),
        _user("second"),
        _asst("m2", text="r2"),
    ]
    turns = hook.build_turns(msgs)
    assert len(turns) == 2
    assert hook.extract_text(hook.get_content(turns[0].assistant_msgs[0])) == "r1"
    assert hook.extract_text(hook.get_content(turns[1].assistant_msgs[0])) == "r2"


def test_build_turns_ignores_assistant_before_user():
    """Defensive: if the JSONL starts with an assistant entry (truncated session
    file, mid-replay, etc), we drop it rather than crash."""
    msgs = [
        _asst("orphan", text="ghost"),
        _user("first real turn"),
        _asst("m1", text="r1"),
    ]
    turns = hook.build_turns(msgs)
    assert len(turns) == 1


def test_build_turns_handles_assistant_without_message_id():
    """Some transcript variants have no message.id; we synthesize one and keep going."""
    msgs = [
        _user("q"),
        {"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "no id"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }},
    ]
    turns = hook.build_turns(msgs)
    assert len(turns) == 1
