# Roadmap

**Goal:** a voice-first language tutor and companion that converses at your level, corrects judiciously, and keeps a persistent learner model per language (see the workshop scenario below).

**Now:** building toward a full course over an hour of conversation a day: the syllabus (es/fr/de, A1–B2), spaced review, topic cards and placement are in; speech got acknowledgements, level-aware patience and pace, barge-in and stressed recasts. Next: your own recordings as a speech test set, then daily use to tune it. The public README is drafted (milestone 9). Nov 2026, after a real session: the learner's subject now leads (the mentor asks what's up, teaches inside it, and tells history, art or literature from a tidbit on every syllabus item when there's nothing to discuss), and false voice switches mid-sentence are fixed.

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

## 1. Phase 0: text-only core loop · Done

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

**Done when:**
- I can hold a 15-minute Spanish conversation in the CLI that stays at my level.
- Corrections arrive as natural recasts unless I've configured explicit mode; spelling and
  diacritics are never corrected in conversation; frequency `off` produces zero corrections.
- Session 2 demonstrably knows what happened in session 1 (topics, promised follow-ups).
- All of this works for a second language (French) with zero code changes.

## 2. Phase 1: learner model depth · Done

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

**Done when:**
- After ~5 sessions, `faria` can show me my top-5 recurring error patterns per language,
  each with real example utterances and counts.
- The CEFR estimate moves in the right direction when I deliberately play above/below
  my level, and its confidence grows across sessions.
- A recurring error I keep making gets escalated: the tutor starts correcting it inline
  and it appears in the focus list.

## 3. Phase 1.5: companion foundations (conversation as UI, multilingual sessions) · Done

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

**Done when:**
- Mid-conversation "auf Deutsch, bitte" switches the tutor into German at my German level;
  subsequent errors land in the German profile, and "back to English" works the same way.
- "Correct me less" changes behavior immediately and persists to my preferences.
- With pivots enabled, an unannounced switch happens, and my stumbling (or coping) shows
  up as a comprehension observation in the right profile.
- A transcript reads mentor-like: the tutor introduced at least one topic/story unprompted,
  and the conversation is not question-answer ping-pong.

## 4. Phase 2: audio · In progress · ~4.5 s to first audio vs the < 2 s target (before Oct 2026 changes)

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

**Done when:**
- Wearing a headset at my desk, I can hold a spoken conversation with < 2s response
  latency, with the same learner-model behavior as text.
- Swapping STT or TTS backend is a config change, not a code change.

**October 2026 speech work** (ADR-029..033), built and unit-tested, not yet heard by a human:
- Instant acknowledgement ("mm", "ajá") the moment you stop *(done)*
- Patience and speaking pace by level; "slower, please" as a spoken command *(done)*
- Early transcription during the pause, plus a mid-thought / finished verdict on it *(done)*
- Barge-in: talking over Faria stops its voice *(done)*
- Stressed recasts: corrected words said after a pause, slower, and bold on screen *(done)*
- Prompt-cache warm-up at session start (first word ~8 s → ~1.4 s measured) and a cached history *(done)*
- OpenAI speech recognition and voice (one voice for every language), switched off until the key is in `.env` *(built, untested live)*
- Your recordings as a test set (~50 short clips per language) to compare recognition options on numbers
- Later, as we play with it: context hints for new words, checking whether recognition hides learner errors, mixed-language turns, pronunciation feedback

**Status (built 2026-07-20):** the loop is live end-to-end (`faria serve` + browser at
localhost:8765) with identical learner-model behavior — verified. Backend swap is a config
change — met. Latency is the open item: **~4.5s** to first audio on pure CPU (measured:
STT ~2s + first Claude token ~2s + TTS ~0.5s) vs. the <2s budget; instrumentation is in
place and the closing paths (chiefly CUDA for whisper) are in ADR-022. The headset test
itself needs a human with a microphone — that's you.

## 5. Phase 2.5: developer console · In progress · ledger and prompt lab built; replay noted for later

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
- **Prompt lab — replay (noted for later):** run recorded learner turns through two prompt variants
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

**Done when:**
- I can tell, from data rather than memory, whether a persona edit improved anything.
- Two prompt variants can be compared over the same recorded turns without doing two live
  sessions.
- A VAD setting can be chosen by replaying one recording, not by guessing between utterances.

## 6. The course: syllabus, review and topics · In progress · 292 syllabus items + 25 topic cards drafted, 0 checked

The vision: a mentor you could do an hour a day with and come out with a real course, without
it ever feeling like a lesson plan. Suggestions, never an agenda; you decide length and
direction (ADR-034).

