"""Round 27 — the share link opens as a page, not as a protocol dump.

A shared link is the one screen of this product that a person sees without an
account, without context and without a reason to trust it. Handing them raw JSON
says "the work is in there somewhere"; a page says what was made, in which
languages, until when, and shows the subtitles themselves.

These tests also hold the security line that comes with publishing text: cue
content is client data, rendered on an unauthenticated origin-visible page, so it
must be escaped, and the page must not become an executable surface.
"""
import json
import re
from pathlib import Path

import pytest

from app import db


@pytest.fixture()
def shared(client, auth, tmp_path):
    """A done job with real artifacts and a share link, plus the owner's token."""
    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    job = db.create_job(uid, "subtitles", "uz", "ru", 2.0, "take.wav",
                        {"align": True, "words": True})
    db.set_status_if(job["id"], "done", ("queued", "running"))
    art = tmp_path / "art"
    art.mkdir()
    from app.ling.srt import Cue, format_srt
    (art / "subs_ru.srt").write_text(format_srt([
        Cue(1, 0.2, 1.6, "<img src=x onerror=alert(1)> birinchi qator"),
        Cue(2, 2.0, 3.4, "[S1] Ikkinchi cümle — salom"),
        Cue(3, 4.0, 5.2, "Uchinchi"),
    ]), encoding="utf-8")
    db.add_artifact(job["id"], "srt", str(art / "subs_ru.srt"))
    (art / "transcript.txt").write_text("Salom", encoding="utf-8")
    db.add_artifact(job["id"], "transcript", str(art / "transcript.txt"))
    share = client.post(f"/api/jobs/{job['id']}/share", headers=auth,
                        data={"ttl_hours": "24"})
    assert share.status_code == 200, share.text
    return {"job": job, "url": share.json()["share_url"], "art": art}


HTML = {"Accept": "text/html,application/xhtml+xml"}


def test_a_browser_gets_a_page_and_an_sdk_gets_json(client, shared):
    r = client.get(shared["url"], headers=HTML)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html"), r.headers["content-type"]
    body = r.text
    assert "<!DOCTYPE html>" in body and "Ovoz" in body
    # the JSON contract is untouched: keys, shape and status are what they were
    j = client.get(shared["url"])
    assert j.status_code == 200
    assert set(j.json()) == {"job_type", "artifacts", "expires_at"}
    assert j.json()["job_type"] == "subtitles"
    assert "/dl/srt" in j.json()["artifacts"]["srt"]


def test_the_page_names_the_job_and_never_the_upload(client, shared):
    body = client.get(shared["url"], headers=HTML).text
    # the default language is Uzbek, and the type is shown as a word people read,
    # not as the value stored in the `type` column
    assert "Subtitrlar" in body and ">subtitles<" not in body
    assert "uz → ru" in body
    assert "take.wav" not in body, "the customer's filename leaked to the page"
    assert "/dl/media" not in body and "/api/jobs" not in body
    assert "/dl/srt" in body and "/dl/transcript" in body
    assert str(shared["art"]) not in body, "a filesystem path reached the page"


def test_subtitle_text_is_rendered_escaped(client, shared):
    """The caption text is the customer's own, shown on a public page: an unescaped
    `<img onerror>` there is stored XSS served to every recipient who opens the
    link, and the link is exactly the thing people forward to each other.

    The text must still be *readable* — escaping is not deleting. So the assertion
    is about tags, not substrings: the payload survives as characters, and no tag
    in the document carries an event attribute.
    """
    body = client.get(shared["url"], headers=HTML).text
    assert "onerror=alert(1)" in body, "the text should still be readable"
    assert "&lt;img src=x" in body, "the page did not escape the uploader's markup"
    assert not re.search(r"<[^>]*\son\w+=", body), "an inline handler escaped"
    assert not re.search(r"<img[^>]*>", body), "the injected tag was revived"
    assert "<script" not in body.lower()


def test_the_subtitles_themselves_are_on_the_page(client, shared):
    body = client.get(shared["url"], headers=HTML).text
    assert "Ikkinchi cümle" in body and "Uchinchi" in body
    # a range, not a start: whoever received the link judges density from length
    # (`-->` arrives escaped as `--&gt;`, which is the same thing proven safe)
    assert "00:00:02,000 --&gt; 00:00:03,400" in body
    # the speaker tag is kept where it belongs: the label, not counted as a word
    assert "[S1] Ikkinchi" in body


def test_the_page_and_the_data_cannot_be_confused_in_a_cache(client, shared):
    """Live QA reproduced this 3 times: a JSON request to a URL that had just been
    opened as a page returned the cached HTML, because the answer depends on
    `Accept` and `Accept-Language` and said so nowhere. A CDN between the studio
    and a recipient would do the same to real traffic, so the answer must carry
    both headers — and a link that changes with time must not be stored at all.
    """
    for headers in (HTML, {"Accept": "application/json"}):
        r = client.get(shared["url"], headers=headers)
        assert r.headers.get("vary") == "Accept, Accept-Language", r.headers
        assert "no-store" in (r.headers.get("cache-control") or ""), r.headers
    missing = client.get("/s/nesuch", headers=HTML)
    assert missing.headers.get("vary") == "Accept, Accept-Language"


def test_file_links_are_in_the_owners_order_and_the_type_is_localised(client, shared):
    """The recipient reads a result, not a listing of database columns: the chips
    come in the order the owner's card shows them, and `subtitles` is only ever an
    internal identifier."""
    ru = client.get(shared["url"], headers={**HTML, "Accept-Language": "ru"}).text
    order = [ru.index(f'/dl/{kind}') for kind in ("transcript", "srt")]
    assert order == sorted(order), "the share page reordered the files"
    assert "Субтитры" in ru
    assert ">subtitles<" not in ru and "subtitles</span>" not in ru, \
        "a database enum reached a human page"


