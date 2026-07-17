# Decisions

Short ADR-style records. Format: context → decision → consequences. Newest at the bottom.

---

## ADR-001 — Python + FastAPI backend, src-layout

Solo dev, strong Python/NLP background; the interesting logic (pedagogy, prompt assembly,
audio orchestration) benefits from the Python ecosystem (spaCy, faster-whisper, transformers).
FastAPI gives WebSocket + REST + pydantic in one framework for Phase 2+. src-layout so the
package is import-tested the way it will be installed, and publishable later without surgery.
Phase 0 has no server at all — the CLI drives `session/lifecycle.py` directly, which forces
the domain logic to stay transport-independent.

## ADR-002 — Claude API as the tutor brain; Sonnet for conversation, Haiku for offline work

API spend concentrates on the tutor brain; peripherals stay local/free. Conversation uses
`claude-sonnet-5` — a deliberate choice of Sonnet-class over Opus-class: conversational
tutoring is many short turns where latency and per-turn cost dominate, and Sonnet 5 is
near-Opus on this kind of work. Summaries and the end-of-session sweep use `claude-haiku-4-5`
(non-interactive, high-volume, schema-constrained — capability floor is much lower). Both are
config values, not code. Responses stream so (Phase 2) TTS can start before the reply is done.
Note: Sonnet 5 runs adaptive thinking by default; we explicitly disable it for conversation
latency (config `models.thinking`) and revisit for the annotation sidecar if tool-trigger
rates suffer (thinking-off models are less tool-eager).

## ADR-003 — Error detection: sidecar in the conversation call + end-of-session sweep

Options considered: (a) structured annotations returned by the same call that generates the
reply (tool-use sidecar), (b) a separate analyzer call per utterance, (c) end-of-session
analysis only. **Chosen: (a) + a session-end sweep.** One call per turn keeps cost linear and
— decisively — the model that decides *whether to correct in-flight* is the same model that
*logs the error*, so the log and the conversation can't disagree about what happened. The
Haiku sweep at session end catches misses and produces aggregate signals with full-transcript
context. Consequences: detector provenance is recorded per event (`error_events.detector`,
e.g. `sidecar:claude-sonnet-5` vs `sweep:claude-haiku-4-5`) so the two detectors' reliability
can be compared later — inter-annotator agreement between sidecar and sweep is itself a
useful quality signal. Tutor prompt carries the annotation-tool schema, costing some tokens
per turn.

## ADR-004 — Multi-dimensional CEFR model with an evidence trail

A single CEFR number hides the typical asymmetry (comprehension B1, production A2) and gives
prompt assembly less to work with. **Chosen:** continuous per-dimension scores (~1.0=A1 …
6.0=C2) for lexical range, grammatical accuracy, syntactic complexity, comprehension (+
fluency in the audio phase), each with its own confidence; overall = weighted aggregate.
Raw per-session observations are stored in `level_observations` so the estimate can be
recomputed when the update rule changes — estimates are a *view over evidence*, not
destructive state. Consequence: slightly more schema, and `domain/levels.py` must define an
update rule (EWMA-style to start) rather than just overwriting a number.

## ADR-005 — Correction policy is a deterministic module, not prompt vibes

The LLM is good at *executing* a correction gracefully and bad at *consistently deciding*
when to correct across sessions. Split: `domain/correction.py` owns the deterministic parts —
budgets (max inline corrections per turn/window), severity thresholds, recurrence escalation,
the deferred-recap queue — and compiles them into explicit prompt directives; the LLM makes
the in-flight micro-decision within those constraints and reports what it did via the sidecar;
`decide()` then reconciles/tags each logged event (`action` column). Consequences: policy is
unit-testable and tunable per profile (`correction_aggressiveness`) without prompt archaeology;
there can be disagreement between what the prompt allowed and what the model did, which is
logged rather than hidden.

## ADR-006 — SQLite + hand-rolled forward-only migrations

