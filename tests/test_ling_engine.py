"""Exhaustive tests for the proprietary Uzbek language engine (the moat).

Covers script detection, reversible transliteration, idempotency, official
apostrophe rendering, glossary-protected conversion, code-switch signals and
the public /api/v1/ling endpoints.
"""
import pytest

from app.ling import engine as ling
from app.ling.romanizer import (detect_script, from_new_latin, is_new_latin,
                                looks_cyrillic_uz, normalize_uzbek, to_new_latin,
                                to_official_apostrophe, transliterate)


# ---------- script detection ----------

def test_detect_latin():
    r = detect_script("Salom, bu O'zbek tili")
    assert r["script"] == "latin"
    assert r["confidence"] >= 0.8


def test_detect_cyrillic():
    r = detect_script("Салом, бу Ўзбек тили")
    assert r["script"] == "cyrillic"
    assert r["cyrillic"] > r["latin"]


def test_detect_mixed():
    # deliberate code-switch: half Cyrillic half Latin
    r = detect_script("Привет hello мир world тест")
    assert r["script"] == "mixed"


def test_detect_other_when_no_letters():
    r = detect_script("123 456 !!!")
    assert r["script"] == "other"
    assert r["confidence"] == 0.0


# ---------- transliteration round-trips ----------

@pytest.mark.parametrize("word", [
    "O'zbekiston", "salom", "rahmat", "ma'no", "g'ala", "o'rta",
    "Shvetsiya", "Chirchiq", "task spirali",
])
def test_roundtrip_latin_is_lossless(word):
    cyr = transliterate(word, "cyrillic")
    assert looks_cyrillic_uz(cyr)
    assert transliterate(cyr, "latin") == normalize_uzbek(word)


def test_transliterate_is_idempotent_latin():
    once = transliterate("Ўзбек тили", "latin")
    twice = transliterate(once, "latin")
    assert once == twice == "O'zbek tili"


def test_transliterate_is_idempotent_cyrillic():
    once = transliterate("O'zbek tili", "cyrillic")
    twice = transliterate(once, "cyrillic")
    assert once == twice == "Ўзбек тили"


def test_transliterate_does_not_mangle_target_script():
    # already-Latin text going to Latin must not be corrupted
    assert transliterate("assalomu alaykum", "latin") == "assalomu alaykum"


def test_title_case_digraph_is_not_shouted():
    # capital single Cyrillic letter mapping to a digraph → Title Case, not ALL CAPS
    assert transliterate("Шветсия", "latin") == "Shvetsiya"
    assert transliterate("Халқаро", "latin") == "Xalqaro"


def test_all_caps_run_stays_upper():
    assert transliterate("ШВЕТСИЯ", "latin") == "SHVETSIYA"


def test_all_caps_across_word_boundaries():
    # every word is an independent ALL-CAPS run → all stay uppercase
    assert transliterate("Я ЛЮБЛЮ ТЕБЯ", "latin") == "YA LYUBLYU TEBYA"


def test_eng_letter_is_transliterated():
    # ң is a real Uzbek Cyrillic letter → must not leak through as mixed script
    assert transliterate("ОҢ", "latin") == "ONG"
    assert "ң" not in transliterate("ОҢ", "latin")


def test_bogus_letter_removed_from_table():
    from app.ling.romanizer import CYR_TO_LAT
    assert "ӆ" not in CYR_TO_LAT


# ---------- official apostrophe ----------

def test_official_apostrophe_only_marks_o_and_g():
    out = to_official_apostrophe("o'rg'ilgan")
    assert "o\u02bb" in out and "g\u02bb" in out
    # a lone apostrophe after other letters stays untouched
    assert to_official_apostrophe("ma'no") == "ma'no"


def test_official_apostrophe_caps():
    assert "O\u02bb" in to_official_apostrophe("O'zbekiston")


# ---------- engine facade ----------

def test_analyze_full_shape():
    a = ling.analyze("Ўзбек тили")
    for key in ("script", "confidence", "latin", "cyrillic", "official",
                "new_latin", "words", "chars", "code_switch_hints", "glossary_hits"):
        assert key in a
    assert a["latin"] == "O'zbek tili"
    assert a["words"] == 2


def test_analyze_empty_is_safe():
    a = ling.analyze("")
    assert a["script"] == "other"
    assert a["words"] == 0


def test_analyze_glossary_hits():
    a = ling.analyze("Salom CocaCola do'stim", {"CocaCola": "Coca-Cola"})
    assert "CocaCola" in a["glossary_hits"]


def test_code_switch_hints_nonzero():
    # Russian inline marker must register (was structurally 0 before fix)
    assert ling.analyze("Салом, это хорошо дуруст")["code_switch_hints"] > 0


