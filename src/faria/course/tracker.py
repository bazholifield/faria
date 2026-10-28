"""The course layer's three hooks into a session (ADR-034).

- notes():       at context load, what the mentor *may* draw on: words due for review,
                 one or two syllabus items worth introducing, items worth revisiting,
                 stories not yet told, or placement probes for a language with no level.
- after_turn():  record what the mentor taught (note_teaching), then update every word
                 under review from what was just said: used by the learner, annotated as
                 an error, or heard from the tutor.
- apply_sweep(): the end-of-session sweep reports which tracked structures the learner
                 used correctly; that moves coverage introduced -> practised -> solid.

Every hook is best-effort: a course-layer failure is logged and the conversation goes on.
"""

from __future__ import annotations

import logging

from faria.config import CourseConfig
from faria.db.repository import Repository
from faria.course import curriculum, lemmas, review
from faria.domain.levels import band
from faria.domain.models import CourseNotes, LanguageProfile
from faria.speech.emphasis import strip_marks
from faria.tutor.teaching import parse_teaching

log = logging.getLogger("faria.course")


class CourseTracker:
    def __init__(self, repo: Repository, config: CourseConfig) -> None:
        self._store = repo
        self._config = config

    def _items(self, language: str) -> list[dict]:
        return curriculum.load(self._config.curriculum_dir, language)

    # -- planning --------------------------------------------------------------

    def notes(self, profile: LanguageProfile, is_home: bool) -> CourseNotes | None:
        if is_home:
            return None
        try:
            return self._notes(profile)
        except Exception:  # noqa: BLE001
            log.exception("course notes failed; continuing without them")
            return None

    def _notes(self, profile: LanguageProfile) -> CourseNotes:
        cfg, at = self._config, review.now()
        items = self._items(profile.language)
        index = curriculum.by_id(items)
        notes = CourseNotes(
            variety=cfg.varieties.get(profile.language),
            due_words=[(r["display"], r["gloss"]) for r in
                       self._store.due_vocab_words(profile.id, at, cfg.max_due_words)],
        )
        coverage = self._store.syllabus_coverage(profile.id)
        notes.tracked_items = [(i, index[i]["title"]) for i, row in coverage.items()
                               if row["state"] != "solid" and i in index]
        if profile.level_overall is None:
            notes.placement = curriculum.placement_probes(items)
            return notes
        level = band(profile.level_overall).rstrip("+")
        notes.new_items = curriculum.next_items(items, level, set(coverage), cfg.new_items_offered)
        notes.revisit_items = [index[i] for i, row in coverage.items()
                               if row["state"] == "practised" and i in index
                               and row["due_at"] and row["due_at"] <= at.strftime("%Y-%m-%d %H:%M:%S")][:2]
        notes.topics = curriculum.topics(items, level, self._store.topics_told(profile.id),
                                         cfg.topics_offered)
        notes.tracked_items += [(i["id"], i["title"]) for i in notes.new_items]
        return notes

    # -- after every turn ---------------------------------------------------------

    def after_turn(self, profile_id: int, language: str, session_id: int | None,
                   learner_utterance_id: int | None, learner_text: str, reply_text: str,
                   final: object, error_spans: list[str]) -> None:
        try:
            self._after_turn(profile_id, language, session_id, learner_utterance_id,
                             learner_text, reply_text, final, error_spans)
        except Exception:  # noqa: BLE001
            log.exception("course update failed; continuing")

    def _after_turn(self, profile_id, language, session_id, learner_utterance_id,
                    learner_text, reply_text, final, error_spans) -> None:
        cfg, at = self._config, review.now()
        known = curriculum.by_id(self._items(language))
        fresh: set[str] = set()
        for block in getattr(final, "content", []):
            if getattr(block, "type", None) != "tool_use" or block.name != "note_teaching":
                continue
            taught = parse_teaching(block.input)
            for word in taught.words:
                key = lemmas.key(word.text, language)
                if key and self._store.add_vocab_word(
                        profile_id, key, word.text, word.gloss or None, "asked" if word.asked else "taught",
                        review.new_card(at, cfg.review_intervals_days), session_id, learner_utterance_id):
                    fresh.add(key)
            for item_id in taught.syllabus_items:
                if item_id in known:  # ignore ids the model invented
                    self._store.mark_syllabus_introduced(profile_id, item_id, session_id)
            if taught.topic and taught.topic in known:
                self._store.mark_topic_told(profile_id, taught.topic, session_id)

        said = lemmas.lemmas(learner_text, language)
        heard = lemmas.lemmas(strip_marks(reply_text), language)
        wrong = [lemmas.lemmas(span, language) for span in error_spans if span]
        for row in self._store.vocab_words(profile_id):
            key = row["lemma"]
            if key in fresh:
                continue
            card = self._store.vocab_card(row)
            if any(lemmas.contains(seq, key) for seq in wrong):
                kind, card = "wrong", review.wrong(card, at, cfg.review_intervals_days)
            elif lemmas.contains(said, key):
                kind, card = "used", review.used(card, at, cfg.review_intervals_days)
            elif lemmas.contains(heard, key):
                kind, card = "heard", review.heard(card, at)
            else:
                continue
            self._store.save_vocab_card(row, card, kind, session_id, learner_utterance_id)

    # -- end of session ------------------------------------------------------------

    def apply_sweep(self, profile_id: int, session_id: int | None, uses: list) -> None:
        """uses: the sweep's structure_uses (item_id, correct_uses, incorrect_uses)."""
        try:
            cfg, at = self._config, review.now()
            coverage = self._store.syllabus_coverage(profile_id)
            for use in uses:
                row = coverage.get(use.item_id)
                if use.correct_uses > use.incorrect_uses:
                    count = (row["sessions_correct"] if row else 0) + (
                        0 if row and row["last_session_id"] == session_id and row["state"] != "introduced" else 1)
                    state = "solid" if count >= cfg.solid_after_sessions else "practised"
                    box = min(count, review.MAX_BOX)
                    self._store.set_syllabus_coverage(profile_id, use.item_id, state, count, session_id, box,
                                             at + review.interval(box, cfg.review_intervals_days))
                elif use.incorrect_uses and row:
                    # struggling again: back to practised, due tomorrow
                    self._store.set_syllabus_coverage(profile_id, use.item_id, "practised", row["sessions_correct"],
                                             session_id, 1, at + review.interval(1, cfg.review_intervals_days))
        except Exception:  # noqa: BLE001
            log.exception("coverage update failed; continuing")
