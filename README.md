# Faria

A personal AI language tutor and conversation partner — JARVIS meets Abbé Faria. It converses
with you in your target language at your level, corrects you judiciously, and maintains a
persistent learner model per language: CEFR estimate, categorized error history, topics,
preferences, and session summaries.

Backend-first design: all intelligence (Claude API, learner model, correction policy, STT/TTS
orchestration) lives in the Python backend. Clients are thin — a CLI in Phase 0, a mobile web
page later.

See [ROADMAP.md](ROADMAP.md) for the phased plan and [DECISIONS.md](DECISIONS.md) for why
things are the way they are.

## Quickstart (Phase 0)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # then put your ANTHROPIC_API_KEY in .env
faria chat --language es      # talk; /end for summary+exit, /quit to abort
pytest                        # run the test suite
```

## Repository structure

```
faria/
├── pyproject.toml              # Packaging (src-layout, hatchling), deps, `faria` entry point
├── README.md                   # This file
├── ROADMAP.md                  # Phased plan with exit criteria
├── DECISIONS.md                # ADR-style records of technical/domain decisions
├── .env.example                # Template for secrets (.env is gitignored)
├── config/
│   └── default.toml            # Tunables: models, correction/pattern budgets, pivots, audio
├── data/                       # Runtime data — SQLite DB (gitignored, created on first run)
├── src/faria/
│   ├── __init__.py             # Package marker + version
│   ├── cli.py                  # `faria chat`, `faria patterns`, `faria migrate`
│   ├── config.py               # Settings: code defaults <- TOML overrides <- env vars
│   ├── db/
│   │   ├── connection.py       # SQLite connection factory (WAL, foreign keys, Row factory)
│   │   ├── migrate.py          # Forward-only migration runner over db/migrations/*.sql
│   │   ├── migrations/         # 0001 schema, 0002 correction style, 0003 model_action,
│   │   │                       # 0004 learner-scoped sessions + per-utterance language
│   │   └── repository.py       # Typed data access — the only module that writes SQL
│   ├── domain/                 # Pedagogy lives here; no I/O, no LLM calls
│   │   ├── models.py           # Dataclasses mirroring DB rows + SessionContext aggregate
│   │   ├── taxonomy.py         # Error taxonomy (2-level), anchored severity scale
│   │   ├── levels.py           # CEFR bands + estimation update rules, wired into session end
│   │   └── correction.py       # Correction policy engine, wired into the live sidecar
│   ├── tutor/                  # Everything that talks to the Claude API
│   │   ├── brain.py            # Streaming client wrapper around the Messages API
│   │   ├── prompts.py          # System-prompt assembly: persona, home base, pivots
│   │   ├── schema.py           # Tool schema + parser for the error-annotation sidecar
│   │   ├── control.py          # Tool schema + parser for session control (switch/correct/pivot)
│   │   ├── summarizer.py       # End-of-session narrative summary (messages.parse)
│   │   └── sweep.py            # End-of-session error sweep + CEFR evidence, per target language
│   ├── session/
│   │   └── lifecycle.py        # SessionRunner: multi-language contexts, turns, sweep, end
│   ├── speech/                 # Phase 2 — interfaces only for now
│   │   ├── stt.py              # Transcriber ABC + per-language override registry
│   │   └── tts.py              # Synthesizer ABC (Piper local, API, XTTS later)
│   └── server/                 # Phase 2/3 — FastAPI app, WebSocket audio loop, REST, web/
│       └── __init__.py         # Placeholder; see ROADMAP Phase 2
└── tests/
    ├── conftest.py             # tmp-DB fixture
    ├── test_config.py          # Defaults + TOML override behavior
    ├── test_migrations.py      # Migrations apply cleanly, idempotently, and upgrade in place
    ├── test_repository.py      # Row round-trips, pattern lifecycle, language-scoped summaries
    ├── test_taxonomy.py        # Category/subcategory consistency, prompt rendering
    ├── test_correction.py      # Correction policy rules, one test per rule
    ├── test_levels.py          # Level-estimator properties (inertia, decay, aggregation)
    ├── test_schema.py          # Annotation-sidecar parsing
    └── test_control.py         # Session-control-tool parsing
```

The pedagogy modules (`correction.py`, `levels.py`, `prompts.py`) carry working
implementations written to the specs in their docstrings — the modules most worth arguing
with and reshaping against real sessions. Their behavior is pinned by tests
(`test_correction.py`, `test_levels.py`) — when you change a rule, change its test.