def test_glossary_chained_amplification_is_bounded():
    # a public caller must not be able to blow up output size via chained rules
    terms = {"a": "b" * 120, "bb": "c" * 120, "cccc": "d" * 120,
             "dddd": "e" * 120, "eeee": "f" * 120}
    out = ling.convert("a a a a", to="latin", terms=terms)["result"]
    assert len(out) < 10_000


def test_glossary_regex_metachars_are_literal():
    # back-references in a term value must NOT be interpreted as a re template
    from app.ling.glossary import apply_terms
    assert apply_terms("cat", {"cat": r"\1"}) == r"\1"
    assert apply_terms("cat", {"cat": r"a\g<0>b"}) == r"a\g<0>b"


def test_convert_official_latin():
    r = ling.convert("g'oz o'rta", to="latin", official=True)
    assert "g\u02bb" in r["result"] and "o\u02bb" in r["result"]


def test_convert_glossary_survives_script_flip():
    # brand term must be restored even after Cyrillic round-trip
    r = ling.convert("Ўзбекча", to="latin", terms={"O'zbekcha": "UZBEK"})
    assert r["result"] == "UZBEK"


def test_detect_helper():
    assert ling.detect("Salom")["script"] == "latin"


# ---------- 2026 alphabet reform (Ö/Ğ/Ş/Ç) ----------

def test_to_new_latin_core_marks():
    assert to_new_latin("O'zbekiston") == "\u00d6zbekiston"      # Ö
    assert to_new_latin("g'oz") == "\u011foz"                    # ğ
    assert to_new_latin("Shvetsiya") == "\u015evetsiya"          # Ş
    assert to_new_latin("Chirchik") == "\u00c7ir\u00e7ik"         # Ç… (title Ch → uppercase Ç)


def test_to_new_latin_leaves_plain_apostrophe():
    # ma'no has no o'/g' — the lone apostrophe must survive untouched
    assert to_new_latin("ma'no") == "ma'no"


def test_to_new_latin_is_idempotent():
    once = to_new_latin("O'zbekiston g'ala")
    assert once == to_new_latin(once) == to_new_latin(to_new_latin(once))


def test_new_latin_roundtrip_lowercase():
    src = "o'zbek o'rin g'oz shahar chora"
    assert from_new_latin(to_new_latin(src)) == src


def test_new_latin_roundtrip_title_case():
    src = "O'zbekiston Shahrisabz Chimkent G'allakor"
    assert from_new_latin(to_new_latin(src)) == src


def test_is_new_latin_detector():
    assert is_new_latin("\u00d6zbek \u015e\u010f")
    assert not is_new_latin("O'zbek Sh")
    # code-review M1: capital Ğ must be in the set (was a Ǝ typo)
    assert is_new_latin("\u011e")
    assert not is_new_latin("\u018e")


def test_cyrillic_to_new_latin_pipeline():
    # Ўзбек тили → legacy O'zbek tili → reform Özbek tili
    assert transliterate("Ўзбек тили", "new_latin") == "\u00d6zbek tili"


def test_new_latin_input_folds_back_to_legacy_latin():
    # three-way: new Latin input must canonicalise to legacy Latin, not mangle
    assert transliterate("\u00d6zbek tili", "latin") == "O'zbek tili"
    assert transliterate("\u00d6zbek tili", "cyrillic") == "Ўзбек тили"


def test_new_latin_all_caps_source_collapses_to_upper_single_letter():
    assert to_new_latin("SH") == "\u015e"
    assert to_new_latin("CH") == "\u00c7"


def test_from_new_latin_recovers_all_caps_context():
    # code-review M3: ŞAHAR must expand to SHAHAR, never the mangled ShAHAR
    assert from_new_latin("\u015eAHAR") == "SHAHAR"
    assert from_new_latin("\u00d6ZBEK") == "O'ZBEK"
    assert from_new_latin("\u015eahar") == "Shahar"      # title case unchanged
    # word-final capital digraph inside an ALL-CAPS word still expands fully
    assert from_new_latin("I\u015e") == "ISH"
    assert from_new_latin("\u015e") == "SH"              # lone capital → shout form


def test_all_caps_new_latin_roundtrip():
    src = "SHAHAR CHIRCHIQ"
    assert from_new_latin(to_new_latin(src)) == src
    # title case round-trips too, and the two never collide
    assert from_new_latin(to_new_latin("Shahrisabz")) == "Shahrisabz"


