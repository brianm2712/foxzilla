"""Movie / TV / Videos classification, and show/movie naming helpers."""
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


# An item whose own name says it is instructional goes to Videos. Checked on
# the item name only (not the files inside): a course's lessons are often
# numbered like episodes, and a TV show's episode titles can say anything.
# Kept narrow on purpose - a sorting folder in the inbox is the reliable way.
VIDEO_KEYWORDS = re.compile(
    r"\b(tutorials?|courses?|masterclass(es)?|instructionals?|lessons?|"
    r"seminars?|workshops?|bjj|jiu[\s._-]?jitsu|grappling)\b", re.IGNORECASE)

# A film's year: 1920-2049, not part of a longer number (1080p, x264, 2160p).
YEAR_RE = re.compile(r"(?<!\d)(19[2-9]\d|20[0-4]\d)(?!\d)")
# Release words that sit between a title and its year and are not the title.
EDITION_WORDS = re.compile(
    r"\b(extended(\s+(cut|edition))?|unrated|uncut|remastered|theatrical(\s+cut)?|"
    r"director'?s\s+cut|imax|proper|repack)\b", re.IGNORECASE)


def title_year(text):
    """
    'Underworld.Evolution.2006.1080p.BrRip.x264' -> ('Underworld Evolution', '2006')
    'Underworld EXTENDED (2003) [1080p]'         -> ('Underworld', '2003')
    No year, or nothing before it              -> (None, None)

    The LAST year wins, so a title that is itself a number keeps it:
    '1917 (2019)' -> ('1917', '2019'), 'Blade Runner 2049 (2017)' likewise.
    """
    s = re.sub(r"[._]", " ", text)
    found = list(YEAR_RE.finditer(s))
    if not found:
        return None, None
    m = found[-1]
    title = EDITION_WORDS.sub(" ", s[:m.start()])
    title = re.sub(r"\s+", " ", title).strip(" -([{")
    if not title:
        return None, None
    return title, m.group(1)


def video_title_year(rel):
    """Title and year from a video's own name, else from its nearest folder."""
    parts = rel.split(os.sep)
    for text in [os.path.splitext(parts[-1])[0]] + parts[-2::-1]:
        t, y = title_year(text)
        if y:
            return t, y
    return None, None


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

    Returns ("movie" | "tv" | "video", reason). A reason starting "film pack"
    tells the movie path that every titled video is a film of its own.
    """
    if VIDEO_KEYWORDS.search(name):
        return "video", "instructional word in name"
    haystack = " ".join([name] + rel_paths)
    if any(p.search(haystack) for p in EPISODE_PATTERNS):
        return "tv", "episode marker in name"
    if SEASON_NAME_PATTERN.search(name):
        return "tv", "season marker in title"
    top_dirs = {p.split(os.sep)[0] for p in rel_paths if os.sep in p}
    if any(SEASON_DIR_PATTERN.match(d) for d in top_dirs):
        return "tv", "season subdirectory"
    vids = [p for p in rel_paths if os.path.splitext(p)[1].lower() in VIDEO_EXTS]
    if len(vids) >= 2:
        # A box set of films (Underworld, Harry Potter): several videos that
        # each carry their own year. v2 called these a season - "N video
        # files, no movie structure" - and filed the films as episodes.
        years = {v: video_title_year(v)[1] for v in vids}
        distinct = {y for y in years.values() if y}
        titled = sum(1 for y in years.values() if y)
        if len(distinct) >= 2 and titled * 2 >= len(vids):
            return "movie", f"film pack: {len(distinct)} films with their own years"
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
