"""Работа с SRT: парсинг, форматирование, сдвиг, экспорт в ASS."""
from __future__ import annotations

import re
from dataclasses import dataclass

_TIMECODE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


@dataclass
class Cue:
    index: int
    start: float   # секунды
    end: float
    text: str


def parse_srt(content: str) -> list[Cue]:
    blocks = re.split(r"\n\s*\n", content.strip())
    cues: list[Cue] = []
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        # ищем строку времени: она может быть 1-й (если индекс пропущен) или 2-й
        time_line_idx = next(
            (i for i, ln in enumerate(lines[:2]) if _TIMECODE.search(ln)), None
        )
        if time_line_idx is None:
            continue
        idx = int(lines[0]) if time_line_idx == 1 and lines[0].strip().isdigit() else len(cues) + 1
        times = _TIMECODE.findall(lines[time_line_idx])
        if len(times) != 2:
            continue
        start = _to_seconds(times[0])
        end = _to_seconds(times[1])
        text = "\n".join(lines[time_line_idx + 1:])
        cues.append(Cue(idx, start, end, text))
    return cues


def _to_seconds(parts: tuple[str, str, str, str]) -> float:
    """Timecode → seconds, with one division at the end.

    Summing `h*3600 + m*60 + s + ms/1000` looks identical and is not: the parts
    are rounded separately, so `00:00:03,470` came back as 3.4699999999999998 while
    every engine that produced the file had written 3.47. A parse→format round trip
    that shifts the last bit is how a 1 ms "drift" appears in a diff nobody can
    explain — so the arithmetic is done in integer milliseconds and divided once."""
    h, m, s, ms = parts
    return (int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1_000 + int(ms)) / 1000.0


def format_timecode(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_srt(cues: list[Cue]) -> str:
    out = []
    for i, c in enumerate(cues, start=1):
        out.append(
            f"{i}\n{format_timecode(c.start)} --> {format_timecode(c.end)}\n{c.text}\n"
        )
    return "\n".join(out)


def shift(cues: list[Cue], delta: float) -> list[Cue]:
    return [Cue(c.index, max(0.0, c.start + delta), max(0.0, c.end + delta), c.text) for c in cues]


def duration(cues: list[Cue]) -> float:
    return max((c.end for c in cues), default=0.0)


def to_ass(cues: list[Cue], title: str = "Ovoz Studio") -> str:
    """Экспорт в ASS для burn-in через ffmpeg: subtitles=x:force_style..."""
    header = (
        "[Script Info]\n"
        f"Title: {title}\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1280\nPlayResY: 720\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Alignment\n"
        "Style: Default,Arial,42,&H00FFFFFF,&H00000000,2\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Text\n"
    )
    def ass_time(sec: float) -> str:
        h = int(sec // 3600)
        m = int(sec % 3600 // 60)
        s = sec % 60
        return f"{h}:{m:02d}:{s:05.2f}"
    events = []
    for c in cues:
        text = c.text.replace("\n", "\\N")
        events.append(f"Dialogue: 0,{ass_time(c.start)},{ass_time(c.end)},{text}")
    return header + "\n".join(events) + "\n"
