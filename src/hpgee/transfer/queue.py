"""Bounded dispatch, one deferred recovery pass, and disk-backed eager requests."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import os
from pathlib import Path
import time


def run_queue(ids, worker, *, workers, on_result, retry_rounds=1, seconds=None, on_tick=None,
              before_submit=None):
    """Run each ID once, then optionally retry failures in one later pass.

    ``before_submit`` returning False stops new submissions (for example a disk
    limit); in-flight work drains and unsent IDs stay pending, not failed.
    """
    ids = list(ids)
    if workers < 1 or retry_rounds not in (0, 1) or len(set(ids)) != len(ids):
        raise ValueError('Positive workers, unique request IDs and at most one recovery pass required')
    deadline = time.monotonic() + seconds if seconds is not None else float('inf')
    pending = ids
    successful = set()
    admission_stopped = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for round_index in range(retry_rounds + 1):
            iterator = iter(pending)
            futures = {}
            more = True
            while futures or (more and time.monotonic() < deadline):
                while more and len(futures) < workers and time.monotonic() < deadline:
                    uid = next(iterator, None)
                    if uid is None:
                        more = False
                        break
                    if before_submit is not None and before_submit() is False:
                        admission_stopped = True
                        more = False
                        break
                    futures[pool.submit(worker, uid)] = uid
                if not futures:
                    break
                done, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    uid = futures.pop(future)
                    row = dict(future.result(), round=round_index)
                    if row['status'] in ('success', 'skipped'):
                        successful.add(uid)
                    on_result(row)
                if on_tick:
                    on_tick()
            pending = [uid for uid in ids if uid not in successful]
            if admission_stopped or not pending or time.monotonic() >= deadline:
                break
    return dict(complete=not pending, pending=pending, succeeded=len(successful), required=len(ids),
                admission_stopped=admission_stopped)


class PreparedRequests:
    """One private node-local file; offsets, rather than image graphs, stay in RAM."""

    def __init__(self, path, items):
        self.path = Path(path)
        self.index = {}
        self.fd = None
        created = False
        try:
            with self.path.open('xb') as stream:
                created = True
                self.path.chmod(0o600)
                for uid, body in items:
                    if uid in self.index:
                        raise ValueError('Duplicate prepared request')
                    payload = body.encode('utf8')
                    self.index[uid] = (stream.tell(), len(payload))
                    stream.write(payload)
            self.fd = os.open(self.path, os.O_RDONLY)
        except BaseException:
            if created:
                self.path.unlink(missing_ok=True)
            raise

    def __getitem__(self, uid):
        offset, size = self.index[uid]
        return os.pread(self.fd, size, offset).decode('utf8')

    def __enter__(self):
        return self

    def __exit__(self, *error):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.path.unlink(missing_ok=True)