def test_the_expiry_names_its_timezone(client, shared):
    """An unlabelled `17:14` is read in the recipient's own clock; the stored moment
    is UTC, so in Tashkent the link looks five hours younger than it is."""
    body = client.get(shared["url"], headers=HTML).text
    assert "UTC" in body


def test_a_shared_dubbing_gets_a_player_and_nothing_else_does(client, shared):
    assert "<audio" not in client.get(shared["url"], headers=HTML).text
    art = shared["art"]
    wav = art / "dubbing.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 40)
    db.add_artifact(shared["job"]["id"], "dubbing", str(wav))
    body = client.get(shared["url"], headers=HTML).text
    assert '<audio controls src="/s/' in body and "/dl/dubbing" in body


def test_the_page_follows_the_visitors_language(client, shared):
    ru = client.get(shared["url"], headers={**HTML, "Accept-Language": "ru,en;q=0.8"}).text
    uz = client.get(shared["url"], headers=HTML).text           # default uz
    en = client.get(shared["url"], headers={**HTML, "Accept-Language": "en"}).text
    assert "Текст субтитров" in ru and "Subtitr matni" in uz and "Subtitle text" in en
    assert 'lang="ru"' in ru and 'lang="uz"' in uz and 'lang="en"' in en
    # a language the product does not speak still gets a page, in the default one
    weird = client.get(shared["url"], headers={**HTML, "Accept-Language": "de"}).text
    assert 'lang="uz"' in weird


def test_expired_and_missing_links_answer_html_too(client, shared, auth):
    r = client.get("/s/nosuchid", headers=HTML)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "Havola topilmadi" in r.text
    # the same answer for a client that asked for data
    j = client.get("/s/nosuchid")
    assert j.status_code == 404 and j.json()["error_code"] == "not_found"

    sid = shared["url"].rsplit("/", 1)[-1]
    conn = db.get_conn()
    conn.execute("UPDATE shares SET expires_at = ? WHERE id = ?",
                 ("2000-01-01T00:00:00+00:00", sid))
    conn.commit()
    gone = client.get(shared["url"], headers=HTML)
    assert gone.status_code == 410
    assert ("истёк" in gone.text) or ("tugagan" in gone.text), gone.text[:200]
    # Round 27 deliberately splits the generic `gone` into specific codes: an
    # integrator can tell an expired link from a download-capped one. That is the
    # whole point of the JSON half, so it is pinned here rather than left implicit.
    gj = client.get(shared["url"])
    assert gj.status_code == 410 and gj.json()["error_code"] == "share_expired", gj.text
    assert client.get(f"{shared['url']}/dl/srt").status_code == 410, \
        "an expired link must not keep serving files"


def test_the_share_page_carries_the_same_security_headers(client, shared):
    r = client.get(shared["url"], headers=HTML)
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src" in csp and "unsafe-eval" not in csp
    assert r.headers.get("x-content-type-options") == "nosniff"


# ─── gates: the page cannot drift away from the rest of the product ────────────

def test_every_artifact_kind_has_a_name_on_the_page():
    from app.main import _JOB_ARTIFACT_KINDS, _SHARE_ARTIFACT_LABELS
    missing = set(_JOB_ARTIFACT_KINDS) - set(_SHARE_ARTIFACT_LABELS)
    assert not missing, f"share page would print a database column name: {sorted(missing)}"


def test_share_page_labels_exist_in_every_language():
    from app.main import _SHARE_LABELS
    assert set(_SHARE_LABELS) == {"uz", "ru", "en"}, _SHARE_LABELS.keys()
    sets = {lang: set(labels) for lang, labels in _SHARE_LABELS.items()}
    assert sets["uz"] == sets["ru"] == sets["en"], {
        k: sorted(v - sets["uz"]) for k, v in sets.items()}
    empty = [(lang, key) for lang, labels in _SHARE_LABELS.items()
             for key, value in labels.items() if not value.strip()]
    assert not empty, f"share page labels with no text: {empty}"


def test_the_share_page_is_styled_on_a_phone_too():
    """The link is opened in a messenger, on a phone: the live QA found the share
    page had no media query at all while the rest of the product has eighteen."""
    css = (Path(__file__).resolve().parents[1] / "static" / "styles.css").read_text(
        encoding="utf-8")
    media = re.findall(r"@media[^{]+\{(.+?)\n\}", css, re.S)
    assert any(".share-wrap" in block for block in media), \
        "the share page never reflows"


def test_the_share_page_is_a_server_rendered_page_with_no_scripts():
    """The share page is HTML the server wrote, so the CSP stays as strict as it is
    for the SPA: no inline handlers, no scripts, nothing that would make an
    anonymous page the one place the policy is loose."""
    from app.main import _share_html
    src = json.dumps  # noqa: F841  (keeps json imported for readers of this file)
    html = _share_html("abc123", {"job_id": "j", "job_type": "subtitles",
                                  "expires_at": "2026-01-01T00:00:00+00:00",
                                  "src": "uz", "tgt": "ru"},
                       {"id": "j", "type": "subtitles", "src": "uz", "tgt": "ru",
                        "minutes": 1.0, "meta": {}}, {}, "en")
    assert "<script" not in html.lower() and " onerror" not in html
    assert " onclick=" not in html and "<style" not in html
    assert '<link rel="stylesheet" href="/styles.css">' in html
