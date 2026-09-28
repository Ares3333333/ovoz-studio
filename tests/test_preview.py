"""Round 26 — the in-app preview: /caption, /media, and who may see them.

The product already computed word timings and speaker turns; a person could only
read them by downloading files. A preview is where that work becomes visible, so
these tests are about the two things a preview gets wrong: it must show the timings
the engine wrote (not a second interpretation of the same file), and it must never
hand the customer's raw recording to somebody who found a link.
"""
import json
from pathlib import Path

import pytest

from app import db
from app.ling.srt import Cue, format_srt
from conftest import unique_contact


def _uid(client, auth) -> str:
    """The user behind the session fixture: a preview belongs to whoever can ask."""
    return client.get("/api/me", headers=auth).json()["user"]["id"]


@pytest.fixture()
def preview_job(client, auth, tmp_path):
    """A job with its real artifacts on disk and a tape as its own source."""
    uid = _uid(client, auth)
    wav = tmp_path / "take.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 40 + b"junkjunkjunk")   # 48 bytes
    job = db.create_job(uid, "subtitles", "uz", "ru", 1.0, str(wav),
                        {"align": True, "words": True, "diarize": True})
    art = tmp_path / "art"
    art.mkdir()
    (art / "subs_ru.srt").write_text(format_srt([
        Cue(1, 0.20, 1.60, "Birinci cümle"),
        Cue(2, 2.00, 3.40, "Ikkinchi cümle"),
        Cue(3, 4.00, 5.20, "Uchinchi cümle"),
    ]), encoding="utf-8")
    db.add_artifact(job["id"], "srt", str(art / "subs_ru.srt"))
    return {"job": db.get_job(job["id"]), "art": art, "uid": uid}


def _attach(job, art, kind, name, payload):
    path = art / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    db.add_artifact(job["id"], kind, str(path))


# ─── /caption: the engine's own numbers, in one call ───────────────────────────

def test_a_speaker_tag_is_a_chip_and_not_a_word(client, auth, preview_job):
    """The pipeline writes `[S1] Salom…` into the cue so a burned-in file names its
    speaker. A preview that keeps the tag inside the line and also shows a speaker
    chip says it twice, and worse: the word counter bills the bracket, so the card
    promises N words and the karaoke lights up N+2 spans, one of them a parenthesis.

    The server splits them once — chip gets the number, line keeps the speech.
    """
    from app import db as _db
    jid, art = preview_job["job"]["id"], preview_job["art"]
    (art / "tagged.srt").write_text(format_srt([
        Cue(1, 0.20, 1.60, "[S1] Salom ergash"),
        Cue(2, 2.00, 3.40, "[S2] Ikkinchi cümle"),
    ]), encoding="utf-8")
    _db.add_artifact(jid, "srt", str(art / "tagged.srt"))
    _attach(preview_job["job"], art, "words", "words.json", {"cues": [
        {"i": 1, "words": [{"w": "Salom", "s": 0.3, "e": 0.8},
                           {"w": "ergash", "s": 0.9, "e": 1.5}]},
        {"i": 2, "words": [{"w": "Ikkinchi", "s": 2.1, "e": 2.8},
                           {"w": "cümle", "s": 2.9, "e": 3.3}]},
    ]})
    cap = client.get(f"/api/jobs/{jid}/caption", headers=auth).json()
    assert [c["text"] for c in cap["cues"]] == ["Salom ergash", "Ikkinchi cümle"]
    assert [c["speaker"] for c in cap["cues"]] == [1, 2], cap["cues"]
    assert cap["words"] == 4 and cap["count"] == cap["total"] == 2
    assert cap["demo"] is False


def test_a_demo_transcript_is_marked_inside_the_preview_too(client, auth, preview_job):
    """The chip on the job card answers "where did this text come from?" while the
    list is open. A person watching the preview has closed the list — the same fact
    has to be visible there, or the demo reads like a recognised transcript."""
    from app import db as _db
    jid = preview_job["job"]["id"]
    _db.merge_job_meta(jid, {"asr_demo": True, "asr_reason": "binary missing"})
    cap = client.get(f"/api/jobs/{jid}/caption", headers=auth).json()
    assert cap["demo"] is True


