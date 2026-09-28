"""Оркестратор обработки jobs: ASR → нормализация → перевод (глоссарий) → субтитры/даббинг."""
from __future__ import annotations

import json
import math
import subprocess
import time
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
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
# --- диаризация и слушатели: свои acoustic-признаки на всём оплаченном материале ---
# Раньше и диаризация, и оба слушателя читали первые 895 с и молчали об этом: для
# 20-минутной ленты это тихая разница между «оплачено» и «сделано». Теперь по ленте
# идёт ОДИН проход, памяти в нём — один блок, а потолок определяет оплата, а не
# движок. Час 8 кГц моно — 180 000 чисел огибающей (~6 МБ) вместо 58 МБ отсчётов:
# второй раз ленту декодировать незачем и неоткуда брать.
MAX_JOB_TAPE_SEC = 3600.0      # потолок слушания одного job'а
LISTEN_BLOCK_SEC = 60.0        # сколько ленты держим в руках одновременно
LISTEN_READ_TIMEOUT_SEC = 60.0 # блок из ffmpeg приходит за миллисекунды
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
    """Гарантирует PCM 22050 Гц mono 16-bit, чтобы каждый байт микшера был реальной
    секундой звука, а не единицей другой частоты. Sample rate — это масштаб
    времени, а не качество.

    Два режима сбоя, которые показывает живой голос и никогда не покажет заглушка:
    MP3 с расширением .wav (не RIFF вовсе) и честный RIFF WAV, но с частотой 24 кГц
    (у нейросетевых дикторов это норма). Прежняя версия лечила только первое, поэтому
    реальная дорожка на 24 кГц попадала на сетку 22050 — даббинг играл на ~7% быстрее,
    и каждая следующая реплика съезжала из своей паузы. Если заголовок уже ровно
    22050/mono/16 — не трогаем; иначе нормализуем через ffmpeg.
    """
    with open(path, "rb") as f:
        is_riff = f.read(4) == b"RIFF"
    if is_riff:
        try:
            with wave.open(str(path), "rb") as w:
                if (w.getnchannels() == 1 and w.getsampwidth() == 2
                        and w.getframerate() == 22050):
                    return
        except wave.Error:
            pass  # RIFF, но не разборный PCM (float/экзотика) — нормализуем ниже
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
    файл не аудио: диаризация тогда деградирует до текста, но job не падает.

    Only for tests and tiny fixtures: a real job reads the tape through
    `_tape_pass`, which never holds the whole recording in memory."""
    try:
        proc = subprocess.run(
            [settings.ffmpeg_bin, "-v", "error", "-i", str(path),
             "-ac", "1", "-ar", str(DIAR_PCM_RATE), "-f", "s16le", "-"],
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


def _profile_spans(segments: list[Segment]) -> list[tuple[int, int, int, int]]:
    """Окна ленты, из которых собирается профиль голоса: (начало, конец, реплика, окно).

    Та же математика, что была внутри `_voices`, только без длины ленты: поток ещё
    не знает, где он кончится. Реплика, уезжающая за конец записи, не получит
    профиля и будет посчитана — раньше она получала огрызок по другую сторону
    `min(len(pcm), …)` и молчала, а это тот же тихий дефект, против которого
    написан весь этот модуль.
    """
    window = DIAR_MAX_SAMPLES * DIAR_STRIDE   # full-rate samples one profile covers
    spans: list[tuple[int, int, int, int]] = []
    for i, s in enumerate(segments):
        # The window is selected in full-rate samples and only then decimated:
        # dividing the offset by the stride as well reads every profile from the
        # wrong moment in the tape (twice as early), which blends two speakers into
        # one acoustic fingerprint.
        a = max(0, int(s.start * DIAR_PCM_RATE))
        span = int(max(s.end, s.start) * DIAR_PCM_RATE) - a
        if span <= 0:
            continue
        if span <= window:
            spans.append((a, a + span, i, 0))
            continue
        # A real cue is 2-6 s and one profile costs 1 s: taking only its first
        # second described the onset (a breath, a stressed vowel) instead of the
        # voice. A few windows spread over the cue describe the whole utterance.
        n = min(DIAR_VOICE_WINDOWS, span // window)
        last = span - window
        for k in range(n):
            off = a + last * k // max(1, n - 1)
            spans.append((off, off + window, i, k))
    spans.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    return spans


class _SpanBook:
    """Собирает из потока только нужные ленте окна — и тут же о них забывает.

    Час ленты — это сотни мегабайт отсчётов, а профили голоса — полторы тысячи
    троек. Поэтому каждое окно дописывается, пока поток через него идёт, а как
    только оно закрыто, от него остаются три числа и буфер освобождается. В работе
    живёт не больше окон, чем их влезает в один блок: память прохода ограничена
    блоком, а не длиной ленты.
    """

    def __init__(self, spans: list[tuple[int, int, int, int]], count: int) -> None:
        self.spans = spans
        self.count = count
        self.cursor = 0
        self.open: list[list] = []          # [start, end, cue, part, bytearray]
        self.parts: dict[int, dict[int, tuple]] = {}
        self.lost: set[int] = set()         # реплики, чьё окно уехало за ленту
        self.expected: dict[int, int] = {}
        for _, _, cue, _ in spans:
            self.expected[cue] = self.expected.get(cue, 0) + 1

    def consume(self, base: int, data: bytes) -> None:
        """Один блок ленты: `base` — абсолютный номер первого отсчёта блока."""
        end = base + len(data) // 2
        while self.cursor < len(self.spans) and self.spans[self.cursor][0] < end:
            a, b, cue, part = self.spans[self.cursor]
            self.open.append([a, b, cue, part, bytearray()])
            self.cursor += 1
        still: list[list] = []
        for a, b, cue, part, buf in self.open:
            lo, hi = max(a, base), min(b, end)
            if hi > lo:
                buf += data[(lo - base) * 2:(hi - base) * 2]
            if b <= end:
                self._close(cue, part, buf)
            else:
                still.append([a, b, cue, part, buf])
        self.open = still

    def _close(self, cue: int, part: int, buf: bytearray) -> None:
        pcm = array("h")
        pcm.frombytes(bytes(buf))
        self.parts.setdefault(cue, {})[part] = _voice_of(pcm[::DIAR_STRIDE])

    def finish(self) -> tuple[list, int]:
        """Профили по репликам в исходном порядке + сколько реплик без голоса.

        Реплика теряет профиль целиком, если хоть одно её окно не влезло в ленту:
        усреднять половину окна с отсутствующей половиной значит показать слушателя,
        которого не было. Три способа не получить профиля — и все три обязаны быть
        посчитаны, потому что «off_tape: 0» рядом с половиной карточек без голоса —
        это ровно тот тихий дефект, против которого написан весь этот раунд:
          * окно открылось и не закрылось (поток кончился внутри);
          * окно не открылось вовсе (реплика целиком за прослушанным куском);
          * окна закрылись, но ни одно не дало профиля (окно короче 32 отсчётов)."""
        for _a, _b, cue, _part, _buf in self.open:
            self.lost.add(cue)          # поток кончился раньше, чем закрылось окно
        self.open = []
        for _a, _b, cue, _part in self.spans[self.cursor:]:
            self.lost.add(cue)          # окно даже не открылось: реплики нет в ленте
        voices: list = [None] * self.count
        for cue, got in self.parts.items():
            if cue in self.lost or len(got) != self.expected[cue]:
                self.lost.add(cue)      # окно не открылось вовсе
                continue
            live = _avg_profile([got[k] for k in sorted(got)])
            if live is None:
                self.lost.add(cue)      # замеры пустые: голоса на этом окне нет
                continue
            voices[cue] = live
        return voices, len(self.lost)


def _heard_budget(billed_minutes: float) -> tuple[float, float]:
    """Сколько ленты слушаем и что обязаны назвать оплаченным.

    Потолок — оплаченный таймлайн плюс запас, а не терпение клиента: тогда
    «граница не уехала за конец аудио» означает «не уехала за то, за что
    заплатили». Сверху стоит потолок job'а — не окошечко движка: час ленты
    слушается ровно, без декадации, а второго часа в оплаченном таймлайне просто
    нет. О событии говорит оплаченное время без запаса: запас — наш внутренний
    буфер, а не обещание клиенту.

    Честно о самом потолке: сегодня он выше потолка загрузки (30 минут таймлайна
    и 4 МБ тела), поэтому ни одна оплаченная задача не обрезается вовсе —
    `MAX_JOB_TAPE_SEC` это запас под следующий лимит хранилища, а не обещание часа.
    Ветка «heard … of …» остаётся нужной для контейнера, который наврал о своей
    длительности: там оплаченное время кончается раньше ленты, и смолчать — значит
    вернуть клиенту частичную работу как полную.
    """
    paid = max(billed_minutes, 0.1) * 60.0
    return min(paid + TIMELINE_SLACK_SEC, MAX_JOB_TAPE_SEC), paid


def _heard_report(heard: float, want: float, paid: float) -> tuple[dict, str]:
    """О чём job обязан сказать, когда слушание кончилось раньше оплаты.

    Разделение на «до» и «после» потока не косметика: решение принимает эта
    функция, и тесты судят её, а не удавку ffmpeg. `{heard_sec, paid_sec,
    window_sec}` — не украшение: `message` читает человек в логах, а карточку
    задачи в трёх языках собирает клиент, и собирать её надо по числам.
    """
    if heard >= want - 0.5 and heard < paid - 0.5:
        return ({"heard_sec": round(heard), "paid_sec": round(paid),
                 "window_sec": round(want)},
                f" (heard {heard:.0f}s of {paid:.0f}s; one job listens to "
                f"{want:.0f}s)")
    return {}, ""


def _drain(read_block, billed_minutes: float, deadline: float = 0.0,
           book: "_SpanBook | None" = None, need_curve: bool = True) -> dict | None:
    """Один проход по ленте: `read_block(nbytes)` отдаёт следующий кусок PCM.

    Петля живёт здесь, а не внутри ffmpeg-обвязки, по той же причине, по какой
    огибающая живёт в движке: тесты должны судить НАСТОЯЩИЙ порядок reads —
    потолок, блоки, дедлайн — а не его копию, нарисованную в fixture.
    `_tape_pass` приносит только пайп, `_drain` — все решения.

    Три границы, и все три обязаны сойтись руками, а не удачей:
      * читается ровно `min(оплачено + запас, потолок)`, запрос урезан по остатку
        budget'а: без этого последний read слушает на целый блок больше
        обещанного, и `heard_sec` расходится с `reach_sec` на 59 с (проверено на
        потолке, который не кратен блоку);
      * блок кончается на границе отсчёта: огибающая уносит неполный кадр сама, а
        абсолютные смещения окон профиля считаются в отсчётах, и кривой байт
        сдвинул бы каждое окно на полотсчёта — тот самый дефект, из-за которого
        два голоса сливаются в один;
      * `None` — только когда труба не дала ничего. Лента в триста отсчётов — не
        «отсутствующая аудиодорожка»: это `too_short`, и отказывать обязан
        движок, а не транспорт, иначе клиент читает про несуществующий WAV.
    """
    want, paid = _heard_budget(billed_minutes)
    block = max(1, int(LISTEN_BLOCK_SEC * DIAR_PCM_RATE))
    stop_at = int(want * DIAR_PCM_RATE)
    env = align_mod.Envelope(DIAR_PCM_RATE, LISTEN_BLOCK_SEC) if need_curve else None
    read = 0
    tail = b""
    while read < stop_at:
        if deadline and time.monotonic() > deadline:
            raise TimeoutError(f"job exceeded {JOB_TIMEOUT_SEC}s")
        chunk = read_block(min(block, stop_at - read) * 2)
        if not chunk:
            break            # лента кончилась; недочитанный полубайт — не отсчёт
        data, tail = tail + chunk, b""
        if len(data) % 2:
            data, tail = data[:-1], data[-1:]
        if not data:
            continue
        if env is not None:
            env.feed(data)
        if book is not None:
            book.consume(read, data)
        read += len(data) // 2
    if read == 0:
        return None
    heard = read / DIAR_PCM_RATE
    trunc, note = _heard_report(heard, want, paid)
    out = {"heard_sec": round(heard, 3), "paid_sec": round(paid, 3),
           "reach_sec": round(want, 3), "truncation": trunc, "note": note}
    if env is not None:
        # Коротче одного измеримого кадра огибающей не бывает: шагам нужен не
        # валящийся движок, а код отказа — их читает карточка задачи.
        if read < align_mod.MIN_SAMPLES:
            out["refusal"] = "too_short"
        else:
            out["curve"] = env.curve()
        out["blocks"] = env.blocks
        out["block_sec"] = env.block_sec
        out["peak_samples"] = env.peak_samples
    if book is not None:
        voices, lost = book.finish()
        out["voices"] = voices
        out["off_tape"] = lost
    return out


def _tape_pass(source: Path, billed_minutes: float, deadline: float = 0.0,
               book: "_SpanBook | None" = None,
               need_curve: bool = True) -> dict | None:
    """Проход ffmpeg по ленте: `need_curve` — огибающая всей записи, `book` — окна
    профилей голоса, и то и другое блоками, никогда не удерживая запись целиком.

    Профили собираются ОТДЕЛЬНЫМ проходом (`_profiles_pass`), а не в этом: окна
    профиля задаёт таймлайн, а Jimlik этот таймлайн передвигает. Снимать отпечаток
    голоса по окну, которое выравниватель уже отменил, — значит спорить о двух
    голосах там, где лента показывала один. Второй проход стоит перекодировки
    того же материала: 16-минутная лента — ~1 с (job целиком: 2 с).

    Здесь только пайп: `None`, если ffmpeg не запускается (не-аудио, битый файл) —
    оба движка и диаризация тогда честно отказываются, а job живёт.

    Порядок уборки — убить, потом присоединить, потом закрыть. `close()` на пайпе,
    из которого рабочий поток всё ещё читает, ждёт лок `BufferedReader` и упирается
    в конец ffmpeg: тогда обещанный таймаут чтения не возвращает управление вообще
    (замерено: 98,8 с против 0,5 с), job висит в `running` без возврата кредита, а
    `atexit`-join пула вешает останов сервера. Таймаут потому и поднимает
    `TimeoutError`, а не тихий `None`: повисший ffmpeg не имеет права выглядеть
    отсутствующим."""
    want, _paid = _heard_budget(billed_minutes)
    cmd = [settings.ffmpeg_bin, "-v", "error", "-i", str(source),
           "-t", f"{want:.3f}", "-ac", "1", "-ar", str(DIAR_PCM_RATE),
           "-f", "s16le", "pipe:1"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None
    pool: ThreadPoolExecutor | None = None

    def read_block(nbytes: int) -> bytes:
        try:
            return pool.submit(proc.stdout.read, nbytes).result(
                timeout=LISTEN_READ_TIMEOUT_SEC)
        except FutureTimeout:
            raise TimeoutError(
                f"ffmpeg produced no tape for {LISTEN_READ_TIMEOUT_SEC:g}s")

    try:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ovoz-tape")
        return _drain(read_block, billed_minutes, deadline, book=book,
                      need_curve=need_curve)
    finally:
        if proc.poll() is None:
            proc.kill()                  # снимает блокировку read() раньше всего
        if pool is not None:
            try:
                pool.shutdown(wait=True, cancel_futures=True)
            except TypeError:            # старые Python: нет cancel_futures
                pool.shutdown(wait=True)
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _profiles_pass(source: Path, segments: list[Segment], billed_minutes: float,
                   deadline: float = 0.0) -> dict | None:
    """Второй, ограниченный проход — профили голоса по ФИНАЛЬНОМУ таймлайну."""
    return _tape_pass(source, billed_minutes, deadline,
                      book=_SpanBook(_profile_spans(segments), len(segments)),
                      need_curve=False)


def _align_step(jid: str, tape: dict | None, segments: list[Segment]) -> list[Segment]:
    """Шаг «Ovoz Jimlik»: границы реплик переставляются в настоящие паузы ленты.

    Движок получает уже измеренную огибающую всей ленты: лента порезана по
    оплаченному таймлайну ПЕРЕД движком (в `_tape_pass`), тогда «граница не может
    уехать за конец аудио» автоматически означает «не может уехать за то, за что
    заплатили», и отдельного потолка после выравнивания не нужно. Отчёт живёт в
    артефакте align.json; здесь возвращаются только исправленные сегменты — при
    отказе движка исходные, потому что неверное выравнивание хуже отсутствующего."""
    if tape is None:
        db.add_job_event(jid, "align", "skipped: no audio track (text-only source)",
                         {"code": "skipped", "reason": "no_audio"})
        return segments
    if "curve" not in tape:
        # Труба дала материал короче одного измеримого кадра. Это не «аудио нет»:
        # файл клиент прислал настоящий, и назвать отсутствие огибающей
        # отсутствием дорожки — значит отправить клиента искать WAV, которого он
        # уже загрузил.
        why = tape.get("refusal") or "no_audio"
        db.add_job_event(jid, "align", f"skipped: {why}",
                         {"code": "skipped", "reason": why})
        return segments
    note, trunc = tape["note"], tape["truncation"]
    cues = [srt_mod.Cue(i + 1, s.start, s.end, s.text)
            for i, s in enumerate(segments)]
    try:
        report = align_mod.align_curve(cues, tape["curve"],
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


def _words_step(jid: str, tape: dict | None, cues: list) -> None:
    """Шаг «Ovoz So'z»: у каждой карточки появляется время каждого слова.

    Работает по финальным карточкам — тем, что уйдут в .srt/.ass: клиент платит за
    субтитры, и подсветка обязана включать то слово, которое он видит на экране,
    а не его черновик до вёрстки. Огибающая — та же, что у Jimlik: два разных
    уха на одной ленте означали бы два несовместимых рассказа об одном файле.
    Отказ движок пишет событием, а не молчанием: оплаченный флажок не должен
    исчезать без объяснения."""
    if tape is None:
        db.add_job_event(jid, "words",
                         "skipped: no audio track (text-only source)",
                         {"code": "skipped", "reason": "no_audio"})
        return
    if "curve" not in tape:
        why = tape.get("refusal") or "no_audio"
        db.add_job_event(jid, "words", f"skipped: {why}",
                         {"code": "skipped", "reason": why})
        return
    note, trunc = tape["note"], tape["truncation"]
    try:
        report = word_mod.words_curve(cues, tape["curve"])
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
    billed = float(job.get("minutes") or 0)
    tape = None
    if meta.get("align") or meta.get("diarize") or meta.get("words"):
        _check_deadline()
        tape = _tape_pass(source, billed, deadline)
    if meta.get("align"):
        _check_deadline()
        segments = _align_step(jid, tape, segments)

    # 1.7) диаризация «Ovoz Turn»: кто держит пол. Свой движок, без моделей;
    # при отсутствующем ffmpeg/не-аудио молча работаем только по тексту.
    turns = None
    if meta.get("diarize"):
        _check_deadline()
        # Профили — по финальному таймлайну: после Jimlik, а не до него.
        prof = (_profiles_pass(source, segments, billed, deadline)
                if tape is not None else None)
        voices = prof["voices"] if prof else None
        turns = diar_mod.analyze_turns(segments, voices=voices)
        read = diar_mod.summary(turns)
        # Профиль голоса — замер, а не текст: реплика, чьё окно не влезло в ленту,
        # не получает голоса и должна быть названа числом, а не исчезать в
        # «N speakers / M turns» без единой цифры. Без ленты голос считают по
        # тексту —
        # это другой факт и другой код: тот же счётчик без оговорки врал бы о том,
        # что движок слышал два голоса там, где он видел только два абзаца.
        if prof is None:
            db.add_job_event(jid, "diarize",
                             f"{read['speakers']} speakers / {read['turns']} turns "
                             f"(text only: no audio track)",
                             {"code": "turns_text", "speakers": read["speakers"],
                              "turns": read["turns"]})
        else:
            lost = prof["off_tape"]
            db.add_job_event(
                jid, "diarize",
                f"{read['speakers']} speakers / {read['turns']} turns"
                + (f", {lost} cues off-tape" if lost else "") + prof["note"],
                {"code": "turns", "speakers": read["speakers"],
                 "turns": read["turns"], "off_tape": lost,
                 "heard_sec": prof["heard_sec"],
                 "truncation": prof["truncation"] or None})
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
        _words_step(jid, tape, cues)

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
        # Кастинг по голосам: если диаризация заказана, реплика каждого спикера
        # получает свой голос (Madina/Sardor…), а не один диктор на весь диалог.
        # Без диаризации speakers=None → один основной голос (обратная совместимость).
        speakers = ([turns[i].speaker if i < len(turns) else 0
                     for i in range(len(translated))] if turns else None)
        # Честность кастинга: голосов на язык два, спикеров диаризация даёт до 8.
        # Если реальных голосов больше, чем пара, спикеры делят два голоса по циклу —
        # это надо сказать, а не выдать «два разных голоса» там, где их два на восьмерых.
        voiced = {sp for sp in (speakers or []) if sp}
        if len(voiced) > 2:
            db.add_job_event(jid, "tts",
                             f"casting: {len(voiced)} speakers share 2 voices (cycled)")
        rep = _mix_dubbing(jid, translated, tts, tgt, art, speakers=speakers)
        # Голос длиннее окна — это не баг, который надо спрятать: клиент имеет право
        # знать, что даббинг разошёлся с таймкодами, а не получить «готово» и тихую
        # нарезку. Числа финитные; путь файла наружу не идёт.
        if rep["shifted"] or rep["clipped"]:
            note = (f"retimed: {rep['shifted']} of {rep['lines']} voices moved so none "
                    f"overlap (drift {rep['drift_sec']:.1f}s, total {rep['total_sec']:.1f}s)")
            if rep["clipped"]:
                note += ", tail clipped at the paid ceiling"
            db.add_job_event(jid, "tts", note)


def _dub_schedule(segments: list[Segment], actuals: list[float],
                  cap_sec: float) -> tuple[list[float], float, bool]:
    """Куда встать каждому голосу, чтобы ни два не звучали одновременно.

    Реплика даббинга не должна накладываться на предыдущий голос лишь потому, что её
    окно в транскрипте было коротким: нейросетевая фраза длиннее паузы, которую
    transcript под неё зарезервировал. Ставим реплику на её cue-старт, только если
    предыдущий голос уже закончился, иначе — сразу за ним; порядок хранится, наложения
    нет. На хорошо подогнанном звуке (заглушка или голос, уложившийся в бюджет) это
    в точности воспроизводит cue-хронологию, поэтому для того, что и было верно,
    изменение невидимо.

    `cap_sec` — жёсткий потолок длины дорожки. Прежний микшер мерял буфер по
    max(seg.end), который уже ограничен биллингом; сдвиг голосов может раздуть его
    произвольно (провайдер, у которого каждая фраза втрое длиннее окна, — это OOM).
    Хвост за потолком обрезается, и это возвращается честным `clipped`, а не молча.
    """
    starts: list[float] = []
    head = 0.0
    for s, dur in zip(segments, actuals):
        start = max(s.start, head)
        starts.append(start)
        head = start + max(0.0, dur)
    total = max(head, max((s.end for s in segments), default=0.0), 1.0)
    clipped = total > cap_sec
    return starts, min(total, cap_sec), clipped


def _mix_dubbing(jid: str, translated: list[Segment], tts, tgt: str, art: Path,
                 speakers: list[int] | None = None) -> dict:
    rate = 22050
    # Два прохода, но НЕ удержанием всех голосов в памяти: первый проход измеряет
    # реальную длительность каждой реплики и оставляет PCM на диске (по одному
    # файлу на реплику), второй — перечитывает и подмешивает по одному файлу. Пик памяти —
    # O(одна строка), как в прежнем inline-микшере: cap ограничивает выходной буфер,
    # а этот приём не даёт входному накоплению стать тем, что съест RAM.
    tmps: list[Path] = []
    actuals: list[float] = []
    try:
        for idx, s in enumerate(translated):
            tmp = art / f"line_{idx}_{int(s.start * 1000)}.wav"
            # speaker>0 chooses an alternate voice for this diarized speaker; 0/None
            # keeps the single primary voice (stub and non-diarized jobs unchanged).
            spk = speakers[idx] if speakers else 0
            tts.synthesize(s.text, tgt, tmp, dur_sec=s.end - s.start, speaker=spk)
            _ensure_wav(tmp)  # now guarantees 22050 mono 16-bit PCM
            with wave.open(str(tmp), "rb") as w:
                sr = w.getframerate() or rate
                actuals.append(w.getnframes() / sr)
            tmps.append(tmp)

        span = max((s.end for s in translated), default=1.0)
        # Потолок памяти: даб может раздвинуться, но не безобразно. Вдвое длиннее
        # оплаченной ленты + запас — это всё, что микшер выделит.
        cap_sec = span * 2.0 + TIMELINE_SLACK_SEC
        starts, total, clipped = _dub_schedule(translated, actuals, cap_sec)
        samples = bytearray(int(total * rate) * 2)  # 16-bit mono
        for start, tmp in zip(starts, tmps):
            offset = int(start * rate) * 2
            if offset >= len(samples):
                continue                        # весь голос за потолком — тишина
            with wave.open(str(tmp), "rb") as w:
                data = w.readframes(w.getnframes())
            for i in range(0, len(data) - 1, 2):
                pos = offset + i
                if pos + 1 >= len(samples):
                    break
                cur = int.from_bytes(samples[pos:pos + 2], "little", signed=True)
                add = int.from_bytes(data[i:i + 2], "little", signed=True)
                mixed = max(-32768, min(32767, cur + add))
                samples[pos:pos + 2] = mixed.to_bytes(2, "little", signed=True)
    finally:
        for tmp in tmps:
            tmp.unlink(missing_ok=True)         # temp-файлы не переживают job

    out = art / "dubbing.wav"
    with wave.open(str(out), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(samples))
    db.add_artifact(jid, "dubbing", str(out))
    # Честный отчёт: сколько голосов пришлось сдвинуть, и был ли обрезан хвост.
    shifted = sum(1 for s, st in zip(translated, starts) if st > s.start + 1e-6)
    drift = max((st + d - s.end for s, st, d in zip(translated, starts, actuals)),
                default=0.0)
    return {"lines": len(translated), "shifted": shifted, "clipped": clipped,
            "drift_sec": round(drift, 3), "total_sec": round(total, 3)}
