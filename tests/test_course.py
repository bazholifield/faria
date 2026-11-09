from pathlib import Path
"""The course layer (ADR-034): Leitner review, word matching, curriculum selection, and the
taught -> used -> wrong cycle and coverage updates through a real SessionRunner."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from faria.config import CourseConfig, Settings
from faria.course import curriculum, lemmas, review
from faria.session.lifecycle import SessionRunner
from faria.tutor.prompts import assemble_system_prompt

from test_lifecycle import FakeBrain, _Block

INTERVALS = [1, 3, 7, 14, 30]
T0 = datetime(2026, 10, 27, 12, 0, 0)

SYLLABUS = """language: es
name: Spanish
levels: [A1, A2, B1, B2]
items:
  - id: es.a1.ser-estar
    level: A1
    strand: grammar
    title: "ser and estar"
    status: checked
  - id: es.a2.preterite
    level: A2
    strand: grammar
    title: "Preterite"
    status: draft
  - id: es.a2.cut-me
    level: A2
    strand: grammar
    title: "Cut item"
    status: cut
  - id: es.a2.past-weekend
    level: A2
    strand: function
    title: "Talk about last weekend"
    status: draft
  - id: es.b1.subjunctive
    level: B1
    strand: grammar
    title: "Present subjunctive"
    status: draft
  - id: es.a2.topic-mercado
    level: A2
    strand: topic
    title: "Markets in Mexico"
    facts: ["Tianguis are open-air street markets."]
    status: draft
