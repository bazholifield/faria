"""Small REST surface: health + read-only learner-model views.

The realtime loop lives on the websocket (ws.py); these endpoints exist so a
client (or curl) can see profiles, levels, and patterns without opening a
session. Config-editing endpoints wait until a client actually needs them
(Phase 3/4) — the conversation is the UI for settings (ADR-015).
"""

from __future__ import annotations

from fastapi import APIRouter

from faria.db.connection import connect
from faria.db.repository import Repository
from faria.domain.levels import band
from faria.server.app import ServerDeps
from faria.tutor.prompts import language_name


def build_rest_router(deps: ServerDeps) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health() -> dict:
        return {"ok": True}

    @router.get("/profiles")
    def profiles() -> list[dict]:
        conn = connect(deps.settings.db_path)
        try:
            rows = conn.execute(
                "SELECT language, level_overall, level_confidence, "
                "correction_aggressiveness, correction_style FROM language_profiles "
                "ORDER BY updated_at DESC"
            ).fetchall()
            return [
                {
                    "language": r["language"],
                    "language_name": language_name(r["language"]),
                    "level": band(r["level_overall"]) if r["level_overall"] is not None else None,
                    "level_score": r["level_overall"],
                    "confidence": r["level_confidence"],
                    "correction_frequency": r["correction_aggressiveness"],
                    "correction_style": r["correction_style"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    @router.get("/patterns/{language}")
    def patterns(language: str, limit: int = 10) -> list[dict]:
        conn = connect(deps.settings.db_path)
        try:
            repo = Repository(conn)
            row = conn.execute(
                "SELECT id FROM language_profiles WHERE language = ?", (language,)
            ).fetchone()
            if row is None:
                return []
            return [
                {
                    "feature_key": p.feature_key,
                    "status": p.status,
                    "count": p.occurrence_count,
                    "examples": [
                        {"said": s, "correction": c} for s, c in repo.pattern_examples(p.id)
                    ],
                }
                for p in repo.list_patterns(row["id"], limit=limit)
            ]
        finally:
            conn.close()

    return router