def test_mixed_cyrillic_new_latin_folds_to_legacy():
    # code-review M2: to='latin' contract — NO reform letter may leak when the
    # input also contains Cyrillic
    out = transliterate("\u040e\u0437\u0431\u0435\u043a \u00d6zbek", "latin")
    assert out == "O'zbek O'zbek"
    assert transliterate(out, "latin") == out            # idempotent now


def test_detect_script_counts_reform_letters():
    # code-review M6: pure new-Latin text used to classify as 'other'
    r = detect_script("\u015e\u015e\u015e\u00c7\u00c7\u00c7")
    assert r["script"] == "latin"
    assert r["latin"] == 6


# ---------- kill-switch machinery must fail OPEN, never crash ----------
# load()/set_flag() mutate module globals AND rewrite the backing file, so
# every case runs against a redirected tmp store and restores global state.

import app.flags as fl


def _flags_case(tmp_path, monkeypatch, request, content=None):
    """Point flags storage at a tmp file, load it, and guarantee the module
    global is restored afterwards (load/set_flag mutate it in place)."""
    path = tmp_path / "feature_flags.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    saved = dict(fl._flags)
    request.addfinalizer(lambda: setattr(fl, "_flags", saved))
    monkeypatch.setattr(fl, "_FLAGS_FILE", path)
    fl.load()
    return path


def test_flags_load_survives_non_dict_file(tmp_path, monkeypatch, request):
    # code-review M5: a hand-mangled feature_flags.json must not stop boot
    _flags_case(tmp_path, monkeypatch, request, "null")
    assert fl.get_all()["uzbek_language_engine"]["enabled"] is True


def test_flags_load_coerces_non_dict_entry(tmp_path, monkeypatch, request):
    _flags_case(tmp_path, monkeypatch, request,
                '{"uzbek_language_engine": false}')
    assert isinstance(fl.get_all()["uzbek_language_engine"], dict)


def test_flag_defaults_not_aliased(tmp_path, monkeypatch, request):
    # shallow _DEFAULTS.copy() let set_flag mutate the module literal itself
    _flags_case(tmp_path, monkeypatch, request)  # fresh store → defaults
    fl.set_flag("uzbek_language_engine", enabled=False)
    assert fl.get_all()["uzbek_language_engine"]["enabled"] is False
    assert fl._DEFAULTS["uzbek_language_engine"]["enabled"] is True


def test_official_apostrophe_source_converts_to_new_latin():
    # official U+02BB form (oʻ/gʻ) is a valid conversion source too
    assert to_new_latin("O\u02bbzbekiston") == "\u00d6zbekiston"


def test_analyze_exposes_new_latin():
    assert ling.analyze("Ўзбек тили")["new_latin"] == "\u00d6zbek tili"


def test_convert_to_new_latin_via_engine():
    r = ling.convert("O'zbekiston", to="new_latin")
    assert r["result"] == "\u00d6zbekiston"


# ---------- public API endpoints ----------

def test_api_transliterate_endpoint(client):
    resp = client.post("/api/v1/ling/transliterate",
                       json={"text": "Ўзбек тили", "to": "latin"})
    assert resp.status_code == 200
    assert resp.json()["result"] == "O'zbek tili"


def test_api_transliterate_rejects_bad_target(client):
    resp = client.post("/api/v1/ling/transliterate",
                       json={"text": "salom", "to": "klingon"})
    assert resp.status_code == 422


def test_api_transliterate_new_latin_target(client):
    resp = client.post("/api/v1/ling/transliterate",
                       json={"text": "\u040e\u0437\u0431\u0435\u043a \u0442\u0438\u043b\u0438", "to": "new_latin"})
    assert resp.status_code == 200
    assert resp.json()["result"] == "\u00d6zbek tili"


def test_api_transliterate_normalises_target(client):
    # code-review m1: one target vocabulary shared by the endpoint and the
    # engine — aliases resolve for every caller, unknown names never pass
    assert client.post("/api/v1/ling/transliterate",
                       json={"text": "salom", "to": " Latin "}).status_code == 200
    assert client.post("/api/v1/ling/transliterate",
                       json={"text": "salom", "to": "CYRILLIC"}).status_code == 200
    assert client.post("/api/v1/ling/transliterate",
                       json={"text": "salom", "to": "newlatin"}).status_code == 200
    assert client.post("/api/v1/ling/transliterate",
                       json={"text": "salom", "to": "new_latin"}).status_code == 200
    for bad in ("klingon", "", "latn"):
        assert client.post("/api/v1/ling/transliterate",
                           json={"text": "salom", "to": bad}).status_code == 422


