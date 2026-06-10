"""
Plate-number normalisation — the "safety net" for the LLM.

The LLM is the source of truth for plate extraction. The system prompt
(`app/prompts.py`) explicitly tells it to handle:

  - Pinyin province ("hu" → 沪, "yue" → 粤, "shang hai" → 沪)
  - Spoken digits in pinyin ("yi er san si wu" → "12345")
  - Spoken digits in Chinese characters ("一二三四五" → "12345")
  - Self-correction ("沪A1234 哦不对 沪A12345" → "沪A12345")
  - Trailing filler words ("沪A12345啊" → "沪A12345")
  - Misrecognition ("湖A12345" might be 沪; "苏" might be 鄂, etc.)

This module is the deterministic safety net for the cases the LLM
*occasionally* misses. It runs on whatever the LLM puts in
`update_visitor_info(plate=...)`. If the value is already a valid plate
it returns as-is; if it's noisy but recoverable it cleans it up; if it
looks unrecognisable it returns the raw value so the LLM can re-prompt.

What this function does NOT do:
  - Guess a missing character (LLM is better at asking "is that 沪A12345?")
  - Disambiguate a single Chinese character (湖 vs 沪 — context-sensitive)
  - Re-interpret pinyin mid-string (province always comes first; mid-string
    pinyin is not handled here)
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Final valid plate pattern: province char + city letter + 5-6 alphanumeric
PLATE_FINAL = re.compile(r"^[\u4e00-\u9fa5][A-Z][A-Z0-9]{5,7}$")

# 中文 数字 → ASCII 数字
ZH_DIGIT: dict[str, str] = {
    "零": "0", "〇": "0",
    "一": "1", "壹": "1", "幺": "1",
    "二": "2", "贰": "2", "两": "2",
    "三": "3", "叁": "3",
    "四": "4", "肆": "4",
    "五": "5", "伍": "5",
    "六": "6", "陆": "6",
    "七": "7", "柒": "7",
    "八": "8", "捌": "8",
    "九": "9", "玖": "9",
}

# Trailing Chinese filler words users often tack on
ZH_FILLERS = set("啊呀哈嘿哦嗯呢吧了哇哎呐咯唄耶喽啰呗")

# Common pinyin → digit (one-syllable per word, separated by spaces)
PY_DIGIT: dict[str, str] = {
    "ling": "0", "yi": "1", "er": "2", "san": "3", "si": "4", "wu": "5",
    "liu": "6", "qi": "7", "ba": "8", "jiu": "9",
    "liang": "2",  # 两
}

# Pinyin → province (for when the user says "hu A12345" instead of "沪A12345")
# These are pinyin SYLLABLES, not first letters. We cover both 2- and 3-letter
# forms. Order matters: longer matches first.
# Note: ambiguous cases (jin→津 vs 晋) are resolved by which is more commonly
# the user's intent given the context — we pick the more common one (津 for
# "jin", since Tianjin is more often heard than 晋 for Shanxi).
PROVINCE_PINYIN: dict[str, str] = {
    "bei": "京", "jing": "京",  # Beijing (北)
    "tian": "津",  # Tianjin (津)
    "jin": "晋",  # Shanxi (晋) — "jin" is more commonly Shanxi in plates
    "shang": "沪", "hai": "沪", "hu": "沪",  # Shanghai (沪)
    "guang": "粤", "yue": "粤",  # Guangdong (粤)
    "chong": "渝", "yu": "渝",  # Chongqing (渝)
    "chuan": "川", "si": "川",  # Sichuan (川)
    "su": "苏", "jiang": "苏",  # Jiangsu (苏)
    "zhe": "浙",  # Zhejiang (浙)
    "lu": "鲁", "shan": "鲁",  # Shandong (鲁)
    "ji": "冀", "he": "冀", "bei2": "冀",  # Hebei (冀)
    "shan2": "晋",  # 山西 (晋)
    "nei": "蒙", "meng": "蒙",  # 内蒙古 (蒙)
    "liao": "辽",  # Liaoning (辽)
    "lin": "吉", "ji2": "吉",  # Jilin (吉)
    "hei": "黑", "long": "黑",  # 黑龙江 (黑)
    "an": "皖", "wan": "皖",  # Anhui (皖)
    "fu": "闽", "min": "闽",  # Fujian (闽)
    "gan": "赣",  # Jiangxi (赣)
    "hen": "豫", "yu2": "豫",  # Henan (豫)
    "hu2": "鄂", "e": "鄂",  # Hubei (鄂)
    "hu3": "湘", "xiang": "湘",  # Hunan (湘)
    "hai2": "琼", "qiong": "琼",  # Hainan (琼)
    "gui": "贵", "qian": "贵",  # Guizhou (贵)
    "nan": "桂", "gui2": "桂",  # Guangxi (桂)
    "yun": "云", "dian": "云",  # Yunnan (云)
    "xi": "藏", "zang": "藏",  # Tibet (藏)
    "shaanxi": "陕", "shan3": "陕", "shao": "陕",  # Shaanxi (陕)
    "long2": "甘", "su2": "甘", "gan2": "甘",  # Gansu (甘)
    "qing": "青",  # Qinghai (青)
    "ning": "宁",  # Ningxia (宁)
    "jiang3": "新", "xin": "新",  # Xinjiang (新)
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_separators(s: str) -> str:
    """Remove spaces, dots, dashes, middle-dots that the user might have said."""
    return re.sub(r"[\s\.\-·_，、,。]", "", s)


def _strip_trailing_filler(s: str) -> str:
    """Remove trailing single-char Chinese filler words (啊呀哈嘿...)."""
    while s and s[-1] in ZH_FILLERS:
        s = s[:-1]
    return s


def _substitute_zh_digits(s: str) -> str:
    """Replace 中文数字 (零一二三...) with ASCII digits."""
    return "".join(ZH_DIGIT.get(ch, ch) for ch in s)


def _looks_like_province_pinyin(token: str) -> str | None:
    """If `token` is a known province pinyin, return the province char."""
    return PROVINCE_PINYIN.get(token.lower())


def _looks_like_digit_pinyin(token: str) -> str | None:
    """If `token` is a known digit pinyin, return the digit."""
    return PY_DIGIT.get(token.lower())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_plate(raw: str) -> str:
    """Best-effort deterministic plate normalisation.

    Returns the cleaned form. If it looks like a valid Chinese plate
    (province char + city letter + 5-7 alphanumeric), returns it as-is.
    Otherwise returns the cleaned raw value so the LLM can reason about it.

    The LLM is expected to do the heavy lifting (pinyin, self-correction,
    digit-pinyin conversion). This function is the safety net for the
    LLM's most common slip-ups.
    """
    if not raw:
        return raw

    s = raw.strip()
    s = _strip_trailing_filler(s)
    s = _substitute_zh_digits(s)

    # If it ALREADY looks like a valid plate, return as-is.
    upper = s.upper()
    no_sep = _strip_separators(upper)
    if PLATE_FINAL.match(no_sep):
        return no_sep

    # Tokenise by spaces. Each token might be:
    #   - a province in pinyin  (e.g. "hu")
    #   - a city letter(s)      (e.g. "A")
    #   - digit pinyin words    (e.g. "yi" "er" "san")
    #   - digits                (e.g. "12345")
    #   - a Chinese province    (e.g. "沪")
    tokens = s.split()
    if len(tokens) > 1:
        return _process_spaced(tokens)

    # Single token. Either it's a clean plate, or a pinyin province, or noise.
    return no_sep


def _process_spaced(tokens: list[str]) -> str:
    """Process a list of whitespace-separated tokens."""
    out: list[str] = []
    for t in tokens:
        # Chinese province char alone (e.g. "沪")
        if len(t) == 1 and "\u4e00-\u9fff" >= t >= "\u4e00":
            out.append(t)
            continue
        # Pinyin province (e.g. "hu", "jing", "shang")
        prov = _looks_like_province_pinyin(t)
        if prov is not None and not out:
            out.append(prov)
            continue
        # City letter (e.g. "A", "SH")
        if t.isalpha() and t.upper() == t and len(t) <= 2:
            out.append(t.upper())
            continue
        # Digit pinyin (e.g. "yi", "er")
        dig = _looks_like_digit_pinyin(t)
        if dig is not None:
            out.append(dig)
            continue
        # Digits (e.g. "12345")
        if t.isdigit():
            out.append(t)
            continue
        # Mixed/garbage - keep as-is
        out.append(t.upper())
    return "".join(out)


def pick_last_correction(text: str) -> str:
    """If the user said two plates, return the last one.

    The LLM is expected to do this, but this is a safety net.
    """
    markers = [
        r"哦?不[对是]?", r"等?等", r"改[一]?[下个]?",
        r"不[对]?是[.,，。]?是", r"其实[是]?", r"应该是",
        r"我[说]?[错]?[了]?是", r"更正[一]?[下个]?",
        r"[\s，,。]是",  # " 是" / ",是" / "，是" — the "是" after a separator
    ]
    pattern = "|".join(markers)
    parts = re.split(f"({pattern})", text, flags=re.IGNORECASE)
    if len(parts) <= 1:
        return text
    # Walk backwards and return the first non-marker chunk after the last marker.
    for i in range(len(parts) - 1, -1, -1):
        chunk = parts[i].strip()
        if chunk and not re.match(pattern, chunk, flags=re.IGNORECASE):
            return chunk
    return text


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cases = [
        # (raw, expected, description)
        ("沪A12345", "沪A12345", "perfect"),
        ("沪a12345", "沪A12345", "lowercase"),
        ("沪A一二三四五", "沪A12345", "Chinese digits"),
        ("沪A12345啊", "沪A12345", "trailing filler 啊"),
        ("沪A12345呢", "沪A12345", "trailing filler 呢"),
        ("沪 A 1 2 3 4 5", "沪A12345", "spaced digits"),
        ("沪·A·1·2·3·4·5", "沪A12345", "dotted"),
        ("hu a12345", "沪A12345", "pinyin province"),
        ("jing A12345", "京A12345", "pinyin jing"),
        ("yue A12345", "粤A12345", "pinyin yue"),
        ("shang A12345", "沪A12345", "pinyin shang"),
        ("hu a yi er san si wu", "沪A12345", "spaced pinyin everything"),
        ("shao A12345", "陕A12345", "pinyin shao"),
        ("hei A12345", "黑A12345", "pinyin hei"),
        ("lu A12345", "鲁A12345", "pinyin lu"),
        ("沪A1234", "沪A1234", "too short (leave alone)"),
        ("湖A12345", "湖A12345", "ambiguous (湖 not in map)"),
        ("hello", "HELLO", "garbage (let LLM re-prompt)"),
        ("苏A12345", "苏A12345", "valid 2-letter prefix"),
        ("赣A12345", "赣A12345", "valid 赣"),
    ]
    pass_n = 0
    fail_n = 0
    for raw, expected, desc in cases:
        got = normalize_plate(raw)
        ok = got == expected
        mark = "✓" if ok else "✗"
        if ok:
            pass_n += 1
        else:
            fail_n += 1
        print(f"  {mark} {desc:35s} {raw!r:35s} → {got!r:20s} (expected {expected!r})")
    print(f"\n  {pass_n}/{pass_n+fail_n} pass")
