#!/usr/bin/env python3
"""
Claude Code -> Langfuse hook

Runs as a Claude Code Stop hook. Reads the session transcript JSONL
incrementally, groups messages into turns, and emits Langfuse traces.

Gated on TRACE_TO_LANGFUSE=true env var (fail-open when unset so the
hook is a safe no-op for disabled runs).

Durability model:
  - Every turn is written to a per-session outbox file BEFORE attempting
    to flush to Langfuse. State (offset/turn_count) advances on outbox
    write, not on successful flush.
  - Every invocation begins by scanning the outbox across all sessions
    and re-attempting flush. Langfuse unreachable = outbox grows; next
    successful connectivity drains it (within the same session or from
    a future session).
  - auth_check() guards against emitting when Langfuse is unreachable.

Streaming merge:
  Claude Code's JSONL emits progressive updates of the same assistant
  message.id with one new tool_use per update. Naive last-wins dedup
  drops earlier tool_uses on multi-tool turns. build_turns calls
  _merge_streaming_entries to union tool_uses across update entries.

Original-shape source: https://github.com/langfuse/langfuse-docs/blob/main/content/integrations/other/claude-code.mdx
"""

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- Langfuse import (fail-open) ---
try:
    from langfuse import Langfuse, propagate_attributes
except Exception:
    sys.exit(0)

# --- Paths ---
STATE_DIR = Path.home() / ".claude" / "state"
LOG_FILE = STATE_DIR / "langfuse_hook.log"
STATE_FILE = STATE_DIR / "langfuse_state.json"
LOCK_FILE = STATE_DIR / "langfuse_state.lock"
OUTBOX_DIR = STATE_DIR / "langfuse_outbox"

DEBUG = os.environ.get("CC_LANGFUSE_DEBUG", "").lower() == "true"
MAX_CHARS = int(os.environ.get("CC_LANGFUSE_MAX_CHARS", "20000"))

# ----------------- Logging -----------------
def _log(level: str, message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} [{level}] {message}\n")
    except Exception:
        # Never block
        pass

def debug(msg: str) -> None:
    if DEBUG:
        _log("DEBUG", msg)

def info(msg: str) -> None:
    _log("INFO", msg)

def warn(msg: str) -> None:
    _log("WARN", msg)

def error(msg: str) -> None:
    _log("ERROR", msg)

# ----------------- State locking (best-effort) -----------------
class FileLock:
    def __init__(self, path: Path, timeout_s: float = 2.0):
        self.path = path
        self.timeout_s = timeout_s
        self._fh = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            import fcntl  # Unix only
            deadline = time.time() + self.timeout_s
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() > deadline:
                        break
                    time.sleep(0.05)
        except Exception:
            # If locking isn't available, proceed without it.
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass

def load_state() -> dict[str, Any]:
    try:
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        debug(f"save_state failed: {e}")

def state_key(session_id: str, transcript_path: str) -> str:
    # stable key even if session_id collides
    raw = f"{session_id}::{transcript_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# ----------------- Hook payload -----------------
def read_hook_payload() -> dict[str, Any]:
    """
    Claude Code hooks pass a JSON payload on stdin.
    This script tolerates missing/empty stdin by returning {}.
    """
    try:
        data = sys.stdin.read()
        if not data.strip():
            return {}
        return json.loads(data)
    except Exception:
        return {}

def extract_session_and_transcript(payload: dict[str, Any]) -> tuple[str | None, Path | None]:
    """
    Tries a few plausible field names; exact keys can vary across hook types/versions.
    Prefer structured values from stdin over heuristics.
    """
    session_id = (
        payload.get("sessionId")
        or payload.get("session_id")
        or payload.get("session", {}).get("id")
    )

    transcript = (
        payload.get("transcriptPath")
        or payload.get("transcript_path")
        or payload.get("transcript", {}).get("path")
    )

    if transcript:
        try:
            transcript_path = Path(transcript).expanduser().resolve()
        except Exception:
            transcript_path = None
    else:
        transcript_path = None

    return session_id, transcript_path

