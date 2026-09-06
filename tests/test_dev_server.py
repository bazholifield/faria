"""Developer console (ADR-027): the run ledger over a real database.

Drives sessions through SessionRunner with a scripted brain, then reads them
back through the HTTP surface the console page uses.
"""

import pytest
from fastapi.testclient import TestClient

from faria.config import Settings
from faria.server.dev import create_dev_app
from faria.session.lifecycle import SessionRunner

from test_lifecycle import FakeBrain
from test_run_metadata import MeteredBrain, _Usage


@pytest.fixture()
def client(tmp_path, repo):
    """Console over the same tmp DB the repo fixture writes to."""
    db_path = tmp_path / "test.db"
    return TestClient(create_dev_app(db_path, Settings()))


def _run_session(repo, *, language="es", turns=("hola",), brain=None):
    brain = brain or MeteredBrain()
    runner = SessionRunner(repo, brain, Settings())
    runner.start("bazh", language=language, mode="audio")
    for text in turns:
        brain.queue("Ya veo.")
        list(runner.turn(text))
    return runner


def test_health_reports_the_db_it_reads(client, tmp_path):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["db"] == str(tmp_path / "test.db")


def test_empty_ledger_is_not_an_error(client):
    body = client.get("/api/runs").json()
    assert body["runs"] == []
    assert body["current_prompt_version"]


def test_ledger_row_carries_cost_latency_and_prompt_version(client, repo):
    runner = _run_session(repo, turns=("hola", "qué tal"))
    runner.attach_turn_timings(stt_s=1.8, first_delta_s=2.2, first_audio_s=2.9)

    body = client.get("/api/runs").json()
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["mode"] == "audio"
    assert run["learner_turns"] == 2
    assert run["languages"] == ["es"]
    assert run["calls"] == 2
    assert run["cost_usd"] > 0
    assert run["first_audio_median_s"] == 2.9
    # persona text is unchanged since the run, so the ledger shouldn't flag it
    assert run["prompt_version"] == body["current_prompt_version"]


def test_unpriced_calls_are_counted_not_costed(client, repo):
    """A brain whose model has no price entry must not fabricate a cost."""
    _run_session(repo, brain=FakeBrain())  # model 'fake-model'

    run = client.get("/api/runs").json()["runs"][0]
    assert run["cost_usd"] is None
    assert run["unpriced_calls"] == 1


def test_detail_exposes_transcript_metrics_and_prompt_list(client, repo):
    runner = _run_session(repo, turns=("hola",))
    runner.attach_turn_timings(stt_s=1.5, first_delta_s=2.0, first_audio_s=2.6)

    d = client.get("/api/runs/1").json()
    assert d["totals"]["calls"] == 1
    assert [p["language_name"] for p in d["prompts"]] == ["Spanish"]
    learner_turn = next(t for t in d["turns"] if t["speaker"] == "learner")
    assert learner_turn["text"] == "hola"
    assert learner_turn["metrics"]["first_audio_s"] == 2.6
    assert learner_turn["metrics"]["model"] == "claude-sonnet-5"


def test_detail_404s_for_an_unknown_session(client):
    assert client.get("/api/runs/999").status_code == 404


def test_prompt_endpoint_returns_the_exact_text_that_was_sent(client, repo):
    runner = _run_session(repo)
    prompt_hash = client.get("/api/runs/1").json()["prompts"][0]["hash"]

    body = client.get(f"/api/prompts/{prompt_hash}").json()
    assert body["text"] == runner._system
    assert body["used_by"] == [{"session_id": 1, "seq": 0, "language": "es"}]


def test_prompt_endpoint_404s_for_unknown_hash(client):
    assert client.get("/api/prompts/deadbeef").status_code == 404


def test_config_drift_lists_knobs_that_changed_since_the_run(tmp_path, repo):
    """The question you actually have about an old run."""
    settings = Settings()
    settings.audio.vad_silence_ms = 1200
    SessionRunner(repo, MeteredBrain(), settings).start("bazh", language="es")

    # console running with the default 700ms
    client = TestClient(create_dev_app(tmp_path / "test.db", Settings()))
    drift = client.get("/api/runs/1").json()["config_drift"]
    assert {"key": "audio.vad_silence_ms", "run": 1200, "current": 700} in drift


def test_no_drift_when_config_matches(client, repo):
    _run_session(repo)
    assert client.get("/api/runs/1").json()["config_drift"] == []


def test_config_endpoint_exposes_settings_and_schema(client):
    body = client.get("/api/config").json()
    assert body["settings"]["models"]["conversation"] == "claude-sonnet-5"
    assert "properties" in body["schema"]


# -- prompt lab ---------------------------------------------------------------


@pytest.fixture()
def persona_sandbox(tmp_path, monkeypatch):
    """Redirect persona storage so edit tests never touch the shipped files."""
    from faria.tutor import prompts

    sandbox = tmp_path / "personas"
    sandbox.mkdir()
    (sandbox / "mentor.md").write_text(prompts.personas()["mentor"] + "\n", encoding="utf-8")
    monkeypatch.setattr(prompts, "_PERSONA_DIR", sandbox)
    prompts.reload_prompt_text()
    yield sandbox
    prompts.reload_prompt_text()  # drop the sandbox from the cache


def test_personas_endpoint_lists_text_and_version(client):
    body = client.get("/api/personas").json()
    assert body["version"]
    names = [p["name"] for p in body["personas"]]
    assert "mentor" in names
    mentor = next(p for p in body["personas"] if p["name"] == "mentor")
    assert "Abbé Faria" in mentor["text"]
    assert mentor["chars"] == len(mentor["text"])


