from types import SimpleNamespace
import sys

from hpgee.transfer.progress import TransferProgress


def test_progress_estimates_remaining_time_and_counts_only_received_bytes(monkeypatch, capsys):
    clock = [0.0]
    monkeypatch.setattr("hpgee.transfer.progress.time.monotonic", lambda: clock[0])
    with TransferProgress(4, "L30") as progress:
        clock[0] = 10.0
        progress(SimpleNamespace(status="success", bytes_downloaded=2_000_000))
    output = capsys.readouterr().err
    assert "0/4 ETA ?" in output
    assert "1/4 ETA 0:00:30" in output
    assert "BANDWIDTH~ 0.20 MB/s" in output
    assert "\r" not in output  # Slurm logs retain readable lines.


def test_progress_includes_failed_and_skipped_blocks_without_inventing_bytes(monkeypatch, capsys):
    clock = [0.0]
    monkeypatch.setattr("hpgee.transfer.progress.time.monotonic", lambda: clock[0])
    with TransferProgress(3, "S1") as progress:
        clock[0] = 2.0
        for status in ("success", "failed", "skipped"):
            progress(SimpleNamespace(status=status, bytes_downloaded=1_000_000 if status == "success" else 0))
    output = capsys.readouterr().err
    assert "3/3 ETA 0:00:00" in output
    assert "BANDWIDTH~ 0.50 MB/s" in output
    assert "ok=1 failed=1 skipped=1" in output


def test_disabled_progress_leaves_debug_terminal_untouched(capsys):
    with TransferProgress(1, "debug", enabled=False) as progress:
        progress(SimpleNamespace(status="success", bytes_downloaded=100))
    assert capsys.readouterr().err == ""


def test_terminal_progress_rewrites_one_line_and_ends_with_newline(monkeypatch):
    from io import StringIO
    class Terminal(StringIO):
        def isatty(self):
            return True
    terminal = Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    with TransferProgress(1, "L30") as progress:
        progress(SimpleNamespace(status="success", bytes_downloaded=1))
    assert "\r" in terminal.getvalue()
    assert terminal.getvalue().endswith("\n")


def test_output_failure_disables_display_without_interrupting_work(monkeypatch):
    from io import StringIO
    class BrokenStream(StringIO):
        def write(self, text):
            raise OSError("output unavailable")
    monkeypatch.setattr(sys, "stderr", BrokenStream())
    with TransferProgress(1, "L30") as progress:
        progress(SimpleNamespace(status="success", bytes_downloaded=100))
    assert not progress.enabled
