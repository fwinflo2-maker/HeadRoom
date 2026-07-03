from __future__ import annotations

from headroom.transforms.content_detector import ContentType, detect_content_type


def test_detect_content_type_space_separated_json_objects() -> None:
    result = detect_content_type('{"title":"one"} {"title":"two"}')

    assert result.content_type == ContentType.JSON_ARRAY
    assert result.metadata["item_count"] == 2
    assert result.metadata["is_dict_array"] is True
    assert result.metadata["was_space_separated"] is True


def test_detect_content_type_json_lines() -> None:
    result = detect_content_type('{"title":"one"}\n{"title":"two"}')

    assert result.content_type == ContentType.JSON_ARRAY
    assert result.metadata["item_count"] == 2
    assert result.metadata["is_dict_array"] is True
    assert result.metadata["was_json_lines"] is True
