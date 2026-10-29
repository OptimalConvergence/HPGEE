"""Install or verify a physical instruction-only copy of the HPGEE skill."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil


def inventory(root: Path) -> dict[Path, Path]:
    files = {}
    if root.is_symlink():
        raise ValueError(f"Skill directory must not be a symlink: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Skill files must not be symlinks: {path}")
        if path.is_file():
            files[path.relative_to(root)] = path
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1] / "skills" / "efficient-gee"
    destination = args.destination.expanduser().absolute()
    if source.resolve() == destination.resolve():
        parser.error("Destination must be separate from the versioned skill source")
    try:
        expected, existing = inventory(source), inventory(destination)
        extra = existing.keys() - expected.keys()
        if extra:
            raise ValueError(f"Unmanaged destination files; preserve or relocate first: {sorted(map(str, extra))}")
        changed = [name for name, path in expected.items()
                   if name not in existing or path.read_bytes() != existing[name].read_bytes()]
        if args.check:
            if changed:
                raise ValueError(f"Skill copy differs: {sorted(map(str, changed))}")
        else:
            for name in changed:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(expected[name], target)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"{'Verified' if args.check else 'Installed'} {len(expected)} skill files: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
