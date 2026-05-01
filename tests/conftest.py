"""Common pytest fixtures for the Simkl provider test suite."""

import logging
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from typing import cast

import pytest
from anibridge.utils.limiter import Limiter
from anibridge.utils.types import ProviderLogger

from anibridge.providers.list.simkl.client import SimklClient
from anibridge.providers.list.simkl.list import SimklListEntry, SimklListProvider
from anibridge.providers.list.simkl.models import (
    SimklAccount,
    SimklIds,
    SimklListEntryState,
    SimklListItem,
    SimklListStatus,
    SimklMedia,
    SimklMediaKind,
    SimklUser,
    SimklUserSettings,
)
from tests.fakes import FakeSimklClient


@pytest.fixture()
def media_factory() -> Callable[[int, str, SimklMediaKind], SimklMedia]:
    """Return a factory that builds Simkl media objects with sane defaults."""

    def _build(
        simkl_id: int,
        title: str,
        kind: SimklMediaKind = SimklMediaKind.ANIME,
    ) -> SimklMedia:
        return SimklMedia(
            title=title,
            year=1998,
            poster="11/abcdef",
            endpoint_type=kind,
            ids=SimklIds(simkl=simkl_id, slug=title.lower().replace(" ", "-")),
            total_episodes=26 if kind is not SimklMediaKind.MOVIES else None,
        )

    return _build


@pytest.fixture()
def anime_media(media_factory: Callable[[int, str, str], SimklMedia]) -> SimklMedia:
    """Sample anime media item."""
    return media_factory(101, "Cowboy Bebop", SimklMediaKind.ANIME)


@pytest.fixture()
def movie_media(media_factory: Callable[[int, str, str], SimklMedia]) -> SimklMedia:
    """Sample movie media item."""
    return media_factory(202, "Your Name", SimklMediaKind.MOVIES)


@pytest.fixture()
def anime_entry(anime_media: SimklMedia) -> SimklListItem:
    """Sample cached anime list item."""
    return SimklListItem(
        status=SimklListStatus.WATCHING,
        watched_episodes_count=3,
        total_episodes_count=26,
        user_rating=8,
        added_to_watchlist_at=datetime(2024, 1, 1, tzinfo=UTC),
        last_watched_at=datetime(2024, 1, 4, tzinfo=UTC),
        anime_type="tv",
        show=anime_media,
    )


@pytest.fixture()
def movie_entry(movie_media: SimklMedia) -> SimklListItem:
    """Sample cached movie list item."""
    return SimklListItem(
        status=SimklListStatus.COMPLETED,
        user_rating=9,
        added_to_watchlist_at=datetime(2024, 2, 1, tzinfo=UTC),
        last_watched_at=datetime(2024, 2, 2, tzinfo=UTC),
        movie=movie_media,
    )


@pytest.fixture()
def fake_client(
    anime_media: SimklMedia,
    movie_media: SimklMedia,
    anime_entry: SimklListItem,
    movie_entry: SimklListItem,
) -> FakeSimklClient:
    """Provide a fake Simkl client seeded with deterministic media objects."""
    anime_id = anime_media.ids.canonical_simkl_id
    movie_id = movie_media.ids.canonical_simkl_id
    if anime_id is None or movie_id is None:
        raise ValueError("Test media is missing a Simkl id.")

    return FakeSimklClient(
        medias={
            anime_id: anime_media,
            movie_id: movie_media,
        },
        entries={
            anime_id: anime_entry,
            movie_id: movie_entry,
        },
    )


@pytest.fixture()
def provider(fake_client: FakeSimklClient) -> SimklListProvider:
    """Return a SimklListProvider wired to the fake client."""
    provider = SimklListProvider(
        logger=cast(ProviderLogger, logging.getLogger("tests.provider")),
        config={"client_id": "client-id", "token": "token"},
    )
    provider._client = cast(SimklClient, fake_client)
    provider._user = None
    return provider


@pytest.fixture()
def entry_factory(
    provider: SimklListProvider,
) -> Callable[..., SimklListEntry]:
    """Return a helper that wraps media objects in SimklListEntry instances."""

    def _build(media: SimklMedia, item: SimklListItem | None = None) -> SimklListEntry:
        entry_state = provider._build_entry_state(media, item)
        return SimklListEntry(provider, media=media, entry_state=entry_state)

    return _build


@pytest.fixture(autouse=True)
def disable_rate_limiter() -> Generator:
    """Disable limiter behavior for fast tests."""
    previous = Limiter.DISABLED
    Limiter.DISABLED = True
    yield
    Limiter.DISABLED = previous


@pytest.fixture()
def settings() -> SimklUserSettings:
    """Return a deterministic Simkl user settings payload."""
    return SimklUserSettings(
        user=SimklUser(name="Test User"),
        account=SimklAccount(id=99, timezone="UTC", type="free"),
    )


@pytest.fixture()
def entry_state_factory() -> Callable[[int, SimklMediaKind], SimklListEntryState]:
    """Return a helper for building normalized entry state objects."""

    def _build(media_id: int, kind: SimklMediaKind) -> SimklListEntryState:
        return SimklListEntryState(media_id=media_id, kind=kind)

    return _build
