from faria.speech.sentences import SentenceAssembler


def feed_chars(assembler: SentenceAssembler, text: str) -> list[str]:
    """Worst-case streaming: one character per delta."""
    out = []
    for ch in text:
        out.extend(assembler.feed(ch))
    return out


def test_basic_split_and_flush():
    a = SentenceAssembler()
    got = feed_chars(a, "Hola. ¿Cómo estás? Bien, gracias")
    assert got == ["Hola.", "¿Cómo estás?"]
    assert a.flush() == "Bien, gracias"
    assert a.flush() is None


def test_decimal_and_ellipsis_do_not_split_early():
    a = SentenceAssembler()
    got = feed_chars(a, "Cuesta 2.50 euros hoy. Bueno... vamos a ver. Sí. ")
    # "Bueno... vamos" must NOT split (lowercase continuation = pause);
    # the final "Sí." stays buffered until flush (nothing follows it yet).
    assert got == ["Cuesta 2.50 euros hoy.", "Bueno... vamos a ver."]
    assert a.flush() == "Sí."


def test_trailing_quotes_stay_with_sentence():
    a = SentenceAssembler()
    got = feed_chars(a, 'Ella dijo "no." Y se fue. Punto final')
    assert got == ['Ella dijo "no."', "Y se fue."]
    assert a.flush() == "Punto final"


def test_multi_delta_chunks():
    a = SentenceAssembler()
    out = []
    for delta in ["Guten Tag! Wie ", "geht es dir? Ich ", "hoffe gut."]:
        out.extend(a.feed(delta))
    assert out == ["Guten Tag!", "Wie geht es dir?"]
    assert a.flush() == "Ich hoffe gut."


def test_long_unpunctuated_text_still_flushes():
    a = SentenceAssembler()
    words = ("palabra " * 60).strip()  # 480 chars, no terminal punctuation
    got = a.feed(words)
    assert got, "buffer cap should force a cut"
    assert all(len(s) < 300 for s in got)