# ----------------- Transcript parsing helpers -----------------
def get_content(msg: dict[str, Any]) -> Any:
    if not isinstance(msg, dict):
        return None
    if "message" in msg and isinstance(msg.get("message"), dict):
        return msg["message"].get("content")
    return msg.get("content")

def get_role(msg: dict[str, Any]) -> str | None:
    # Claude Code transcript lines commonly have type=user/assistant OR message.role
    t = msg.get("type")
    if t in ("user", "assistant"):
        return t
    m = msg.get("message")
    if isinstance(m, dict):
        r = m.get("role")
        if r in ("user", "assistant"):
            return r
    return None

def is_tool_result(msg: dict[str, Any]) -> bool:
    role = get_role(msg)
    if role != "user":
        return False
    content = get_content(msg)
    if isinstance(content, list):
        return any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content)
    return False

def iter_tool_results(content: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_result":
                out.append(x)
    return out

def iter_tool_uses(content: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_use":
                out.append(x)
    return out

def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "text":
                parts.append(x.get("text", ""))
            elif isinstance(x, str):
                parts.append(x)
        return "\n".join([p for p in parts if p])
    return ""

def truncate_text(s: str, max_chars: int = MAX_CHARS) -> tuple[str, dict[str, Any]]:
    if s is None:
        return "", {"truncated": False, "orig_len": 0}
    orig_len = len(s)
    if orig_len <= max_chars:
        return s, {"truncated": False, "orig_len": orig_len}
    head = s[:max_chars]
    return head, {"truncated": True, "orig_len": orig_len, "kept_len": len(head), "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest()}

def get_model(msg: dict[str, Any]) -> str:
    m = msg.get("message")
    if isinstance(m, dict):
        return m.get("model") or "claude"
    return "claude"

def extract_usage(assistant_msgs: list[dict[str, Any]]) -> dict[str, int]:
    """Sum token counts across all assistant messages in a turn.

    Returned dict goes to Langfuse as `usage_details`. Keys match the
    Anthropic API field names so Langfuse's catalog can price `input` +
    `output` directly; cache tokens are recorded but not priced (single
    input price applies to uncached input only).
    """
    totals = {
        "input": 0,
        "output": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    for msg in assistant_msgs:
        m = msg.get("message")
        if not isinstance(m, dict):
            continue
        u = m.get("usage")
        if not isinstance(u, dict):
            continue
        totals["input"] += int(u.get("input_tokens") or 0)
        totals["output"] += int(u.get("output_tokens") or 0)
        totals["cache_read_input_tokens"] += int(u.get("cache_read_input_tokens") or 0)
        totals["cache_creation_input_tokens"] += int(u.get("cache_creation_input_tokens") or 0)
    return {k: v for k, v in totals.items() if v > 0}

def get_message_id(msg: dict[str, Any]) -> str | None:
    m = msg.get("message")
    if isinstance(m, dict):
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            return mid
    return None

# ----------------- Incremental reader -----------------
@dataclass
class SessionState:
    offset: int = 0
    buffer: str = ""
    turn_count: int = 0

def load_session_state(global_state: dict[str, Any], key: str) -> SessionState:
    s = global_state.get(key, {})
    return SessionState(
        offset=int(s.get("offset", 0)),
        buffer=str(s.get("buffer", "")),
        turn_count=int(s.get("turn_count", 0)),
    )

def write_session_state(global_state: dict[str, Any], key: str, ss: SessionState) -> None:
    global_state[key] = {
        "offset": ss.offset,
        "buffer": ss.buffer,
        "turn_count": ss.turn_count,
        "updated": datetime.now(timezone.utc).isoformat(),
    }

def read_new_jsonl(transcript_path: Path, ss: SessionState) -> tuple[list[dict[str, Any]], SessionState]:
    """
    Reads only new bytes since ss.offset. Keeps ss.buffer for partial last line.
    Returns parsed JSON lines (best-effort) and updated state.
    """
    if not transcript_path.exists():
        return [], ss

    try:
        with open(transcript_path, "rb") as f:
            f.seek(ss.offset)
            chunk = f.read()
            new_offset = f.tell()
    except Exception as e:
        debug(f"read_new_jsonl failed: {e}")
        return [], ss

    if not chunk:
        return [], ss

    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        text = chunk.decode(errors="replace")

    combined = ss.buffer + text
    lines = combined.split("\n")
    # last element may be incomplete
    ss.buffer = lines[-1]
    ss.offset = new_offset

    msgs: list[dict[str, Any]] = []
    for line in lines[:-1]:
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except Exception:
            continue

    return msgs, ss

# ----------------- Streaming merge -----------------
def _merge_streaming_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse N streaming updates of one assistant message.id into one merged view.

    Claude Code's JSONL emits progressive updates: each new tool_use arrives
    in its own entry with ONLY that tool_use in message.content (not the
    cumulative list). Naive last-wins semantics drop earlier tool_uses on
    multi-tool turns. Here we union all tool_uses across entries (dedup by
    tool_use.id) and keep the latest non-empty text.
    """
    if len(entries) == 1:
        return entries[0]

    base = dict(entries[-1])
    base_msg = base.get("message")
    if not isinstance(base_msg, dict):
        return entries[-1]
    base["message"] = dict(base_msg)

    seen_tool_ids: set = set()
    merged_tool_uses: list[dict[str, Any]] = []
    for e in entries:
        for tu in iter_tool_uses(get_content(e)):
            tid = tu.get("id")
            if tid and tid not in seen_tool_ids:
                seen_tool_ids.add(tid)
                merged_tool_uses.append(dict(tu))

    latest_text_blocks: list[dict[str, Any]] = []
    for e in reversed(entries):
        content = get_content(e)
        if isinstance(content, list):
            blocks = [x for x in content
                      if isinstance(x, dict) and x.get("type") == "text" and x.get("text")]
            if blocks:
                latest_text_blocks = blocks
                break

    base["message"]["content"] = list(latest_text_blocks) + merged_tool_uses
    return base

# ----------------- Turn assembly -----------------
@dataclass
class Turn:
    user_msg: dict[str, Any]
    assistant_msgs: list[dict[str, Any]]
    tool_results_by_id: dict[str, Any]

def build_turns(messages: list[dict[str, Any]]) -> list[Turn]:
    """Group transcript rows into turns: user -> assistant (merged across
    streaming updates) -> tool_results.
    """
    turns: list[Turn] = []
    current_user: dict[str, Any] | None = None
    assistant_order: list[str] = []
    assistant_entries: dict[str, list[dict[str, Any]]] = {}
    tool_results_by_id: dict[str, Any] = {}

    def flush_turn():
        nonlocal current_user, assistant_order, assistant_entries, tool_results_by_id, turns
        if current_user is None:
            return
        if not assistant_entries:
            return
        merged = [_merge_streaming_entries(assistant_entries[mid])
                  for mid in assistant_order if mid in assistant_entries]
        if not merged:
            return
        turns.append(Turn(
            user_msg=current_user,
            assistant_msgs=merged,
            tool_results_by_id=dict(tool_results_by_id),
        ))

    for msg in messages:
        role = get_role(msg)

        if is_tool_result(msg):
            for tr in iter_tool_results(get_content(msg)):
                tid = tr.get("tool_use_id")
                if tid:
                    tool_results_by_id[str(tid)] = tr.get("content")
            continue

        if role == "user":
            flush_turn()
            current_user = msg
            assistant_order = []
            assistant_entries = {}
            tool_results_by_id = {}
            continue

        if role == "assistant":
            if current_user is None:
                continue
            mid = get_message_id(msg) or f"noid:{len(assistant_order)}"
            if mid not in assistant_entries:
                assistant_order.append(mid)
                assistant_entries[mid] = []
            assistant_entries[mid].append(msg)
            continue

    flush_turn()
    return turns

# ----------------- Langfuse emit -----------------
def _tool_calls_from_assistants(assistant_msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for am in assistant_msgs:
        for tu in iter_tool_uses(get_content(am)):
            tid = tu.get("id") or ""
            calls.append({
                "id": str(tid),
                "name": tu.get("name") or "unknown",
                "input": tu.get("input") if isinstance(tu.get("input"), (dict, list, str, int, float, bool)) else {},
            })
    return calls



def _compute_run_tags(transcript_path: Path) -> list[str]:
    """Build the tag list for a turn from current process env vars.

    Always emits "claude-code". Adds "persona:<id>" if PERSONA set,
    "project:<name>" from CC_LANGFUSE_PROJECT_TAG (or transcript path
    parent dir name fallback), and "backfill" if CC_LANGFUSE_BACKFILL=true.

    Called at WRITE time (live emit and outbox-write) so the tags reflect
    the originating session's context, not whichever session happens to
    drain the outbox later.
    """
    run_tags = ["claude-code"]
    persona = os.environ.get("PERSONA", "").strip()
    if persona:
        run_tags.append(f"persona:{persona}")
    project_tag = os.environ.get("CC_LANGFUSE_PROJECT_TAG", "").strip()
    if not project_tag:
        parent = transcript_path.parent.name
        if parent:
            project_tag = parent.rstrip("/").rsplit("-", 1)[-1]
    if project_tag:
        run_tags.append(f"project:{project_tag}")
    if os.environ.get("CC_LANGFUSE_BACKFILL", "").lower() == "true":
        run_tags.append("backfill")
    return run_tags


def emit_turn(
    langfuse: Langfuse,
    session_id: str,
    turn_num: int,
    turn: Turn,
    transcript_path: Path,
    tags: list[str] | None = None,
) -> None:
    """Emit a turn to Langfuse using the OTel-style 4.x SDK API.

    Builds: trace root span -> generation observation (model + usage_details
    so Langfuse can price) -> N tool observations.

    `tags` lets the caller pin pre-computed tags (used by the outbox drain
    so a session that drains another session's outbox doesn't overwrite the
    persona/project tags with its own env). When None, tags are computed
    from current env vars via _compute_run_tags().

    Timestamps note: the 4.x public API stamps observations at wall-clock
    "now" rather than at the JSONL entry timestamp. For the live hook this
    is within ~1s of the actual turn end (the hook fires on Stop), which is
    fine. For the backfill driver, traces land at backfill-emit time, not
    original session time. The JSONL-derived timestamps are persisted in
    metadata for users who need chronological correlation.
    """
    user_text_raw = extract_text(get_content(turn.user_msg))
    user_text, user_text_meta = truncate_text(user_text_raw)

    last_assistant = turn.assistant_msgs[-1]
    assistant_text_raw = extract_text(get_content(last_assistant))
    assistant_text, assistant_text_meta = truncate_text(assistant_text_raw)

    model = get_model(turn.assistant_msgs[0])
    tool_calls = _tool_calls_from_assistants(turn.assistant_msgs)
    for c in tool_calls:
        if c["id"] and c["id"] in turn.tool_results_by_id:
            out_raw = turn.tool_results_by_id[c["id"]]
            out_str = out_raw if isinstance(out_raw, str) else json.dumps(out_raw, ensure_ascii=False)
            out_trunc, out_meta = truncate_text(out_str)
            c["output"] = out_trunc
            c["output_meta"] = out_meta
        else:
            c["output"] = None

    run_tags = tags if tags is not None else _compute_run_tags(transcript_path)
    usage = extract_usage(turn.assistant_msgs)

    # JSONL timestamps preserved in metadata since 4.x doesn't accept
    # start_time/end_time on the public observation API.
    user_ts_str = turn.user_msg.get("timestamp")
    first_asst_ts_str = turn.assistant_msgs[0].get("timestamp")
    last_asst_ts_str = last_assistant.get("timestamp")

    trace_name = f"Claude Code - Turn {turn_num}"
    with propagate_attributes(session_id=session_id, tags=run_tags, trace_name=trace_name):
        with langfuse.start_as_current_observation(
            name=trace_name,
            as_type="span",
            input={"role": "user", "content": user_text},
            metadata={
                "source": "claude-code",
                "session_id": session_id,
                "turn_number": turn_num,
                "transcript_path": str(transcript_path),
                "user_text": user_text_meta,
                "jsonl_user_timestamp": user_ts_str,
            },
        ) as trace_span:
            with langfuse.start_as_current_observation(
                name="Claude Response",
                as_type="generation",
                model=model,
                input={"role": "user", "content": user_text},
                output={"role": "assistant", "content": assistant_text},
                usage_details=usage if usage else None,
                metadata={
                    "assistant_text": assistant_text_meta,
                    "tool_count": len(tool_calls),
                    "jsonl_first_assistant_timestamp": first_asst_ts_str,
                    "jsonl_last_assistant_timestamp": last_asst_ts_str,
                },
            ):
                pass

            for tc in tool_calls:
                in_obj = tc["input"]
                if isinstance(in_obj, str):
                    in_obj, in_meta = truncate_text(in_obj)
                else:
                    in_meta = None
                with langfuse.start_as_current_observation(
                    name=f"Tool: {tc['name']}",
                    as_type="tool",
                    input=in_obj,
                    metadata={
                        "tool_name": tc["name"],
                        "tool_id": tc["id"],
                        "input_meta": in_meta,
                        "output_meta": tc.get("output_meta"),
                    },
                ) as tool_obs:
                    tool_obs.update(output=tc.get("output"))

            trace_span.update(output={"role": "assistant", "content": assistant_text})


# ----------------- Outbox -----------------
def _outbox_session_dir(session_id: str) -> Path:
    return OUTBOX_DIR / session_id

def _outbox_path(session_id: str, turn_num: int) -> Path:
    return _outbox_session_dir(session_id) / f"turn-{turn_num:06d}.json"

def outbox_write(
    session_id: str,
    turn_num: int,
    turn: Turn,
    transcript_path: Path,
    tags: list[str] | None = None,
) -> Path:
    """Serialize a turn to disk. Atomic via tmp + rename.

    Tags are computed at write time (or passed by caller) and persisted
    in the payload so a future drain cycle from a different session
    cannot overwrite them with its own env vars.
    """
    p = _outbox_path(session_id, turn_num)
    p.parent.mkdir(parents=True, exist_ok=True)
    if tags is None:
        tags = _compute_run_tags(transcript_path)
    payload = {
        "schema": 2,
        "session_id": session_id,
        "turn_num": turn_num,
        "transcript_path": str(transcript_path),
        "tags": tags,
        "user_msg": turn.user_msg,
        "assistant_msgs": turn.assistant_msgs,
        "tool_results_by_id": turn.tool_results_by_id,
        "written": datetime.now(timezone.utc).isoformat(),
    }
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    return p

def _load_outbox_file(f: Path) -> tuple[str, int, Turn, Path, list[str] | None] | None:
    """Reload an outbox payload. Returns (session_id, turn_num, Turn, transcript_path, tags).

    `tags` is None for schema=1 payloads written before tag-pinning was
    added; emit_turn's fallback recomputes them from current env vars
    (acceptable degradation since drain happens close in time and the
    drained turn is from a recent outage).
    """
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        debug(f"outbox parse failed for {f}: {e}")
        return None
    try:
        turn = Turn(
            user_msg=data["user_msg"],
            assistant_msgs=data["assistant_msgs"],
            tool_results_by_id=data.get("tool_results_by_id", {}),
        )
        return (
            data["session_id"],
            int(data["turn_num"]),
            turn,
            Path(data.get("transcript_path", "")),
            data.get("tags"),
        )
    except Exception as e:
        debug(f"outbox record malformed {f}: {e}")
        return None

def outbox_scan_and_flush(langfuse: Langfuse) -> tuple[int, int]:
    """Retry any pending outbox files across all sessions. Returns (flushed, remaining)."""
    if not OUTBOX_DIR.exists():
        return 0, 0
    flushed = 0
    remaining = 0
    files = sorted(OUTBOX_DIR.glob("*/turn-*.json"))
    for f in files:
        rec = _load_outbox_file(f)
        if rec is None:
            remaining += 1
            continue
        session_id, turn_num, turn, transcript_path, tags = rec
        try:
            emit_turn(langfuse, session_id, turn_num, turn, transcript_path, tags=tags)
            try:
                f.unlink()
            except Exception:
                pass
            flushed += 1
        except Exception as e:
            debug(f"outbox emit failed for {f}: {e}")
            remaining += 1
    try:
        for d in OUTBOX_DIR.iterdir():
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
    except Exception:
        pass
    return flushed, remaining


# ----------------- Main -----------------
def main() -> int:
    start = time.time()
    debug("Hook started")

    if os.environ.get("TRACE_TO_LANGFUSE", "").lower() != "true":
        return 0

    public_key = os.environ.get("CC_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("CC_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("CC_LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"

    if not public_key or not secret_key:
        return 0

    payload = read_hook_payload()
    session_id, transcript_path = extract_session_and_transcript(payload)
    has_current_work = bool(
        session_id
        and transcript_path
        and transcript_path.exists()
    )

    try:
        langfuse = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    except Exception:
        return 0

    try:
        reachable = bool(langfuse.auth_check())
    except Exception:
        reachable = False
    if not reachable:
        info("Langfuse unreachable; turns will be queued to outbox (no emit).")

    try:
        with FileLock(LOCK_FILE):
            # 1) Drain any stale outbox files from prior runs / other sessions.
            if reachable:
                f_count, r_count = outbox_scan_and_flush(langfuse)
                if f_count or r_count:
                    info(f"Outbox retry: flushed={f_count} remaining={r_count}")

            # 2) Process current session's new turns (if any).
            emitted_this_run = 0
            if has_current_work:
                state = load_state()
                key = state_key(session_id, str(transcript_path))
                ss = load_session_state(state, key)

                msgs, ss = read_new_jsonl(transcript_path, ss)
                if msgs:
                    turns = build_turns(msgs)
                    for t in turns:
                        emitted_this_run += 1
                        turn_num = ss.turn_count + emitted_this_run
                        try:
                            outbox_write(session_id, turn_num, t, transcript_path)
                        except Exception as e:
                            warn(f"outbox write failed for turn {turn_num}: {e}")
                            emitted_this_run -= 1
                            break
                        if reachable:
                            try:
                                emit_turn(langfuse, session_id, turn_num, t, transcript_path)
                                try:
                                    _outbox_path(session_id, turn_num).unlink()
                                except Exception:
                                    pass
                            except Exception as e:
                                debug(f"emit_turn failed for turn {turn_num}: {e}")
                    ss.turn_count += emitted_this_run
                write_session_state(state, key, ss)
                save_state(state)

        try:
            langfuse.flush()
        except Exception:
            pass

        dur = time.time() - start
        if has_current_work:
            info(f"Processed {emitted_this_run} new turns in {dur:.2f}s (session={session_id}, reachable={reachable})")
        else:
            info(f"No current session; outbox-only pass in {dur:.2f}s (reachable={reachable})")
        return 0

    except Exception as e:
        debug(f"Unexpected failure: {e}")
        return 0

    finally:
        try:
            langfuse.shutdown()
        except Exception:
            pass

if __name__ == "__main__":
    sys.exit(main())
