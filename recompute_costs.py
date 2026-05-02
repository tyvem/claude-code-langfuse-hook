#!/usr/bin/env python3
"""
Recompute Langfuse generation costs after a model catalog update.

Walks the project's GENERATION observations, finds ones with `modelId=null`
(Langfuse couldn't match the model name to a catalog entry at ingest time
- typically because the catalog was missing that entry), and posts a
generation-update event for each. Re-emitting the model + usage triggers
Langfuse to re-attempt the catalog match and compute cost.

Use after:
  - Adding a new entry to the model catalog (`POST /api/public/models`).
  - Fixing a price entry on an existing catalog model.
  - Recovering from a window where ingest happened with a stale catalog.

Idempotent: observations already matched (modelId set) are skipped, so
re-running is safe.

Reads Langfuse credentials from environment variables:

    LANGFUSE_PUBLIC_KEY
    LANGFUSE_SECRET_KEY
    LANGFUSE_BASE_URL          # optional; defaults to cloud.langfuse.com

Usage:
    recompute_costs.py [--dry-run] [--model NAME] [--limit N]
"""

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone

# This file imports nothing from hook.py at the moment, but kept on path
# in case future helpers are added (mirrors backfill.py layout).


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="Report scope; do not post updates")
    parser.add_argument("--model", help="Only observations whose model name contains this substring")
    parser.add_argument("--limit", type=int, help="Stop after N updates")
    parser.add_argument("--from-start-time", help="ISO8601 lower bound on observation start time (server requires a window)")
    parser.add_argument("--to-start-time", help="ISO8601 upper bound on observation start time")
    parser.add_argument("--days", type=int, default=30, help="Convenience: scan last N days (default 30) if --from-start-time omitted")
    args = parser.parse_args()

    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"
    if not pk or not sk:
        print("ERROR: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set in the environment.", file=sys.stderr)
        return 2

    from langfuse import Langfuse
    from langfuse.api.resources.ingestion.types.ingestion_event import IngestionEvent_GenerationUpdate
    from langfuse.api.resources.ingestion.types.update_generation_body import UpdateGenerationBody

    langfuse = Langfuse(public_key=pk, secret_key=sk, host=host)

    # The v1 API rejects unbounded scans, so we chunk by day.
    from datetime import timedelta
    if args.from_start_time:
        from_start = datetime.fromisoformat(args.from_start_time.replace("Z", "+00:00"))
    else:
        from_start = datetime.now(timezone.utc) - timedelta(days=args.days)
    to_start = (
        datetime.fromisoformat(args.to_start_time.replace("Z", "+00:00"))
        if args.to_start_time else datetime.now(timezone.utc)
    )
    print(f"Window: {from_start.isoformat()} -> {to_start.isoformat()}")

    seen = 0
    candidates = []
    chunk_start = from_start
    chunk_size = timedelta(days=1)
    while chunk_start < to_start:
        chunk_end = min(chunk_start + chunk_size, to_start)
        page = 1
        while True:
            res = langfuse.api.observations.get_many(
                type="GENERATION", page=page, limit=50,
                from_start_time=chunk_start, to_start_time=chunk_end,
            )
            data = res.data or []
            if not data:
                break
            for o in data:
                seen += 1
                if o.model_id is not None:
                    continue
                if not o.model:
                    continue
                if args.model and args.model.lower() not in o.model.lower():
                    continue
                candidates.append(o)
                if args.limit and len(candidates) >= args.limit:
                    break
            if args.limit and len(candidates) >= args.limit:
                break
            meta = res.meta
            if not meta or page >= (meta.total_pages or 1):
                break
            page += 1
        if args.limit and len(candidates) >= args.limit:
            break
        chunk_start = chunk_end

    print(f"Scanned {seen} generations, {len(candidates)} need re-pricing")

    if args.dry_run or not candidates:
        for o in candidates[:20]:
            print(f"  DRY  {o.id}  model={o.model}  trace={o.trace_id}")
        if len(candidates) > 20:
            print(f"  ... +{len(candidates) - 20} more")
        return 0

    # Post generation-update events in batches of 100.
    now_iso = datetime.now(timezone.utc).isoformat()
    batch = []
    updated = 0
    for o in candidates:
        batch.append(IngestionEvent_GenerationUpdate(
            id=uuid.uuid4().hex,
            timestamp=now_iso,
            body=UpdateGenerationBody(
                id=o.id,
                trace_id=o.trace_id,
                model=o.model,
                usage_details=o.usage_details or None,
            ),
        ))
        if len(batch) >= 100:
            langfuse.api.ingestion.batch(batch=batch)
            updated += len(batch)
            print(f"  posted {len(batch)} (cumulative {updated})", flush=True)
            batch = []
    if batch:
        langfuse.api.ingestion.batch(batch=batch)
        updated += len(batch)
        print(f"  posted {len(batch)} (cumulative {updated})")

    langfuse.flush()
    print(f"\nDone: {updated} generation-update events posted (Langfuse re-matches catalog async)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
