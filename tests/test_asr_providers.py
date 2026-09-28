# -*- coding: utf-8 -*-
"""Round 23 — real speech recognition, and the honesty around its absence.

Two things are tested here. The first is the boring one: the two whisper CLIs are
asked differently and answer differently, and confusing their units turns a 90-second
tape into a 90-hour one without any error being raised.

The second is the reason the file exists. A deployment can ask for real ASR and not
have the binary; the job then completes on invented text. That outcome must be named
by the server (event, meta, /api/v1/info), shown to the customer, and never reached
by reading a `[демо]` prefix out of a subtitle file.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.providers import asr as A
from app.providers import translate as TR
from app.providers import tts as TTS

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audio_studio import interview, wav_bytes  # noqa: E402


# ─── dialects ─────────────────────────────────────────────────────────────────

def test_auto_dialect_reads_the_evidence_not_the_hope():
    assert A.WhisperASR("whisper-cli").dialect == "whispercpp"
    assert A.WhisperASR("/opt/bin/main").dialect == "whispercpp"
    assert A.WhisperASR("whisper", model="models/ggml-small.bin").dialect == "whispercpp"
    assert A.WhisperASR("whisper").dialect == "openai"


def test_an_explicit_dialect_beats_the_guess():
    assert A.WhisperASR("whisper-cli", "openai").dialect == "openai"
    assert A.WhisperASR("whisper", "whispercpp", "m.bin").dialect == "whispercpp"


def test_the_two_clis_are_asked_in_their_own_words(tmp_path):
    audio, out = tmp_path / "take.wav", tmp_path / "job"
    cpp = A.WhisperASR("whisper-cli", model="models/ggml.bin")
    cmd = cpp.command(audio, out, "uz")
    assert cmd[:2] == ["whisper-cli", "-m"] and "models/ggml.bin" in cmd
    assert "-f" in cmd and "-oj" in cmd and "--language" in cmd
    assert str(out / "take.out") in cmd            # -of names the stem it will write
    oai = A.WhisperASR("whisper").command(audio, out, "uz")
    assert oai[:2] == ["whisper", str(audio)]
    assert "--output_format" in oai and "json" in oai and "--output_dir" in oai
    assert oai[-2:] == ["--language", "uzbek"]
    # a language the CLI does not know must not be claimed at it
    assert "--language" not in A.WhisperASR("whisper").command(audio, out, "en")


def test_both_schemas_land_on_float_seconds():
    oai = A.WhisperASR("whisper").parse(
        {"segments": [{"start": 1.5, "end": 3.25, "text": " salom "},
                      {"start": 3.5, "end": 4.0, "text": "   "}]})
    assert [(s.start, s.end, s.text) for s in oai] == [(1.5, 3.25, "salom")]
    cpp = A.WhisperASR("whisper-cli", model="m.bin").parse(
        {"transcription": [{"offsets": {"from": 1500, "to": 3250}, "text": "salom"},
                           {"offsets": {"from": 3250, "to": 4000}, "text": ""}]})
    assert [(s.start, s.end, s.text) for s in cpp] == [(1.5, 3.25, "salom")]
    # milliseconds, not seconds: the same numbers read with the wrong unit would
    # put a card 1000x later than the tape and never fail
    assert cpp[0].start == pytest.approx(oai[0].start)


# ─── running a binary (faked at the process boundary, everything else real) ───

def _fake_run(payload, box):
    """A subprocess stand-in that writes where the command said to write."""
    def run(cmd, **kw):
        class R:
            returncode, stdout, stderr = 0, "", ""
        out = Path(cmd[cmd.index("--output_dir") + 1]) if "--output_dir" in cmd \
            else Path(cmd[cmd.index("-of") + 1]).parent
        out.mkdir(parents=True, exist_ok=True)
        stem = Path(cmd[cmd.index("-of") + 1]).name if "-of" in cmd else "take"
        (out / (stem + ".json")).write_text(json.dumps(payload), encoding="utf-8")
        box.append(list(cmd))
        return R()
    return run


@pytest.mark.parametrize("dialect,payload", (
    ("openai", {"segments": [{"start": 0.0, "end": 2.0, "text": "salom dunyo"}]}),
    ("whispercpp", {"transcription": [{"offsets": {"from": 0, "to": 2000},
                                       "text": "salom dunyo"}]}),
))
def test_transcribe_reads_its_own_output_dir_and_leaves_nothing_behind(
        tmp_path, monkeypatch, dialect, payload):
    audio = tmp_path / "take.wav"
    audio.write_bytes(b"RIFF")
    box = []
    monkeypatch.setattr(A.subprocess, "run", _fake_run(payload, box))
    asr = A.WhisperASR("whisper-cli" if dialect == "whispercpp" else "whisper",
                       dialect, "m.bin")
    segs = asr.transcribe(audio, "uz")
    assert [(s.start, s.text) for s in segs] == [(0.0, "salom dunyo")]
    assert box and box[0][0] == asr.binary
    # the isolated directory is ours for the run and gone after it
    assert not (tmp_path / ".asr_tmp_take").exists()


def test_a_failing_binary_raises_with_its_stderr(tmp_path, monkeypatch):
    audio = tmp_path / "take.wav"
    audio.write_bytes(b"RIFF")

    class R:
        returncode, stdout, stderr = 2, "", "model file missing"

    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError) as exc:
        A.WhisperASR("whisper").transcribe(audio, "uz")
    assert "model file missing" in str(exc.value)


def test_a_binary_that_produced_nothing_is_said_so(tmp_path, monkeypatch):
    audio = tmp_path / "take.wav"
    audio.write_bytes(b"RIFF")

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="no json output"):
        A.WhisperASR("whisper").transcribe(audio, "uz")


# ─── status(): claimed against provable ───────────────────────────────────────

@pytest.fixture()
def cfg(monkeypatch):
    """A copy of the real settings a test may override.

    Copied field by field from the live object rather than written out by hand: a
    status() that reads a new setting must not be testable only because the test
    happened to know the field exists.
    """
    names = ["asr_provider", "whisper_bin", "whisper_model", "asr_dialect",
             "asr_model", "asr_compute",
             "translate_provider", "openai_api_key", "openai_model", "tts_provider"]
    conf = SimpleNamespace(**{n: getattr(settings, n) for n in names})
    for mod in (A, TR, TTS):
        monkeypatch.setattr(mod, "settings", conf)
    return conf


def test_the_default_install_says_so_in_words(cfg):
    cfg.asr_provider = "sim"
    st = A.status()
    assert st["mode"] == "sim" and "no provider configured" in st["reason"]
    assert A.get_asr().name == "sim-asr"


def test_real_provider_with_no_binary_configured(cfg):
    cfg.asr_provider, cfg.whisper_bin = "real", ""
    st = A.status()
    assert st["mode"] == "sim" and "without OVOZ_WHISPER_BIN" in st["reason"]


def test_a_configured_binary_that_is_not_installed_is_reported_without_naming_it(
        cfg, monkeypatch):
    """The customer needs to know the machine is not doing real ASR; a stranger does
    not need to learn which binary and which path this host tried to run."""
    cfg.asr_provider, cfg.whisper_bin = "real", "/opt/whisper/bin/definitely-not-here-9f2c"
    monkeypatch.setattr(A.shutil, "which", lambda _: None)
    st = A.status()
    assert st["mode"] == "sim" and st["code"] == "binary_missing"
    assert "OVOZ_WHISPER_BIN" in st["reason"]
    assert "definitely-not-here" not in st["reason"] and "definitely-not-here" not in json.dumps(
        {k: v for k, v in st.items() if k != "operator"})
    assert st["operator"]["binary_found"] is False
    assert A.get_asr().name == "sim-asr", "silently pretending is the bug"


def test_whisper_cpp_without_a_model_is_incomplete_not_broken(cfg, monkeypatch):
    cfg.asr_provider, cfg.whisper_bin, cfg.whisper_model = "real", "whisper-cli", ""
    monkeypatch.setattr(A.shutil, "which", lambda _: "/usr/bin/whisper-cli")
    st = A.status()
    assert st["operator"]["dialect"] == "whispercpp" and st["code"] == "no_model_configured"
    assert "OVOZ_WHISPER_MODEL" in st["reason"]
    cfg.whisper_model = str(Path("/srv/models") / "ggml-absent.bin")
    st2 = A.status()
    assert st2["code"] == "model_missing" and st2["operator"]["model_found"] is False
    assert "ggml-absent" not in st2["reason"], "the reason string is shown to customers"


def test_a_complete_whisper_install_is_reported_as_real(cfg, tmp_path, monkeypatch):
    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"\x00")
    cfg.asr_provider, cfg.whisper_bin, cfg.whisper_model = "real", "whisper-cli", str(model)
    monkeypatch.setattr(A.shutil, "which", lambda _: "/usr/bin/whisper-cli")
    st = A.status()
    assert st["mode"] == "real" and st["code"] == "ok" and st["reason"] == ""
    assert st["operator"]["binary_found"] and st["operator"]["model_found"]
    assert st["operator"]["dialect"] == "whispercpp"
    got = A.get_asr()
    assert got.name == "whisper" and got.model == str(model)


def test_translate_and_tts_answer_the_same_question(cfg):
    cfg.translate_provider, cfg.openai_api_key = "openai", ""
    st = TR.status()
    assert st["mode"] == "sim" and st["code"] == "no_key"
    assert "without OPENAI_API_KEY" in st["reason"]
    assert st["operator"]["key_present"] is False
    cfg.tts_provider = "edge"
    t = TTS.status()
    # edge_tts is not a dependency of this app: the honest answer is the reason
    assert t["mode"] in ("real", "sim")
    if t["mode"] == "sim":
        assert t["code"] == "package_missing" and "edge_tts is not installed" in t["reason"]


ADMIN = {"Authorization": "Bearer test-admin-key"}


def test_public_diagnostics_name_variables_never_values(client, cfg, tmp_path):
    """/api/v1/info is anonymous and up to 300/min. It answers the customer's
    question («is this real or demo?»), and the operator's question (which binary,
    which model path) is answered only behind the admin key."""
    model = tmp_path / "ggml-SHOULD-NOT-LEAK.bin"
    model.write_bytes(b"\x00")
    cfg.asr_provider, cfg.whisper_bin, cfg.whisper_model = "real", "/opt/whisper/bin/whisper-cli", str(model)
    cfg.openai_api_key = "sk-SUPERSECRET-should-never-leave-the-process"
    body = json.dumps(client.get("/api/v1/info").json()["providers"])
    for leak in ("SHOULD-NOT-LEAK", "SUPERSECRET", "/opt/whisper", "whisper-cli"):
        assert leak not in body, leak
    pub = client.get("/api/v1/info").json()["providers"]
    assert set(pub["asr"]) == {"provider", "mode", "code", "reason"}, pub["asr"]
    adm = client.get("/api/admin/status", headers=ADMIN).json()["providers"]
    assert adm["asr"]["operator"]["model_found"] is True
    assert "SHOULD-NOT-LEAK" in json.dumps(adm), "the operator half must be the useful one"


def test_public_diagnostics_survive_a_broken_self_check(client, monkeypatch):
    """A stage that cannot answer must not take the endpoint down, and must not
    quote the exception: class names and messages are the map of the machine."""
    def boom():
        raise FileNotFoundError("[Errno 2] no such file: /srv/secrets/whisper.bin")
    monkeypatch.setattr(A, "status", boom)
    r = client.get("/api/v1/info")
    assert r.status_code == 200, r.text
    p = r.json()["providers"]["asr"]
    assert p["mode"] == "unknown" and p["code"] == "self_check_failed"
    assert "whisper.bin" not in json.dumps(p) and "FileNotFound" not in json.dumps(p)


# ─── what a finished job tells about its own text ─────────────────────────────

def _submit(client, auth, name, blob, jtype="subtitles"):
    return client.post("/api/jobs", headers=auth,
                       files={"file": (name, blob, "application/octet-stream")},
                       data={"jtype": jtype, "src": "uz", "tgt": "ru"}).json()["job"]


def test_audio_without_a_real_engine_is_marked_demo(client, auth):
    job = _submit(client, auth, "take.wav", wav_bytes(interview()))
    assert job["status"] == "done", job
    assert job["engines"]["asr"] == "sim"
    assert job["engines"]["asr_demo"] is True
    # the chip has to be able to explain itself, and the reason is the server's
    # words, not a string the UI invents
    assert job["engines"]["asr_reason"].strip()
    assert "demo" in job["engines"]["asr_reason"].lower()


def test_the_customer_own_transcript_is_not_called_a_demo(client, auth):
    srt = ("1\n00:00:00,000 --> 00:00:02,000\nAssalomu alaykum.\n\n"
           "2\n00:00:02,200 --> 00:00:05,000\nRahmat, ustoz.\n")
    job = _submit(client, auth, "script.srt", srt.encode("utf-8"))
    assert job["status"] == "done", job
    assert job["engines"]["asr_demo"] is False


def test_the_event_and_the_meta_agree(client, auth):
    """The card reads meta, support reads the timeline; one of them drifting is how
    a disclosure becomes a rumour."""
    from app import db
    job = _submit(client, auth, "take.wav", wav_bytes(interview()))
    ev = [e for e in db.job_timeline(job["id"]) if e["step"] == "asr"][-1]
    assert ev["data"]["code"] == "demo_transcript", ev
    assert ev["data"]["mode"] == "sim" and ev["data"]["segments"] >= 1
    meta = db.get_job(job["id"])["meta"]
    assert meta["asr_demo"] is True and meta["asr_mode"] == "sim"
    # one fact, three readers: the timeline line, the job meta and the API payload
    assert meta["asr_reason"] in ev["message"], (meta["asr_reason"], ev["message"])
    assert meta["asr_reason"] == job["engines"]["asr_reason"]


def test_speech_to_text_type_carries_the_same_flag(client, auth):
    job = _submit(client, auth, "take.wav", wav_bytes(interview()), jtype="transcribe")
    assert job["engines"]["asr_demo"] is True
    assert "transcript" in job["artifacts"]


# ─── faster-whisper: the real, offline, no-key Python provider (Round 30) ──────

class _FakeSpec:
    """Stands in for importlib.util.find_spec so the faster branch is testable
    whether or not faster-whisper is installed in the running environment (CI has
    no heavy CTranslate2 wheel; this machine does)."""

    def __init__(self, present): self._p = present
    def __call__(self, name): return object() if (self._p and name == "faster_whisper") else None


def test_faster_without_the_package_is_honest_not_silent(cfg, monkeypatch):
    """`OVOZ_ASR_PROVIDER=faster` with the library missing must not hand a customer a
    demo transcript and call it real — the whole point of the Round-24 status split."""
    monkeypatch.setattr(A.importlib.util, "find_spec", _FakeSpec(False))
    cfg.asr_provider = "faster"
    cfg.asr_model = "base"
    st = A.status()
    assert st["mode"] == "sim" and st["code"] == "package_missing", st


def test_faster_needs_a_model_size(cfg, monkeypatch):
    monkeypatch.setattr(A.importlib.util, "find_spec", _FakeSpec(True))
    cfg.asr_provider = "faster"
    cfg.asr_model = ""
    st = A.status()
    assert st["mode"] == "sim" and st["code"] == "no_model_configured", st


def test_faster_configured_and_present_reports_real(cfg, monkeypatch):
    monkeypatch.setattr(A.importlib.util, "find_spec", _FakeSpec(True))
    cfg.asr_provider = "faster"
    cfg.asr_model = "base"
    cfg.asr_compute = "int8"
    st = A.status()
    assert st["mode"] == "real" and st["code"] == "ok", st
    assert st["operator"]["faster_model"] == "base"


def test_get_asr_reuses_one_loaded_model_per_size(cfg, monkeypatch):
    """The weights are big; a fresh WhisperModel per job would pay ~10 s of load on
    every request. get_asr must hand back the same instance for the same (size,cpu)."""
    monkeypatch.setattr(A.importlib.util, "find_spec", _FakeSpec(True))
    cfg.asr_provider = "faster"
    cfg.asr_model = "small"
    cfg.asr_compute = "int8"
    A._FASTER_SINGLETON.clear()
    a = A.get_asr()
    b = A.get_asr()
    assert isinstance(a, A.FasterWhisperASR)
    assert a is b and a.model_size == "small", "model instance was not reused"


def test_faster_transcribe_maps_segments_to_the_shared_contract(tmp_path, monkeypatch):
    """faster-whisper yields its own segment objects; the pipeline speaks Segment with
    float seconds. A unit (fake model) so it runs offline and in CI."""
    class Seg:
        def __init__(self, s, e, t): self.start, self.end, self.text = s, e, t

    class FakeModel:
        def transcribe(self, path, language=None, beam_size=5):
            return [Seg(0.0, 3.2, "  salom  "), Seg(3.2, 5.0, ""),
                    Seg(5.0, 8.0, "kv")], object()

    prov = A.FasterWhisperASR("base")
    prov._model = FakeModel()          # bypass the real weight load
    segs = prov.transcribe(tmp_path / "a.mp3", "uz")
    assert [s.text for s in segs] == ["salom", "kv"], segs   # blank dropped, stripped
    assert segs[0].start == 0.0 and segs[0].end == 3.2


def test_public_info_never_leaks_the_faster_model_or_package(client, monkeypatch):
    """`/api/v1/info` is anonymous. Which whisper model this host runs and whether the
    package is installed is operator topology, not a customer fact."""
    st = client.get("/api/v1/info").json()["providers"]["asr"]
    assert "operator" not in st
    assert "faster_model" not in st and "faster_package" not in st
