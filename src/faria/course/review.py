"""Leitner spaced review, adapted to conversation (ADR-034).

Classic Leitner: cards live in numbered boxes; a correct answer moves a card up a box
(reviewed less often), a wrong one sends it back to box 1. Here there are no cards.
Evidence comes from the conversation, which is fuzzier than a flashcard answer, so the
rules are deliberately simple:

- used:  the learner said the word. When it was due (or brand new, box 0), it moves up
         a box and is next due after that box's interval; using it again before it's
         due just counts, so one chatty session can't rocket a word to box 5.
- wrong: an error was annotated on the word: back to box 1, due tomorrow.
- heard: the tutor used it. Light exposure, not proof of memory: no box move, but a
         word that was due gets pushed back a day so it stays in circulation.

Stages for display: introduced (box 0, never used), learning (boxes 1-3), solid (4-5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MAX_BOX = 5
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # what SQLite's datetime('now') writes (UTC)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def parse_time(text: str | None) -> datetime | None:
    return datetime.strptime(text, TIME_FORMAT) if text else None


@dataclass
class Card:
    box: int
    due_at: datetime
    times_used: int = 0
    times_wrong: int = 0
    times_heard: int = 0


def interval(box: int, intervals_days: list[int]) -> timedelta:
    return timedelta(days=intervals_days[min(max(box, 1), len(intervals_days)) - 1])


def new_card(now: datetime, intervals_days: list[int]) -> Card:
    return Card(box=0, due_at=now + interval(1, intervals_days))


def used(card: Card, now: datetime, intervals_days: list[int]) -> Card:
    card.times_used += 1
    if card.box == 0 or now >= card.due_at:
        card.box = min(card.box + 1, MAX_BOX)
        card.due_at = now + interval(card.box, intervals_days)
    return card


def wrong(card: Card, now: datetime, intervals_days: list[int]) -> Card:
    card.times_wrong += 1
    card.box = 1
    card.due_at = now + interval(1, intervals_days)
    return card


def heard(card: Card, now: datetime) -> Card:
    card.times_heard += 1
    if now >= card.due_at:
        card.due_at = now + timedelta(days=1)
    return card


def stage(box: int, times_used: int) -> str:
    if box == 0 and times_used == 0:
        return "introduced"
    return "solid" if box >= 4 else "learning"
