"""Dependency-free block progress for terminals and batch logs."""
from collections import Counter
from datetime import timedelta
import sys
import time


class TransferProgress:
    """Estimated ETA and task-payload MB/s over wall time, including publication.

    Payload bytes come from terminal TaskResults; earlier retry bodies are not
    counted. The benchmark's HTTP-span throughput is recorded separately.
    """

    def __init__(self, total, label, *, enabled=True):
        self.total = total
        self.label = label
        self.enabled = enabled
        self.stream = sys.stderr
        self.started = self.last_rendered = time.monotonic()
        self.done = self.bytes = self.shown = 0
        self.counts = Counter()

    def __enter__(self):
        if self.enabled:
            self.render()
        return self

    def __call__(self, result):
        if self.enabled:
            self.done += 1
            self.bytes += result.bytes_downloaded
            self.counts[result.status] += 1
            if self.done == self.total or time.monotonic() - self.last_rendered >= 1:
                self.render()

    def write(self, line, *, final=False):
        try:
            terminal = self.stream.isatty()
            if final:
                if terminal:
                    print(file=self.stream, flush=True)
            else:
                print(("\r\033[K" if terminal else "") + line,
                      end="" if terminal else "\n", file=self.stream, flush=True)
        except (OSError, ValueError):
            self.enabled = False

    def render(self):
        self.last_rendered = time.monotonic()
        self.shown = self.done
        elapsed = max(self.last_rendered - self.started, 1e-9)
        eta = str(timedelta(seconds=int(elapsed * (self.total - self.done) / self.done))) if self.done else "?"
        filled = int(20 * self.done / self.total) if self.total else 20
        bar = "#" * filled + "-" * (20 - filled)
        self.write(f"{self.label} [{bar}] {self.done}/{self.total} ETA {eta} "
                   f"BANDWIDTH~ {self.bytes / elapsed / 1e6:.2f} MB/s "
                   f"ok={self.counts['success']} failed={self.counts['failed']} skipped={self.counts['skipped']}")

    def __exit__(self, *exc):
        if self.enabled:
            if self.shown != self.done:
                self.render()
            self.write("", final=True)
