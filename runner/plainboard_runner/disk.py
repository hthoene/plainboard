"""Folder sizes, measured fast: several ``du`` at once over the parts of the asked folders (#191).

With millions of small files (build outputs, node_modules, caches) du spends nearly all its time in the
kernel, one ``stat`` after another; on ZFS that is some 17 µs a file. Several du at once spread that over
the cores (4.5 M files: 81 s with one du, 22 s with one per agent folder). So each asked folder is split
``SPLIT_DEPTH`` levels down: what lies directly in the levels above is ``lstat``-ed here, each folder at
the bottom goes to a du (at the lowest priority, ``WORKERS`` at once for the whole runner), and the sums
are added up again. Same numbers as one du (bytes on disk, one file system), except that a file
hard-linked into two parts counts in each.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

WORKERS = max(2, min(12, os.cpu_count() or 2))
SPLIT_DEPTH = 3
BATCH = 64  # folders per du: fewer processes, still small enough to share the work out evenly
_slots = threading.BoundedSemaphore(WORKERS)  # all du of this runner together


def _bytes(st: os.stat_result) -> int:
    return st.st_blocks * 512 or st.st_size  # on disk, as du counts


def _du(paths: list[str], deadline: float) -> dict[str, tuple[int, float]] | None:
    """Bytes and newest change of each path, from one du; None when it did not finish in time."""
    cmd = ["du", "-skx", "--time", "--time-style=+%s", "--", *paths]
    if shutil.which("ionice"):
        cmd = ["ionice", "-c3", *cmd]
    if shutil.which("nice"):
        cmd = ["nice", "-n19", *cmd]
    with _slots:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            out = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=left).stdout
        except subprocess.TimeoutExpired:
            return None
    found: dict[str, tuple[int, float]] = {}
    for line in out.decode(errors="replace").splitlines():
        kb, _, rest = line.partition("\t")
        at, _, path = rest.partition("\t")
        if kb.isdigit():
            found[path] = (int(kb) * 1024, float(at) if at.isdigit() else 0.0)
    return found


class _Node:
    __slots__ = ("path", "own", "newest", "children", "leaf", "kids")

    def __init__(self, path: str, own: int, newest: float) -> None:
        self.path, self.own, self.newest = path, own, newest
        self.children: list[_Node] = []  # folders split further
        self.leaf = False  # measured by a du
        self.kids: dict[str, tuple[int, float]] = {}  # files (and the like) directly in it


def _split(path: str, dev: int, depth: int, leaves: list[_Node], seen: set[tuple[int, int]]) -> _Node | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    node = _Node(path, _bytes(st), st.st_mtime)
    if not stat.S_ISDIR(st.st_mode) or st.st_dev != dev:
        if st.st_dev != dev:
            return None
        return node
    if depth <= 0:
        node.leaf = True
        leaves.append(node)
        return node
    try:
        with os.scandir(path) as it:
            names = [e.path for e in it]
    except OSError:
        return node
    for child in names:
        try:
            cst = os.lstat(child)
        except OSError:
            continue
        if cst.st_dev != dev:
            continue
        if stat.S_ISDIR(cst.st_mode):
            sub = _split(child, dev, depth - 1, leaves, seen)
            if sub is not None:
                node.children.append(sub)
            continue
        if cst.st_nlink > 1:
            if (cst.st_dev, cst.st_ino) in seen:
                node.kids[child] = (0, cst.st_mtime)
                continue
            seen.add((cst.st_dev, cst.st_ino))
        node.kids[child] = (_bytes(cst), cst.st_mtime)
    return node


def _sum(node: _Node, measured: dict[str, tuple[int, float]], out: dict[str, tuple[int, float]], top: bool, children: bool) -> tuple[int, float] | None:
    """Bytes and newest change of a node; None when a part of it was not measured. Fills ``out`` for the asked
    folder and, with ``children``, for each entry right in it."""
    if node.leaf:
        got = measured.get(node.path)
    else:
        total, newest, whole = node.own, node.newest, True
        for child in node.children:
            part = _sum(child, measured, out, False, False)
            if part is None:
                whole = False
                continue
            total, newest = total + part[0], max(newest, part[1])
            if top and children:
                out[child.path] = part
        for path, (n, at) in node.kids.items():
            total, newest = total + n, max(newest, at)
            if top and children:
                out[path] = (n, at)
        got = (total, newest) if whole else None
    if top and got is not None:
        out[node.path] = got
    return got


def sizes(paths: list[str], timeout: float = 3600, children: bool = False) -> dict[str, tuple[int, float]]:
    """Bytes on disk and newest change (unix time) of each path and, with ``children``, of each entry right in it,
    on the path's own file system. A path not measured completely by the timeout is left out."""
    deadline = time.monotonic() + timeout
    leaves: list[_Node] = []
    seen: set[tuple[int, int]] = set()
    roots: list[_Node] = []
    for path in dict.fromkeys(paths):
        try:
            dev = os.lstat(path).st_dev
        except OSError:
            continue
        node = _split(path, dev, SPLIT_DEPTH, leaves, seen)
        if node is not None:
            roots.append(node)
    measured: dict[str, tuple[int, float]] = {}
    if leaves:
        names = sorted(n.path for n in leaves)
        size = max(1, min(BATCH, len(names) // (WORKERS * 4)))
        batches = [names[i : i + size] for i in range(0, len(names), size)]
        with ThreadPoolExecutor(WORKERS) as pool:
            for batch, got in zip(batches, pool.map(lambda b: _du(b, deadline), batches)):
                if got is not None:
                    measured.update(got)
                    measured.update({p: (0, 0.0) for p in batch if p not in got})  # gone meanwhile
    out: dict[str, tuple[int, float]] = {}
    for node in roots:
        _sum(node, measured, out, True, children)
    return out

