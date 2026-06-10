"""Tests for the plate normaliser."""
import pytest

from app.plate import normalize_plate, pick_last_correction


# --- Pinyin province conversion --------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("hu a12345", "沪A12345"),
    ("hu A 1 2 3 4 5", "沪A12345"),
    ("shang A12345", "沪A12345"),
    ("hai A12345", "沪A12345"),
    ("jing A12345", "京A12345"),
    ("yue A12345", "粤A12345"),
    ("shao A12345", "陕A12345"),
    ("lu A12345", "鲁A12345"),
    ("chuan A12345", "川A12345"),
    ("yu A12345", "渝A12345"),
    ("chong A12345", "渝A12345"),
    ("hei A12345", "黑A12345"),
    ("ji A12345", "冀A12345"),
    ("jin A12345", "晋A12345"),
    ("wan A12345", "皖A12345"),
    ("min A12345", "闽A12345"),
])
def test_pinyin_province(raw, expected):
    assert normalize_plate(raw) == expected


# --- Spaced pinyin everything -------------------------------------------------

def test_pinyin_everything_spaced():
    """User says the whole plate in pinyin, one word per token."""
    assert normalize_plate("hu a yi er san si wu") == "沪A12345"


def test_pinyin_everything_spaced_mixed():
    assert normalize_plate("shang A yi er san si wu") == "沪A12345"


# --- Chinese digit conversion ------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("沪A一二三四五", "沪A12345"),
    ("沪A零一二三四五", "沪A012345"),
    ("沪A壹贰叁肆伍", "沪A12345"),
    ("沪A两三四五", "沪A2345"),  # 两→2
])
def test_chinese_digits(raw, expected):
    assert normalize_plate(raw) == expected


# --- Spacing / separator cleanup ---------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("沪 A 1 2 3 4 5", "沪A12345"),
    ("沪·A·1·2·3·4·5", "沪A12345"),
    ("沪-A-1-2-3-4-5", "沪A12345"),
    ("沪.A.1.2.3.4.5", "沪A12345"),
])
def test_separators(raw, expected):
    assert normalize_plate(raw) == expected


# --- Trailing filler ---------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("沪A12345啊", "沪A12345"),
    ("沪A12345呢", "沪A12345"),
    ("沪A12345呗", "沪A12345"),
    ("沪A12345啊呀", "沪A12345"),
    ("沪A12345呢吧", "沪A12345"),
    ("沪A12345啊呢吧", "沪A12345"),
])
def test_trailing_filler(raw, expected):
    assert normalize_plate(raw) == expected


# --- Already-valid plates pass through --------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("沪A12345", "沪A12345"),
    ("苏A12345", "苏A12345"),
    ("京A12345", "京A12345"),
    ("赣A12345", "赣A12345"),
    ("陕A12345", "陕A12345"),
    ("Z12345", "Z12345"),  # pure alphanumeric (special plates)
    ("SHC12345", "SHC12345"),  # 3-letter city (e.g. SH)
])
def test_passthrough(raw, expected):
    assert normalize_plate(raw) == expected


# --- Garbage / ambiguous -----------------------------------------------------

def test_garbage_passthrough():
    """Garbage is returned cleaned but unaltered, so the LLM can re-prompt."""
    assert normalize_plate("hello") == "HELLO"


def test_ambiguous_chinese_char_passthrough():
    """湖 (lake) is not in the province map. Don't guess — let LLM ask."""
    assert normalize_plate("湖A12345") == "湖A12345"


def test_too_short_passthrough():
    """4 digits after city letter: leave alone, don't pad (LLM will ask)."""
    assert normalize_plate("沪A1234") == "沪A1234"


# --- Self-correction detection (for LLM tooling) ----------------------------

def test_pick_last_correction_simple():
    assert pick_last_correction("沪A1234 哦不对 沪A12345") == "沪A12345"


def test_pick_last_correction_no_marker():
    assert pick_last_correction("沪A12345") == "沪A12345"


def test_pick_last_correction_equals():
    assert pick_last_correction("不是苏B 是粤B") == "粤B"


# --- Idempotency: normalising twice gives same result -----------------------

@pytest.mark.parametrize("raw", [
    "沪A12345",
    "hu a12345",
    "沪A一二三四五",
    "沪A12345啊",
])
def test_idempotent(raw):
    once = normalize_plate(raw)
    twice = normalize_plate(once)
    assert once == twice
