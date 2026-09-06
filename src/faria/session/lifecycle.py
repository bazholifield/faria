"""SessionRunner: orchestrates one tutoring session end to end.

Transport-independent on purpose (ADR-001): the Phase 0 CLI drives this
directly; the Phase 2 WebSocket handler will drive the same object with audio
transcripts. Nothing here knows about terminals or sockets.

Companion mode (ADR-014/015/019, Phase 1.5): a session is learner-scoped, not
language-scoped. It can start at "home base" (the learner's native language,
for ambient companionship) or directly in a target language (focused
practice, the CLI's ``--language`` flag), and move between languages mid-
conversation — on the learner's request, or the tutor's own initiative (a
consent-gated surprise pivot). SessionRunner keeps one SessionContext +
CorrectionPolicy per language actually visited this session (``_contexts`` /
``_policies``, keyed by language code) and swaps which one is "active";
conversation history (``_history``) is a single shared list spanning every
language, since it's one continuous conversation.

Lifecycle:
  start()  — load learner + the starting language's context, assemble the
             system prompt, open a session row.
  turn()   — persist the learner utterance (tagged with the active language),
             stream the reply, persist it, then process both tools' output:
             annotate_errors (skipped entirely at home base — nothing to
             correct in the learner's native language) feeds
             CorrectionPolicy.decide(); update_session_state applies any
             language switch or settings change, taking effect next turn.
  end()    — generate the whole-session narrative summary, run one
             end-of-session sweep per target language actually spoken,
             fold each sweep's level_observations into that language's CEFR
             estimate, close the session row.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterator

from anthropic.types import Message

from faria.config import Settings
from faria.db.repository import Repository
from faria.domain.correction import (
    Aggressiveness,
    CorrectionPolicy,
    CorrectionStyle,
    TurnState,
)
from faria.domain.levels import aggregate_overall, days_since, decay_confidence, update_dimension
from faria.domain.models import (
    ErrorEvent,
    LanguageProfile,
    Learner,
    SessionContext,
    TokenUsage,
    Utterance,
)
from faria.domain.taxonomy import ErrorCategory, ErrorSubcategory, Severity
from faria.speech.stt import TranscriptionResult
from faria.tutor.brain import TutorBrain
from faria.tutor.control import SESSION_CONTROL_TOOL, SessionStateChange, parse_control_call
from faria.tutor.pricing import estimate_cost
from faria.tutor.prompts import assemble_system_prompt, prompt_template_version
from faria.tutor.schema import ERROR_ANNOTATION_TOOL, parse_annotated_errors
from faria.tutor.summarizer import SessionSummary, generate_summary, to_summary_row
from faria.tutor.sweep import SessionSweep, SweepLevelObservation, generate_sweep


class SessionRunner:
    def __init__(self, repo: Repository, brain: TutorBrain, settings: Settings) -> None:
        self._repo = repo
        self._brain = brain
        self._settings = settings

        self._learner: Learner | None = None
        self._session_id: int | None = None
        self._home_language: str = ""
        self._active_language: str = ""
        self._contexts: dict[str, SessionContext] = {}
        self._policies: dict[str, CorrectionPolicy] = {}
        self._ctx: SessionContext | None = None  # == self._contexts[self._active_language]
        self._policy: CorrectionPolicy | None = None  # == self._policies[self._active_language]

        self._system: str = ""
        self._history: list[dict] = []
        self._turn_index: int = 0
        self._mode: str = "text"
        self._turn_state = TurnState()  # shared across languages: one correction budget per session
        self._last_pivot_turn: int = -(10**6)  # far in the past, so the first pivot is never blocked
        self._last_metric_id: int | None = None  # conversation row awaiting audio timings

    @property
    def context(self) -> SessionContext:
        """The currently active language's context."""
        assert self._ctx is not None, "call start() first"
        return self._ctx

    @property
    def active_language(self) -> str:
        assert self._active_language, "call start() first"
        return self._active_language

    @property
    def recap_queue(self) -> list[ErrorEvent]:
        """Errors deferred this session (CorrectionAction.DEFERRED_RECAP), for /end."""
        return list(self._turn_state.recap_queue)

    # -- lifecycle ------------------------------------------------------------

    def start(
        self,
        learner_name: str,
        language: str | None = None,
        goal: str | None = None,
        mode: str = "text",
    ) -> SessionContext:
        """Start a session. ``language`` omitted -> home base (the learner's
        native language, ambient companion mode); given -> start directly in
        that target language (focused practice). ``mode`` is "text" (CLI) or
        "audio" (the Phase 2 speech loop)."""
        self._learner = self._repo.get_or_create_learner(learner_name)
        session = self._repo.create_session(self._learner.id, mode=mode, goal=goal)
        self._session_id = session.id
        self._mode = mode
        self._home_language = self._learner.native_language

        starting_language = language or self._home_language
        self._contexts[starting_language] = self._load_language_context(starting_language, goal=goal)
        self._policies[starting_language] = self._build_policy(
            self._contexts[starting_language].profile
        )
        self._active_language = starting_language
        self._stamp_run()
        self._rebuild_prompt()
        return self.context

    def turn(self, user_text: str, stt: TranscriptionResult | None = None) -> Iterator[str]:
        """One conversation turn: yields the tutor reply as text deltas.

        ``stt`` is present for audio sessions: word confidences land in the
        utterance's meta JSON, and low-confidence words on an otherwise
        confident utterance become phonology:pronunciation_suspected events
        (ROADMAP Phase 2)."""
        assert (
            self._session_id is not None
            and self._policy is not None
            and self._ctx is not None
        ), "call start() first"
        self._policy.start_turn(self._turn_state)  # budget window tracks turns from day one
        meta = self._stt_meta(stt) if stt is not None else None
        learner_utt = self._repo.add_utterance(
            self._session_id,
            self._turn_index,
            "learner",
            user_text,
            language=self._active_language,
            meta=meta,
        )
        self._turn_index += 1
        suspect_words: set[str] = set()
        if stt is not None:
            self._log_pronunciation_suspects(stt, learner_utt.id)
            suspect_words = self._suspect_words(stt)
        # The persisted utterance is the clean transcript; the HISTORY copy may
        # carry a bracketed transcription note so the model knows which words
        # not to trust (ADR-023). The prompt's audio section explains the
        # convention.
        history_text = user_text
        if suspect_words:
            flagged = ", ".join(sorted(suspect_words))
            history_text = f"{user_text}\n[transcription note: uncertain words — {flagged}]"
        self._history.append({"role": "user", "content": history_text})

        # No point asking the model to grammar-check the learner's own native
        # language — the annotation tool only rides along in a target language.
        detecting_errors = self._active_language != self._home_language
        tools = [SESSION_CONTROL_TOOL]
        if detecting_errors:
            tools.append(ERROR_ANNOTATION_TOOL)

        with self._brain.stream_reply(self._system, self._history, tools=tools) as stream:
            for delta in stream.text_stream:
                yield delta
            final = stream.get_final_message()

        self._record_call_metrics(
            stage="conversation",
            final=final,
            turn_index=learner_utt.turn_index,
            language=self._active_language,
        )
        reply_text = "".join(
            block.text for block in final.content if block.type == "text"
        )
        self._repo.add_utterance(
            self._session_id, self._turn_index, "tutor", reply_text, language=self._active_language
        )
        self._turn_index += 1
        # Only the plain text goes into history — dropping tool_use blocks
        # means we never send a matching tool_result back; both tools are
        # one-shot side channels, not round trips (ADR-003).
        self._history.append({"role": "assistant", "content": reply_text})

        if detecting_errors:
            self._process_annotations(final, learner_utt.id, suspect_words)
        self._process_control_calls(final)

    def end(self) -> SessionSummary | None:
        """Close the session; returns the narrative summary (None if nothing was said)."""
        assert self._session_id is not None
        utterances = self._repo.session_utterances(self._session_id)
        if not utterances:
            self._repo.end_session(self._session_id, status="abandoned")
            return None

        summary = generate_summary(
            utterances,
            self._settings.models,
            on_usage=lambda response: self._record_call_metrics("summary", response),
        )
        self._repo.store_summary(
            to_summary_row(summary, self._session_id, self._settings.models.summarizer)
        )

        self._run_sweeps(utterances)

        self._repo.end_session(self._session_id, status="completed")
        return summary

    def abandon(self) -> None:
        """Close without a summary (user quit or crash cleanup)."""
        if self._session_id is not None:
            self._repo.end_session(self._session_id, status="abandoned")

    # -- language contexts ----------------------------------------------------

    def _load_language_context(self, language: str, goal: str | None = None) -> SessionContext:
        assert self._learner is not None
        profile = self._repo.get_or_create_profile(
            self._learner.id,
            language,
            self._settings.correction.default_aggressiveness,
            self._settings.correction.default_style,
        )
        self._repo.decay_pattern_statuses(
            profile.id,
            self._settings.correction.pattern_improving_after_days,
            self._settings.correction.pattern_resolved_after_days,
        )
        return SessionContext(
            learner=self._learner,
            profile=profile,
            dimensions=self._repo.get_dimensions(profile.id),
            recent_summaries=self._repo.recent_summaries(
                language, self._settings.memory.recent_summaries_in_prompt
            ),
            focus_patterns=self._repo.focus_patterns(
                profile.id, self._settings.correction.recurrence_escalation_threshold
            ),
            known_feature_keys=self._repo.known_feature_keys(profile.id),
            goal=goal,
        )

    def _build_policy(self, profile: LanguageProfile) -> CorrectionPolicy:
        return CorrectionPolicy(
            self._settings.correction,
            Aggressiveness(profile.correction_aggressiveness),
            CorrectionStyle(profile.correction_style),
        )

    def _rebuild_prompt(self) -> None:
        self._ctx = self._contexts[self._active_language]
        self._policy = self._policies[self._active_language]
        frequency, other_languages = self._pivot_settings()
        self._system = assemble_system_prompt(
            self._ctx,
            self._policy.prompt_directives(self._ctx.focus_patterns, self._ctx.profile.level_overall),
            pivot_frequency=frequency,
            other_known_languages=other_languages,
            audio=self._mode == "audio",
        )
        # Keep the exact text, not just a version tag: reading back what was
        # actually sent is the difference between an evidence trail and a
        # label (migration 0005).
        if self._session_id is not None:
            self._repo.record_session_prompt(
                self._session_id, self._active_language, self._system
            )

    def _pivot_settings(self) -> tuple[str, list[str]]:
        """(pivot_frequency, other eligible languages) — a session-wide concept
        (ADR-019), not one language profile's data, so it's assembled fresh
        each prompt rebuild rather than stored on SessionContext."""
        assert self._learner is not None
        home_ctx = self._contexts.get(self._home_language)
        frequency = "off"
        if home_ctx is not None:
            frequency = home_ctx.profile.preferences.get("pivot_frequency", "off")
        others = [
            lang
            for lang in self._repo.known_target_languages(self._learner.id, self._home_language)
            if lang != self._active_language
        ]
        return frequency, others

    # -- run metadata ----------------------------------------------------------
    # Every session records what produced it (config + prompt text) and what
    # each API call cost in tokens and seconds. See migration 0005.

    def _stamp_run(self) -> None:
        assert self._session_id is not None
        from faria import __version__

        self._repo.stamp_session_run(
            self._session_id,
            config_snapshot=self._settings.model_dump(mode="json"),
            prompt_template_version=prompt_template_version(),
            faria_version=__version__,
        )

    def _record_call_metrics(
        self,
        stage: str,
        final: object,
        turn_index: int | None = None,
        language: str | None = None,
    ) -> None:
        """Persist one API call's token usage and estimated cost.

        Tolerant by design: a brain that reports no usage (the scripted fake in
        tests, or a backend that omits it) still gets a row, with zeros. A
        metrics write must never be the thing that breaks a conversation, so
        failures here are swallowed rather than propagated.
        """
        if self._session_id is None:
            return
        model = getattr(final, "model", None)
        usage = TokenUsage.from_usage(getattr(final, "usage", None))
        try:
            metric_id = self._repo.record_turn_metrics(
                self._session_id,
                stage=stage,
                turn_index=turn_index,
                language=language,
                model=model,
                usage=usage,
                cost_usd=estimate_cost(model, usage),
            )
        except Exception:  # noqa: BLE001 — telemetry must not break the session
            return
        if stage == "conversation":
            self._last_metric_id = metric_id

    def attach_turn_timings(
        self,
        stt_s: float | None = None,
        first_delta_s: float | None = None,
        first_audio_s: float | None = None,
    ) -> None:
        """Attach the audio pipeline's stage timings to the turn just finished.

        Called by the websocket layer (ws.py), which is the only place that
        knows them — the text CLI has no STT or TTS stage. Safe to call when
        no turn has run: it's a no-op.
        """
        if self._last_metric_id is None:
            return
        try:
            self._repo.attach_turn_timings(
                self._last_metric_id,
                stt_s=stt_s,
                first_delta_s=first_delta_s,
                first_audio_s=first_audio_s,
            )
        except Exception:  # noqa: BLE001 — telemetry must not break the session
            pass

    # -- audio: STT metadata + pronunciation suspicion -------------------------

    def _suspect_words(self, stt: TranscriptionResult) -> set[str]:
        """Words the recognizer itself wasn't sure about — candidates for
        mistranscription, not learner errors (ADR-023). Normalized to
        lowercase alpha for span matching."""
        threshold = self._settings.audio.stt_suspect_threshold
        out = set()
        for w in stt.words:
            if w.confidence < threshold:
                normalized = "".join(ch for ch in w.word.lower() if ch.isalpha())
                if normalized:
                    out.add(normalized)
        return out

    @staticmethod
    def _stt_meta(stt: TranscriptionResult) -> dict:
        return {
            "stt": {
                "confidence": round(stt.confidence, 3),
                "detected_language": stt.language,  # kept for future evidence-routing refinement
                "low_words": [
                    [w.word, round(w.confidence, 2)] for w in stt.words if w.confidence < 0.7
                ],
            }
        }

    def _log_pronunciation_suspects(self, stt: TranscriptionResult, utterance_id: int) -> None:
        """Low-confidence words on an otherwise-confident utterance -> logged
        phonology events. Deliberately bypasses CorrectionPolicy.decide():
        these are STT suspicions, not model-detected errors — never corrected,
        never recapped, but pattern-counted so "he consistently garbles
        'juntos'" becomes visible over time."""
        assert self._ctx is not None and self._session_id is not None
        if self._active_language == self._home_language:
            return  # no pronunciation tracking for the learner's native language
        threshold = self._settings.audio.pronunciation_suspect_threshold
        if stt.confidence <= 0.5:
            return  # whole utterance is shaky: an audio-quality problem, not pronunciation
        for w in stt.words:
            if w.confidence > threshold:
                continue
            word_key = "".join(ch for ch in w.word.lower() if ch.isalpha())
            if not word_key:
                continue
            feature_key = f"phonology:pronunciation_suspected:{word_key}"
            pattern = self._repo.upsert_pattern(
                self._ctx.profile.id,
                ErrorCategory.PHONOLOGY,
                ErrorSubcategory.PRONUNCIATION_SUSPECTED,
                feature_key,
                f"STT repeatedly uncertain about '{word_key}'",
                fossilization_threshold=self._settings.correction.fossilization_threshold,
            )
            event = ErrorEvent(
                utterance_id=utterance_id,
                category=ErrorCategory.PHONOLOGY,
                subcategory=ErrorSubcategory.PRONUNCIATION_SUSPECTED,
                severity=Severity.SLIP,
                confidence=round(min(1.0 - w.confidence, 0.6), 2),  # a suspicion, never inline-worthy
                feature_key=feature_key,
                span_text=w.word.strip(),
                detector="stt:faster-whisper",
                action="logged_silent",
            )
            self._repo.record_error_event(self._session_id, event, pattern.id)

    # -- annotation sidecar ---------------------------------------------------

    def _process_annotations(
        self, final: Message, learner_utterance_id: int, suspect_words: set[str] | None = None
    ) -> None:
        assert self._ctx is not None and self._policy is not None and self._session_id is not None
        detector = f"sidecar:{final.model}"
        seen_spans: set[str] = set()
        for block in final.content:
            if block.type != "tool_use" or block.name != "annotate_errors":
                continue
            for raw_event in parse_annotated_errors(block.input, learner_utterance_id, detector):
                # Observed live: the model can report the identical span under
                # two different feature_keys within ONE call. Exact-span
                # duplicates within a single batch are safe to collapse in
                # code (unlike cross-call fingerprint drift, which needs
                # fuzzy/semantic matching — see tutor/sweep.py's docstring).
                span_key = (raw_event.span_text or "").strip().lower()
                if span_key and span_key in seen_spans:
                    continue
                seen_spans.add(span_key)
                # ADR-023: an "error" inside a word the STT itself flagged as
                # uncertain is quarantined — kept on the record for later
                # inspection, but no pattern link (recurrence untouched), no
                # correction, and a marked detector so these rows are
                # excludable from any analysis.
                if suspect_words and self._span_is_suspect(raw_event.span_text, suspect_words):
                    self._repo.record_error_event(
                        self._session_id,
                        replace(
                            raw_event,
                            action="logged_silent",
                            confidence=min(raw_event.confidence, 0.3),
                            detector=f"{detector}:stt_suspect",
                        ),
                        pattern_id=None,
                    )
                    continue
                pattern = self._repo.upsert_pattern(
                    self._ctx.profile.id,
                    raw_event.category,
                    raw_event.subcategory,
                    raw_event.feature_key,
                    raw_event.explanation,
                    fossilization_threshold=self._settings.correction.fossilization_threshold,
                )
                action = self._policy.decide(
                    raw_event, pattern, self._turn_state, self._ctx.profile.level_overall
                )
                self._repo.record_error_event(
                    self._session_id, replace(raw_event, action=action), pattern.id
                )

    @staticmethod
    def _span_is_suspect(span_text: str | None, suspect_words: set[str]) -> bool:
        if not span_text:
            return False
        span_norm = {
            "".join(ch for ch in token.lower() if ch.isalpha())
            for token in span_text.split()
        }
        return bool(span_norm & suspect_words)

    # -- session control tool -------------------------------------------------

    def _process_control_calls(self, final: Message) -> None:
        for block in final.content:
            if block.type != "tool_use" or block.name != "update_session_state":
                continue
            self._apply_state_change(parse_control_call(block.input))

    def _apply_state_change(self, change: SessionStateChange) -> None:
        assert self._ctx is not None and self._learner is not None
        if change.action == "switch_language":
            self._handle_switch_language(change)
        elif change.action == "set_correction_frequency" and change.correction_frequency:
            self._repo.update_correction_settings(
                self._ctx.profile.id, aggressiveness=change.correction_frequency, style=None
            )
            self._refresh_active_context()
        elif change.action == "set_correction_style" and change.correction_style:
            self._repo.update_correction_settings(
                self._ctx.profile.id, aggressiveness=None, style=change.correction_style
            )
            self._refresh_active_context()
        elif change.action == "set_pivot_frequency" and change.pivot_frequency:
            self._set_pivot_preference(change.pivot_frequency)

    def _handle_switch_language(self, change: SessionStateChange) -> None:
        if not change.language or change.language == self._active_language:
            return
        if change.reason == "tutor_pivot":
            gap = self._turn_state.turn_index - self._last_pivot_turn
            if gap < self._settings.correction.min_turns_between_pivots:
                return  # cooldown not met -- silently ignore this pivot attempt
            self._last_pivot_turn = self._turn_state.turn_index
        if change.language not in self._contexts:
            self._contexts[change.language] = self._load_language_context(change.language)
            self._policies[change.language] = self._build_policy(
                self._contexts[change.language].profile
            )
        self._active_language = change.language
        self._rebuild_prompt()

    def _refresh_active_context(self) -> None:
        """Re-read the active profile after a correction-settings change and
        rebuild the prompt so it takes effect next turn."""
        assert self._learner is not None
        lang = self._active_language
        fresh_profile = self._repo.get_or_create_profile(
            self._learner.id,
            lang,
            self._settings.correction.default_aggressiveness,
            self._settings.correction.default_style,
        )
        self._contexts[lang] = replace(self._contexts[lang], profile=fresh_profile)
        self._policies[lang] = self._build_policy(fresh_profile)
        self._rebuild_prompt()

    def _set_pivot_preference(self, frequency: str) -> None:
        """Pivot frequency is stored on the HOME profile's preferences (ADR-019)
        — it's a whole-session/companion-mode setting, not tied to whichever
        target language happens to be active when the learner asks for it."""
        if self._home_language not in self._contexts:
            self._contexts[self._home_language] = self._load_language_context(self._home_language)
            self._policies[self._home_language] = self._build_policy(
                self._contexts[self._home_language].profile
            )
        home_ctx = self._contexts[self._home_language]
        new_prefs = dict(home_ctx.profile.preferences)
        new_prefs["pivot_frequency"] = frequency
        self._repo.update_preferences(home_ctx.profile.id, new_prefs)
        fresh_profile = replace(home_ctx.profile, preferences=new_prefs)
        self._contexts[self._home_language] = replace(home_ctx, profile=fresh_profile)
        self._rebuild_prompt()  # pivot section renders regardless of which language is active

    # -- end-of-session sweep + CEFR estimation --------------------------------

    def _run_sweeps(self, utterances: list[Utterance]) -> None:
        """One sweep call per non-native language the learner actually spoke
        this session (ADR-014) — the sweep is scoped to one target language,
        so a companion session that visited German and French runs it twice,
        each against the full transcript but told to ignore the other."""
        target_languages = sorted(
            {
                u.language
                for u in utterances
                if u.speaker == "learner" and u.language != self._home_language
            }
        )
        for language in target_languages:
            if language not in self._contexts:
                self._contexts[language] = self._load_language_context(language)
            sweep = generate_sweep(
                utterances,
                language,
                self._settings.models,
                on_usage=lambda response, lang=language: self._record_call_metrics(
                    "sweep", response, language=lang
                ),
            )
            self._apply_sweep(sweep, self._contexts[language].profile.id)

    def _apply_sweep(self, sweep: SessionSweep, profile_id: int) -> None:
        assert self._session_id is not None
        utterance_by_turn = {
            u.turn_index: u.id
            for u in self._repo.session_utterances(self._session_id)
            if u.speaker == "learner"
        }
        detector = f"sweep:{self._settings.models.summarizer}"
        seen: dict[int, set[str]] = {}
        for item in sweep.missed_errors:
            utt_id = utterance_by_turn.get(item.turn_index)
            if utt_id is None:
                continue  # model cited a turn index that doesn't exist; skip defensively
            if utt_id not in seen:
                seen[utt_id] = self._repo.pattern_feature_keys_for_utterance(utt_id)
            if item.feature_key in seen[utt_id]:
                continue  # the live sidecar already caught this one
            pattern = self._repo.upsert_pattern(
                profile_id,
                item.category,
                item.subcategory,
                item.feature_key,
                item.explanation,
                fossilization_threshold=self._settings.correction.fossilization_threshold,
            )
            event = ErrorEvent(
                utterance_id=utt_id,
                category=item.category,
                subcategory=item.subcategory,
                severity=item.severity,
                confidence=item.confidence,
                feature_key=item.feature_key,
                span_text=item.span_text,
                correction=item.correction,
                explanation=item.explanation,
                interference_source=item.interference_source,
                action="logged_silent",  # session is over; nothing left to act on live
                detector=detector,
            )
            self._repo.record_error_event(self._session_id, event, pattern.id)
            seen[utt_id].add(item.feature_key)

        for obs in sweep.level_observations:
            self._apply_level_observation(profile_id, obs)
        self._recompute_overall(profile_id)

    def _apply_level_observation(self, profile_id: int, obs: SweepLevelObservation) -> None:
        assert self._session_id is not None
        self._repo.record_observation(
            profile_id,
            self._session_id,
            obs.dimension,
            obs.value,
            obs.weight,
            source="llm_sweep",
            detail={"rationale": obs.rationale},
        )
        existing = {d.dimension: d for d in self._repo.get_dimensions(profile_id)}
        prior = existing.get(obs.dimension)
        if prior is not None:
            prior_confidence = decay_confidence(prior.confidence, days_since(prior.updated_at))
            prior_score = prior.score
        else:
            prior_confidence, prior_score = 0.0, None
        new_score, new_confidence = update_dimension(prior_score, prior_confidence, obs.value, obs.weight)
        self._repo.upsert_dimension(profile_id, obs.dimension, new_score, new_confidence)

    def _recompute_overall(self, profile_id: int) -> None:
        dims = {d.dimension: (d.score, d.confidence) for d in self._repo.get_dimensions(profile_id)}
        if not dims:
            return
        score, confidence = aggregate_overall(dims)
        self._repo.set_overall_level(profile_id, score, confidence)
