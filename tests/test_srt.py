from app.ling.srt import Cue, duration, format_srt, format_timecode, parse_srt, shift, to_ass

SAMPLE = """1
00:00:01,000 --> 00:00:03,500
Привет мир

2
00:00:03,500 --> 00:00:06,000
Вторая строка
с переносом
"""


def test_parse_and_format_roundtrip():
    cues = parse_srt(SAMPLE)
    assert len(cues) == 2
    assert cues[0].start == 1.0 and cues[0].end == 3.5
    assert cues[1].text == "Вторая строка\nс переносом"
    again = parse_srt(format_srt(cues))
    assert [(c.start, c.end, c.text) for c in again] == [(c.start, c.end, c.text) for c in cues]


def test_timecode_format():
    assert format_timecode(3661.5) == "01:01:01,500"
    assert format_timecode(0) == "00:00:00,000"


def test_shift_and_duration():
    cues = parse_srt(SAMPLE)
    shifted = shift(cues, -1.0)
    assert shifted[0].start == 0.0
    assert duration(cues) == 6.0


def test_to_ass_contains_events():
    cues = parse_srt(SAMPLE)
    ass = to_ass(cues)
    assert "[Events]" in ass
    assert "Dialogue: 0," in ass
    assert "Привет мир" in ass


def test_parse_tolerates_missing_index():
    body = "00:00:00,000 --> 00:00:01,000\ntext\n"
    cues = parse_srt(body)
    assert cues[0].text == "text"