def test_api_target_alias_matches_canonical_output(client):
    alias = client.post("/api/v1/ling/transliterate",
                        json={"text": "\u040e\u0437\u0431\u0435\u043a", "to": "new"}).json()["result"]
    canonical = client.post("/api/v1/ling/transliterate",
                            json={"text": "\u040e\u0437\u0431\u0435\u043a", "to": "new_latin"}).json()["result"]
    assert alias == canonical == "\u00d6zbek"


def test_api_terms_accept_official_apostrophe(client):
    # code-review m2: a term keyed with official U+02BB must still match the
    # ASCII-apostrophe transliteration output
    resp = client.post("/api/v1/ling/transliterate", json={
        "text": "\u040e\u0437\u0431\u0435\u043a\u0438\u0441\u0442\u043e\u043d",
        "terms": {"O\u02bbzbekiston": "OZ"}})
    assert resp.status_code == 200
    assert resp.json()["result"] == "OZ"


def test_api_analyze_endpoint(client):
    resp = client.post("/api/v1/ling/analyze", json={"text": "Салом дуруст"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["script"] == "cyrillic"
    assert body["latin"] == "Salom durust"


def test_api_rejects_non_string_text(client):
    resp = client.post("/api/v1/ling/analyze", json={"text": ["a", "b"]})
    assert resp.status_code == 422


def test_api_rejects_non_object_body(client):
    resp = client.post("/api/v1/ling/analyze", json=[1, 2, 3])
    assert resp.status_code == 422


def test_api_requires_text(client):
    assert client.post("/api/v1/ling/analyze", json={}).status_code == 422


def test_api_detect_endpoint(client):
    resp = client.get("/api/v1/ling/detect", params={"text": "O'zbek"})
    assert resp.status_code == 200
    assert resp.json()["script"] == "latin"


def test_api_rejects_oversize(client):
    resp = client.post("/api/v1/ling/analyze", json={"text": "a" * 6000})
    assert resp.status_code == 413


def test_api_rejects_oversize_body_by_content_length(client):
    # review gap: the middleware pre-parse had no test of its own
    resp = client.post("/api/v1/ling/analyze",
                       content=b'{"text": "' + b"a" * 200_000 + b'"}',
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 413
    assert resp.headers.get("connection") == "close"
    # The refusal must be readable, not just status-coded: live probing showed a
    # 413 sent while the client was still writing tore the socket, so the SDK got
    # a reset instead of this body (in-process TestClient cannot reproduce the
    # reset — scripts/probe_layout_live.py is what proves the drain works).
    assert resp.json()["error_code"] == "payload_too_large"
    assert "too large" in resp.json()["detail"].lower()


def test_api_rejects_oversize_chunked_body(client):
    # no Content-Length at all: the stream-level cap must still shed the body
    def chunks():
        yield b'{"text": "'
        for _ in range(20):
            yield b"a" * 10_000
        yield b'"}'
    resp = client.post("/api/v1/ling/analyze", content=chunks(),
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 413
    # the cap is enforced mid-stream, so this path drains without buffering and
    # still owes the caller a readable, structured refusal
    assert resp.json()["error_code"] == "payload_too_large"


def test_api_official_ignored_for_non_latin_targets(client):
    # contract question the review asked: official + new_latin must not put a
    # turned comma back into the reformed alphabet
    resp = client.post("/api/v1/ling/transliterate", json={
        "text": "\u040e\u0437\u0431\u0435\u043a", "to": "new_latin", "official": True})
    assert resp.status_code == 200
    assert resp.json()["result"] == "\u00d6zbek"
    assert "\u02bb" not in resp.json()["result"]


def test_api_analyze_never_leaks_reform_letters_in_latin_field(client):
    # review gap: property that 'latin' is always canonical legacy Latin
    for text in ("\u00d6zbek \u015e\u010f", "\u040e\u0437\u0431\u0435\u043a \u00d6zbek",
                 "\u04ab\u0430\u04bb\u0430\u0440", "O\u02bbzbekiston"):
        body = client.post("/api/v1/ling/analyze", json={"text": text}).json()
        assert not is_new_latin(body["latin"]), text


def test_analyze_reports_script_states_present():
    a = ling.analyze("\u040e\u0437\u0431\u0435\u043a \u00d6zbek")
    assert a["has_cyrillic_letters"] and a["has_reform_letters"]
    assert a["has_legacy_latin_letters"]      # latin field is ASCII-normalised
    assert not ling.analyze("salom")["has_reform_letters"]


def test_glossary_case_only_duplicate_keys_are_deterministic():
    # review NIT: {"SH":x,"Sh":y} used to resolve by iteration order
    from app.ling.glossary import apply_terms
    first = apply_terms("SH", {"SH": "ONE", "Sh": "TWO"})
    second = apply_terms("SH", {"Sh": "TWO", "SH": "ONE"})
    assert first == second
