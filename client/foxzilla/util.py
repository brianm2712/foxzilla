"""Names and small formatting helpers shared by every part of Foxzilla."""
from __future__ import annotations

import datetime as dt

APP = "foxzilla"
VERSION = "0.2"


def bar(done, total, width=12):
    """A little text progress bar, since ttk cannot put one inside a cell."""
    if not total:
        return ""
    filled = max(0, min(width, round(width * done / total)))
    return "█" * filled + "░" * (width - filled) + f" {done * 100 // total:>3}%"


def human(n):
    if not n:
        return ""
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


def when(ts):
    if not ts:
        return ""
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
