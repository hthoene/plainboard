import os
import subprocess
from pathlib import Path

from plainboard_runner import disk


def _du(path: Path) -> int:
    return int(subprocess.run(["du", "-skx", str(path)], capture_output=True, check=True).stdout.split()[0]) * 1024


def _tree(root: Path) -> None:
    deep = root / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "big").write_bytes(os.urandom(300_000))
    (root / "top").write_bytes(os.urandom(50_000))
    (root / "a" / "mid").write_bytes(os.urandom(70_000))
    (root / "x").mkdir()
    (root / "x" / "y").write_bytes(os.urandom(20_000))
    os.utime(deep / "big", (2_000_000_000, 2_000_000_000))
    os.sync()  # ZFS counts the blocks a little later


def test_sizes_match_du_in_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(disk, "SPLIT_DEPTH", 2)
    _tree(tmp_path)
    found = disk.sizes([str(tmp_path), str(tmp_path / "missing")], children=True)
    assert abs(found[str(tmp_path)][0] - _du(tmp_path)) < 4096  # du rounds each part up to KiB
    assert found[str(tmp_path)][1] == 2_000_000_000  # the newest change deep down
    assert set(found) == {str(tmp_path / n) for n in ("", "a", "top", "x")}
    assert all(abs(found[str(tmp_path / n)][0] - _du(tmp_path / n)) < 4096 for n in ("a", "top", "x"))
    assert disk.sizes([str(tmp_path / "top")]) == {str(tmp_path / "top"): found[str(tmp_path / "top")]}


def test_unfinished_paths_left_out(tmp_path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.setattr(disk, "_du", lambda paths, deadline: None)
    assert disk.sizes([str(tmp_path)]) == {}
