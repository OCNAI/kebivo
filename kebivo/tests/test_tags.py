import pytest

from kebivo.feedback.tags import normalize_tag_v1


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (" Customer Service ", "customer-service"),
        ("IT", "it"),
        ("Ｆｅａｔｕｒｅ", "feature"),
        ("A\t\nB", "a-b"),
        ("__A---B__", "a-b"),
        ("Équipe", "équipe"),
        ("a" * 64, "a" * 64),
    ],
)
def test_normalize_tag_v1(source: str, expected: str) -> None:
    assert normalize_tag_v1(source) == expected


@pytest.mark.parametrize(
    "source",
    ["", "_-_", "a" * 65, "bug!", "a/b", "a.b", "a\x00b", "a\u200bb", "a\u202eb", "❤"],
)
def test_normalize_tag_rejects_invalid_characters(source: str) -> None:
    with pytest.raises(ValueError):
        normalize_tag_v1(source)