def test_caption_carries_words_and_speakers_from_the_engine_files(client, auth,
                                                                  preview_job):
    jid, art, job = preview_job["job"]["id"], preview_job["art"], preview_job["job"]
    _attach(job, art, "words", "words.json", {"cues": [
        {"i": 1, "words": [{"w": "Birinci", "s": 0.2, "e": 0.8},
                           {"w": "cümle", "s": 0.9, "e": 1.5}]},
        {"i": 2, "words": [{"w": "Ikkinchi", "s": 2.1, "e": 2.8},
                           {"w": "cümle", "s": 2.9, "e": 3.3}]},
        {"i": 3, "words": []},
    ]})
    _attach(job, art, "diarization", "diarization.json", {"lines": [
        {"start": 0.0, "end": 1.8, "speaker": 0, "text": "a", "cues": [1]},
        {"start": 1.9, "end": 5.9, "speaker": 1, "text": "b", "cues": [2, 3]},
    ]})
    r = client.get(f"/api/jobs/{jid}/caption", headers=auth)
    assert r.status_code == 200, r.text
    cap = r.json()
    assert cap["count"] == 3 and cap["words"] == 4 and cap["karaoke"] is True
    assert cap["speakers"] is True and cap["truncated"] is False
    cues = cap["cues"]
    assert [c["text"] for c in cues] == ["Birinci cümle", "Ikkinchi cümle",
                                        "Uchinchi cümle"]
    assert cues[0]["words"][0] == {"w": "Birinci", "s": 0.2, "e": 0.8}
    assert cues[2]["words"] == []                 # the engine refused this cue
    assert [c["speaker"] for c in cues] == [0, 1, 1], cues
    assert cues[0]["start"] == pytest.approx(0.2)
    # A media description says what the file is, never where it lives.
    blob = json.dumps(cap)
    assert str(art) not in blob and str(job["source_path"]) not in blob
    assert cap["media"]["kind"] == "audio" and cap["media"]["mime"] == "audio/wav"


def test_a_speaker_is_bound_by_time_and_not_by_a_cached_index(client, auth,
                                                              preview_job):
    """Layout moves cards between turns and diarization counts turns, not cards:
    any index cache would be a guess, and a wrong chip on a preview is worse than
    no chip — the person has no way to see that the speaker label is invented."""
    jid, art, job = preview_job["job"]["id"], preview_job["art"], preview_job["job"]
    _attach(job, art, "diarization", "diarization.json", {"lines": [
        {"start": 3.5, "end": 6.0, "speaker": 2, "text": "x", "cues": []},
    ]})
    cues = client.get(f"/api/jobs/{jid}/caption", headers=auth).json()["cues"]
    assert [c["speaker"] for c in cues] == [None, None, 2], cues


def test_caption_of_a_job_without_artifacts_is_an_empty_answer_not_an_error(client,
                                                                           auth):
    job = db.create_job(_uid(client, auth), "subtitles", "uz", "ru", 0.1,
                        "notes.srt", {})
    r = client.get(f"/api/jobs/{job['id']}/caption", headers=auth)
    assert r.status_code == 200, r.text
    cap = r.json()
    assert cap["cues"] == [] and cap["count"] == 0 and cap["karaoke"] is False
    assert cap["media"] is None                   # a .srt upload is not playable media


def test_caption_refuses_to_emit_a_number_a_browser_cannot_parse(client, auth,
                                                                 preview_job):
    """`NaN`/`Infinity` are legal Python and illegal JSON: one of them in the answer
    breaks `response.json()` on the client, which is the same failure as no answer.
    """
    from app import main
    jid, art, job = preview_job["job"]["id"], preview_job["art"], preview_job["job"]
    _attach(job, art, "words", "words.json", {"cues": [
        {"i": 1, "words": [{"w": "nan", "s": float("nan"), "e": float("inf")},
                           {"w": "ok", "s": 0.5, "e": 0.9}]},
    ]})
    body = client.get(f"/api/jobs/{jid}/caption", headers=auth).content.decode()
    assert "NaN" not in body and "Infinity" not in body, body[:200]
    cap = json.loads(body, parse_constant=lambda c: (_ for _ in ()).throw(
        ValueError(f"non-finite JSON constant {c}")))
    assert cap["cues"][0]["words"][0] == {"w": "nan", "s": 0.0, "e": 0.0}
    assert main._num(None) == 0.0 and main._num("x") == 0.0 and main._num(1.5) == 1.5