def test_saving_a_persona_changes_the_template_version(client, persona_sandbox):
    before = client.get("/api/personas").json()["version"]
    res = client.put("/api/personas/mentor", json={"text": "You are terse. Say little."})
    assert res.status_code == 200
    assert res.json()["version"] != before

    # written to disk, and served back on the next read
    assert "terse" in (persona_sandbox / "mentor.md").read_text()
    assert client.get("/api/personas").json()["personas"][0]["text"] == "You are terse. Say little."


def test_creating_a_new_persona_adds_a_file(client, persona_sandbox):
    client.put("/api/personas/storyteller", json={"text": "You tell long stories."})
    names = [p["name"] for p in client.get("/api/personas").json()["personas"]]
    assert names == ["mentor", "storyteller"]
    assert (persona_sandbox / "storyteller.md").exists()


@pytest.mark.parametrize("name", ["../escape", "with/slash", "UPPER", "", "  "])
def test_persona_names_that_could_escape_the_directory_are_rejected(client, persona_sandbox, name):
    res = client.put(f"/api/personas/{name}", json={"text": "x"})
    assert res.status_code in (400, 404, 405)  # rejected by validation or by routing
    assert not list(persona_sandbox.glob("*escape*"))


def test_empty_persona_text_is_rejected(client, persona_sandbox):
    assert client.put("/api/personas/mentor", json={"text": "   "}).status_code == 400
    assert "Abbé Faria" in (persona_sandbox / "mentor.md").read_text()  # unchanged


def test_preview_renders_labeled_sections(client, repo):
    _run_session(repo)  # gives the es profile some history

    d = client.get("/api/preview?language=es").json()
    names = [s["name"] for s in d["sections"]]
    assert names[0] == "persona:mentor"
    assert "correction" in names and "control" in names
    assert d["text"] == "\n\n".join(s["text"] for s in d["sections"])
    assert d["profile_exists"] is True


def test_preview_reflects_overrides_without_persisting_them(client, repo):
    _run_session(repo)

    off = client.get("/api/preview?language=es&correction_frequency=off").json()
    assert "Correction policy: none" in off["text"]
    # the profile itself is untouched
    row = repo._conn.execute(
        "SELECT correction_aggressiveness FROM language_profiles WHERE language = 'es'"
    ).fetchone()
    assert row["correction_aggressiveness"] == "balanced"


def test_preview_of_an_unstudied_language_creates_no_profile(client, repo):
    """Looking at a prompt must not bring a profile into existence."""
    before = repo._conn.execute("SELECT COUNT(*) c FROM language_profiles").fetchone()["c"]

    d = client.get("/api/preview?language=ja").json()
    assert d["profile_exists"] is False
    assert d["language_name"] == "Japanese"
    assert d["sections"]  # still renders, on config defaults

    after = repo._conn.execute("SELECT COUNT(*) c FROM language_profiles").fetchone()["c"]
    assert after == before


def test_preview_does_not_age_the_focus_list_it_is_showing(client, repo):
    """SessionRunner decays pattern statuses on load; a preview must not."""
    _run_session(repo)
    repo._conn.execute(
        "UPDATE error_patterns SET last_seen_at = datetime('now', '-100 days')"
    )
    repo._conn.commit()
    before = [r["status"] for r in repo._conn.execute("SELECT status FROM error_patterns")]

    client.get("/api/preview?language=es")

    after = [r["status"] for r in repo._conn.execute("SELECT status FROM error_patterns")]
    assert after == before


def test_home_base_preview_omits_correction_sections(client, repo):
    d = client.get("/api/preview?language=en").json()
    assert d["is_home_base"] is True
    assert "correction" not in [s["name"] for s in d["sections"]]


def test_preview_audio_and_pivot_toggles_add_their_sections(client, repo):
    _run_session(repo)
    plain = client.get("/api/preview?language=es").json()
    assert "audio" not in [s["name"] for s in plain["sections"]]

    audio = client.get("/api/preview?language=es&audio=true").json()
    assert "audio" in [s["name"] for s in audio["sections"]]
    assert "transcription note" in audio["text"]


def test_preview_uses_the_persona_override(client, persona_sandbox):
    client.put("/api/personas/terse", json={"text": "You are terse."})
    d = client.get("/api/preview?language=es&persona=terse").json()
    assert d["sections"][0]["name"] == "persona:terse"
    assert d["sections"][0]["text"] == "You are terse."


def test_preview_targets_lists_real_profiles_and_personas(client, repo):
    _run_session(repo)
    body = client.get("/api/preview-targets").json()
    assert "mentor" in body["personas"]
    assert [p["language"] for p in body["profiles"]] == ["es"]
    assert body["profiles"][0]["language_name"] == "Spanish"


def test_ledger_tolerates_sessions_predating_the_migration(client, repo):
    """Rows written before 0005 have no config or prompt version."""
    repo._conn.execute("INSERT INTO learners (name) VALUES ('old')")
    repo._conn.execute("INSERT INTO sessions (learner_id, mode) VALUES (1, 'text')")
    repo._conn.commit()

    run = client.get("/api/runs").json()["runs"][0]
    assert run["has_config"] is False
    assert run["prompt_version"] is None
    assert run["cost_usd"] is None

    d = client.get(f"/api/runs/{run['id']}").json()
    assert d["config_snapshot"] is None
    assert d["config_drift"] == []
