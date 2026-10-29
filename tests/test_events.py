import json
import sys
from unittest.mock import patch

import pytest

from hpgee.reporting.events import record_outcome


def test_success_is_silent_and_does_not_invoke_hook(tmp_path, capsys):
    with patch("hpgee.reporting.events.subprocess.run") as hook:
        event = record_outcome(
            tmp_path, "job-1", ok=True, details={"validated": True}, on_error=["unused"]
        )
    assert event["ok"]
    hook.assert_not_called()
    assert capsys.readouterr().out == ""


def test_failure_hook_receives_persisted_event_once(tmp_path):
    hook = tmp_path / "hook.py"
    marker = tmp_path / "received.jsonl"
    hook.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "event=json.loads(Path(sys.argv[2]).read_text())\n"
        "with open(sys.argv[1], 'a') as out: out.write(json.dumps(event)+'\\n')\n"
    )
    command = [sys.executable, str(hook), str(marker)]
    args = {"ok": False, "details": {"exit_code": "1:0"}, "on_error": command}
    event = record_outcome(tmp_path / "events", "job-2", **args)
    assert event["notification"]["returncode"] == 0
    assert record_outcome(tmp_path / "events", "job-2", **args) == event
    received = marker.read_text().splitlines()
    assert len(received) == 1
    assert json.loads(received[0])["ok"] is False


def test_hook_failure_preserves_failure_for_later_inspection(tmp_path):
    event = record_outcome(
        tmp_path, "job-3", ok=False, details={}, on_error=[str(tmp_path / "missing-command")]
    )
    assert event["notification"]["error_type"] == "FileNotFoundError"
    assert json.loads((tmp_path / "job-3.json").read_text()) == event


def test_conflicting_outcomes_cannot_reuse_an_event_id(tmp_path):
    record_outcome(tmp_path, "job-4", ok=False, details={})
    with pytest.raises(ValueError, match="different terminal outcome"):
        record_outcome(tmp_path, "job-4", ok=True, details={})