def test_caption_stops_at_its_own_ceiling_and_says_so(client, auth, tmp_path):
    """A preview is a screen, not a file transfer. But a truncation nobody can see
    is the lie this project keeps fixing, so `truncated` travels with the answer."""
    from app import main
    job = db.create_job(_uid(client, auth), "subtitles", "uz", "ru", 1.0,
                        "long.srt", {})
    art = tmp_path / "huge"
    art.mkdir()
    many = [Cue(i + 1, i * 2.0, i * 2.0 + 1.2, f"kartochka {i + 1}")
            for i in range(2000)]
    (art / "subs.srt").write_text(format_srt(many), encoding="utf-8")
    db.add_artifact(job["id"], "srt", str(art / "subs.srt"))
    cap = main._caption_of(db.get_job(job["id"]))
    assert cap["count"] == main._CAPTION_MAX_CUES < len(many)
    assert cap["truncated"] is True
    # the ceiling is above the longest legal job, so no paid job is cut in practice
    from app import pipeline
    assert main._CAPTION_MAX_CUES > pipeline.MAX_SEGMENTS_PER_JOB


# ─── /media: the customer's own recording, and nobody else's ──────────────────

def test_media_plays_the_uploaded_tape_to_its_owner(client, auth, preview_job):
    jid = preview_job["job"]["id"]
    r = client.get(f"/api/jobs/{jid}/media", headers=auth)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav"
    assert "attachment" not in (r.headers.get("content-disposition") or "")
    assert len(r.content) == Path(preview_job["job"]["source_path"]).stat().st_size


def test_media_of_a_text_upload_is_refused_without_leaking_a_path(client, auth,
                                                                  tmp_path):
    src = tmp_path / "script.srt"
    src.write_text("1\n00:00:00,000 --> 00:00:01,000\nSalom\n", encoding="utf-8")
    job = db.create_job(_uid(client, auth), "subtitles", "uz", "ru", 0.1,
                        str(src), {})
    r = client.get(f"/api/jobs/{job['id']}/media", headers=auth)
    assert r.status_code == 410, r.text
    body = r.json()["detail"]
    assert str(src) not in body and "tmp" not in body.lower(), body


def test_media_reports_gone_when_retention_purged_the_file(client, auth, preview_job):
    jid = preview_job["job"]["id"]
    Path(preview_job["job"]["source_path"]).unlink()
    assert client.get(f"/api/jobs/{jid}/media", headers=auth).status_code == 410
    cap = client.get(f"/api/jobs/{jid}/caption", headers=auth).json()
    assert cap["media"] is None and cap["count"] == 3      # the preview still works


def test_media_and_caption_are_owner_only_and_need_a_token(client, auth, preview_job):
    jid = preview_job["job"]["id"]
    other = db.create_user("Other", unique_contact())["id"]
    hdr = {"Authorization": "Bearer " + db.create_session(other)}
    assert client.get(f"/api/jobs/{jid}/media", headers=hdr).status_code == 404
    assert client.get(f"/api/jobs/{jid}/caption", headers=hdr).status_code == 404
    assert client.get(f"/api/jobs/{jid}/media").status_code in (401, 403)
    assert client.get(f"/api/jobs/{jid}/caption").status_code in (401, 403)


def test_share_page_offers_results_and_never_the_source(client, auth, preview_job):
    """A share link is a promise about the work, not about the raw upload: the
    recording a person uploaded is not part of what they agreed to publish."""
    jid = preview_job["job"]["id"]
    db.set_status_if(jid, "done", ("queued", "running"))   # only finished work shares
    share = client.post(f"/api/jobs/{jid}/share", headers=auth, data={})
    assert share.status_code == 200, share.text
    page = client.get(share.json()["share_url"])
    assert page.status_code == 200
    kinds = list((page.json().get("artifacts") or {}).keys())
    assert "srt" in kinds and "media" not in kinds
    blob = json.dumps(page.json())
    assert str(preview_job["job"]["source_path"]) not in blob
