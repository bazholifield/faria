from faria.speech.lang_router import SentenceLanguageRouter


def router() -> SentenceLanguageRouter:
    return SentenceLanguageRouter(["en", "es", "fr", "de", "ar"])


def reconstructs(runs, text):
    return "".join(t for t, _ in runs) == text


# -- the safety invariant: base text never switches voice (a false switch is a
#    real glitch; a missed foreign phrase is just the status quo) -------------

def test_no_false_switch_in_monolingual_base():
    r = router()
    cases = [
        ("Not a bad way to spend an afternoon, all told, my friend.", "en"),
        ("I once spent fourteen years somewhere far less comfortable than this.", "en"),
        ("What are we working on today? Tell me everything about it.", "en"),
        # accented base language: Spanish 'qué' must NOT read as French
        ("Ayer fuiste a la playa con tu hermana, qué bien, me alegro mucho.", "es"),
        ("Das Wort beschreibt die Sehnsucht nach der Ferne, nicht wahr?", "de"),
    ]
    for text, base in cases:
        runs = r.split_runs(text, base)
        assert reconstructs(runs, text), runs
        assert all(lang == base for _, lang in runs), f"false switch in {text!r}: {runs}"


# -- switching on genuine multi-word foreign phrases --------------------------

def test_switches_on_multiword_foreign_phrase():
    r = router()
    for text, base, want in [
        ("You could say bonjour mon ami to greet your friend.", "en", "fr"),
        ("In Spanish we say la sobremesa es sagrada, you know.", "en", "es"),
    ]:
        runs = r.split_runs(text, base)
        assert reconstructs(runs, text)
        foreign = [(t, l) for t, l in runs if l != base]
        assert foreign, f"missed the foreign phrase in {text!r}"
        assert foreign[0][1] == want


def test_single_run_when_no_foreign_content():
    r = router()
    runs = r.split_runs("Just an ordinary English sentence right here.", "en")
    assert runs == [("Just an ordinary English sentence right here.", "en")]


def test_short_string_is_single_run():
    r = router()
    assert r.split_runs("Sí, claro.", "es") == [("Sí, claro.", "es")]


def test_empty_language_set_never_splits():
    r = SentenceLanguageRouter([])
    text = "In Spanish we say la sobremesa es sagrada after a meal."
    assert r.split_runs(text, "en") == [(text, "en")]


def test_reconstruction_always_holds():
    r = router()
    for text in [
        "A mix of English and un peu de français dans la phrase.",
        "Pure English, nothing else here at all.",
        "¿Vamos a la playa o prefieres quedarte en casa hoy?",
    ]:
        runs = r.split_runs(text, "en")
        assert reconstructs(runs, text)


def test_detect_whole_string():
    r = router()
    assert r.detect("Das Wort Fernweh beschreibt eine tiefe Sehnsucht.", "en") == "de"
    assert r.detect("Not a bad afternoon at all, really and truly.", "en") == "en"
