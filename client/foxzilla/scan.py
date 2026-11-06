"""The pre-flight scan: what is already at the destination, and what to do."""
from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .backends import Backend


CLEAR, IDENTICAL, SAME_SIZE, CONFLICT = "clear", "identical", "same size", "conflict"


@dataclass
class PlanItem:
    src_path: str
    dst_path: str
    size: int
    verdict: str
    is_dir: bool = False

    @property
    def name(self):
        return posixpath.basename(self.src_path.rstrip("/")) or self.src_path

    @property
    def exists(self):
        return self.verdict != CLEAR


def compare_paths(src: Backend, src_path: str, dst: Backend, dst_path: str,
                  dst_size: int | None = None) -> str:
    """
    Decide whether a file is already at the destination.

    Size is checked first because it is nearly free and settles most cases.
    Only if the sizes match do we ask both ends for a hash, and only when both
    can produce one without transferring the file. Where they cannot - a
    chrooted account with no shell, S3, WebDAV - the honest answer is "same
    size", not "identical", and the caller is told which it got.

    `dst_size` may be passed in when the caller already listed the destination
    directory, which avoids a per-file round trip.
    """
    if dst_size is None:
        dst_size = dst.size_of(dst_path)
    if dst_size < 0:
        return CLEAR
    src_size = src.size_of(src_path)
    if src_size < 0 or src_size != dst_size:
        return CONFLICT
    a = src.checksum(src_path)
    b = dst.checksum(dst_path) if a else None
    if a and b:
        return IDENTICAL if a == b else CONFLICT
    return SAME_SIZE


def plan_transfer(src: Backend, src_path: str, dst: Backend, dst_path: str,
                  is_dir: bool, note=None, cancelled=None) -> list[PlanItem]:
    """
    Walk what is about to be sent and check each file against the destination.

    Directories are expanded here rather than at transfer time so the user is
    shown the real picture - "3 of 40 already there" - before anything moves.

    The destination is listed once per directory rather than stat'ed once per
    file: over SFTP each stat is a separate process, so a forty-file folder
    went from about forty round trips to one.
    """
    out: list[PlanItem] = []
    stack = [(src_path, dst_path, is_dir)]
    scanned = 0
    while stack:
        if cancelled is not None and cancelled.is_set():
            break
        sp, dp, isdir = stack.pop()

        if not isdir:
            out.append(PlanItem(sp, dp, src.size_of(sp),
                                compare_paths(src, sp, dst, dp)))
            continue

        out.append(PlanItem(sp, dp, 0, CLEAR, is_dir=True))
        try:
            entries = src.listdir(sp)
        except Exception:                            # noqa: BLE001
            continue
        try:
            known = {e.name: e.size for e in dst.listdir(dp) if not e.is_dir}
        except Exception:                            # noqa: BLE001
            known = {}                               # destination absent: all new
        for e in entries:
            if e.is_dir:
                stack.append((src.join(sp, e.name), posixpath.join(dp, e.name), True))
                continue
            scanned += 1
            if note:
                note(scanned, e.name)
            child_dst = posixpath.join(dp, e.name)
            out.append(PlanItem(
                src.join(sp, e.name), child_dst, e.size,
                compare_paths(src, src.join(sp, e.name), dst, child_dst,
                              dst_size=known.get(e.name, -1))))
    return out


def summarise(plan: list[PlanItem]) -> dict:
    files = [p for p in plan if not p.is_dir]
    counts = {v: sum(1 for p in files if p.verdict == v)
              for v in (CLEAR, IDENTICAL, SAME_SIZE, CONFLICT)}
    counts["files"] = len(files)
    counts["bytes"] = sum(p.size for p in files)
    counts["new_bytes"] = sum(p.size for p in files if p.verdict == CLEAR)
    return counts


# What each choice in the scan dialog sends.
WANTED = {"skip": {CLEAR},
          "replace": {CLEAR, CONFLICT},
          "all": {CLEAR, IDENTICAL, SAME_SIZE, CONFLICT}}
