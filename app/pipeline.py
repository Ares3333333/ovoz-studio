"""Оркестратор обработки jobs: ASR → нормализация → перевод (глоссарий) → субтитры/даббинг."""
from __future__ import annotations

import json
import math
import subprocess
import time
import wave
from array import array
from pathlib import Path

from . import db
from .billing import refund_job
from .circuit import get_breaker
from .config import settings
from .ling import align as align_mod
from .ling import diarize as diar_mod
from .ling import layout as lay_mod
from .ling import srt as srt_mod
from .ling import word as word_mod
from .ling.glossary import mask_terms, unmask_terms
from .ling.romanizer import cyr2lat, normalize_uzbek
from .providers.asr import SimASR, get_asr, status as asr_status
from .providers.base import Segment
from .providers.translate import get_translate
from .providers.tts import get_tts

JOB_TYPES = {"transcribe", "subtitles", "dubbing", "document"}
# Потолок таймлайна: никогда не доверяем парсингу сегментов больше, чем оплачено.
# (атака: `0-60000|Salom` в .txt стоит 0.1 кредита, но alloc'ит 2.6GB в _mix_dubbing)
TIMELINE_SLACK_SEC = 60.0
# Жёсткий потолок сегментов: защита от 40k cue в 4MB .srt при 10 мин кредита
MAX_SEGMENTS_PER_JOB = 1500
# Стена часов на job: не даём воркеру висеть вечно
JOB_TIMEOUT_SEC = 600  # 10 min per job max
# --- диаризация: свои acoustic-признаки считаем на ограниченном окне ---
# Окно прослушивания задает движок, а не пайплайн: `ffmpeg -t` обрезает ленту ровно
# до того, что оба слушателя соглашаются прочитать, иначе «выровнено 15 минут из 20»
# превращалось бы в тихую разницу между оплаченным и сделанным.
# Минус несколько секунд — не щедрость, а цена точности: `ffmpeg` пишет целыми
# блоками, и декод ровно до границы окна может дать на кадр больше. Тогда job,
# купленный как раз на окно, ловил бы `too_long` и терял обе платные опции целиком
# — та же тихая потеря, против которой и делан этот релиз. Пять секунд из 900 дешевле,
# чем отказ на границе; тест проверяет, что запас остался внутри окна движка.
LISTEN_MARGIN_SEC = 5
DIAR_MAX_ANALYZE_SEC = int(align_mod.MAX_LISTEN_SEC) - LISTEN_MARGIN_SEC
DIAR_PCM_RATE = 8000         # mono 8 kHz s16le — достаточно для голоса/тембра
DIAR_STRIDE = 2              # берём каждый 2-й отсчёт (эффективные 4 kHz)
DIAR_MAX_SAMPLES = 4000      # отсчётов в ОДНОМ окне профиля (после декадации) ≈ 1 с
DIAR_VOICE_WINDOWS = 4       # таких окон усредняем по реплике: 1500 сегментов ≠ 10 мин CPU
# --- «Ovoz Jimlik»: на сколько разрешаем уезжать границе реплики к паузе ---
ALIGN_MAX_SHIFT_SEC = 0.60


def _avg_profile(profs: list) -> tuple | None:
    """Среднее по нескольким профилям одного сегмента. Пустой список — None."""
    live = [p for p in profs if p]
    if not live:
        return None
    dims = len(live[0])
    return tuple(round(sum(p[k] for p in live) / len(live), 4)
                 for k in range(dims))


def _clamp_segments(segments: list[Segment], billed_minutes: float) -> list[Segment]:
    cap = max(billed_minutes, 0.1) * 60.0 + TIMELINE_SLACK_SEC
    safe: list[Segment] = []
    for s in segments:
        if s.end <= 0 or s.end > cap or s.start < 0 or s.start >= s.end:
            continue  # мусорный тайминг — выбрасываем, не падаем
        safe.append(s)
    return safe


