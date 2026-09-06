"""The developer console: a separate, localhost-only app (ADR-027).

Deliberately NOT mounted on `faria serve`. That app is the learner-facing
surface and is meant to be reachable from a phone over LAN/Tailscale in Phase
3; this one reads the learner model, will eventually edit config and prompt
text, and spends API money. Those belong on different ports, and this one
binds loopback only (cli.py enforces it).

This is not a settings screen for the tutor — ADR-015 stands, the learner
changes a session by talking to it. The console tunes *defaults and prompt
text* between sessions, and shows the evidence for whether a change helped.

First panel: the Run Ledger — every session with the config and prompt text
that produced it, plus latency and spend (migration 0005). Read-only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from faria.config import Settings, load_settings
from faria.db.connection import connect
from faria.db.repository import Repository
from faria.domain.correction import Aggressiveness, CorrectionPolicy, CorrectionStyle
from faria.domain.models import LanguageProfile, Learner, SessionContext
from faria.tutor.prompts import (
    assemble_sections,
    language_name,
    personas,
    save_persona,
)

WEB_DIR = Path(__file__).parent / "web"


def create_dev_app(db_path: Path, settings: Settings | None = None) -> FastAPI:
    """Build the console over one database.

    ``db_path`` is explicit rather than taken from settings: the ledger is
    usually pointed at the real tutor DB while experiments run against a
    separate dev DB (FARIA_DATA_DIR).
    """
    settings = settings or load_settings()
    app = FastAPI(title="faria-dev", docs_url=None, redoc_url=None)
    app.include_router(_build_router(db_path, settings))
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "dev.html")

    @app.get("/prompts")
    def prompt_lab() -> FileResponse:
        return FileResponse(WEB_DIR / "prompts.html")

    return app


def _build_router(db_path: Path, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api")

    def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = connect(db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    @router.get("/health")
    def health() -> dict:
        return {"ok": True, "db": str(db_path)}

    @router.get("/runs")
    def runs(limit: int = 100) -> dict:
        """The ledger: one row per session, newest first."""
        sessions = query(
            """
            SELECT s.id, s.mode, s.status, s.goal, s.started_at, s.ended_at,
                   s.prompt_template_version, s.faria_version,
                   s.config_snapshot IS NOT NULL AS has_config,
                   (SELECT COUNT(*) FROM utterances u
                     WHERE u.session_id = s.id AND u.speaker = 'learner') AS learner_turns,
                   (SELECT COUNT(DISTINCT u.language) FROM utterances u
                     WHERE u.session_id = s.id) AS language_count,
                   (SELECT GROUP_CONCAT(DISTINCT u.language) FROM utterances u
                     WHERE u.session_id = s.id) AS languages
            FROM sessions s
            ORDER BY s.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        if not sessions:
            return {"runs": [], "current_prompt_version": _current_prompt_version()}

        ids = [row["id"] for row in sessions]
        placeholders = ",".join("?" * len(ids))
        metrics = query(
            f"SELECT * FROM turn_metrics WHERE session_id IN ({placeholders})", tuple(ids)
        )
        by_session: dict[int, list[sqlite3.Row]] = {}
        for row in metrics:
            by_session.setdefault(row["session_id"], []).append(row)

        return {
            "current_prompt_version": _current_prompt_version(),
            "runs": [
                {
                    "id": s["id"],
                    "mode": s["mode"],
                    "status": s["status"],
                    "goal": s["goal"],
                    "started_at": s["started_at"],
                    "ended_at": s["ended_at"],
                    "prompt_version": s["prompt_template_version"],
                    "faria_version": s["faria_version"],
                    "has_config": bool(s["has_config"]),
                    "learner_turns": s["learner_turns"],
                    "languages": _language_labels(s["languages"]),
                    **_aggregate_metrics(by_session.get(s["id"], [])),
                }
                for s in sessions
            ],
        }

    @router.get("/runs/{session_id}")
    def run_detail(session_id: int) -> dict:
        rows = query("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="no such session")
        session = rows[0]

        snapshot = json.loads(session["config_snapshot"]) if session["config_snapshot"] else None
        prompts = query(
            """
            SELECT sp.seq, sp.language, sp.prompt_hash, sp.created_at, LENGTH(pv.text) AS chars
            FROM session_prompts sp JOIN prompt_versions pv ON pv.hash = sp.prompt_hash
            WHERE sp.session_id = ? ORDER BY sp.seq
            """,
            (session_id,),
        )
        metrics = query(
            "SELECT * FROM turn_metrics WHERE session_id = ? ORDER BY id", (session_id,)
        )
        utterances = query(
            "SELECT * FROM utterances WHERE session_id = ? ORDER BY turn_index", (session_id,)
        )
        summary = query(
            "SELECT summary_text, next_focus FROM session_summaries WHERE session_id = ?",
            (session_id,),
        )

        metrics_by_turn = {m["turn_index"]: m for m in metrics if m["stage"] == "conversation"}
        return {
            "id": session["id"],
            "mode": session["mode"],
            "status": session["status"],
            "goal": session["goal"],
            "started_at": session["started_at"],
            "ended_at": session["ended_at"],
            "prompt_version": session["prompt_template_version"],
            "faria_version": session["faria_version"],
            "current_prompt_version": _current_prompt_version(),
            "config_drift": _config_drift(snapshot, settings),
            "config_snapshot": snapshot,
            "totals": _aggregate_metrics(metrics),
            "prompts": [
                {
                    "seq": p["seq"],
                    "language": p["language"],
                    "language_name": language_name(p["language"]),
                    "hash": p["prompt_hash"],
                    "chars": p["chars"],
                }
                for p in prompts
            ],
            "offline_calls": [
                {
                    "stage": m["stage"],
                    "language": m["language"],
                    "model": m["model"],
                    "input_tokens": m["input_tokens"],
                    "output_tokens": m["output_tokens"],
                    "cost_usd": m["cost_usd"],
                }
                for m in metrics
                if m["stage"] != "conversation"
            ],
            "turns": [
                _turn_view(u, metrics_by_turn.get(u["turn_index"])) for u in utterances
            ],
            "summary": summary[0]["summary_text"] if summary else None,
            "next_focus": json.loads(summary[0]["next_focus"]) if summary else [],
        }

    @router.get("/prompts/{prompt_hash}")
    def prompt_text(prompt_hash: str) -> dict:
        rows = query("SELECT * FROM prompt_versions WHERE hash = ?", (prompt_hash,))
        if not rows:
            raise HTTPException(status_code=404, detail="no such prompt")
        used_by = query(
            "SELECT session_id, seq, language FROM session_prompts WHERE prompt_hash = ? "
            "ORDER BY session_id DESC",
            (prompt_hash,),
        )
        return {
            "hash": rows[0]["hash"],
            "text": rows[0]["text"],
            "created_at": rows[0]["created_at"],
            "used_by": [
                {"session_id": u["session_id"], "seq": u["seq"], "language": u["language"]}
                for u in used_by
            ],
        }

    # -- prompt lab -----------------------------------------------------------
    # Render the exact prompt a session would get, and edit the persona text
    # that produces it. Rendering costs nothing — it's the same assembly the
    # runner does, against real profile data, with no API call.

    @router.get("/personas")
    def list_personas() -> dict:
        blocks = personas()
        return {
            "version": _current_prompt_version(),
            "personas": [
                {"name": name, "text": text, "chars": len(text)}
                for name, text in sorted(blocks.items())
            ],
        }

    @router.put("/personas/{name}")
    def put_persona(name: str, body: PersonaEdit) -> dict:
        try:
            version = save_persona(name, body.text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": name, "version": version}

    @router.get("/preview-targets")
    def preview_targets() -> dict:
        """Profiles that exist in this DB, so the preview can use real data."""
        rows = query(
            "SELECT lp.language, lp.level_overall, l.name AS learner_name, "
            "l.native_language FROM language_profiles lp "
            "JOIN learners l ON l.id = lp.learner_id ORDER BY lp.updated_at DESC"
        )
        return {
            "profiles": [
                {
                    "language": r["language"],
                    "language_name": language_name(r["language"]),
                    "learner_name": r["learner_name"],
                    "native_language": r["native_language"],
                    "level_overall": r["level_overall"],
                }
                for r in rows
            ],
            "personas": sorted(personas()),
        }

    @router.get("/preview")
    def preview(
        language: str,
        persona: str | None = None,
        correction_frequency: str | None = None,
        correction_style: str | None = None,
        pivot_frequency: str = "off",
        audio: bool = False,
        goal: str | None = None,
    ) -> dict:
        """The assembled prompt for a language, section by section.

        Strictly read-only: unlike SessionRunner, this never creates a profile
        and never decays pattern statuses. Previewing a language you haven't
        studied yet must not bring its profile into existence, and looking at
        a prompt must not age the focus list you're looking at.
        """
        conn = connect(db_path)
        try:
            ctx = _preview_context(
                conn, settings, language, persona, correction_frequency,
                correction_style, goal,
            )
            policy = CorrectionPolicy(
                settings.correction,
                Aggressiveness(ctx.profile.correction_aggressiveness),
                CorrectionStyle(ctx.profile.correction_style),
            )
            others = [
                row["language"]
                for row in conn.execute(
                    "SELECT language FROM language_profiles WHERE language != ?", (language,)
                ).fetchall()
            ]
        finally:
            conn.close()

        sections = assemble_sections(
            ctx,
            policy.prompt_directives(ctx.focus_patterns, ctx.profile.level_overall),
            pivot_frequency=pivot_frequency,
            other_known_languages=others,
            audio=audio,
        )
        text = "\n\n".join(s.text for s in sections)
        return {
            "language": language,
            "language_name": language_name(language),
            "is_home_base": ctx.profile.language == ctx.learner.native_language,
            "profile_exists": ctx.profile.id != 0,
            "level_overall": ctx.profile.level_overall,
            "persona": ctx.profile.preferences.get("persona", "mentor"),
            "correction_frequency": ctx.profile.correction_aggressiveness,
            "correction_style": ctx.profile.correction_style,
            "focus_patterns": [
                {"feature_key": p.feature_key, "count": p.occurrence_count}
                for p in ctx.focus_patterns
            ],
            "sections": [
                {"name": s.name, "text": s.text, "chars": len(s.text)} for s in sections
            ],
            "chars": len(text),
            "text": text,
        }

    @router.get("/config")
    def config() -> dict:
        """The config this console resolved at startup, and its schema.

        Read-only in this pass — the knob editor needs settings to be
        reloadable at runtime, which is its own piece of work.
        """
        return {
            "settings": settings.model_dump(mode="json"),
            "schema": Settings.model_json_schema(),
            "db_path": str(db_path),
        }

    return router


class PersonaEdit(BaseModel):
    text: str


def _preview_context(
    conn,
    settings: Settings,
    language: str,
    persona: str | None,
    correction_frequency: str | None,
    correction_style: str | None,
    goal: str | None,
) -> SessionContext:
    """A SessionContext assembled read-only, for previewing a prompt.

    Deliberately not SessionRunner._load_language_context: that one calls
    get_or_create_profile and decay_pattern_statuses, both of which write. A
    preview of an unstudied language returns a synthetic profile (id 0) built
    from config defaults rather than creating a row for a language the learner
    has never spoken.
    """
    repo = Repository(conn)
    learner_row = conn.execute("SELECT * FROM learners LIMIT 1").fetchone()
    if learner_row is None:
        learner = Learner(id=0, name="learner", native_language="en", other_languages=[])
    else:
        learner = Learner(
            id=learner_row["id"],
            name=learner_row["name"],
            native_language=learner_row["native_language"],
            other_languages=json.loads(learner_row["other_languages"]),
        )

    row = conn.execute(
        "SELECT * FROM language_profiles WHERE language = ? LIMIT 1", (language,)
    ).fetchone()
    if row is None:
        profile = LanguageProfile(
            id=0,
            learner_id=learner.id,
            language=language,
            level_overall=None,
            level_confidence=0.0,
            correction_aggressiveness=settings.correction.default_aggressiveness,
            correction_style=settings.correction.default_style,
            explanation_language="adaptive",
            preferences={},
        )
    else:
        profile = Repository._profile_from_row(row)

    # Overrides let you see how a knob reshapes the prompt without persisting
    # anything — the whole point of a preview.
    preferences = dict(profile.preferences)
    if persona:
        preferences["persona"] = persona
    profile = replace(
        profile,
        preferences=preferences,
        correction_aggressiveness=correction_frequency or profile.correction_aggressiveness,
        correction_style=correction_style or profile.correction_style,
    )

    if profile.id == 0:
        return SessionContext(learner=learner, profile=profile, goal=goal)
    return SessionContext(
        learner=learner,
        profile=profile,
        dimensions=repo.get_dimensions(profile.id),
        recent_summaries=repo.recent_summaries(
            language, settings.memory.recent_summaries_in_prompt
        ),
        focus_patterns=repo.focus_patterns(
            profile.id, settings.correction.recurrence_escalation_threshold
        ),
        known_feature_keys=repo.known_feature_keys(profile.id),
        goal=goal,
    )


def _current_prompt_version() -> str:
    """Read fresh each call so edited persona files show up without a restart."""
    from faria.tutor.prompts import prompt_template_version, reload_prompt_text

    reload_prompt_text()
    return prompt_template_version()


def _language_labels(concatenated: str | None) -> list[str]:
    if not concatenated:
        return []
    return sorted({code for code in concatenated.split(",") if code})


def _turn_view(utterance: sqlite3.Row, metric: sqlite3.Row | None) -> dict:
    view: dict[str, Any] = {
        "turn_index": utterance["turn_index"],
        "speaker": utterance["speaker"],
        "text": utterance["text"],
        "language": utterance["language"],
    }
    if metric is not None:
        view["metrics"] = {
            "model": metric["model"],
            "input_tokens": metric["input_tokens"],
            "output_tokens": metric["output_tokens"],
            "cache_read_tokens": metric["cache_read_tokens"],
            "cost_usd": metric["cost_usd"],
            "stt_s": metric["stt_s"],
            "first_delta_s": metric["first_delta_s"],
            "first_audio_s": metric["first_audio_s"],
        }
    return view


def _aggregate_metrics(rows: list[sqlite3.Row]) -> dict:
    """Cost, tokens, and the latency figures the <2s budget is judged on."""
    cost = sum(r["cost_usd"] or 0.0 for r in rows)
    priced = [r for r in rows if r["cost_usd"] is not None]
    first_audio = sorted(r["first_audio_s"] for r in rows if r["first_audio_s"] is not None)
    return {
        "calls": len(rows),
        "cost_usd": round(cost, 6) if priced else None,
        "unpriced_calls": len(rows) - len(priced),
        "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
        "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
        "cache_read_tokens": sum(r["cache_read_tokens"] or 0 for r in rows),
        "first_audio_median_s": _median(first_audio),
        "first_audio_worst_s": first_audio[-1] if first_audio else None,
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return round(values[mid], 2)
    return round((values[mid - 1] + values[mid]) / 2, 2)


def _config_drift(snapshot: dict | None, settings: Settings) -> list[dict]:
    """Which knobs differ between this run and the console's current config.

    The question you actually have when looking at an old run is "what is set
    differently now?" — answering it from two JSON blobs by eye is the kind of
    thing that quietly doesn't happen.
    """
    if snapshot is None:
        return []
    current = _flatten(settings.model_dump(mode="json"))
    past = _flatten(snapshot)
    keys = sorted(set(current) | set(past))
    return [
        {"key": key, "run": past.get(key), "current": current.get(key)}
        for key in keys
        if past.get(key) != current.get(key)
    ]


def _flatten(data: dict, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat
