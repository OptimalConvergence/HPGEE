"""Durable terminal outcomes and optional failure-only notification hooks."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path


def record_outcome(
    directory: str | Path,
    event_id: str,
    *,
    ok: bool,
    details: dict,
    on_error: Sequence[str] | None = None,
) -> dict:
    """Record one immutable terminal outcome per ID; notify failures at most once.

    The optional command receives the JSON event path as its final argument.
    It should enqueue a notification and exit within 30 seconds. No shell is used.
    A hook is an integration point, not an installed agent or messaging service.
    Use distinct IDs for distinct job attempts. Supply only non-secret details.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", event_id):
        raise ValueError("Use a simple, nonempty event ID")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{event_id}.json"
    event = {
        "event_id": event_id,
        "ok": bool(ok),
        "details": details,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "notification": None,
    }
    # A hard-link publication is atomic and exclusive, including concurrent callers.
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(event, stream, indent=2)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            previous = json.loads(path.read_text())
            if previous["ok"] != event["ok"] or previous["details"] != details:
                raise ValueError("Event ID already records a different terminal outcome")
            return previous
    finally:
        temporary.unlink(missing_ok=True)
    if not ok and on_error:
        try:
            result = subprocess.run(
                [*on_error, str(path.resolve())],
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            event["notification"] = {"returncode": result.returncode}
        except (OSError, subprocess.TimeoutExpired) as error:
            event["notification"] = {"error_type": type(error).__name__}
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(event, stream, indent=2)
        temporary.replace(path)
    return event
