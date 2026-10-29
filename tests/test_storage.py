from pathlib import Path

import pytest

from geesampler.storage import nfs_mount, require_local_sqlite


def test_nfs_detection_uses_longest_matching_mount():
    info = (
        "1 0 0:1 / / rw - ext4 /dev/root rw\n"
        "2 1 0:2 / /nfs-data0 rw - nfs4 server:/data rw\n"
        "3 2 0:3 / /nfs-data0/cache rw - tmpfs tmpfs rw\n"
    )
    assert nfs_mount("/nfs-data0/job/queue.sqlite", info) == "/nfs-data0"
    assert nfs_mount("/nfs-data0/cache/queue.sqlite", info) is None
    assert nfs_mount("/tmp/job/queue.sqlite", info) is None


def test_local_sqlite_allows_temporary_path(tmp_path):
    require_local_sqlite(tmp_path / "queue.sqlite")


def test_nfs_sqlite_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr("geesampler.storage.nfs_mount", lambda *_: "/nfs-data0")
    with pytest.raises(RuntimeError, match="on NFS mount"):
        require_local_sqlite(tmp_path / "queue.sqlite")
