# claude-code-langfuse-hook

[![CI](https://github.com/tyvem/claude-code-langfuse-hook/actions/workflows/ci.yml/badge.svg)](https://github.com/tyvem/claude-code-langfuse-hook/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Claude Code Stop hook that ships every assistant turn to [Langfuse](https://langfuse.com) as a trace, with a durable on-disk outbox and a fix for tool_use loss under streaming updates.

Langfuse is open-source and works the same against [Langfuse Cloud](https://cloud.langfuse.com) or a [self-hosted instance](https://langfuse.com/self-hosting). This hook treats them identically: point `LANGFUSE_BASE_URL` at whichever you run, and the rest is the same.

The Langfuse docs ([source](https://github.com/langfuse/langfuse-docs/blob/main/content/integrations/other/claude-code.mdx)) include a working starter hook. This one started from there and kept three changes that came out of running the starter hook in production for a few weeks:

1. **Streaming merge.** Naive last-wins dedup of streaming updates drops ~21% of tool_use observations on real sessions (sample of 30 sessions, range 0.6% to 62%). This package unions tool_uses across update entries.
2. **Outbox durability.** Every turn writes to a per-session outbox file before the Langfuse flush attempt. State advances on the disk write, not the network ack. Any-duration outage queues; the next successful connect drains.
3. **Tag pinning.** Persona and project tags are captured at write time and persisted in the outbox payload. A different session draining your outbox cannot overwrite them with its own env.

## Install

Two files (`hook.py` plus the `langfuse` Python package) are all you need for the live hook. The other two scripts (`backfill.py`, `recompute_costs.py`) are optional CLIs.

```bash
git clone https://github.com/tyvem/claude-code-langfuse-hook.git
cd claude-code-langfuse-hook
python3 -m venv .venv
.venv/bin/pip install -e .
```

Or just drop `hook.py` next to a Python that already has `langfuse` installed.

## Quick start

1. Get Langfuse credentials. From your project's settings page in the Langfuse UI, copy the public key, secret key, and host URL.

   - **Self-hosted**: your `LANGFUSE_BASE_URL` is wherever you deployed Langfuse, e.g. `https://langfuse.example.com`. Self-hosting docs: https://langfuse.com/self-hosting.
   - **Cloud**: `LANGFUSE_BASE_URL=https://cloud.langfuse.com` (or the EU region URL listed in your Langfuse dashboard).

2. Export them in the shell that launches `claude`:

   ```bash
   export LANGFUSE_PUBLIC_KEY=pk-lf-...
   export LANGFUSE_SECRET_KEY=sk-lf-...
   export LANGFUSE_BASE_URL=https://langfuse.example.com   # self-hosted or cloud
   export TRACE_TO_LANGFUSE=true
   ```

   The hook is gated on `TRACE_TO_LANGFUSE=true`. Without it, the hook is a no-op, so you can leave the wiring in place and toggle traces on or off per shell.

3. Wire the hook into Claude Code via `~/.claude/settings.json`:

   ```json
   {
     "hooks": {
       "Stop": [
         {
           "matcher": "*",
           "hooks": [
             {
               "type": "command",
               "command": "/usr/bin/env python3 /absolute/path/to/hook.py"
             }
           ]
         }
       ]
     }
   }
   ```

   See [examples/settings.json](examples/settings.json).

4. Run a Claude Code session. After the first turn ends, look in Langfuse for a trace named `Claude Code - Turn 1` tagged `claude-code`.

The hook keeps a small JSON state file at `~/.claude/state/langfuse_state.json` so each invocation only ships turns it hasn't seen. Logs go to `~/.claude/state/langfuse_hook.log`.

## How it works

### Streaming merge

Claude Code's transcript JSONL emits progressive updates of the same assistant `message.id` while the model is still producing tokens. Each update carries one new `tool_use` block (not the cumulative list). The straightforward "keep the latest entry per message.id" approach drops every tool_use that wasn't in the final update.

Across 30 sampled real sessions, the naive approach lost on average 20.85% of tool_use records, with individual sessions ranging from 0.6% (text-heavy work) to 62% (heavy multi-tool sessions). The fix unions tool_uses across all update entries, dedups by `tool_use.id`, and walks back from the final entry until it finds non-empty text. See [tests/test_streaming_merge.py](tests/test_streaming_merge.py) for the exact contract.

### Outbox durability

Every turn writes to `~/.claude/state/langfuse_outbox/<session>/turn-NNNNNN.json` before the hook tries to emit. The state file (offset, turn count) advances on the outbox write, not on the Langfuse ack. Three consequences:

- A Langfuse outage of any duration just fills up the outbox. Nothing is lost.
- Every hook invocation begins with a scan across all session subdirectories under `langfuse_outbox/`, retrying any pending files. Drain doesn't have to happen in the same session that wrote them.
- A pre-flight `auth_check()` keeps emit attempts off the network when Langfuse is down, so the log doesn't fill up with retry noise.

Outbox files are deleted on successful emit. Empty session directories get pruned on the next drain pass.

### Tag pinning

The hook tags every trace with `claude-code`, optionally `persona:<id>` from the `PERSONA` env var, and optionally `project:<name>` from `CC_LANGFUSE_PROJECT_TAG` (or a fallback derived from the transcript's parent directory name).

These tags are computed at outbox-write time, persisted in the payload (`schema=2`), and reused at drain time. A session draining another session's outbox keeps the originating session's tags, even if the draining session has different env vars. Without this, a persona-attributed agent run that goes to outbox during an outage would land in Langfuse with the wrong (or no) persona tag if a different session drained it later.

## Backfill old sessions

`backfill.py` walks `~/.claude/projects/*/<sid>.jsonl` and emits any turns that haven't shipped yet, tagged `backfill`. Per-session progress lives in `~/.claude/state/backfill_state.json`, so re-running is incremental.

```bash
LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... LANGFUSE_BASE_URL=... \
  python3 backfill.py --dry-run                        # report scope
  python3 backfill.py                                  # ship all unshipped
  python3 backfill.py --project myproject --limit 5    # narrow the scope
  python3 backfill.py --only <session-uuid>            # one specific session
```

Backfilled traces use timestamps from the transcript's JSONL entries, so they land at the original session time rather than at backfill-emit time.

## Recompute costs after a model catalog update

Langfuse computes generation cost at ingestion time by matching `model` against the project's catalog. If a model name is missing from the catalog at ingest, the resulting observation has `modelId=null` and no cost. Adding the catalog entry later does not retroactively price existing observations.

`recompute_costs.py` walks GENERATION observations with `modelId=null` and posts `generation-update` events, which makes Langfuse retry the catalog match.

```bash
python3 recompute_costs.py --dry-run                   # report scope
python3 recompute_costs.py                             # default: last 30 days
python3 recompute_costs.py --days 90 --model claude    # broader window, narrow model
```

Idempotent: observations with `modelId` already set are skipped.

## Configuration

| Env var | Purpose |
|---|---|
| `TRACE_TO_LANGFUSE` | Set to `true` to enable the hook. Anything else (or unset) is a no-op. |
| `LANGFUSE_PUBLIC_KEY` | Langfuse project public key. Required. |
| `LANGFUSE_SECRET_KEY` | Langfuse project secret key. Required. |
| `LANGFUSE_BASE_URL` | URL of your Langfuse instance (self-hosted or cloud). Defaults to `https://cloud.langfuse.com`. |
| `PERSONA` | Optional. When set, traces get a `persona:<value>` tag. |
| `CC_LANGFUSE_PROJECT_TAG` | Optional. Overrides the auto-derived project tag. |
| `CC_LANGFUSE_DEBUG` | Set to `true` for verbose hook logs. |
| `CC_LANGFUSE_MAX_CHARS` | Truncation threshold for user/assistant text payloads. Defaults to 20000. |

Credentials live in your shell's environment, however you load them: direnv, 1Password CLI, plain `.env`, your secret manager of choice. The hook does not read any config file.

## Troubleshooting

**Hook seems to do nothing.** Check `TRACE_TO_LANGFUSE=true` is exported in the shell that launched `claude`. Then check `~/.claude/state/langfuse_hook.log` for `Hook started` lines. If those are missing, the hook isn't being invoked: confirm the path in your `settings.json` and that the file is executable by the Python you pointed at it.

**`Langfuse unreachable; turns will be queued to outbox`.** Network or auth issue. The hook is doing the right thing: queueing for later. Check `~/.claude/state/langfuse_outbox/` for accumulated turn files. Once Langfuse is back, the next hook invocation will drain them. For self-hosted instances, common causes are an expired TLS cert, a stopped container, or a reverse proxy that's lost the upstream.

**Traces appear but cost is zero.** Your model isn't in the Langfuse catalog yet, or its catalog entry is missing prices. Add the model under Settings → Models in the Langfuse UI, then run `recompute_costs.py` to retro-price existing traces.

**Outbox files keep accumulating.** Either Langfuse has been down for a long time, or auth keeps failing. Check `~/.claude/state/langfuse_hook.log` for the most recent `reachable=False` line. If auth is the problem, the keys you exported are wrong or expired.

**The hook itself is hanging Claude Code.** The hook has a 15-second budget for the Langfuse SDK calls. If it's hanging longer than that, file an issue with the contents of `langfuse_hook.log`.

## Cost semantics

Langfuse's `totalCost` field uses public API list prices for each model. If you're on a Claude subscription rather than per-token billing, the number is hypothetical. It's still useful for relative comparisons (Opus vs Sonnet, session-to-session) and for "what would this have cost at API prices" sanity checks.

Cache tokens (`cache_read_input_tokens`, `cache_creation_input_tokens`) are recorded in `usageDetails` but not priced separately. The single input price applies to uncached input only, so cost will under-report on cache-heavy sessions.

## Tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

20 tests cover streaming-merge correctness, outbox round-trip, tag pinning, and backwards compatibility with the older outbox payload schema.

## License

MIT. See [LICENSE](LICENSE).
