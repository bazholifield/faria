"""Reading the syllabus files (curriculum/<lang>.yaml, edited in the Helm's Syllabus tab).

Read-only here, re-read whenever a file changes on disk, so an edit made between
sessions is picked up by the next one without restarting the server. Items marked
"cut" are kept on record in the file but never offered.
"""

from __future__ import annotations

from pathlib import Path

import yaml

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

_cache: dict[Path, tuple[float, list[dict]]] = {}


def load(curriculum_dir: Path, language: str) -> list[dict]:
    """All live items for a language, in file (= teaching) order; [] if there's no file."""
    path = curriculum_dir / f"{language}.yaml"
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return []
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = [i for i in doc.get("items", []) if i.get("status") != "cut"]
    _cache[path] = (mtime, items)
    return items


def band_index(band: str) -> int:
    return LEVELS.index(band) if band in LEVELS else 1


def next_items(items: list[dict], band: str, covered: set[str], n: int) -> list[dict]:
    """Grammar and functions worth introducing: the first uncovered ones at the learner's
    level, then the level above. Lower levels are assumed known; their gaps surface as
    errors instead, and the error patterns already handle those."""
    out: list[dict] = []
    for level in LEVELS[band_index(band):band_index(band) + 2]:
        for item in items:
            if len(out) >= n:
                return out
            if (item["level"] == level and item["strand"] in ("grammar", "function")
                    and item["id"] not in covered):
                out.append(item)
    return out


def topics(items: list[dict], band: str, told: set[str], n: int) -> list[dict]:
    """Topic cards not yet told, at or below the level above the learner's
    (the mentor adapts the language; the content just shouldn't be out of reach)."""
    ceiling = band_index(band) + 1
    return [i for i in items if i["strand"] == "topic" and i["id"] not in told
            and band_index(i["level"]) <= ceiling][:n]


def placement_probes(items: list[dict], per_level: int = 2) -> list[tuple[str, list[str]]]:
    """For a learner with no estimate: the first grammar points of each level, as
    structures a placement conversation can try one level at a time."""
    out = []
    for level in LEVELS[:4]:
        titles = [i["title"] for i in items if i["level"] == level and i["strand"] == "grammar"][:per_level]
        if titles:
            out.append((level, titles))
    return out


def by_id(items: list[dict]) -> dict[str, dict]:
    return {i["id"]: i for i in items}
