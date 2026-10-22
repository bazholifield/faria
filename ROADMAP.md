# Roadmap

Phases are cumulative; each has concrete exit criteria ("I can do X"). Pedagogy gets debugged
in text (Phase 0–1) before audio complicates anything (Phase 2+).

## North star: the workshop scenario

It's a weekend afternoon. I'm sketching, or building furniture, and I turn Faria on. It keeps
me company in **English** — tips and advice for what I'm doing, related stories, questions,
but never an interrogation. I say "switch to German" and it pivots, knowing my German level,
staying on the activity. At some point, unannounced, it switches to French to test me and I
have to keep up. Eventually I ask for English back and some feedback on how I did.

What this imposes on the design, tracked through the phases below:

- **English (L1) is home base**, not a fallback. Target languages are entered and left
  fluidly — on request, or by the tutor's own initiative (surprise pivots, with consent knobs).
- **A session is not bound to one language.** Language is a property of the utterance;
  evidence (errors, level observations) routes to the profile of whatever language was in
  play at that moment. New languages (zh, ja, ko, …) bootstrap from zero with English scaffolding.
- **The UI is the conversation.** "Switch to German", "correct me less", "teach me about X"
  are things you *say*, and they persist — the model reports state changes via tool call.
- **The tutor has initiative.** A wise mentor who introduces topics, tells stories, and
  teaches unprompted — not a Q&A vending machine. Talk-ratio/initiative is adjustable.
- **Fully hands-free audio** eventually; even in text phases, behave as if the channel were
  speech (no spelling corrections, ever — ADR-013).

## Phase 0 — Text-only core loop

The scaffold for this phase exists; the work is implementing the stubs and iterating on the
pedagogy until conversations feel like a good tutor, not a chatbot with a persona.

- CLI chat (`faria chat --language es`): streamed conversation with the tutor in the target
  language, at a level set from the profile.
- Learner profile persistence: learner + per-language profile rows, sessions and utterances
  logged.
- Prompt assembly v1 (`tutor/prompts.py`): profile snapshot + recent session summaries +
  correction directives, ordered stable→volatile for prompt-cache friendliness. Baseline
  persona must already read as **mentor, not eager assistant** (full presets in Phase 1.5).
- Correction policy v1 (`domain/correction.py`), per ADR-013:
  - Two orthogonal knobs: **frequency** (off | minimal | balanced | aggressive) and
    **style** (recast | recast_plus_note | explicit). Default: balanced + recast —
    "Yo voy a la playa ayer" → "Ah, ¿fuiste a la playa ayer?"
  - **Orthography is never corrected in conversation**, at any setting. Logged only.
  - Budget/severity/recurrence rules per the module docstring.
- Session summaries: on `/end`, Haiku writes a structured summary (topics, vocab, notable
  errors, level evidence, next focus) stored and injected into the next session.

**Exit criteria**
- I can hold a 15-minute Spanish conversation in the CLI that stays at my level.
- Corrections arrive as natural recasts unless I've configured explicit mode; spelling and
  diacritics are never corrected in conversation; frequency `off` produces zero corrections.
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

## Phase 1.5 — Companion foundations: conversation as UI, multilingual sessions

Prototyped entirely in the text CLI; this is the workshop scenario minus audio.

- **Schema migration (do this early, while the DB is disposable):** sessions become
  learner-scoped (ADR-014); `utterances` gains a `language` column; error events and level
  observations attribute to the profile of the utterance's language.
- **Control tool** (ADR-015): a second tool alongside the annotation sidecar —
  `update_session_state` — which the model calls when I say "switch to German", "correct me
  less", "explain in English for a bit". Changes persist (session state or profile
  preferences) and take effect on the next turn.
- **English home base:** sessions open in English by default; "help me practice X" enters
  language X at my level for that language; brand-new languages get English scaffolding
  with gentle target-language exposure.
- **Surprise pivots:** the tutor may switch languages unannounced to test me — through the
  same control tool, so every pivot is logged. Consent knobs: pivot frequency including
  off, and "not right now" is always respected. My reaction to a pivot is recorded as
  comprehension evidence (a free surprise-probe for the comprehension dimension).