"""


# ---- Leitner -------------------------------------------------------------------------------
def test_leitner_moves():
    card = review.new_card(T0, INTERVALS)
    assert card.box == 0 and card.due_at == T0 + timedelta(days=1)
    review.used(card, T0, INTERVALS)                  # brand new: counts even before due
    assert card.box == 1
    review.used(card, T0 + timedelta(hours=1), INTERVALS)  # not due yet: no promotion
    assert card.box == 1 and card.times_used == 2
    review.used(card, T0 + timedelta(days=2), INTERVALS)   # due: up a box, next due in 3 days
    assert card.box == 2 and card.due_at == T0 + timedelta(days=5)
    review.wrong(card, T0 + timedelta(days=3), INTERVALS)
    assert card.box == 1
    due = card.due_at
    review.heard(card, due + timedelta(hours=1))      # heard when due: pushed a day, no box move
    assert card.box == 1 and card.due_at == due + timedelta(days=1, hours=1)
    assert review.stage(0, 0) == "introduced" and review.stage(4, 3) == "solid"


# ---- word matching -----------------------------------------------------------------------
def test_word_keys_and_matching():
    assert lemmas.key("la playa", "es") == lemmas.key("playa", "es")
    seq = lemmas.lemmas("Ayer fuimos a la playa con mi hermana", "es")
    assert lemmas.contains(seq, lemmas.key("la playa", "es"))
    assert lemmas.contains(lemmas.lemmas("te echo de menos", "es"), lemmas.key("echo de menos", "es"))
    assert not lemmas.contains(seq, lemmas.key("el mercado", "es"))


# ---- curriculum ------------------------------------------------------------------------
def _write_syllabus(tmp_path):
    (tmp_path / "es.yaml").write_text(SYLLABUS, encoding="utf-8")
    return tmp_path


def test_curriculum_selection(tmp_path):
    items = curriculum.load(_write_syllabus(tmp_path), "es")
    assert "es.a2.cut-me" not in {i["id"] for i in items}            # cut items never offered
    nxt = curriculum.next_items(items, "A2", covered={"es.a2.preterite"}, n=2)
    assert [i["id"] for i in nxt] == ["es.a2.past-weekend", "es.b1.subjunctive"]  # own level, then next
    assert curriculum.next_items(items, "B2", covered=set(), n=2) == []  # below-level items assumed known
    assert [t["id"] for t in curriculum.topics(items, "A2", told=set(), n=2)] == ["es.a2.topic-mercado"]
    assert curriculum.topics(items, "A2", told={"es.a2.topic-mercado"}, n=2) == []
    assert curriculum.placement_probes(items)[0] == ("A1", ["ser and estar"])


# ---- through a real session ------------------------------------------------------------------
def _runner(repo, tmp_path):
    settings = Settings(course=CourseConfig(curriculum_dir=_write_syllabus(tmp_path)))
    brain = FakeBrain()
    return SessionRunner(repo, brain, settings), brain


def _say(brain, text, teaching=None, errors=None):
    blocks = [_Block("text", text=text)]
    if teaching is not None:
        blocks.append(_Block("tool_use", name="note_teaching",
                             input={"words": [], "syllabus_items": [], "topic": None, **teaching}))
    if errors is not None:
        blocks.append(_Block("tool_use", name="annotate_errors", input={"errors": errors}))
    brain._script.append((text, blocks))


def test_placement_first_then_taught_word_is_reviewed(repo, tmp_path):
    runner, brain = _runner(repo, tmp_path)
    runner.start("bazh", language="es")
    course = runner.context.course
    assert course.placement and course.variety.startswith("neutral Latin American")
    assert "places them" in brain_prompt(runner)

    _say(brain, "En la *playa*, claro.", teaching={
        "words": [{"text": "la playa", "gloss": "the beach", "asked": True}],
        "syllabus_items": ["es.a2.preterite", "es.made-up.id"], "topic": "es.a2.topic-mercado"})
    list(runner.turn("¿Cómo se dice beach?"))
    store = repo
    pid = runner.context.profile.id
    (word,) = store.vocab_words(pid)
    assert (word["display"], word["source"], word["box"]) == ("la playa", "asked", 0)
    assert set(store.syllabus_coverage(pid)) == {"es.a2.preterite"}           # invented ids ignored
    assert store.topics_told(pid) == {"es.a2.topic-mercado"}

    _say(brain, "¡Qué bien!")
    list(runner.turn("Ayer fui a la playa"))
    assert store.vocab_words(pid)[0]["box"] == 1 and store.vocab_words(pid)[0]["times_used"] == 1

    _say(brain, "Ah, ¿*las playas*?", errors=[{
        "span_text": "las playa", "category": "morphosyntax", "subcategory": "agreement",
        "severity": 1, "confidence": 0.9, "feature_key": "morphosyntax:agreement:number",
        "correction": "las playas", "explanation": "plural", "interference_source": None, "handled": "corrected_recast"}])
    list(runner.turn("Me gustan las playa"))
    row = store.vocab_words(pid)[0]
    assert row["box"] == 1 and row["times_wrong"] == 1


def brain_prompt(runner):
    return runner._system


def test_course_section_offers_due_words_and_next_items(repo, tmp_path):
    runner, _ = _runner(repo, tmp_path)
    ctx = runner.start("bazh", language="es")
    store = repo
    store.add_vocab_word(ctx.profile.id, "mercado", "el mercado", "the market", "taught",
                   review.Card(box=1, due_at=datetime(2020, 1, 1)), None, None)
    repo._conn.execute("UPDATE language_profiles SET level_overall = 2.3 WHERE id = ?", (ctx.profile.id,))
    repo._conn.commit()
    notes = runner._course.notes(repo.get_or_create_profile(ctx.learner.id, "es", "balanced", "recast"),
                                 is_home=False)
    assert notes.placement is None
    assert notes.due_words == [("el mercado", "the market")]
    assert [i["id"] for i in notes.new_items] == ["es.a2.preterite", "es.a2.past-weekend"]
    from dataclasses import replace
    prompt = assemble_system_prompt(replace(runner.context, course=notes), "")
    assert "el mercado (the market)" in prompt and "[es.a2.preterite]" in prompt
    assert "None of this is an agenda" in prompt
    # the learner's subject first; history instead of small talk when they have none
    assert "Open by asking what's going on and what they feel like talking about" in prompt
    assert "history, art, literature or philosophy" in prompt


def test_items_with_a_tidbit_are_offered_as_stories(tmp_path):
    items = curriculum.load(_write_syllabus(tmp_path), "es")
    for i in items:
        if i["id"] == "es.a2.preterite":
            i["tidbit"] = "Cortés's ships were run aground, not burned."
    told = curriculum.topics(items, "A2", told=set(), n=5)
    assert {"es.a2.preterite", "es.a2.topic-mercado"} <= {i["id"] for i in told}
    assert "es.a2.preterite" not in {i["id"] for i in curriculum.topics(items, "A2", told={"es.a2.preterite"}, n=5)}


def test_every_real_syllabus_item_has_a_tidbit():
    root = Path(__file__).resolve().parent.parent / "curriculum"
    for lang in ("es", "fr", "de"):
        missing = [i["id"] for i in curriculum.load(root, lang) if not i.get("tidbit")]
        assert not missing, f"{lang}: no tidbit for {missing}"


def test_session_memory_is_background_not_an_opener(repo, tmp_path):
    from faria.tutor import prompts
    from types import SimpleNamespace as NS
    ctx = NS(recent_summaries=[NS(summary_text="Talked about their two dogs.", next_focus=[])])
    section = prompts._memory_section(ctx, "Spanish", is_home_base=False)
    assert "their two dogs" in section and "don't open with it" in section


def test_sweep_moves_coverage_to_practised_then_solid(repo, tmp_path):
    runner, _ = _runner(repo, tmp_path)
    ctx = runner.start("bazh", language="es")
    store, pid = repo, ctx.profile.id
    use = [SimpleNamespace(item_id="es.a2.preterite", correct_uses=2, incorrect_uses=0)]
    sessions = [repo.create_session(ctx.learner.id).id for _ in range(4)]
    for session in sessions[:3]:
        runner._course.apply_sweep(pid, session, use)
    row = store.syllabus_coverage(pid)["es.a2.preterite"]
    assert row["state"] == "solid" and row["sessions_correct"] == 3
    runner._course.apply_sweep(pid, sessions[3], [SimpleNamespace(item_id="es.a2.preterite", correct_uses=0, incorrect_uses=2)])
    assert store.syllabus_coverage(pid)["es.a2.preterite"]["state"] == "practised"
