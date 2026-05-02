#!/usr/bin/env python3
"""
Backfill historical Claude Code session JSONLs to Langfuse.

Walks ~/.claude/projects/*/<sid>.jsonl and emits unshipped turns to Langfuse,
tagged `backfill`. Per-session progress is tracked in
~/.claude/state/backfill_state.json keyed by sha256(session_id::transcript_path)
so re-running ships only the turns that arrived since the last pass.

Reads Langfuse credentials from environment variables. Set them however
you load secrets (direnv, 1Password CLI, plain shell export, etc.):

    LANGFUSE_PUBLIC_KEY
    LANGFUSE_SECRET_KEY
    LANGFUSE_BASE_URL          # optional; defaults to cloud.langfuse.com

Usage:
    backfill.py [--dry-run] [--project NAME] [--only SESSION_UUID] [--limit N]

Reuses parsing/emit logic from hook.py - see that file for turn-grouping
and Langfuse trace shape.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hook as lh  # noqa: E402

PROJECTS_DIR = Path.home() / ".claude" / "projects"
STATE_FILE = Path.home() / ".claude" / "state" / "backfill_state.json"


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _project_tag(project_dir_name: str) -> str:
    """Derive a project tag from Claude Code's project dir naming.

    Claude Code stores transcripts under ~/.claude/projects/<encoded-cwd>/
    where the dir name is the cwd with slashes turned into dashes. We
    take the last dash-separated segment as the project tag, e.g.
    `-Users-alice-Code-acme` -> `acme`.
    """
    return project_dir_name.rstrip("/").rsplit("-", 1)[-1]


def _read_messages(path: Path) -> list:
    msgs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except Exception:
                continue
    return msgs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="Report what would ship; do not emit")
    parser.add_argument("--project", help="Only sessions whose project dir tag contains this substring")
    parser.add_argument("--only", help="Only this session UUID")
    parser.add_argument("--limit", type=int, help="Process at most N sessions")
    args = parser.parse_args()

    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"
    if not pk or not sk:
        print("ERROR: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set in the environment.", file=sys.stderr)
        return 2

    os.environ["CC_LANGFUSE_BACKFILL"] = "true"

    langfuse = None
    if not args.dry_run:
        from langfuse import Langfuse
        langfuse = Langfuse(public_key=pk, secret_key=sk, host=host)

    state = _load_state()
    total_turns = 0
    total_sessions = 0

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        project_tag = _project_tag(project_dir.name)
        if args.project and args.project.lower() not in project_tag.lower():
            continue

        for jsonl in sorted(project_dir.glob("*.jsonl")):
            sid = jsonl.stem
            if args.only and sid != args.only:
                continue
            if args.limit and total_sessions >= args.limit:
                break

            os.environ["CC_LANGFUSE_PROJECT_TAG"] = project_tag

            key = lh.state_key(sid, str(jsonl))
            already = int(state.get(key, {}).get("last_turn", 0))

            msgs = _read_messages(jsonl)
            if not msgs:
                continue
            turns = lh.build_turns(msgs)
            to_emit = turns[already:]
            if not to_emit:
                continue

            if args.dry_run:
                print(f"  DRY  +{len(to_emit):3d} turns  {project_tag}/{sid[:8]}  (existing={already}, total={len(turns)})")
                total_turns += len(to_emit)
                total_sessions += 1
                continue

            emitted = 0
            for t in to_emit:
                turn_num = already + emitted + 1
                try:
                    lh.emit_turn(langfuse, sid, turn_num, t, jsonl)
                    emitted += 1
                except Exception as e:
                    print(f"  WARN  emit failed: {sid} turn={turn_num}: {e}", file=sys.stderr)
                    break

            if emitted:
                state[key] = {
                    "last_turn": already + emitted,
                    "project_tag": project_tag,
                    "session_id": sid,
                    "transcript_path": str(jsonl),
                    "updated": datetime.now(timezone.utc).isoformat(),
                }
                _save_state(state)
                total_turns += emitted
                total_sessions += 1
                print(f"  +{emitted:3d} turns  {project_tag}/{sid[:8]}  ({already + emitted}/{len(turns)})")

    if langfuse:
        langfuse.flush()
        langfuse.shutdown()

    verb = "would ship" if args.dry_run else "shipped"
    print(f"\nDone: {total_turns} turns {verb} across {total_sessions} sessions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