- **Persona presets** (ADR-016): named persona blocks, default "mentor" — unhurried, tells
  stories, makes statements, no praise inflation, comfortable leading. Initiative knob
  (learner-led ↔ balanced ↔ tutor-led).

**Exit criteria**
- Mid-conversation "auf Deutsch, bitte" switches the tutor into German at my German level;
  subsequent errors land in the German profile, and "back to English" works the same way.
- "Correct me less" changes behavior immediately and persists to my preferences.
- With pivots enabled, an unannounced switch happens, and my stumbling (or coping) shows
  up as a comprehension observation in the right profile.
- A transcript reads mentor-like: the tutor introduced at least one topic/story unprompted,
  and the conversation is not question-answer ping-pong.

## Phase 2 — Audio

- STT adapter (`speech/stt.py`): faster-whisper local implementation behind the interface;
  per-language override registry. **Basque caveat:** Whisper's `eu` support is weak — the
  registry exists precisely so Basque can route to a different backend (e.g. a HiTZ/Aholab
  fine-tuned model or an API) without touching the loop. Language switching (Phase 1.5)
  means the active STT model follows the active language.
- TTS adapter (`speech/tts.py`): Piper (local, free) + one API implementation (e.g. OpenAI
  TTS). Interface shaped so XTTS/ElevenLabs voice cloning slots in later.
- FastAPI app (`server/`): WebSocket endpoint for the realtime loop (audio in → STT →
  tutor → streamed text → TTS → audio out), REST for profile/history/config.
- Latency work: stream Claude output into TTS sentence-by-sentence so audio starts before
  the reply finishes. Budget: **< 2s from end of my utterance to first audio**, ideally ~1.5s.
- Ambient groundwork for the workshop scenario: VAD-based turn taking (no push-to-talk
  required at the desk), tolerance for long learner silences (I'm sanding a board, not
  ignoring the tutor).
- Pronunciation-suspected errors: low-confidence STT spans logged as `phonology` category
  events (suspected, low confidence) rather than treated as grammar errors.

**Exit criteria**
- Wearing a headset at my desk, I can hold a spoken conversation with < 2s response
  latency, with the same learner-model behavior as text.
- Swapping STT or TTS backend is a config change, not a code change.

**Status (built 2026-07-20):** the loop is live end-to-end (`faria serve` + browser at
localhost:8765) with identical learner-model behavior — verified. Backend swap is a config
change — met. Latency is the open item: **~4.5s** to first audio on pure CPU (measured:
STT ~2s + first Claude token ~2s + TTS ~0.5s) vs. the <2s budget; instrumentation is in
place and the closing paths (chiefly CUDA for whisper) are in ADR-022. The headset test
itself needs a human with a microphone — that's you.

## Phase 2.5 — Developer console

Tuning this thing is now the bottleneck: the persona still reads more teacher than mentor,
endpointing still cuts me off, and voices still sound like different people per language.
All three are tuning problems, and none of them can be judged from memory. A separate
localhost console (ADR-027) for working *on* the tutor rather than talking to it.

- **Run metadata substrate (built):** every session records the full resolved config, the
  exact assembled system prompt (content-addressed, one row per rebuild), and per-call
  tokens/cost/latency across all three call sites — conversation, summary, sweep (ADR-028,
  migration 0005). Persona text moved out of Python into editable `.md` files, so
  "which prompt produced this run" has an answer.
- **Run ledger (built):** sessions listed with cost, latency against the <2s budget, and the
  prompt version that produced them; per-run detail shows transcript, per-turn spend, the
  verbatim prompt, and which config knobs have drifted since.
- **Prompt lab — render + edit (built):** the assembled prompt for any profile/language,
  broken into labeled sections with character counts, against real profile data and with no
  API call; correction/pivot/audio/persona overrides re-render it without persisting
  anything. Persona text is editable in the browser and reloads in place. Strictly
  read-only against the learner model: previewing an unstudied language does not create its
  profile, and viewing a prompt does not decay the focus list it is showing.
