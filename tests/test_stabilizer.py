"""Estabilizador probado con una salida real de gemini-3.5-transcribe-live (60 s de un video en inglés)."""
from vozviva.stabilizer import UtteranceStabilizer, norm_text

LAST_INTERIM = (
    "GPT-6 is here, the new generation of OpenAI model, the absolute frontier of what's possible, and it's called Astra. "
    "I have had early access and have been testing it like crazy. And I'm just going to say up front, this is absolutely "
    "the best model I have ever used, and some of the demos that I was able to create with this model are truly mind-blowing. "
    "So make sure you stick around for that towards the end of the video. So let's get into some of the details. So the first "
    "thing I want to show you are the benchmarks because it absolutely blows everything else out of the water. Look at this. "
    "This is AGI 3. This is the benchmark that drops an AI into a game with no other instructions other than complete it. "
    "And it basically saturated this benchmark, which is kind of a recent benchmark, at 98.6%. It also saturated math. "
    "This is frontier math tier four"
)
FINAL = (
    "GPT-6 is here, the new generation of OpenAI model, the absolute frontier of what's possible, and it's called Astra. "
    "I have had early access and have been testing it like crazy.\n\nI'm just going to say upfront, this is absolutely the best "
    "model I have ever used, and some of the demos that I was able to create with this model are truly mind-blowing, so make sure "
    "you stick around for that towards the end of the video.\n\nSo let's get into some of the details. The first thing I want to "
    "show you are the benchmarks because it absolutely blows everything else out of the water. Look at this. This is ARC AGI 3. "
    "This is the benchmark that drops an AI into a game with no other instructions other than complete it, and it basically "
    "saturated this benchmark, which is kind of a recent benchmark, at 98.6%.\n\nIt also saturated math. This is frontier math tier four"
)


def interims():
    """Parciales crecientes como los reales: cada estado llega dos veces y hay un cambio de puntuación."""
    words = LAST_INTERIM.split()
    out = []
    for n in range(1, len(words) + 1, 2):
        text = " ".join(words[:n])
        out += [text, text]
        if n == 21:  # el modelo primero escribió "crazy, and I'm" y después lo corrigió
            out.append(text.replace("crazy.", "crazy, and I'm"))
    out.append(LAST_INTERIM)
    return out


def run():
    counter = iter(range(1, 1000))
    st = UtteranceStabilizer(lambda: next(counter))
    live = []
    for text in interims():
        live += st.interim(text)
    return live, st.final(FINAL)


def test_publishes_sentences_before_the_model_final():
    live, _ = run()
    finals = [a.text for a in live if a.kind == "final"]
    assert finals[0] == "GPT-6 is here, the new generation of OpenAI model, the absolute frontier of what's possible, and it's called Astra."
    assert len(finals) >= 8  # sin estabilizador habría 0 hasta el segundo 68
    assert all(len(f.split()) <= 30 for f in finals)
    # el parcial en pantalla nunca repite lo ya cerrado
    last_partial = [a for a in live if a.kind == "partial"][-1]
    assert last_partial.text.startswith("It also saturated math.") or last_partial.text.startswith("This is frontier")


def test_final_corrects_and_completes_without_duplicates():
    live, final = run()
    committed = {a.seg: a.text for a in live if a.kind == "final"}
    revisions = [a for a in final if a.revision]
    assert any("ARC AGI 3" in r.text and r.previous == "This is AGI 3." for r in revisions)
    assert all(r.seg in committed for r in revisions)
    for r in revisions:
        committed[r.seg] = r.text
    for a in final:
        if not a.revision:
            assert a.seg not in committed
            committed[a.seg] = a.text
    text = " ".join(committed[s] for s in sorted(committed))
    assert norm_text(text) == norm_text(FINAL)  # todo el final, ni más ni menos


def test_flush_closes_pending_text():
    counter = iter(range(1, 100))
    st = UtteranceStabilizer(lambda: next(counter))
    st.interim("Hello every")
    st.interim("Hello everyone and")
    acts = st.flush()
    assert [a.text for a in acts] == ["Hello everyone and"] and not st.active