Single user, single writer, local-first: SQLite is the obvious store. No ORM — the schema IS
the domain model documentation, and repository.py is the single module that speaks SQL.
Migrations are numbered `.sql` files applied in order by a ~40-line runner with a
`schema_migrations` table; forward-only, no down-migrations (restore from backup instead —
it's one file). Switch to Alembic only if/when the project grows contributors or a second
database backend. Enum-ish columns use CHECK constraints *except* the error taxonomy, which
is enforced in Python (`domain/taxonomy.py`) so the taxonomy can evolve without a migration.

## ADR-007 — No vector DB; memory = structured data + injected summaries

Long-term memory is the learner model (structured rows) plus LLM-written session summaries,
the last N of which are injected into the next session's system prompt. This is inspectable,
cheap, and debuggable — you can read exactly what the tutor "remembers". Semantic retrieval
is deferred with explicit triggers listed in ROADMAP ("Flagged"). Vocabulary-exposure tracking
(needed for spaced repetition) is also deferred to Phase 4: until then vocab lives inside
summary JSON, avoiding a table we'd redesign anyway once SRS requirements are real.

## ADR-008 — Config: .env for secrets, TOML for tunables, defaults in code

Secrets (API key) come only from environment/.env (python-dotenv; the Anthropic SDK reads the
env itself — keys never live in config objects or the DB). Tunables are pydantic models with
code defaults, overridable by a TOML file (`FARIA_CONFIG`, default `config/default.toml`).
Defaults-in-code means the package works installed anywhere with zero files; the TOML mirrors
the defaults for discoverability. No personal data in code — learner identity lives in the DB.

## ADR-009 — Two-level error taxonomy with anchored severity, categories closed / features open

Category (6 values) and subcategory (closed sets, `domain/taxonomy.py`) are fixed enums so
counts aggregate reliably across sessions and detectors — the inter-rater-reliability lesson:
free-text labels don't aggregate. The open-ended variation lives in `feature_key`
(`category:subcategory:feature`, e.g. `morphosyntax:agreement:gender_noun_adj`, or a lemma for
lexical items), which links events into `error_patterns` for recurrence counting. Severity is
a 1–4 ordinal with written anchors (slip / minor / disruptive / breakdown) — anchors defined
once in taxonomy.py and embedded verbatim in the detector's tool schema, so human and model
raters share the same scale. Fossilization risk is deliberately *not* a severity level: it's
pattern-level state (`error_patterns.status`), because it's a property of recurrence, not of
a single event.

## ADR-010 — STT/TTS behind interfaces with per-language routing

`speech/stt.py` defines a Transcriber ABC and a registry mapping language → implementation,
because one backend won't fit all languages (Whisper is weak for Basque). `speech/tts.py`
mirrors this for synthesis with a voice parameter, so Piper (local/free) is the default and
API voices or cloning (XTTS/ElevenLabs) are drop-ins. Consequence: the realtime loop is
written once against the interfaces in Phase 2 and never touched when backends change.

## ADR-011 — Thin web client first; Flutter later, if ever

Phase 3 client is a single mobile web page (MediaRecorder + WebSocket) served by FastAPI over
LAN/Tailscale. No frontend framework, no build step, no app-store loop — the phone is a
microphone and a speaker. Flutter is Phase 4 "maybe": it buys background audio, wake-word,
and better mic control, and costs a second codebase. Decide when the web client's actual
limitations are known, not before.

## ADR-012 — Single-learner schema, multi-user-shaped

No auth or multi-tenancy, but the schema has a `learners` table (one row for now) and
everything hangs off `language_profiles(learner_id, language)`. Cost today: one JOIN key.
Benefit: publishing this later as a shareable project is an auth layer, not a schema
migration; and "my partner tries it" doesn't corrupt my learner model.

## ADR-013 — Correction frequency and style are separate axes; recast default; orthography exempt

From the first real session: explicit corrections felt like an eager assistant, and it
corrected a *spelling* error — meaningless for a product that is audio-first even when typed.
**Decision:** two orthogonal per-profile knobs. *Frequency*: off | minimal | balanced |
aggressive ("off" is new — the tutor must be able to shut up entirely). *Style*: recast |
recast_plus_note | explicit; default balanced + recast ("Yo voy a la playa ayer" → "Ah,
¿fuiste a la playa ayer?"). The **orthography category (spelling, diacritics, punctuation) is
never surfaced in conversation at any setting** — logged for stats only. Detector nuance the
sidecar instructions must carry: a load-bearing diacritic ("ayer hablo con ella" for
*habló*) is a tense/conjugation error that happens to surface in writing — classify it as
morphosyntax, not orthography, so it isn't silenced by this rule. Consequences: migration
0002 (add 'off' to the aggressiveness CHECK, add a `correction_style` column); rules v1 in
domain/correction.py gain R0 (orthography → logged_silent, always).

## ADR-014 — Sessions are learner-scoped; language is a per-utterance property

The north-star scenario (ROADMAP) has English as home base with fluid entry/exit of target
languages, including tutor-initiated pivots. A session bound to one `language_profiles` row
cannot represent that. **Decision:** sessions attach to the learner; `utterances` carry a
`language` column; error events and level observations route to the profile of the language
in play for that utterance. Per-language profiles, patterns, and level models are untouched —
the companion vision *depends* on them (pivoting into French "at my level" requires the
French estimate to exist, which is why Phases 0–1 stay single-language and come first).
Consequence: migration scheduled early in Phase 1.5, while the DB is still disposable —
delete `data/` rather than write data-preserving migration code for throwaway sessions.

## ADR-015 — The UI is the conversation: state changes are tool calls

"Switch to German", "correct me less", "teach me about X" are said, not clicked.
**Decision:** a second tool alongside the annotation sidecar — `update_session_state` — which
the model calls when the learner requests a change (and when the model itself initiates one,
e.g. a surprise pivot, so every pivot is logged and auditable). The backend persists the
change (session state or profile `preferences`) and re-renders the context for subsequent
turns. This is how "it learns from the conversation" becomes durable rows instead of vibes.
Consequence: mid-session context changes re-render the system prompt, which invalidates the
prompt cache for the rest of that session — acceptable at personal scale; revisit placement
(volatile state below the cached prefix) if costs ever matter.

## ADR-016 — Persona presets; "mentor" is the default

The desired character is a wise old mentor, not an eager assistant — and eager-assistant is
the model's default gravity, so the persona block must actively counter it: unhurried, tells
stories and teaches unprompted, makes statements rather than ending every turn with a
question, no praise inflation, comfortable taking the lead and comfortable with silence.
**Decision:** personas are named, swappable prompt blocks (start with "mentor"), selected
per profile or per session; the initiative knob (learner-led ↔ balanced ↔ tutor-led talk
ratio) is part of persona configuration. Personality is content for `tutor/prompts.py`
section 1 — versioned prompt text, not code.