- Syllabus for Spanish, French and German, A1–B2: grammar, can-do functions, vocabulary themes, in teaching order (`curriculum/`) *(drafted)*
- Syllabus tab in the Helm to correct it item by item *(done)*
- Topic cards: stories, history, etymology and regional varieties with checked facts *(drafted)*
- The mentor gets an "Ideas" section: words due, one or two new items, stories it could tell *(done)*
- `note_teaching` tool: taught words enter Leitner review in conversation; structures and stories are tracked *(done)*
- Coverage introduced → practised → solid from the end-of-session sweep *(done)*
- Placement conversation for a language with no level yet *(done)*
- Default variety per language, other regions as asides *(done; Spanish base still to confirm)*
- Your pass over the syllabus and topic cards (draft → checked)
- Level adaptation check: play above and below your level; measure whether Faria's own speech changes (sentence length, rarer words) between A2 and B2
- A progress view: words learning/solid, structures covered, per language

**Done when:**
- An hour's conversation introduces new words at about one every ten minutes, brings back due ones, and nobody would call it a lesson.
- After two weeks of daily use, coverage and review data show what you've learned per language, and it matches your sense of it.

## 7. Phase 3: phone client · Next

- Single mobile-friendly web page served by FastAPI: pick a language, hold-to-talk (or VAD),
  hear replies. MediaRecorder + WebSocket; no build toolchain unless it earns its keep.
- Reachable from the phone over LAN and Tailscale (bind address/port in config; document
  the Tailscale HTTPS story since getUserMedia requires a secure context off-localhost —
  `tailscale cert` or serve over the tailnet HTTPS proxy).

**Done when:**
- From my phone on the couch (or away from home via Tailscale), I can do everything
  Phase 2 allowed at my desk.
- Sessions started on the phone and at the desk land in the same learner model.

## 8. Phase 4: polish and extras · Later

- Custom tutor voice (XTTS or ElevenLabs adapter behind the existing TTS interface).
- Longer mini-lessons on request ("tell me about X"), building on the topic cards.
- Vocabulary from frequency lists by level, beyond the words the mentor teaches.
- Maybe: Flutter native client, richer history/stats UI.

**Done when:**
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

## 9. Portfolio track · In progress · public README, tests on GitHub, licence; going public

*Going public in November 2026, before it's fully functional, with the README stating what isn't measured yet. The demo video waits until the quality is where I want it.*

FARIA is the project that best shows the linguist first: a correction policy grounded in how
recasts work, a two-level error taxonomy, a CEFR model with an evidence trail, and 28 recorded
design decisions. What's missing for a public portfolio is an outsider's way in, evidence that
the pedagogy works, and a demo people can watch.

- Outsider-first README: a 30-second pitch, a demo video or GIF, "the linguistics" (recast default and why orthography is never corrected in speech, the error taxonomy, the CEFR evidence model), an architecture diagram, cost and latency from the run ledger; the developer quickstart moves lower *(done Nov 2026, tightened Nov 11; except the video: a placeholder comment marks where it goes; latency is July's measurement until re-measured)*
- **Demo recording (later, once quality is higher):** a 2–3 minute screen recording of a voice session with a language pivot, a correction recap and the patterns view
- Voice choice per learner: Spain's Spanish voice by default; Mexican (es_MX-claude-high, es_MX-ald-medium) and Argentine (es_AR-daniela-high) voices are configured alternatives now, and should become something you can say ("use a Mexican voice"), like "slower, please"
- Later: a text-mode Hugging Face Space where visitors bring their own API key (never a hosted key, which would bill you for strangers' sessions)
- Error-detection evaluation: adjudicate a gold set in the annotation browser (Phase 2.5), then precision and recall per error category
- CEFR estimator check: the Phase 1 "play above/below my level" test, written up with the estimate's trajectory
- Persona metrics from prompt-lab replay: one before/after persona edit with numbers (questions per reply, reply length)
- Latency under the 2 s budget, or a clear account of the remaining gap (ADR-022: API STT or a GPU path)
- GitHub Actions running the test suite (187 tests, fakes for the API; no key needed) *(done Nov 2026: `.github/workflows/tests.yml`, badge in the README)*
- `DESIGN_NOTES.md`: a 2-page summary of the pedagogy decisions for teachers and linguists, linking the ADRs
- Release checks *(Nov 2026: a fresh clone installs and passes the tests; no keys, personal details, real transcripts or `data/` in the files or history; LICENSE: PolyForm Noncommercial 1.0.0, added Nov 11)*: LICENSE: non-commercial (e.g. PolyForm Noncommercial), so others can read and run FARIA but not sell it; confirm test fixtures hold no real session transcripts; make the repo public. History is clean (no `.env` or `data/` ever committed); squashing the early "Phase X updates" commits is optional

**Done when:**
- Someone who has never seen FARIA understands what it does and why in under two minutes, from the README and video alone.
- The pedagogy claims (corrections, error detection, level estimates) each have a number behind them.
