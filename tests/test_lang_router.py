from faria.speech.lang_router import SentenceLanguageRouter


def router() -> SentenceLanguageRouter:
    return SentenceLanguageRouter(["en", "es", "fr", "de", "ar"])


def test_routes_clear_foreign_sentence():
    r = router()
    assert r.detect("En español decimos: la sobremesa es sagrada, amigo mío.", default="en") == "es"
    assert r.detect("Das Wort Fernweh hat keine gute Übersetzung.", default="en") == "de"


def test_keeps_default_for_same_language():
    r = router()
    assert r.detect("Not a bad way to spend an afternoon, all told.", default="en") == "en"


def test_short_fragments_fall_back_to_default():
    r = router()
    assert r.detect("¡Sí!", default="en") == "en"
    assert r.detect("Genau.", default="en") == "en"


def test_empty_language_set_is_inert():
    r = SentenceLanguageRouter([])
    assert r.detect("En español decimos muchas cosas interesantes.", default="en") == "en"
