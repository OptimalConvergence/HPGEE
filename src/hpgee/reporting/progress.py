"""Portable metrics aggregation; never infer dataset completion from log silence."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def summarize_metrics(paths, start: datetime, end: datetime) -> dict:
    """Count timestamped task outcomes and completed payload in [start,end).

    Files are streamed. For large histories run on suitable execution resources.
    Scientific acceptance/final QA live outside generic task metrics and must be
    supplied by the calling workflow. Cache hits contribute no network bytes.
    """
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("Use an increasing timezone-aware interval")
    counts = Counter()
    payload = 0
    retries = 0
    malformed = 0
    latest = None
    files = sorted({Path(path).expanduser().resolve() for path in paths})
    for path in files:
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        raise ValueError("Missing timestamp timezone")
                except (json.JSONDecodeError, KeyError, ValueError):
                    malformed += 1
                    continue
                latest = max(latest, stamp) if latest else stamp
                if not start <= stamp < end:
                    continue
                counts[row["status"]] += 1
                retries += max(0, row.get("attempts", 0) - 1)
                if row["status"] == "success":
                    payload += row.get("bytes_downloaded", 0)
    seconds = (end - start).total_seconds()
    return {
        "start": start.astimezone(timezone.utc).isoformat(),
        "end": end.astimezone(timezone.utc).isoformat(),
        "seconds": seconds,
        "status_counts": dict(counts),
        "successful_image_bytes": payload,
        "image_mib_s": payload / seconds / 2**20,
        "native_retries": retries,
        "malformed_records": malformed,
        "files": len(files),
        "latest_record": latest.isoformat() if latest else None,
        "note": "Task outcomes, not final scientific acceptance; no dataset-completion inference",
    }
