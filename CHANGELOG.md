# Changelog

## 0.2.0 (2026-05-02)

Port to langfuse 4.x.

### Breaking

- Now requires `langfuse>=4.0,<5`. Users on langfuse 3.x must pin to `claude-code-langfuse-hook==0.1.x`.

### Changed

- `emit_turn` rewritten to use the OTel-style 4.x SDK API (`start_as_current_observation` + `propagate_attributes`) instead of the explicit ingestion-API events. Same trace shape: trace span -> generation observation -> N tool observations.
- Observations now stamped at wall-clock "now" rather than JSONL-derived timestamps. The difference is sub-second for the live hook. JSONL timestamps preserved in observation metadata as `jsonl_user_timestamp`, `jsonl_first_assistant_timestamp`, `jsonl_last_assistant_timestamp`.
- Backfill driver: backfilled traces land at backfill-emit time, not original session time. Original-session timestamps are still available via the metadata fields above. This is a regression vs 0.1.x; use the metadata-based query if you need chronological-by-original-time views.

### Internal

- Removed the unused `_parse_ts` helper.

## 0.1.0 (2026-05-02)

Initial release. Pinned to `langfuse>=3.0,<4`.

- Streaming-update tool_use merge in `build_turns` recovers ~21% of tool_uses on real sessions (sample of 30, range 0.6% to 62%) that naive last-wins dedup loses.
- On-disk outbox at `~/.claude/state/langfuse_outbox/<sid>/turn-NNNNNN.json`. State advances on disk write, not Langfuse ack. Outage of any duration queues; next successful connect drains.
- Tag pinning at outbox-write time. A different session draining your outbox can't overwrite persona/project tags with its own env. Schema-bumped from 1 to 2; legacy schema=1 payloads load with `tags=None` and emit_turn falls back to env recomputation.
- Optional CLIs: `backfill.py` for historical sessions, `recompute_costs.py` for re-pricing after model catalog updates.
- 20 pytest cases cover streaming-merge correctness, outbox round-trip, tag pinning, and schema=1 backwards compat.
