"""Model-focused unit tests for the Simkl helpers."""

from anibridge.providers.list.simkl.models import (
    SimklIds,
    SimklListItem,
    SimklListStatus,
    SimklMedia,
    SimklMediaKind,
    SimklSearchType,
)


def test_simkl_ids_normalize_response_and_request_fields() -> None:
    """SimklIds should expose one canonical Simkl id for both response shapes."""
    ids = SimklIds(simkl_id=123, slug="cowboy-bebop", imdb="tt0213338")

    assert ids.canonical_simkl_id == 123

    request_ids = ids.request_ids()
    assert request_ids.simkl == 123
    assert request_ids.simkl_id is None


def test_models_ignore_extra_fields() -> None:
    """API models should ignore fields we do not care about."""
    item = SimklListItem.model_validate(
        {
            "status": "watching",
            "watched_episodes_count": 4,
            "ignored": "value",
        }
    )

    assert item.status == "watching"
    assert item.watched_episodes_count == 4


def test_models_coerce_strenum_fields_from_api_payloads() -> None:
    """API payload strings should parse into the typed enum-backed fields."""
    media = SimklMedia.model_validate({"endpoint_type": "anime", "type": "tv"})
    item = SimklListItem.model_validate({"status": "completed"})

    assert media.endpoint_type is SimklMediaKind.ANIME
    assert media.type is SimklSearchType.TV
    assert item.status is SimklListStatus.COMPLETED