def _ensure_wav(path: Path) -> None:
    """TTS-адаптеры (edge-tts) пишут MP3 в файл с расширением .wav. Если внутри
    не RIFF — конвертируем через ffmpeg в настоящий PCM WAV, иначе wave.open упадёт."""
    with open(path, "rb") as f:
        if f.read(4) == b"RIFF":
            return
    tmp = path.with_suffix(".pcm.wav")
    subprocess.run(
        [settings.ffmpeg_bin, "-y", "-v", "error", "-i", str(path),
         "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le", str(tmp)],
        check=True, timeout=60,
    )
    tmp.replace(path)


def _art_dir(job_id: str) -> Path:
    p = settings.artifacts_dir / job_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pcm(path: Path) -> array | None:
    """Распаковка аудио в честный PCM через ffmpeg. Никаких внешних библиотек и
    никаких моделей: свои признаки считаем по отсчётам. None — если ffmpeg нет или
    файл не аудио: диаризация тогда деградирует до текста, но job не падает."""
    try:
        proc = subprocess.run(
            [settings.ffmpeg_bin, "-v", "error", "-i", str(path),
             "-t", str(DIAR_MAX_ANALYZE_SEC), "-ac", "1", "-ar", str(DIAR_PCM_RATE),
             "-f", "s16le", "-"],
            capture_output=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return array("h", proc.stdout)
    except (BufferError, ValueError):
        return None


def _voice_of(chunk) -> tuple[float, float, float] | None:
    """(громкость, частота нулевых пересечений, яркость) сегмента.

    Яркость — отношение энергии первой разности к энергии сигнала: дешёвый прокси
    спектра без DFT, различающий «грудной» и «тонкий» голоса."""
    n = len(chunk)
    if n < 32:
        return None
    energy = sum(x * x for x in chunk) / n
    if energy <= 0:
        return None
    loud = math.sqrt(energy) / 32768.0
    zcr = sum(1 for i in range(1, n)
              if (chunk[i - 1] < 0) != (chunk[i] < 0)) / (n - 1)
    diff = sum((chunk[i] - chunk[i - 1]) ** 2 for i in range(1, n)) / (n - 1)
    return (round(loud, 4), round(zcr, 4), round(math.sqrt(diff / energy), 4))


def _voices(pcm, segments: list[Segment]) -> list | None:
    """По одному голосовому профилю на сегмент, либо None (только текст)."""
    if pcm is None:
        return None
    out: list = []
    window = DIAR_MAX_SAMPLES * DIAR_STRIDE   # full-rate samples one profile covers
    for s in segments:
        # The window is selected in full-rate samples and only then decimated:
        # dividing the offset by the stride as well reads every profile from the
        # wrong moment in the tape (twice as early), which blends two speakers into
        # one acoustic fingerprint.
        a = max(0, int(s.start * DIAR_PCM_RATE))
        b = min(len(pcm), int(max(s.end, s.start) * DIAR_PCM_RATE))
        span = b - a
        if span <= window:
            out.append(_voice_of(pcm[a:b:DIAR_STRIDE]))
            continue
        # A real cue is 2-6 s and one profile costs 1 s: taking only its first
        # second described the onset (a breath, a stressed vowel) instead of the
        # voice. A few windows spread over the cue describe the whole utterance.
        n = min(DIAR_VOICE_WINDOWS, span // window)
        last = span - window
        out.append(_avg_profile([
            _voice_of(pcm[a + last * k // max(1, n - 1):a + last * k // max(1, n - 1) + window:DIAR_STRIDE])
            for k in range(n)]))
    return out


def _audio_pcm(source: Path) -> array | None:
    """Лента в PCM один раз на job: и диаризация, и выравнивание слушают одно и то
    же окно, и второй вызов ffmpeg за тот же файл был бы чистой растратой часов."""
    try:
        if not Path(source).exists():
            return None
    except OSError:
        return None
    return _pcm(Path(source))


def _listen_slice(pcm, billed_minutes: float) -> tuple[int, str, dict]:
    """Сколько ленты слушает движок и что об этом сказать.

    Возвращает (потолок, строка в событие, данные для клиента). Третье — не
    украшение: `message` читает человек в логах, а карточку задачи в трёх
    языках обязан собирать клиент, и собирать по числам. Английскую фразу
    локализовать нельзя, поэтому и у обрезки есть структура: `{heard_sec,
    paid_sec, window_sec}` или `{}`, когда обрезки не было.

    Потолок — оплаченный таймлайн плюс запас, а не терпение клиента: тогда
    «граница не уехала за конец аудио» означает «не уехала за то, за что
    заплатили». О событии говорит оплаченное время без запаса: запас — наш
    внутренний буфер, а не обещание клиенту. Разрыв между оплаченным и
    прослушанным возможен только когда источник длиннее окна движка — и именно
    тогда событие обязано это назвать, а не ставить галочку и молчать: раньше
    20-минутный ролик получал выравнивание первых 3,5 минут и ни строчки об этом."""
    paid = max(billed_minutes, 0.1) * 60.0
    want = paid + TIMELINE_SLACK_SEC
    ceiling = min(len(pcm), int(want * DIAR_PCM_RATE))
    heard = ceiling / DIAR_PCM_RATE
    tape = len(pcm) / DIAR_PCM_RATE
    window = align_mod.MAX_LISTEN_SEC
    # Источник обрезан декодером (а не клиентом): вот единственный случай, когда
    # оплаченное время не было услышано. Сравниваем с окном декодера, а не движка:
    # между ними запас в несколько секунд, и без него событие молчало бы ровно на
    # тех job'ах, для которых оно и написано.
    if tape >= DIAR_MAX_ANALYZE_SEC - 0.5 and heard < paid - 0.5:
        info = {"heard_sec": round(heard), "paid_sec": round(paid),
                "window_sec": round(window)}
        note = (f" (heard {heard:.0f}s of {paid:.0f}s; the listener's window is "
                f"{window:.0f}s)")
    else:
        info, note = {}, ""
    return ceiling, note, info


def _align_step(jid: str, pcm, segments: list[Segment], billed_minutes: float) -> list[Segment]:
    """Шаг «Ovoz Jimlik»: границы реплик переставляются в настоящие паузы ленты.

    Лента режется по оплаченному таймлайну ПЕРЕД движком: тогда «граница не может
    уехать за конец аудио» автоматически означает «не может уехать за то, за что
    заплатили», и отдельного потолка после выравнивания не нужно. Отчёт живёт в
    артефакте align.json; здесь возвращаются только исправленные сегменты — при
    отказе движка исходные, потому что неверное выравнивание хуже отсутствующего."""
    if pcm is None:
        db.add_job_event(jid, "align", "skipped: no audio track (text-only source)",
                         {"code": "skipped", "reason": "no_audio"})
        return segments
    ceiling, note, trunc = _listen_slice(pcm, billed_minutes)
    cues = [srt_mod.Cue(i + 1, s.start, s.end, s.text)
            for i, s in enumerate(segments)]
    try:
        report = align_mod.align(pcm[:ceiling], DIAR_PCM_RATE, cues,
                                 max_shift=ALIGN_MAX_SHIFT_SEC)
    except align_mod.AlignError as exc:
        # Отказ документируется кодом, а не молчанием: пользователь заказал услугу
        # и должен видеть, что её не было, и почему.
        db.add_job_event(jid, "align", f"skipped: {exc.code}",
                         {"code": "skipped", "reason": exc.code})
        return segments
    s = report["summary"]
    data = {"code": "aligned", "moved": s["moved"], "boundaries": s["boundaries"],
            "in_silence": s["in_silence"], "max_applied": s["max_applied"],
            "gaps": report["audio"]["gaps"], "duration": report["audio"]["duration"]}
    if trunc:
        data["truncation"] = trunc
    db.add_job_event(
        jid, "align",
        f"{s['moved']}/{s['boundaries']} cuts into silence, "
        f"max {s['max_applied']:g}s, {report['audio']['gaps']} pauses{note}",
        data)
    art = _art_dir(jid)
    (art / "align.json").write_text(json.dumps(report, ensure_ascii=False),
                                    encoding="utf-8")
    db.add_artifact(jid, "align", str(art / "align.json"))
    return [Segment(c["start"], c["end"], c["text"]) for c in report["cues"]]


def _words_step(jid: str, pcm, cues: list, billed_minutes: float) -> None:
    """Шаг «Ovoz So'z»: у каждой карточки появляется время каждого слова.

    Работает по финальным карточкам — тем, что уйдут в .srt/.ass: клиент платит за
    субтитры, и подсветка обязана включать то слово, которое он видит на экране,
    а не его черновик до вёрстки. Отказ движок пишет событием, а не молчанием:
    оплаченный флажок не должен исчезать без объяснения."""
    if pcm is None:
        db.add_job_event(jid, "words",
                         "skipped: no audio track (text-only source)",
                         {"code": "skipped", "reason": "no_audio"})
        return
    ceiling, note, trunc = _listen_slice(pcm, billed_minutes)
    try:
        report = word_mod.words(pcm[:ceiling], DIAR_PCM_RATE, cues)
    except align_mod.AlignError as exc:
        # Родительский класс, а не WordError: word.py поднимает и базовый AlignError
        # там, где он одалживает кадровой анализ у align. Поймать только своего —
        # значит уронить job с возвратом кредита вместо `words: skipped: …`.
        db.add_job_event(jid, "words", f"skipped: {exc.code}",
                         {"code": "skipped", "reason": exc.code})
        return
    s = report["summary"]
    data = {"code": "timed", "words": s["words"], "cues": s["cues"],
            "cues_measured": s["cues_measured"],
            "valley_share": s["valley_share"], "longest_word_sec": s["longest_word_sec"]}
    if trunc:
        data["truncation"] = trunc
    db.add_job_event(
        jid, "words",
        f"{s['words']} words on {s['cues_measured']}/{s['cues']} cues, "
        f"{int(round(s['valley_share'] * 100))}% cuts on a real dip{note}",
        data)
    art = _art_dir(jid)
    rep_path = art / "words.json"
    rep_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    db.add_artifact(jid, "words", str(rep_path))
    # Караоке-ASS отдаётся отдельным артефактом, а не вместо subs_*.ass: обычная
    # дорожка должна оставаться обычной дорожкой, иначе \\kf-теги попадут в
    # плееры, которые их не понимают.
    kf_path = art / "subs_karaoke.ass"
    kf_path.write_text(word_mod.to_ass(report), encoding="utf-8")
    db.add_artifact(jid, "ass_karaoke", str(kf_path))


def run_job(job_id: str) -> None:
    """Точка входа воркера. Переходы — только CAS'ом, чтобы не было гонок
    с cancel/retry/restart-recovery."""
    job = db.get_job(job_id)
    if not job:
        return
    if not db.set_status_if(job_id, "running", ("queued", "running")):
        return  # статус уже увёл в другую сторону (например, canceled)
    job = db.get_job(job_id)
    db.add_job_event(job_id, "start", f"type={job['type']} {job['src']}->{job['tgt']}")
    deadline = time.monotonic() + JOB_TIMEOUT_SEC
    try:
        _execute(job, deadline)
        if not db.set_status_if(job_id, "done", ("running",)):
            db.add_job_event(job_id, "orphan", "results kept, job moved on")
            return
        db.add_job_event(job_id, "done", "job completed")
    except (TimeoutError, subprocess.TimeoutExpired):
        # subprocess.TimeoutExpired is NOT a TimeoutError, and it is how a hung
        # ffmpeg/whisper call dies. Normalizing both into the wall-clock sentence
        # keeps job.error localized instead of leaking a Python repr to the UI.
        # The terminal state is committed BEFORE the announcement: a client that
        # reacts to the live frame by refetching must never read a stale "running"
        if db.set_status_if(job_id, "failed", ("running",),
                            error=f"job exceeded {JOB_TIMEOUT_SEC}s"):
            db.add_job_event(job_id, "failed", "job exceeded time limit")
            refund_job(job, reason="refund:timeout")
    except Exception as exc:  # noqa: BLE001 — воркер обязан фиксировать любую ошибку
        if db.set_status_if(job_id, "failed", ("running",), error=str(exc)[:1000]):
            db.add_job_event(job_id, "failed", str(exc)[:500])
            refund_job(job, reason="refund:failed")


def _execute(job: dict, deadline: float = 0) -> None:
    """deadline — monotonic time после которого прерываем."""
    def _check_deadline():
        if deadline and time.monotonic() > deadline:
            raise TimeoutError(f"job exceeded {JOB_TIMEOUT_SEC}s")
    jid = job["id"]
    src, tgt, jtype = job["src"], job["tgt"], job["type"]
    source = Path(job["source_path"])
    asr, tr = get_asr(), get_translate()
    terms = {t["src_term"]: t["tgt_term"] for t in db.glossary_for(job["user_id"])}

    # 1) транскрипция (для document-типа «транскриптом» служит сам текст)
    _check_deadline()  # before ASR which can be long-running
    asr_cb = get_breaker(f"asr:{asr.name}")
    if not asr_cb.allow():
        raise RuntimeError(f"ASR provider '{asr.name}' circuit is OPEN, try later")
    try:
        segments: list[Segment] = asr.transcribe(source, src)
        asr_cb.record_success()
    except Exception:
        asr_cb.record_failure()
        raise
    _check_deadline()  # after ASR subprocess returns
    # защита от тайминг-инъекций: сегменты дальше оплаченного таймлайна вырезаются
    segments = _clamp_segments(segments, float(job.get("minutes") or 0))
    # cap: не больше MAX_SEGMENTS_PER_JOB (защита от DoS через большой .srt)
    if len(segments) > MAX_SEGMENTS_PER_JOB:
        segments = segments[:MAX_SEGMENTS_PER_JOB]
    # Клиенту должно быть больше, чем имя провайдера. Если включён реальный ASR, а
    # машина это доказать не может (нет бинарника или модели), текст в этой задаче
    # придуман. Приставка `[демо]` внутри субтитра — не раскрытие, а строка в файле:
    # её не видно ни в списке задач, ни в API. Поэтому факт кладётся и в событие,
    # и в meta задачи (которую читают и карточка, и эндпоинт) — и говорит ещё и
    # почему. Распознать собственный транскрипт клиента — не «демо»: там текст настоящий.
    is_sim = isinstance(asr, SimASR)
    demo_text = is_sim and asr.invents_text(source)
    st = asr_status()
    mode = "sim" if is_sim else "real"
    detail = f"{asr.name}: {len(segments)} segments"
    meta_patch = {"asr_mode": mode, "asr_demo": demo_text}
    if demo_text:
        detail += f" — demo transcript: {st['reason']}"
        meta_patch["asr_reason"] = st["reason"]
    db.add_job_event(jid, "asr", detail,
                     {"code": "demo_transcript" if demo_text else "transcribed",
                      "segments": len(segments), "mode": mode})
    db.merge_job_meta(jid, meta_patch)

    # 1.5) нормализация узбекского: кириллица → латиница, единый апостроф
    if src == "uz":
        segments = [Segment(s.start, s.end, normalize_uzbek(cyr2lat(s.text))) for s in segments]

    # 1.6) выравнивание «Ovoz Jimlik»: режем границы по настоящим паузам ленты, а
    # не по тем меткам, которые выдал ASR. Дешевле сделать это до диаризации и
    # перевода: оба они работают по таймингам сегментов и получают исправленную карту.
    meta = job.get("meta") or {}
    pcm = None
    if meta.get("align") or meta.get("diarize") or meta.get("words"):
        _check_deadline()
        pcm = _audio_pcm(source)
    if meta.get("align"):
        _check_deadline()
        segments = _align_step(jid, pcm, segments, float(job.get("minutes") or 0))

    # 1.7) диаризация «Ovoz Turn»: кто держит пол. Свой движок, без моделей;
    # при отсутствующем ffmpeg/не-аудио молча работаем только по тексту.
    turns = None
    if meta.get("diarize"):
        _check_deadline()
        voices = _voices(pcm, segments)
        turns = diar_mod.analyze_turns(segments, voices=voices)
        read = diar_mod.summary(turns)
        db.add_job_event(jid, "diarize",
                         f"{read['speakers']} speakers / {read['turns']} turns")
        art0 = _art_dir(jid)
        (art0 / "diarization.json").write_text(
            json.dumps({**read,
                         "lines": [{"start": t.start, "end": t.end, "speaker": t.speaker,
                                    "text": t.text, "cues": list(t.cues)} for t in turns]},
                        ensure_ascii=False), encoding="utf-8")
        db.add_artifact(jid, "diarization", str(art0 / "diarization.json"))

    # текстовые артефакты всегда доступны
    txt_path = _art_dir(jid) / "transcript.txt"
    txt_path.write_text("\n".join(
        diar_mod.plain(turns).split("\n") if turns else [s.text for s in segments]),
        encoding="utf-8")
    db.add_artifact(jid, "transcript", str(txt_path))

    if jtype == "transcribe":
        if meta.get("words"):
            # Пословный тайминг живёт внутри карточек; для «просто транскрипта»
            # карточек нет — отказываем честно и до того, как пользователь решит,
            # что платная опция молча не сработала.
            db.add_job_event(jid, "words",
                             "skipped: word timing needs a subtitle job",
                             {"code": "skipped", "reason": "not_subtitles"})
        return

    # 2) перевод сегментов с защитой глоссария
    db.add_job_event(jid, "translate", f"{tr.name}: {len(segments)} segments ({src}->{tgt})")
    _check_deadline()
    tr_cb = get_breaker(f"translate:{tr.name}")
    if not tr_cb.allow():
        raise RuntimeError(f"Translate provider '{tr.name}' circuit is OPEN, try later")
    translated: list[Segment] = []
    for idx, s in enumerate(segments):
        if idx % 50 == 0:
            _check_deadline()
        masked, mapping = mask_terms(s.text, terms)
        try:
            out = tr.translate(masked, src, tgt)
        except Exception:
            tr_cb.record_failure()
            raise
        translated.append(Segment(s.start, s.end, unmask_terms(out, mapping)))
    tr_cb.record_success()

    # 3) субтитры: целевой + двуязычный вариант + ASS для burn-in
    def _tag(i: int, text: str) -> str:
        # Реплика получает автора только когда диаризация заказана и посчитана.
        if turns and i < len(turns):
            return f"[S{turns[i].speaker}] {text}"
        return text

    cues = [srt_mod.Cue(i + 1, s.start, s.end, _tag(i, s.text))
            for i, s in enumerate(translated)]
    src_cues = [srt_mod.Cue(i + 1, s.start, s.end, _tag(i, s.text))
                for i, s in enumerate(segments)]
    bilingual = [srt_mod.Cue(c.index, c.start, c.end, f"{o.text}\n{c.text}")
                 for c, o in zip(cues, src_cues)]

    # 3.5) вёрстка «Ovoz Qator»: читабельность по законам вещания. Свой движок,
    # модели не нужны. Двуязычный вариант остаётся на таймлайне ASR: там заведомо
    # две строки на реплику, и никакая вёрстка не сделает его читаемым — трогать
    # его значило бы соврать в отчёте.
    layout_report = None
    if meta.get("polish"):
        _check_deadline()
        # Авторство карточек передаём движку вёрстки отдельно: префикс [S1] — это
        # текст, а speaker — замер. Без него «Ovoz Qator» не видит, что две карточки
        # одного окна перекладывают реплику между голосами.
        owners = ([turns[i].speaker if i < len(turns) else 0
                   for i in range(len(cues))] if turns else None)
        layout_report = lay_mod.polish(cues, speakers=owners)
        cues = [srt_mod.Cue(c["i"], c["start"], c["end"], c["text"])
                for c in layout_report["cards"]]
        db.add_job_event(
            jid, "polish",
            f"readability {layout_report['before']['score']:g} -> "
            f"{layout_report['after']['score']:g} "
            f"({layout_report['after']['grade']}), "
            f"{len(layout_report['after']['findings'])} findings, "
            f"drift {layout_report['max_drift']:g}s")
    art = _art_dir(jid)
    srt_path = art / f"subs_{tgt}.srt"
    srt_path.write_text(srt_mod.format_srt(cues), encoding="utf-8")
    db.add_artifact(jid, "srt", str(srt_path))
    bi_path = art / "subs_bilingual.srt"
    bi_path.write_text(srt_mod.format_srt(bilingual), encoding="utf-8")
    db.add_artifact(jid, "srt_bilingual", str(bi_path))
    ass_path = art / f"subs_{tgt}.ass"
    ass_path.write_text(srt_mod.to_ass(cues), encoding="utf-8")
    db.add_artifact(jid, "ass", str(ass_path))
    if layout_report is not None:
        # Отчёт — часть результата: клиент платит за то, что субтитры читаемы, и
        # должен видеть, какие правила остались нарушенными и почему.
        lay_path = art / "layout.json"
        lay_path.write_text(json.dumps(layout_report, ensure_ascii=False),
                            encoding="utf-8")
        db.add_artifact(jid, "layout", str(lay_path))

    # 3.6) «Ovoz So'z»: пословные тайминги по финальным карточкам. После вёрстки —
    # потому что вёрстка меняет границы окон, а таймить слова по черновику значило
    # бы подсвечивать не то, что показано на экране.
    if meta.get("words"):
        _check_deadline()
        _words_step(jid, pcm, cues, float(job.get("minutes") or 0))

    if jtype == "document":
        # для документов текстовый перевод важнее таймингов
        doc_path = art / f"document_{tgt}.txt"
        doc_path.write_text("\n\n".join(s.text for s in translated), encoding="utf-8")
        db.add_artifact(jid, "document", str(doc_path))
        return

    # 4) даббинг: озвучка каждого сегмента + склейка по таймингам (16-бит mono)
    if jtype == "dubbing":
        _check_deadline()
        tts = get_tts()
        db.add_job_event(jid, "tts", f"{tts.name}: {len(translated)} lines")
        _mix_dubbing(jid, translated, tts, tgt, art)


def _mix_dubbing(jid: str, translated: list[Segment], tts, tgt: str, art: Path) -> None:
    total = max((s.end for s in translated), default=1.0)
    rate = 22050
    samples = bytearray(int(total * rate) * 2)  # 16-bit mono

    for s in translated:
        tmp = art / f"line_{int(s.start * 1000)}.wav"
        tts.synthesize(s.text, tgt, tmp, dur_sec=s.end - s.start)
        _ensure_wav(tmp)  # edge-tts отдаёт MP3 в .wav — приведём к честному PCM
        with wave.open(str(tmp), "rb") as w:
            data = w.readframes(w.getnframes())
        # простая нормализация: подмешиваем с позиции start (по верхнему пределу)
        offset = int(s.start * rate) * 2
        for i in range(0, len(data) - 1, 2):
            pos = offset + i
            if pos + 1 >= len(samples):
                break
            cur = int.from_bytes(samples[pos:pos + 2], "little", signed=True)
            add = int.from_bytes(data[i:i + 2], "little", signed=True)
            mixed = max(-32768, min(32767, cur + add))
            samples[pos:pos + 2] = mixed.to_bytes(2, "little", signed=True)
        tmp.unlink(missing_ok=True)

    out = art / "dubbing.wav"
    with wave.open(str(out), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(samples))
    db.add_artifact(jid, "dubbing", str(out))
