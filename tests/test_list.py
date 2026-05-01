"""Tests that focus on the Simkl list provider behavior."""

from collections.abc import Callable
from datetime import datetime

import pytest
from anibridge.list import ListMediaType, ListStatus

from anibridge.providers.list.simkl.list import (
    SimklListEntry,
    SimklListMedia,
    SimklListProvider,
)
from anibridge.providers.list.simkl.models import SimklMedia


def test_entry_status_roundtrip(
    entry_factory: Callable[..., SimklListEntry],
    anime_media: SimklMedia,
) -> None:
    """Status setter/getter pair should round-trip supported statuses."""
    entry = entry_factory(anime_media)

    for status in ListStatus:
        entry.status = status
        if status == ListStatus.REPEATING:
            assert entry.status == ListStatus.CURRENT
        else:
            assert entry.status == status


def test_entry_progress_validation(
    entry_factory: Callable[..., SimklListEntry],
    anime_media: SimklMedia,
) -> None:
    """Progress setter should reject negative numbers."""
    entry = entry_factory(anime_media)
    with pytest.raises(ValueError):
        entry.progress = -1


def test_entry_user_rating_validation(
    entry_factory: Callable[..., SimklListEntry],
    anime_media: SimklMedia,
) -> None:
    """Ratings above the allowed range should be rejected."""
    entry = entry_factory(anime_media)

    with pytest.raises(ValueError):
        entry.user_rating = 101


def test_entry_started_and_finished_setter_use_provider_timezone(
    provider: SimklListProvider,
    entry_factory: Callable[..., SimklListEntry],
    anime_media: SimklMedia,
) -> None:
    """started_at uses provider timezone while finished_at is discarded."""
    entry = entry_factory(anime_media)
    input_dt = datetime(2024, 1, 2, 12, 0)

    entry.started_at = input_dt
    entry.finished_at = input_dt

    assert entry.started_at is not None
    assert entry.started_at.tzinfo == provider._client.user_timezone
    assert entry.finished_at is None


def test_movie_entry_finished_setter_uses_provider_timezone(
    provider: SimklListProvider,
    entry_factory: Callable[..., SimklListEntry],
    movie_media: SimklMedia,
) -> None:
    """Movie-like entries should preserve finished_at with provider timezone."""
    entry = entry_factory(movie_media)
    input_dt = datetime(2024, 2, 2, 12, 0)

    entry.finished_at = input_dt

    assert entry.finished_at is not None
    assert entry.finished_at.tzinfo == provider._client.user_timezone


@pytest.mark.asyncio
async def test_search_wraps_media_into_entries(
    provider: SimklListProvider,
    anime_media: SimklMedia,
    movie_media: SimklMedia,
    fake_client,
) -> None:
    """Search should adapt Simkl media results into AniBridge entries."""
    fake_client.search_results = [anime_media, movie_media]

    results = await provider.search("o")

    assert len(results) == 2
    assert isinstance(results[0], SimklListEntry)
    assert results[0].media().key == str(anime_media.ids.canonical_simkl_id)


@pytest.mark.asyncio
async def test_get_entry_returns_cached_state(
    provider: SimklListProvider,
    anime_media: SimklMedia,
) -> None:
    """get_entry should wrap cached Simkl list state when present."""
    entry = await provider.get_entry(str(anime_media.ids.canonical_simkl_id))

    assert entry is not None
    assert entry.status == ListStatus.CURRENT
    assert entry.progress == 3
    assert entry.repeats is None
    assert entry.user_rating == 80
    assert entry.started_at is not None
    assert entry.started_at.isoformat() == "2024-01-01T00:00:00+00:00"
    assert entry.finished_at is None


@pytest.mark.asyncio
async def test_get_entry_discards_finished_at_for_completed_items(
    provider: SimklListProvider,
    movie_media: SimklMedia,
) -> None:
    """Movie-like completed entries should expose finished_at."""
    entry = await provider.get_entry(str(movie_media.ids.canonical_simkl_id))

    assert entry is not None
    assert entry.started_at is not None
    assert entry.started_at.isoformat() == "2024-02-01T00:00:00+00:00"
    assert entry.finished_at is not None
    assert entry.finished_at.isoformat() == "2024-02-02T00:00:00+00:00"


@pytest.mark.asyncio
async def test_get_entry_ignores_finished_at_from_provider_state(
    provider: SimklListProvider,
    movie_media: SimklMedia,
    fake_client,
) -> None:
    """The don't-remember sentinel should still map to no finished_at."""
    simkl_id = movie_media.ids.canonical_simkl_id
    if simkl_id is None:
        raise ValueError("Test movie media is missing a Simkl id.")
    fake_client.entries[simkl_id].status = "completed"
    fake_client.entries[simkl_id].last_watched_at = datetime(1970, 1, 1)

    entry = await provider.get_entry(str(simkl_id))

    assert entry is not None
    assert entry.finished_at is None


@pytest.mark.asyncio
async def test_update_entry_forwards_normalized_state(
    provider: SimklListProvider,
    fake_client,
    anime_media: SimklMedia,
) -> None:
    """Provider updates should pass normalized Simkl state to the client."""
    entry = await provider.get_entry(str(anime_media.ids.canonical_simkl_id))
    assert entry is not None
    entry.status = ListStatus.COMPLETED
    entry.progress = 26
    entry.repeats = 3
    entry.user_rating = 90
    entry.started_at = datetime(2024, 1, 2, 12, 0)

    await provider.update_entry(str(anime_media.ids.canonical_simkl_id), entry)

    assert fake_client.updated_entry_states[-1].status == "completed"
    assert fake_client.updated_entry_states[-1].progress == 26
    assert fake_client.updated_entry_states[-1].repeats is None
    assert fake_client.updated_entry_states[-1].user_rating == 90
    assert fake_client.updated_entry_states[-1].started_at is not None
    assert (
        fake_client.updated_entry_states[-1].started_at.isoformat()
        == "2024-01-02T12:00:00+00:00"
    )


@pytest.mark.asyncio
async def test_resolve_mapping_descriptors_ignores_unsupported_providers(
    provider: SimklListProvider,
) -> None:
    """Unsupported mapping providers should be ignored."""
    results = await provider.resolve_mapping_descriptors(
        [("anilist", "16498", None), ("mal", "1", None), ("imdb", "tt1", None)]
    )

    assert results == []


@pytest.mark.asyncio
async def test_delete_entry_forwards_simkl_id_and_kind(
    provider: SimklListProvider,
    fake_client,
    anime_media: SimklMedia,
) -> None:
    """Deleting an entry should forward the Simkl identifiers to the client."""
    await provider.delete_entry(str(anime_media.ids.canonical_simkl_id))

    assert fake_client.deleted_media_ids == [
        (anime_media.ids.canonical_simkl_id, "anime")
    ]


def test_media_accessors(
    provider: SimklListProvider,
    anime_media: SimklMedia,
    movie_media: SimklMedia,
) -> None:
    """List media wrappers should expose provider, type, and poster helpers."""
    anime = SimklListMedia(provider, anime_media)
    movie = SimklListMedia(provider, movie_media)

    assert anime.provider() is provider
    assert anime.media_type == ListMediaType.TV
    assert movie.media_type == ListMediaType.MOVIE
    assert anime.poster_image is not None