- **Prompt lab — replay (next):** run recorded learner turns through two prompt variants
  side by side, each turn against its real frozen context, with cost per variant. Plus
  cheap objective persona metrics (questions per reply, share of turns ending in a
  question, reply length) so "tries too hard to teach" becomes a number, not only a vibe.
- **Voice lab:** audition Piper voices in-browser, `piper_length_scale` slider, and a
  language-router inspector showing how a code-switched sentence splits into per-language
  runs (the ADR-026 residual limits, made visible).
- **Turn-taking lab:** record mic snippets, replay the *same* audio through the segmenter at
  different `vad_silence_ms`/aggressiveness values to see where each would cut. Endpointing
  can't be tuned live — every trial is a different utterance. Needs opt-in audio retention.
- **Annotation browser:** walk a session's error events with detector provenance, severity,
  and policy `action` vs the model's `model_action`; adjudicate them into a gold set that
  becomes a detector regression suite.
- **Config console:** knob editor generated from the pydantic schema, each knob tagged with
  when it takes effect (next turn / next session / component reload / restart). Needs
  `Settings` to become reloadable rather than a boot-time value.
- **Language wizard:** add a language end to end — name, Piper catalog search, download,
  audition, STT override. Needs the scattered language identity (`_LANG_NAMES`,
  `piper_voices`, the client's `<select>`) consolidated into one registry.

**Exit criteria**
- I can tell, from data rather than memory, whether a persona edit improved anything.
- Two prompt variants can be compared over the same recorded turns without doing two live
  sessions.
- A VAD setting can be chosen by replaying one recording, not by guessing between utterances.

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
  philosophy segments, triggered by conversation context, tutor initiative, or explicit
  request; lessons logged as topics so they're not repeated.
- Spaced-repetition surfacing: vocabulary-exposure tracking gets its own table (deliberately
  deferred until now — see DECISIONS ADR-007) and the tutor weaves due items and past
  errors into conversation openers.
- Maybe: Flutter native client, richer history/stats UI.

**Exit criteria**
- **The workshop test:** phone on the workbench, hands full — an hour of ambient English
  companionship with two language pivots (one mine, one the tutor's) and a feedback recap
  at the end, without touching the screen once.
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

## Portfolio track (runs alongside Phases 2.5–3)

FARIA is the project that best shows the linguist first: a correction policy grounded in how
recasts work, a two-level error taxonomy, a CEFR model with an evidence trail, and 28 recorded
design decisions. What's missing for a public portfolio is an outsider's way in, evidence that
the pedagogy works, and a demo people can watch.

- [ ] Outsider-first README: a 30-second pitch, a demo video or GIF, "the linguistics" (recast default and why orthography is never corrected in speech, the error taxonomy, the CEFR evidence model), an architecture diagram, cost and latency from the run ledger; the developer quickstart moves lower
- [ ] Demo: a 2–3 minute screen recording of a voice session with a language pivot, a correction recap and the patterns view
- [ ] Decide on a live demo: a text-mode Hugging Face Space where visitors bring their own API key, or video only (a hosted key would bill you for strangers' sessions)
- [ ] Error-detection evaluation: adjudicate a gold set in the annotation browser (Phase 2.5), then precision and recall per error category
- [ ] CEFR estimator check: the Phase 1 "play above/below my level" test, written up with the estimate's trajectory
- [ ] Persona metrics from prompt-lab replay: one before/after persona edit with numbers (questions per reply, reply length)
- [ ] Latency under the 2 s budget, or a clear account of the remaining gap (ADR-022: API STT or a GPU path)
- [ ] GitHub Actions running the test suite (157 tests, fakes for the API; no key needed)
- [ ] `DESIGN_NOTES.md`: a 2-page summary of the pedagogy decisions for teachers and linguists, linking the ADRs
- [ ] Release checks: LICENSE; confirm test fixtures hold no real session transcripts; make the repo public. History is clean (no `.env` or `data/` ever committed); squashing the early "Phase X updates" commits is optional

**Exit criteria**
- Someone who has never seen FARIA understands what it does and why in under two minutes, from the README and video alone.
- The pedagogy claims (corrections, error detection, level estimates) each have a number behind them.
