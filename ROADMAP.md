# Roadmap

Phases are cumulative; each has concrete exit criteria ("I can do X"). Pedagogy gets debugged
in text (Phase 0–1) before audio complicates anything (Phase 2+).

## Phase 0 — Text-only core loop

The scaffold for this phase exists; the work is implementing the stubs and iterating on the
pedagogy until conversations feel like a good tutor, not a chatbot with a persona.

- CLI chat (`faria chat --language es`): streamed conversation with the tutor in the target
  language, at a level set from the profile.
- Learner profile persistence: learner + per-language profile rows, sessions and utterances
  logged.
- Prompt assembly v1 (`tutor/prompts.py`): profile snapshot + recent session summaries +
  correction directives, ordered stable→volatile for prompt-cache friendliness.
- Correction policy v1 (`domain/correction.py`): directives compiled into the prompt;
  `decide()` implements the budget/severity/recurrence rules deterministically.
- Session summaries: on `/end`, Haiku writes a structured summary (topics, vocab, notable
  errors, level evidence, next focus) stored and injected into the next session.

**Exit criteria**
- I can hold a 15-minute Spanish conversation in the CLI that stays at my level.
- The tutor corrects me according to my aggressiveness setting and does not correct
  every error (budget respected).
- Session 2 demonstrably knows what happened in session 1 (topics, promised follow-ups).
- All of this works for a second language (French) with zero code changes.

## Phase 1 — Learner model depth

- Error-annotation sidecar live: the conversation call returns structured `error_events`
  (tool-use sidecar, schema in `tutor/schema.py`) which flow through the policy engine into
  SQLite with pattern linking (`feature_key` fingerprints, recurrence counts).
- End-of-session sweep: a Haiku pass over the full transcript catches missed errors and
  emits aggregate level observations.
- CEFR estimation (`domain/levels.py`): per-dimension incremental updates from observations
  (lexical range, grammatical accuracy, syntactic complexity, comprehension), aggregate
  overall estimate with confidence. Evidence rows kept so estimates can be recomputed.
- End-of-session recap: deferred corrections surfaced in a structured recap at `/end`.
- Pattern lifecycle: recurrence escalation into the prompt's focus list; statuses
  (active → improving → resolved / fossilization_risk) updated from evidence.
- Optional: spaCy-based syntactic-complexity metrics as a computed (non-LLM) signal.

**Exit criteria**
- After ~5 sessions, `faria` can show me my top-5 recurring error patterns per language,
  each with real example utterances and counts.
- The CEFR estimate moves in the right direction when I deliberately play above/below
  my level, and its confidence grows across sessions.
- A recurring error I keep making gets escalated: the tutor starts correcting it inline
  and it appears in the focus list.

## Phase 2 — Audio

- STT adapter (`speech/stt.py`): faster-whisper local implementation behind the interface;
  per-language override registry. **Basque caveat:** Whisper's `eu` support is weak — the
  registry exists precisely so Basque can route to a different backend (e.g. a HiTZ/Aholab
  fine-tuned model or an API) without touching the loop.
- TTS adapter (`speech/tts.py`): Piper (local, free) + one API implementation (e.g. OpenAI
  TTS). Interface shaped so XTTS/ElevenLabs voice cloning slots in later.
- FastAPI app (`server/`): WebSocket endpoint for the realtime loop (audio in → STT →
  tutor → streamed text → TTS → audio out), REST for profile/history/config.
- Latency work: stream Claude output into TTS sentence-by-sentence so audio starts before
  the reply finishes. Budget: **< 2s from end of my utterance to first audio**, ideally ~1.5s.
- Pronunciation-suspected errors: low-confidence STT spans logged as `phonology` category
  events (suspected, low confidence) rather than treated as grammar errors.

**Exit criteria**
- Wearing a headset at my desk, I can hold a spoken conversation with < 2s response
  latency, with the same learner-model behavior as text.
- Swapping STT or TTS backend is a config change, not a code change.

## Phase 3 — Phone client

- Single mobile-friendly web page served by FastAPI: pick a language, hold-to-talk (or VAD),
  hear replies. MediaRecorder + WebSocket; no build toolchain unless it earns its keep.
- Reachable from the phone over LAN and Tailscale (bind address/port in config; document
  the Tailscale HTTPS story since getUserMedia requires a secure context off-localhost —
  `tailscale cert` or serve over the tailnet HTTPS proxy).

**Exit criteria**
- From my phone on the couch (or away from home via Tailscale), I can do everything
  Phase 2 allowed at my desk.
- Sessions started on the phone and at the desk land in the same learner model.

## Phase 4 — Polish and extras

- Custom tutor voice (XTTS or ElevenLabs adapter behind the existing TTS interface).
- Mini-lesson library: structured prompts/content for culture, history, etymology,
  philosophy segments, triggered by conversation context or explicit request; lessons
  logged as topics so they're not repeated.
- Spaced-repetition surfacing: vocabulary-exposure tracking gets its own table (deliberately
  deferred until now — see DECISIONS ADR-007) and the tutor weaves due items and past
  errors into conversation openers.
- Maybe: Flutter native client, richer history/stats UI.

**Exit criteria**
- The tutor opens sessions by naturally recycling vocab/errors that are due for review.
- I can trigger "tell me about X" mini-lessons and they adapt to my level.

## Flagged: when semantic retrieval earns its keep

Long-term memory is structured data + LLM-written summaries injected into the prompt
(ADR-007). That holds while injected context stays small. Revisit (likely a small local
embedding index over summaries/utterances, not a vector-DB service) when any of these hit:

1. Rolling summary window loses things I notice ("we discussed this weeks ago") —
   i.e. > ~30–50 sessions per language and the last-N window is blind to older material.
2. Spaced repetition needs "find prior contexts where I used/heard word X" over the
   full utterance history.
3. Mini-lessons should be grounded in "what has this learner already been taught".
