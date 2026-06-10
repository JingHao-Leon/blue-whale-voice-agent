"""Tests for the visitor schema and slot validation."""
from app.schemas import VisitorInfo


def test_plate_normalisation_chinese():
    v = VisitorInfo(plate="沪a12345")
    assert v.plate == "沪A12345"


def test_plate_normalisation_with_spaces():
    v = VisitorInfo(plate=" 沪A·12345 ")
    assert v.plate == "沪A12345"


def test_plate_invalid_preserved_for_llm():
    # Pinyin or otherwise unrecognised plates should be kept as-is so the
    # LLM can re-prompt or reason about what the user said.
    v = VisitorInfo(plate="hu a12345")
    assert v.plate == "hu a12345"


def test_plate_alphanumeric_preserved():
    v = VisitorInfo(plate="A12345")
    assert v.plate == "A12345"


def test_phone_normalisation_with_country_code():
    v = VisitorInfo(phone="+86 138 1234 5678")
    assert v.phone == "13812345678"  # +86 stripped


def test_to_wechat_card_returns_markdown():
    v = VisitorInfo(
        plate="沪A12345",
        company="蓝色鲸鱼",
        reason="送货",
        contact_name="张师傅",
        phone="13812345678",
        duration="2小时",
    )
    card = v.to_wechat_card()
    assert card.startswith("##")
    assert "沪A12345" in card
    assert "蓝色鲸鱼" in card
    assert "张师傅" in card
    assert "13812345678" in card
    assert "2小时" in card


def test_to_wechat_card_includes_history():
    prior = VisitorInfo(
        plate="沪A12345", reason="送货", started_at=__import__("datetime").datetime(2025, 5, 12, 10, 0)
    )
    v = VisitorInfo(
        plate="沪A12345", reason="送货", phone="13812345678", is_returning=True, call_history=[prior]
    )
    card = v.to_wechat_card()
    assert "📋 历史来访" in card
    assert "2025-05-12" in card


def test_is_complete_shortcut_for_returning():
    v = VisitorInfo(plate="沪A12345", is_returning=True)
    assert v.is_complete()  # returning shortcut


def test_is_complete_requires_phone_for_new_visitor():
    v = VisitorInfo(plate="沪A12345", reason="送货")
    assert not v.is_complete()
    assert "phone" in v.missing_required()
