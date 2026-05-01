"""Unit tests focusing on the SimklClient helper behaviors."""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import aiohttp
import pytest
from anibridge.utils.types import ProviderLogger

from anibridge.providers.list.simkl.client import SimklClient
from anibridge.providers.list.simkl.models import (
    SimklActivities,
    SimklActivityGroup,
    SimklListEntryState,
    SimklListItem,
    SimklListStatus,
    SimklMedia,
    SimklMediaKind,
)


@pytest.fixture()
def client() -> SimklClient:
    """Return a fresh SimklClient instance backed by stub credentials."""
    return SimklClient(
        client_id="client-id",
        token="token",
        logger=cast(ProviderLogger, logging.getLogger("tests.client")),
    )


def test_default_rate_limiter_is_shared_across_clients() -> None:
    """Clients without a custom limit should reuse one global read limiter."""
    first = SimklClient(
        client_id="client-id",
        token="token",
        logger=cast(ProviderLogger, logging.getLogger("tests.client")),
    )
    second = SimklClient(
        client_id="client-id",
        token="token",
        logger=cast(ProviderLogger, logging.getLogger("tests.client")),
    )

    assert first.rate_limit is None
    assert second.rate_limit is None
    assert first._read_limiter is second._read_limiter


def test_custom_rate_limiter_is_local_per_client() -> None:
    """Custom limits should create per-client read limiters and convert to req/sec."""
    first = SimklClient(
        client_id="client-id",
        token="token",
        logger=cast(ProviderLogger, logging.getLogger("tests.client")),
        rate_limit=120,
    )
    second = SimklClient(
        client_id="client-id",
        token="token",
        logger=cast(ProviderLogger, logging.getLogger("tests.client")),
        rate_limit=120,
    )

    assert first._read_limiter is not second._read_limiter
    assert first._read_limiter.rate == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_get_session_creates_and_reuses_client_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_get_session should build the aiohttp session with Simkl headers once."""
    created_headers: list[dict[str, str]] = []

    class DummySession:
        def __init__(self, *, headers: dict[str, str]):
            self.headers = headers
            self.closed = False
            created_headers.append(headers)

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "anibridge.providers.list.simkl.client.aiohttp.ClientSession",
        lambda *, headers: DummySession(headers=headers),
    )

    stub_client = SimklClient(
        client_id="client-id",
        token="abc",
        logger=cast(ProviderLogger, logging.getLogger("tests.client")),
    )

    session_one = await stub_client._get_session()
    assert created_headers[0]["Authorization"] == "Bearer abc"
    assert created_headers[0]["simkl-api-key"] == "client-id"

    session_two = await stub_client._get_session()
    assert session_one is session_two

    cast(DummySession, session_one).closed = True
    session_three = await stub_client._get_session()
    assert session_three is not session_one


@pytest.mark.asyncio
async def test_close_ignores_already_closed_session() -> None:
    """Close should only attempt to run when the session is still open."""

    class DummySession:
        def __init__(self) -> None:
            self.closed = False
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            self.closed = True

    stub_client = SimklClient(
        client_id="client-id",
        token="abc",
        logger=cast(ProviderLogger, logging.getLogger("tests.client")),
    )
    stub_client._session = cast(aiohttp.ClientSession, DummySession())

    await stub_client.close()
    await stub_client.close()

    assert cast(DummySession, stub_client._session).close_calls == 1


@pytest.mark.asyncio
async def test_refresh_list_full_sync_loads_all_kinds(
    client: SimklClient,
    anime_entry: SimklListItem,
    movie_entry: SimklListItem,
) -> None:
    """Full refresh should replace the in-memory list cache for each kind."""

    async def fake_activities() -> SimklActivities:
        group = SimklActivityGroup(all=datetime(2024, 1, 1, tzinfo=UTC))
        return SimklActivities(
            all=datetime(2024, 1, 1, tzinfo=UTC),
            tv_shows=group,
            anime=group,
            movies=group,
        )

    async def fake_request(_method: str, path: str, **_: Any) -> dict:
        assert path == "/sync/all-items/"
        return {
            "shows": [],
            "anime": [anime_entry.model_dump(mode="json", exclude_none=True)],
            "movies": [movie_entry.model_dump(mode="json", exclude_none=True)],
        }

    client.get_activities = fake_activities  # ty: ignore[invalid-assignment]
    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    await client.refresh_user_list(force_full=True)

    assert anime_entry.show is not None
    assert movie_entry.movie is not None
    assert (
        client.get_list_entry(anime_entry.show.ids.canonical_simkl_id or 0) is not None
    )
    assert (
        client.get_list_entry(movie_entry.movie.ids.canonical_simkl_id or 0) is not None
    )


@pytest.mark.asyncio
async def test_refresh_list_ignores_playback_only_changes(
    client: SimklClient,
    anime_entry: SimklListItem,
) -> None:
    """Playback-only activity changes should not trigger list sync calls."""
    base_group = SimklActivityGroup(
        all=datetime(2024, 1, 1, tzinfo=UTC),
        playback=datetime(2024, 1, 1, tzinfo=UTC),
    )
    changed_group = SimklActivityGroup(
        all=datetime(2024, 1, 2, tzinfo=UTC),
        playback=datetime(2024, 1, 2, tzinfo=UTC),
    )
    client._list_activities = SimklActivities(
        all=datetime(2024, 1, 1, tzinfo=UTC),
        settings=None,
        tv_shows=base_group,
        anime=base_group,
        movies=base_group,
    )
    anime_id = (
        anime_entry.show.ids.canonical_simkl_id
        if anime_entry.show is not None
        else None
    )
    if anime_id is None:
        raise ValueError("Test anime entry is missing a Simkl id.")
    client._list_entry_cache[anime_id] = anime_entry

    async def fake_activities() -> SimklActivities:
        return SimklActivities(
            all=datetime(2024, 1, 2, tzinfo=UTC),
            settings=None,
            tv_shows=changed_group,
            anime=changed_group,
            movies=changed_group,
        )

    async def unexpected_request(_method: str, path: str, **_: Any) -> dict:
        raise AssertionError(f"Unexpected sync request: {path}")

    client.get_activities = fake_activities  # ty: ignore[invalid-assignment]
    client._make_request = unexpected_request  # ty: ignore[invalid-assignment]

    await client.refresh_user_list()

    assert client.get_list_entry(anime_id) is anime_entry


@pytest.mark.asyncio
async def test_refresh_list_uses_ratings_endpoint_for_rating_only_changes(
    client: SimklClient,
    anime_entry: SimklListItem,
) -> None:
    """Rating-only changes should use the lighter sync/ratings endpoint."""
    show_media = anime_entry.show
    if show_media is None:
        raise ValueError("Test anime entry is missing show media.")
    anime_id = show_media.ids.canonical_simkl_id
    if anime_id is None:
        raise ValueError("Test anime entry is missing a Simkl id.")
    client._list_entry_cache[anime_id] = anime_entry
    client._media_cache[anime_id] = show_media
    client._media_kind_cache[anime_id] = SimklMediaKind.ANIME
    client._kind_media_ids[SimklMediaKind.ANIME].add(anime_id)
    client._list_activities = SimklActivities(
        all=datetime(2024, 1, 1, tzinfo=UTC),
        settings=None,
        tv_shows=SimklActivityGroup(all=datetime(2024, 1, 1, tzinfo=UTC)),
        anime=SimklActivityGroup(
            all=datetime(2024, 1, 2, tzinfo=UTC),
            rated_at=datetime(2024, 1, 1, tzinfo=UTC),
        ),
        movies=SimklActivityGroup(all=datetime(2024, 1, 1, tzinfo=UTC)),
    )
    calls: list[tuple[str, dict[str, str | int] | None]] = []

    async def fake_activities() -> SimklActivities:
        return SimklActivities(
            all=datetime(2024, 1, 2, tzinfo=UTC),
            settings=None,
            tv_shows=SimklActivityGroup(all=datetime(2024, 1, 1, tzinfo=UTC)),
            anime=SimklActivityGroup(
                all=datetime(2024, 1, 2, tzinfo=UTC),
                rated_at=datetime(2024, 1, 2, tzinfo=UTC),
            ),
            movies=SimklActivityGroup(all=datetime(2024, 1, 1, tzinfo=UTC)),
        )

    async def fake_request(_method: str, path: str, **kwargs: Any) -> dict:
        calls.append((path, cast(dict[str, str | int] | None, kwargs.get("params"))))
        if path == "/sync/ratings/anime":
            updated = anime_entry.model_copy(update={"user_rating": 10})
            return {"anime": [updated.model_dump(mode="json", exclude_none=True)]}
        raise AssertionError(f"Unexpected request path: {path}")

    client.get_activities = fake_activities  # ty: ignore[invalid-assignment]
    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    await client.refresh_user_list()

    assert calls == [
        (
            "/sync/ratings/anime",
            {"date_from": "2024-01-01T00:00:00Z"},
        )
    ]
    cached_entry = client.get_list_entry(anime_id)
    assert cached_entry is not None
    assert cached_entry.user_rating == 10


@pytest.mark.asyncio
async def test_refresh_list_uses_ids_only_for_removal_reconciliation(
    client: SimklClient,
    anime_entry: SimklListItem,
) -> None:
    """Removal reconciliation should use the minified ids-only list response."""
    show_media = anime_entry.show
    if show_media is None:
        raise ValueError("Test anime entry is missing show media.")
    anime_id = show_media.ids.canonical_simkl_id
    if anime_id is None:
        raise ValueError("Test anime entry is missing a Simkl id.")
    client._list_entry_cache[anime_id] = anime_entry
    client._media_cache[anime_id] = show_media
    client._media_kind_cache[anime_id] = SimklMediaKind.ANIME
    client._kind_media_ids[SimklMediaKind.ANIME].add(anime_id)
    client._list_activities = SimklActivities(
        all=datetime(2024, 1, 1, tzinfo=UTC),
        settings=None,
        tv_shows=SimklActivityGroup(all=datetime(2024, 1, 1, tzinfo=UTC)),
        anime=SimklActivityGroup(
            all=datetime(2024, 1, 1, tzinfo=UTC),
            removed_from_list=datetime(2024, 1, 1, tzinfo=UTC),
        ),
        movies=SimklActivityGroup(all=datetime(2024, 1, 1, tzinfo=UTC)),
    )
    calls: list[tuple[str, dict[str, str | int] | None]] = []

    async def fake_activities() -> SimklActivities:
        return SimklActivities(
            all=datetime(2024, 1, 2, tzinfo=UTC),
            settings=None,
            tv_shows=SimklActivityGroup(all=datetime(2024, 1, 1, tzinfo=UTC)),
            anime=SimklActivityGroup(
                all=datetime(2024, 1, 2, tzinfo=UTC),
                removed_from_list=datetime(2024, 1, 2, tzinfo=UTC),
            ),
            movies=SimklActivityGroup(all=datetime(2024, 1, 1, tzinfo=UTC)),
        )

    async def fake_request(_method: str, path: str, **kwargs: Any) -> dict:
        calls.append((path, cast(dict[str, str | int] | None, kwargs.get("params"))))
        if path == "/sync/all-items/":
            return {"shows": [], "anime": [], "movies": []}
        raise AssertionError(f"Unexpected request path: {path}")

    client.get_activities = fake_activities  # ty: ignore[invalid-assignment]
    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    await client.refresh_user_list()

    assert calls == [
        (
            "/sync/all-items/",
            {"extended": "simkl_ids_only"},
        )
    ]
    assert client.get_list_entry(anime_id) is None


@pytest.mark.asyncio
async def test_get_media_uses_search_id_fallback(
    client: SimklClient,
    anime_media: SimklMedia,
) -> None:
    """get_media should fall back to /search/id when the cache is missing."""

    async def noop_refresh(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_request(_method: str, path: str, **_: Any) -> list[dict]:
        assert path == "/search/id"
        return [anime_media.model_dump(mode="json", exclude_none=True)]

    client.refresh_user_list = noop_refresh  # ty: ignore[invalid-assignment]
    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    media = await client.get_media(anime_media.ids.canonical_simkl_id or 0)

    assert media is not None
    assert media.title == anime_media.title


@pytest.mark.asyncio
async def test_search_combines_unique_results(
    client: SimklClient,
    anime_media: SimklMedia,
    movie_media: SimklMedia,
) -> None:
    """Search should merge results from the Simkl search endpoints."""

    async def fake_request(_method: str, path: str, **_: Any) -> list[dict]:
        if path == "/search/anime":
            return [anime_media.model_dump(mode="json", exclude_none=True)]
        if path == "/search/movie":
            return [movie_media.model_dump(mode="json", exclude_none=True)]
        return [anime_media.model_dump(mode="json", exclude_none=True)]

    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    results = await client.search_media("cowboy")

    assert [media.title for media in results] == ["Cowboy Bebop", "Your Name"]


@pytest.mark.asyncio
async def test_update_entry_uses_history_and_ratings_for_completed_movie(
    client: SimklClient,
    movie_media: SimklMedia,
    entry_state_factory: Callable[[int, str], SimklListEntryState],
) -> None:
    """Completed movies should hit history first and ratings second."""
    simkl_id = movie_media.ids.canonical_simkl_id or 0
    calls: list[str] = []
    bodies: list[dict[str, object] | None] = []
    entry_state = entry_state_factory(simkl_id, "movies")
    entry_state.status = SimklListStatus.COMPLETED
    entry_state.user_rating = 90
    entry_state.started_at = datetime(2024, 2, 1, tzinfo=UTC)
    entry_state.finished_at = datetime(2024, 2, 2, tzinfo=UTC)

    async def fake_request(_method: str, path: str, **kwargs: Any) -> dict:
        calls.append(path)
        bodies.append(cast(dict[str, object] | None, kwargs.get("body")))
        return {}

    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    await client.update_media_entry(movie_media, entry_state)

    assert calls == ["/sync/history", "/sync/ratings"]
    assert bodies[0] == {
        "movies": [
            {
                "title": "Your Name",
                "year": 1998,
                "ids": {"simkl": 202, "slug": "your-name"},
                "status": "completed",
                "added_at": "2024-02-01T00:00:00Z",
                "watched_at": "2024-02-02T00:00:00Z",
            }
        ],
        "shows": [],
        "episodes": [],
    }
    assert bodies[1] == {
        "movies": [
            {
                "title": "Your Name",
                "year": 1998,
                "ids": {"simkl": 202, "slug": "your-name"},
                "rating": 9,
            }
        ],
        "shows": [],
        "episodes": [],
    }


@pytest.mark.asyncio
async def test_restore_list_preserves_started_at_backup_field(
    client: SimklClient,
    anime_media: SimklMedia,
) -> None:
    """Backup restore should preserve started_at but ignore repeats and finished_at."""
    backup = [
        {
            "media": anime_media.model_dump(mode="json", exclude_none=True),
            "kind": "anime",
            "status": "watching",
            "progress": 3,
            "repeats": 4,
            "started_at": "2025-10-21T11:32:04Z",
            "finished_at": "2025-10-22T11:32:04Z",
        }
    ]
    updated_entry_states: list[SimklListEntryState] = []

    async def fake_refresh(*_args: Any, **_kwargs: Any) -> None:
        client._list_entry_cache.clear()
        client._media_cache.clear()
        client._media_kind_cache.clear()
        for ids in client._kind_media_ids.values():
            ids.clear()

    async def fake_update_entry(
        _media: SimklMedia, entry_state: SimklListEntryState
    ) -> None:
        updated_entry_states.append(entry_state)

    client.refresh_user_list = fake_refresh  # ty: ignore[invalid-assignment]
    cast(Any, client).update_media_entry = fake_update_entry

    await client.restore_list(json.dumps(backup))

    assert len(updated_entry_states) == 1
    assert updated_entry_states[0].started_at == datetime(
        2025, 10, 21, 11, 32, 4, tzinfo=UTC
    )
    assert updated_entry_states[0].finished_at is None
    assert updated_entry_states[0].repeats is None


@pytest.mark.asyncio
async def test_update_entry_ignores_completed_movie_finished_at(
    client: SimklClient,
    movie_media: SimklMedia,
    movie_entry: SimklListItem,
    entry_state_factory: Callable[[int, str], SimklListEntryState],
) -> None:
    """Completed movie mutations should ignore finished_at values."""
    simkl_id = movie_media.ids.canonical_simkl_id or 0
    client._list_entry_cache[simkl_id] = movie_entry
    calls: list[str] = []
    bodies: list[dict[str, object] | None] = []
    entry_state = entry_state_factory(simkl_id, "movies")
    entry_state.status = SimklListStatus.COMPLETED
    entry_state.user_rating = 90
    entry_state.started_at = datetime(2024, 2, 1, tzinfo=UTC)
    entry_state.finished_at = datetime(2024, 2, 5, tzinfo=UTC)

    async def fake_request(_method: str, path: str, **kwargs: Any) -> dict:
        calls.append(path)
        bodies.append(cast(dict[str, object] | None, kwargs.get("body")))
        return {}

    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    await client.update_media_entry(movie_media, entry_state)

    assert calls == ["/sync/history", "/sync/ratings"]
    assert bodies[0] == {
        "movies": [
            {
                "title": "Your Name",
                "year": 1998,
                "ids": {"simkl": 202, "slug": "your-name"},
                "status": "completed",
                "added_at": "2024-02-01T00:00:00Z",
                "watched_at": "2024-02-05T00:00:00Z",
            }
        ],
        "shows": [],
        "episodes": [],
    }


@pytest.mark.asyncio
async def test_update_entry_adjusts_anime_progress(
    client: SimklClient,
    anime_media: SimklMedia,
    anime_entry: SimklListItem,
    entry_state_factory: Callable[[int, str], SimklListEntryState],
) -> None:
    """Anime progress changes should translate into add/remove history calls."""
    simkl_id = anime_media.ids.canonical_simkl_id or 0
    client._list_entry_cache[simkl_id] = anime_entry
    calls: list[str] = []
    bodies: list[dict[str, object] | None] = []
    entry_state = entry_state_factory(simkl_id, "anime")
    entry_state.status = SimklListStatus.WATCHING
    entry_state.progress = 1

    async def fake_request(_method: str, path: str, **kwargs: Any) -> dict:
        calls.append(path)
        bodies.append(cast(dict[str, object] | None, kwargs.get("body")))
        return {}

    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    await client.update_media_entry(anime_media, entry_state)

    assert calls == [
        "/sync/add-to-list",
        "/sync/history/remove",
        "/sync/ratings/remove",
    ]
    assert bodies[0] == {
        "movies": [],
        "shows": [
            {
                "title": "Cowboy Bebop",
                "year": 1998,
                "ids": {"simkl": 101, "slug": "cowboy-bebop"},
                "to": "watching",
            }
        ],
        "episodes": [],
    }


@pytest.mark.asyncio
async def test_update_entry_updates_anime_started_at_in_place(
    client: SimklClient,
    anime_media: SimklMedia,
    anime_entry: SimklListItem,
    entry_state_factory: Callable[[int, str], SimklListEntryState],
) -> None:
    """Anime started_at changes should be handled through add-to-list only."""
    simkl_id = anime_media.ids.canonical_simkl_id or 0
    client._list_entry_cache[simkl_id] = anime_entry
    calls: list[str] = []
    bodies: list[dict[str, object] | None] = []
    entry_state = entry_state_factory(simkl_id, "anime")
    entry_state.status = SimklListStatus.WATCHING
    entry_state.progress = 3
    entry_state.started_at = datetime(2024, 1, 2, tzinfo=UTC)
    entry_state.user_rating = 80

    async def fake_request(_method: str, path: str, **kwargs: Any) -> dict:
        calls.append(path)
        bodies.append(cast(dict[str, object] | None, kwargs.get("body")))
        return {}

    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    await client.update_media_entry(anime_media, entry_state)

    assert calls == ["/sync/add-to-list", "/sync/ratings"]
    assert bodies[1] == {
        "movies": [],
        "shows": [
            {
                "title": "Cowboy Bebop",
                "year": 1998,
                "ids": {"simkl": 101, "slug": "cowboy-bebop"},
                "rating": 8,
            }
        ],
        "episodes": [],
    }


@pytest.mark.asyncio
async def test_update_entry_marks_added_anime_episodes_with_dont_remember_date(
    client: SimklClient,
    anime_media: SimklMedia,
    anime_entry: SimklListItem,
    entry_state_factory: Callable[[int, str], SimklListEntryState],
) -> None:
    """Added anime episodes should use Simkl's don't-remember watched date."""
    simkl_id = anime_media.ids.canonical_simkl_id or 0
    client._list_entry_cache[simkl_id] = anime_entry
    calls: list[str] = []
    bodies: list[dict[str, object] | None] = []
    entry_state = entry_state_factory(simkl_id, "anime")
    entry_state.status = SimklListStatus.WATCHING
    entry_state.progress = 5
    entry_state.finished_at = datetime(2024, 1, 4, tzinfo=UTC)

    async def fake_request(_method: str, path: str, **kwargs: Any) -> dict:
        calls.append(path)
        bodies.append(cast(dict[str, object] | None, kwargs.get("body")))
        return {}

    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    await client.update_media_entry(anime_media, entry_state)

    assert calls == [
        "/sync/add-to-list",
        "/sync/history",
        "/sync/ratings/remove",
    ]
    assert bodies[1] == {
        "movies": [],
        "shows": [
            {
                "title": "Cowboy Bebop",
                "year": 1998,
                "ids": {"simkl": 101, "slug": "cowboy-bebop"},
                "episodes": [
                    {"number": 4, "watched_at": "1970-01-01T00:00:00Z"},
                    {"number": 5, "watched_at": "1970-01-01T00:00:00Z"},
                ],
            }
        ],
        "episodes": [],
    }


@pytest.mark.asyncio
async def test_update_entry_uses_movie_payload_for_anime_movies(
    client: SimklClient,
    anime_media: SimklMedia,
    entry_state_factory: Callable[[int, str], SimklListEntryState],
) -> None:
    """Anime movies should mutate through movie payloads, not show payloads."""
    anime_movie = anime_media.model_copy(update={"anime_type": "movie"})
    simkl_id = anime_movie.ids.canonical_simkl_id or 0
    calls: list[str] = []
    bodies: list[dict[str, object] | None] = []
    entry_state = entry_state_factory(simkl_id, "anime")
    entry_state.status = SimklListStatus.COMPLETED
    entry_state.started_at = datetime(2024, 9, 20, 21, 33, 25, tzinfo=UTC)
    entry_state.finished_at = datetime(2024, 9, 20, 21, 33, 26, tzinfo=UTC)

    async def fake_request(_method: str, path: str, **kwargs: Any) -> dict:
        calls.append(path)
        bodies.append(cast(dict[str, object] | None, kwargs.get("body")))
        return {}

    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    await client.update_media_entry(anime_movie, entry_state)

    assert calls == ["/sync/history"]
    assert bodies[0] == {
        "movies": [
            {
                "title": "Cowboy Bebop",
                "year": 1998,
                "ids": {"simkl": 101, "slug": "cowboy-bebop"},
                "status": "completed",
                "added_at": "2024-09-20T21:33:25Z",
                "watched_at": "2024-09-20T21:33:26Z",
            }
        ],
        "shows": [],
        "episodes": [],
    }


@pytest.mark.asyncio
async def test_delete_entry_uses_movie_payload_for_anime_movies(
    client: SimklClient,
    anime_media: SimklMedia,
) -> None:
    """Anime movies should be removed through movie payloads."""
    anime_movie = anime_media.model_copy(update={"anime_type": "movie"})
    bodies: list[dict[str, object] | None] = []

    async def fake_request(_method: str, path: str, **kwargs: Any) -> dict:
        assert path == "/sync/history/remove"
        bodies.append(cast(dict[str, object] | None, kwargs.get("body")))
        return {}

    client._make_request = fake_request  # ty: ignore[invalid-assignment]

    await client.delete_media_entry(anime_movie, SimklMediaKind.ANIME)

    assert bodies == [
        {
            "movies": [
                {
                    "title": "Cowboy Bebop",
                    "year": 1998,
                    "ids": {"simkl": 101, "slug": "cowboy-bebop"},
                }
            ],
            "shows": [],
            "episodes": [],
        }
    ]
