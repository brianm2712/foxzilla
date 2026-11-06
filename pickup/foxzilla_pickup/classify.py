"""Movie vs TV classification, and show/movie naming helpers."""
from __future__ import annotations

import os
import re

from .common import VIDEO_EXTS


EPISODE_PATTERNS = [
    re.compile(r"S\d{1,2}[\s._-]?E\d{1,3}", re.IGNORECASE),
    re.compile(r"\b\d{1,2}x\d{2}\b"),
    re.compile(r"\bEp(?:isode)?[\s._-]?\d{1,3}\b", re.IGNORECASE),
]
SEASON_DIR_PATTERN = re.compile(r"^(season|series|s)[\s._-]?\d{1,2}\b", re.IGNORECASE)
SEASON_NAME_PATTERN = re.compile(r"\b(season|series)[\s._-]?\d{1,2}\b", re.IGNORECASE)


# A DVD rip whose largest video holds more than this share of all the video
# bytes is a film plus its extras, however many extras there are. Real movie
# discs measured 66-78%; a TV season's episodes are near-equal, and even a
# "play all" title only reaches about half, since it is the sum of the rest.
FEATURE_SHARE = 0.6


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def classify(name, rel_paths, sizes=None):
    """
    Decide movie vs TV.

    v1 looked only at names, so a DVD-ripped season whose files are called
    B1_t00.mkv fell through to the movie path - and the movie path then
    deleted every file below its "large file" threshold. Structure is checked
    here too, and the classifier is only ever allowed to be wrong in a way
    that is recoverable.

    `sizes` maps each rel path to its byte size. Without it, a movie disc
    ripped with five or more extras is indistinguishable from a season.
    """
    haystack = " ".join([name] + rel_paths)
    if any(p.search(haystack) for p in EPISODE_PATTERNS):
        return "tv", "episode marker in name"
    if SEASON_NAME_PATTERN.search(name):
        return "tv", "season marker in title"
    top_dirs = {p.split(os.sep)[0] for p in rel_paths if os.sep in p}
    if any(SEASON_DIR_PATTERN.match(d) for d in top_dirs):
        return "tv", "season subdirectory"
    vids = [p for p in rel_paths if os.path.splitext(p)[1].lower() in VIDEO_EXTS]
    if sizes and len(vids) >= 2:
        total = sum(sizes.get(v, 0) for v in vids)
        biggest = max(sizes.get(v, 0) for v in vids)
        if total and biggest / total > FEATURE_SHARE:
            return "movie", f"one feature holds {biggest * 100 // total}% of the video"
    if len(vids) >= 5:
        # Many similarly-sized videos with no movie structure is far more
        # likely a ripped season than a film with 5+ full-length extras.
        return "tv", f"{len(vids)} video files, no movie structure"
    return "movie", "default"


def movie_item_name(title, year):
    """'Title (Year)' as a single safe path component."""
    safe = re.sub(r"[/\\]", "-", title).replace(":", "").strip(". ")
    safe = re.sub(r"\s+", " ", safe)
    return f"{safe} ({year})" if year else safe


def guess_show_name(filename):
    base = os.path.splitext(filename)[0]
    for pattern in EPISODE_PATTERNS[:2]:
        m = pattern.search(base)
        if m:
            base = base[:m.start()]
            break
    base = base.replace(".", " ").replace("_", " ").strip(" -")
    return base or os.path.splitext(filename)[0]
