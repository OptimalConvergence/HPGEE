"""Refuse page-oriented SQLite workloads on NFS-mounted paths."""

from pathlib import Path


def nfs_mount(path: str | Path, mountinfo: str) -> str | None:
    resolved = Path(path).expanduser().resolve()
    best: tuple[int, str, str] | None = None
    for line in mountinfo.splitlines():
        try:
            left, right = line.split(" - ", 1)
            mount = Path(bytes(left.split()[4], "utf-8").decode("unicode_escape"))
            kind = right.split()[0]
        except (IndexError, ValueError):
            continue
        if not resolved.is_relative_to(mount):
            continue
        if best is None or len(str(mount)) > best[0]:
            best = len(str(mount)), str(mount), kind
    return best[1] if best and best[2] in {"nfs", "nfs4"} else None


def require_local_sqlite(path: str | Path) -> None:
    """Make NFS database placement an explicit startup error, not a cluster incident."""
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text()
    except OSError:
        return
    mount = nfs_mount(path, mountinfo)
    if mount:
        raise RuntimeError(
            f"SQLite database {path} is on NFS mount {mount}; "
            "stage the database on allocated node-local storage before running"
        )
