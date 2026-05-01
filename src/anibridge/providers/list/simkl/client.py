"""Simkl REST client."""

import importlib.metadata
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import msgspec
from anibridge.utils.limiter import Limiter
from anibridge.utils.types import ProviderLogger

from anibridge.providers.list.simkl.models import (
    SimklActivities,
    SimklActivityGroup,
    SimklAllItems,
    SimklEpisodePayload,
    SimklItemPayload,
    SimklListBackupEntry,
    SimklListEntryState,
    SimklListItem,
    SimklListStatus,
    SimklMedia,
    SimklMediaKind,
    SimklMemo,
    SimklMutationBody,
    SimklMutationResponse,
    SimklSearchType,
    SimklUserSettings,
)

__all__ = ["SimklClient"]

global_simkl_read_limiter = Limiter(360 / 60, capacity=4)
global_simkl_write_limiter = Limiter(1, capacity=1)

_MEDIA_KINDS = (
    SimklMediaKind.SHOWS,
    SimklMediaKind.ANIME,
    SimklMediaKind.MOVIES,
)

_ACTIVITY_FIELD_BY_KIND = {
    SimklMediaKind.SHOWS: "tv_shows",
    SimklMediaKind.ANIME: "anime",
    SimklMediaKind.MOVIES: "movies",
}

_LIST_ACTIVITY_FIELDS = (
    SimklListStatus.PLANTOWATCH.value,
    SimklListStatus.WATCHING.value,
    SimklListStatus.COMPLETED.value,
    SimklListStatus.HOLD.value,
    SimklListStatus.DROPPED.value,
)

_DONT_REMEMBER_WATCHED_AT = datetime(1970, 1, 1, tzinfo=UTC)


class SimklClient:
    """Client for interacting with the Simkl REST API."""

    API_URL: ClassVar[str] = "https://api.simkl.com"

    _PROVIDER_TO_LOOKUP: ClassVar[dict[str, tuple[str, SimklSearchType]]] = {
        "anidb": ("anidb", SimklSearchType.ANIME),
        "tmdb_movie": ("tmdb", SimklSearchType.MOVIE),
        "tvdb_show": ("tvdb", SimklSearchType.TV),
    }

    def __init__(
        self,
        *,
        client_id: str,
        token: str,
        logger: ProviderLogger,
        rate_limit: int | None = None,
    ) -> None:
        """Initialize the Simkl client."""
        self.client_id = client_id
        self.token = token
        self.log = logger
        self.rate_limit = rate_limit
        self._session: aiohttp.ClientSession | None = None

        if rate_limit is None:
            self._read_limiter = global_simkl_read_limiter
        else:
            self._read_limiter = Limiter(rate_limit / 60, capacity=1)
        self._write_limiter = global_simkl_write_limiter

        self.user: SimklUserSettings | None = None
        self.user_timezone = UTC
        self._list_activities: SimklActivities | None = None
        self._list_entry_cache: dict[int, SimklListItem] = {}
        self._media_cache: dict[int, SimklMedia] = {}
        self._media_kind_cache: dict[int, SimklMediaKind] = {}
        self._kind_media_ids: dict[SimklMediaKind, set[int]] = {
            SimklMediaKind.SHOWS: set(),
            SimklMediaKind.ANIME: set(),
            SimklMediaKind.MOVIES: set(),
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session."""
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "simkl-api-key": self.client_id,
                "User-Agent": (
                    "anibridge-simkl-provider/"
                    + importlib.metadata.version("anibridge-simkl-provider")
                ),
            }
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    def clear_cache(self) -> None:
        """Clear cached activities, list entries, and media."""
        self._list_activities = None
        self._list_entry_cache.clear()
        self._media_cache.clear()
        self._media_kind_cache.clear()
        for ids in self._kind_media_ids.values():
            ids.clear()

    async def initialize(self) -> None:
        """Fetch the current user and warm the list cache."""
        self.clear_cache()
        self.user = await self.get_user()
        timezone_name = self.user.account.timezone
        if timezone_name:
            try:
                self.user_timezone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                self.user_timezone = UTC
        await self.refresh_user_list(force_full=True)

    async def get_user(self) -> SimklUserSettings:
        """Return the authenticated user's settings."""
        data = await self._make_request("POST", "/users/settings")
        return SimklUserSettings.model_validate(data)

    async def get_activities(self) -> SimklActivities:
        """Return activity timestamps for the authenticated user."""
        data = await self._make_request("GET", "/sync/activities")
        return SimklActivities.model_validate(data)

    async def refresh_user_list(self, *, force_full: bool = False) -> None:
        """Refresh the cached list state using the activities endpoint first."""
        activities = await self.get_activities()
        if force_full or self._list_activities is None:
            await self._refresh_user(activities)
            await self._full_sync_all()
            self._list_activities = activities
            return

        await self._refresh_user(activities)
        if not any(
            self._has_relevant_group_changes(kind, activities) for kind in _MEDIA_KINDS
        ):
            self._list_activities = activities
            return

        removed_kinds = tuple(
            kind for kind in _MEDIA_KINDS if self._removed_changed(kind, activities)
        )
        if removed_kinds:
            await self._reconcile_removed_kinds(removed_kinds)

        for kind in _MEDIA_KINDS:
            previous_group = self._get_activity_group(self._list_activities, kind)
            current_group = self._get_activity_group(activities, kind)
            if previous_group is None or current_group is None:
                continue

            if self._list_changed(previous_group, current_group):
                date_from = previous_group.all or self._list_activities.all
                await self._incremental_sync_kind(kind, date_from)
                continue

            if self._ratings_changed(previous_group, current_group):
                await self._incremental_sync_ratings(kind, previous_group.rated_at)

        self._list_activities = activities

    async def get_media(self, simkl_id: int) -> SimklMedia | None:
        """Return media metadata for a Simkl id."""
        await self.refresh_user_list()
        if media := self._media_cache.get(simkl_id):
            return media

        data = await self._make_request("GET", "/search/id", params={"simkl": simkl_id})
        results = [SimklMedia.model_validate(item) for item in data or []]
        if not results:
            return None
        media = results[0]
        normalized = self._normalize_media(
            self._get_media_kind_from_media(media), media
        )
        if normalized.ids.canonical_simkl_id is not None:
            self._media_cache[normalized.ids.canonical_simkl_id] = normalized
            self._media_kind_cache[normalized.ids.canonical_simkl_id] = (
                self._get_media_kind_from_media(normalized)
            )
        return normalized

    def get_list_entry(self, simkl_id: int) -> SimklListItem | None:
        """Return a cached list entry if one is present."""
        return self._list_entry_cache.get(simkl_id)

    def get_media_kind(self, simkl_id: int) -> SimklMediaKind | None:
        """Return the cached kind for a media id if present."""
        return self._media_kind_cache.get(simkl_id)

    async def search_media(self, query: str, *, limit: int = 10) -> list[SimklMedia]:
        """Search Simkl across anime, movies, and tv shows."""
        results: list[SimklMedia] = []
        seen: set[int] = set()

        for search_type in (
            SimklSearchType.ANIME,
            SimklSearchType.MOVIE,
            SimklSearchType.TV,
        ):
            payload = await self._make_request(
                "GET",
                f"/search/{search_type.value}",
                params={"q": query, "limit": limit},
            )
            for item in payload or []:
                media = self._normalize_media(
                    search_type, SimklMedia.model_validate(item)
                )
                simkl_id = media.ids.canonical_simkl_id
                if simkl_id is None or simkl_id in seen:
                    continue
                seen.add(simkl_id)
                self._media_cache[simkl_id] = media
                self._media_kind_cache[simkl_id] = self._get_media_kind_from_media(
                    media
                )
                results.append(media)
                if len(results) >= limit:
                    return results

        return results

    async def resolve_media_id(
        self, provider: str, entry_id: str, scope: str | None = None
    ) -> str | None:
        """Resolve an external descriptor to a Simkl media id."""
        if provider == "simkl":
            return entry_id

        if (lookup := self._PROVIDER_TO_LOOKUP.get(provider)) is None:
            return None
        normalized_provider, normalized_type = lookup

        params: dict[str, str | int] = {
            normalized_provider: entry_id,
            "type": normalized_type,
        }
        data = await self._make_request("GET", "/search/id", params=params)
        results = [SimklMedia.model_validate(item) for item in data or []]
        if not results:
            return None

        media = self._normalize_media(
            self._get_media_kind_from_media(results[0]), results[0]
        )
        simkl_id = media.ids.canonical_simkl_id
        if simkl_id is None:
            return None
        self._media_cache[simkl_id] = media
        self._media_kind_cache[simkl_id] = self._get_media_kind_from_media(media)
        return str(simkl_id)

    async def update_media_entry(
        self, media: SimklMedia, entry_state: SimklListEntryState
    ) -> None:
        """Apply a normalized entry state to Simkl."""
        current_entry = self._list_entry_cache.get(entry_state.media_id)
        mutation_kind = self._get_mutation_kind(media, entry_state.kind)
        if mutation_kind is SimklMediaKind.MOVIES:
            await self._update_movie_entry(media, entry_state, current_entry)
        else:
            await self._update_show_entry(media, entry_state, current_entry)

        current_rating = (
            current_entry.user_rating if current_entry is not None else None
        )
        desired_rating = self._rating_to_simkl(entry_state.user_rating)
        if desired_rating is None and current_rating is not None:
            await self._remove_rating(media, mutation_kind)
        elif desired_rating is not None:
            await self._set_rating(media, mutation_kind, desired_rating)

        self.clear_cache()

    async def delete_media_entry(self, media: SimklMedia, kind: SimklMediaKind) -> None:
        """Remove an item from Simkl history and watchlists."""
        await self._remove_entry(media, self._get_mutation_kind(media, kind))
        self.clear_cache()

    async def backup_list(self) -> str:
        """Serialize the current cached list state into JSON."""
        await self.refresh_user_list(force_full=True)
        items: list[SimklListBackupEntry] = []
        for simkl_id in sorted(self._list_entry_cache):
            entry = self._list_entry_cache[simkl_id]
            media = self._media_cache[simkl_id]
            kind = self._media_kind_cache[simkl_id]
            items.append(
                SimklListBackupEntry(
                    media=media,
                    kind=kind,
                    status=entry.status,
                    progress=self._list_entry_progress(kind, entry),
                    user_rating=(
                        entry.user_rating * 10
                        if entry.user_rating is not None
                        else None
                    ),
                    started_at=entry.added_to_watchlist_at,
                )
            )
        return msgspec.json.encode(
            [item.model_dump(mode="json", exclude_none=True) for item in items]
        ).decode()

    async def restore_list(self, backup: str) -> None:
        """Restore a Simkl backup produced by backup_list."""
        payload = msgspec.json.decode(backup)
        backups = [SimklListBackupEntry.model_validate(item) for item in payload]

        await self.refresh_user_list(force_full=True)
        desired_ids = {
            item.media.ids.canonical_simkl_id
            for item in backups
            if item.media.ids.canonical_simkl_id
        }

        for simkl_id, _entry in list(self._list_entry_cache.items()):
            if simkl_id not in desired_ids:
                media = self._media_cache.get(simkl_id)
                kind = self._media_kind_cache.get(simkl_id)
                if media is not None and kind is not None:
                    await self.delete_media_entry(media, kind)

        for item in backups:
            simkl_id = item.media.ids.canonical_simkl_id
            if simkl_id is None:
                continue
            await self.update_media_entry(
                item.media,
                SimklListEntryState(
                    media_id=simkl_id,
                    kind=item.kind,
                    status=item.status,
                    progress=item.progress,
                    user_rating=item.user_rating,
                    started_at=item.started_at,
                    review=item.review,
                ),
            )

    async def _full_sync_all(self) -> None:
        data = await self._make_request("GET", "/sync/all-items/")
        parsed = SimklAllItems.model_validate(data or {})
        for kind in _MEDIA_KINDS:
            self._replace_kind(kind, getattr(parsed, kind.value))

    async def _reconcile_removed_kinds(self, kinds: Sequence[SimklMediaKind]) -> None:
        data = await self._make_request(
            "GET",
            "/sync/all-items/",
            params={"extended": "simkl_ids_only"},
        )
        parsed = SimklAllItems.model_validate(data or {})
        for kind in kinds:
            self._remove_missing_kind_items(kind, getattr(parsed, kind.value))

    def _remove_missing_kind_items(
        self, kind: SimklMediaKind, items: Sequence[SimklListItem]
    ) -> None:
        current_ids = {
            simkl_id
            for item in items
            for simkl_id in [self._get_list_item_simkl_id(item)]
            if simkl_id is not None
        }
        for simkl_id in list(self._kind_media_ids[kind]):
            if simkl_id in current_ids:
                continue
            self._list_entry_cache.pop(simkl_id, None)
            self._media_cache.pop(simkl_id, None)
            self._media_kind_cache.pop(simkl_id, None)
            self._kind_media_ids[kind].discard(simkl_id)

    async def _incremental_sync_kind(
        self, kind: SimklMediaKind, date_from: datetime | None
    ) -> None:
        params: dict[str, str | int] = {}
        if date_from is not None:
            params["date_from"] = self._format_datetime(date_from)
        data = await self._make_request(
            "GET", f"/sync/all-items/{kind.value}/", params=params
        )
        parsed = SimklAllItems.model_validate(data or {})
        for item in getattr(parsed, kind.value):
            self._store_list_item(kind, item)

    async def _incremental_sync_ratings(
        self, kind: SimklMediaKind, date_from: datetime | None
    ) -> None:
        params: dict[str, str | int] = {}
        if date_from is not None:
            params["date_from"] = self._format_datetime(date_from)
        data = await self._make_request(
            "POST", f"/sync/ratings/{kind.value}", params=params
        )
        parsed = SimklAllItems.model_validate(data or {})
        for item in getattr(parsed, kind.value):
            simkl_id = self._get_list_item_simkl_id(item)
            if simkl_id is None:
                continue
            cached = self._list_entry_cache.get(simkl_id)
            if cached is None:
                self._store_list_item(kind, item)
                continue
            cached.user_rating = item.user_rating
            cached.user_rated_at = item.user_rated_at
            if item.status is not None:
                cached.status = item.status
            if item.last_watched_at is not None:
                cached.last_watched_at = item.last_watched_at
            if item.last_watched is not None:
                cached.last_watched = item.last_watched
            if item.next_to_watch is not None:
                cached.next_to_watch = item.next_to_watch

    async def _update_movie_entry(
        self,
        media: SimklMedia,
        entry_state: SimklListEntryState,
        current_entry: SimklListItem | None,
    ) -> None:
        status = self._get_list_status(entry_state.status, SimklListStatus.PLANTOWATCH)
        if status is SimklListStatus.COMPLETED:
            payload = SimklMutationBody(
                movies=[
                    self._build_item_payload(
                        media,
                        status=status,
                        started_at=entry_state.started_at,
                        finished_at=entry_state.finished_at,
                        review=entry_state.review,
                    )
                ]
            )
            await self._make_request(
                "POST",
                "/sync/history",
                body=payload.model_dump(mode="json", exclude_none=True),
            )
            return

        payload = SimklMutationBody(
            movies=[
                self._build_item_payload(
                    media,
                    to=status,
                    started_at=entry_state.started_at,
                    review=entry_state.review,
                )
            ]
        )
        await self._make_request(
            "POST",
            "/sync/add-to-list",
            body=payload.model_dump(mode="json", exclude_none=True),
        )

    async def _update_show_entry(
        self,
        media: SimklMedia,
        entry_state: SimklListEntryState,
        current_entry: SimklListItem | None,
    ) -> None:
        status = self._get_list_status(entry_state.status, SimklListStatus.WATCHING)
        current_progress = self._list_entry_progress(entry_state.kind, current_entry)

        if status is SimklListStatus.COMPLETED:
            payload = SimklMutationBody(
                shows=[
                    self._build_item_payload(
                        media,
                        status=status,
                        started_at=entry_state.started_at,
                        review=entry_state.review,
                    )
                ]
            )
            await self._make_request(
                "POST",
                "/sync/history",
                body=payload.model_dump(mode="json", exclude_none=True),
            )
            return

        list_payload = SimklMutationBody(
            shows=[
                self._build_item_payload(
                    media,
                    to=status,
                    started_at=entry_state.started_at,
                    review=entry_state.review,
                )
            ]
        )
        await self._make_request(
            "POST",
            "/sync/add-to-list",
            body=list_payload.model_dump(mode="json", exclude_none=True),
        )

        if self._get_media_kind(entry_state.kind) is not SimklMediaKind.ANIME:
            return

        target_progress = max(entry_state.progress or 0, 0)
        if target_progress > current_progress:
            add_payload = SimklMutationBody(
                shows=[
                    self._build_item_payload(
                        media,
                        episodes=self._episodes(
                            range(current_progress + 1, target_progress + 1),
                            watched_at=_DONT_REMEMBER_WATCHED_AT,
                        ),
                        review=entry_state.review,
                    )
                ]
            )
            await self._make_request(
                "POST",
                "/sync/history",
                body=add_payload.model_dump(mode="json", exclude_none=True),
            )
        elif target_progress < current_progress:
            remove_payload = SimklMutationBody(
                shows=[
                    self._build_item_payload(
                        media,
                        episodes=self._episodes(
                            range(target_progress + 1, current_progress + 1)
                        ),
                    )
                ]
            )
            await self._make_request(
                "POST",
                "/sync/history/remove",
                body=remove_payload.model_dump(mode="json", exclude_none=True),
            )

    async def _set_rating(
        self,
        media: SimklMedia,
        kind: SimklMediaKind,
        rating: int,
    ) -> None:
        payload = self._build_mutation_body(
            kind,
            [self._build_item_payload(media, rating=rating)],
        )
        response = await self._make_request(
            "POST",
            "/sync/ratings",
            body=payload.model_dump(mode="json", exclude_none=True),
        )
        SimklMutationResponse.model_validate(response or {})

    async def _remove_rating(self, media: SimklMedia, kind: SimklMediaKind) -> None:
        payload = self._build_mutation_body(kind, [self._build_item_payload(media)])
        await self._make_request(
            "POST",
            "/sync/ratings/remove",
            body=payload.model_dump(mode="json", exclude_none=True),
        )

    def _build_mutation_body(
        self, kind: SimklMediaKind, items: list[SimklItemPayload]
    ) -> SimklMutationBody:
        """Build a typed mutation body for movie or show payloads."""
        if kind is SimklMediaKind.MOVIES:
            return SimklMutationBody(movies=items)
        return SimklMutationBody(shows=items)

    def _build_item_payload(
        self,
        media: SimklMedia,
        *,
        to: SimklListStatus | None = None,
        status: SimklListStatus | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        rating: int | None = None,
        review: str | None = None,
        episodes: list[SimklEpisodePayload] | None = None,
    ) -> SimklItemPayload:
        return SimklItemPayload(
            title=media.title or None,
            year=media.year,
            ids=media.ids.request_ids(),
            to=to,
            status=status,
            added_at=started_at,
            watched_at=finished_at,
            rating=rating,
            memo=SimklMemo(text=review, is_private=False) if review else None,
            episodes=episodes,
        )

    async def _remove_entry(self, media: SimklMedia, kind: SimklMediaKind) -> None:
        payload = self._build_mutation_body(kind, [self._build_item_payload(media)])
        await self._make_request(
            "POST",
            "/sync/history/remove",
            body=payload.model_dump(mode="json", exclude_none=True),
        )

    def _replace_kind(
        self, kind: SimklMediaKind, items: Sequence[SimklListItem]
    ) -> None:
        for simkl_id in list(self._kind_media_ids[kind]):
            self._list_entry_cache.pop(simkl_id, None)
            self._media_cache.pop(simkl_id, None)
            self._media_kind_cache.pop(simkl_id, None)
        self._kind_media_ids[kind].clear()
        for item in items:
            self._store_list_item(kind, item)

    def _store_list_item(self, kind: SimklMediaKind, item: SimklListItem) -> None:
        media = item.movie if kind is SimklMediaKind.MOVIES else item.show
        if media is None:
            return
        normalized = self._normalize_media(kind, media, item)
        simkl_id = normalized.ids.canonical_simkl_id
        if simkl_id is None:
            return
        self._list_entry_cache[simkl_id] = item
        self._media_cache[simkl_id] = normalized
        self._media_kind_cache[simkl_id] = kind
        self._kind_media_ids[kind].add(simkl_id)

    def _get_list_item_simkl_id(self, item: SimklListItem) -> int | None:
        media = item.movie or item.show
        if media is None:
            return None
        return media.ids.canonical_simkl_id

    def _normalize_media(
        self,
        kind: SimklMediaKind | SimklSearchType | str,
        media: SimklMedia,
        item: SimklListItem | None = None,
    ) -> SimklMedia:
        normalized_kind = self._get_media_kind(kind)

        total = media.total_episodes or media.ep_count
        if item is not None and item.total_episodes_count is not None:
            total = item.total_episodes_count

        return media.model_copy(
            update={
                "endpoint_type": normalized_kind,
                "anime_type": (item.anime_type if item is not None else None)
                or media.anime_type,
                "total_episodes": total,
                "ep_count": total,
            }
        )

    def _list_entry_progress(
        self, kind: SimklMediaKind, entry: SimklListItem | None
    ) -> int:
        if entry is None:
            return 0
        if kind is SimklMediaKind.MOVIES:
            return (
                1
                if self._get_list_status(entry.status) is SimklListStatus.COMPLETED
                else 0
            )
        return entry.watched_episodes_count or 0

    def _episodes(
        self, numbers: range, watched_at: datetime | None = None
    ) -> list[SimklEpisodePayload]:
        return [
            SimklEpisodePayload(number=number, watched_at=watched_at)
            for number in numbers
        ]

    def _get_media_kind(
        self, kind: SimklMediaKind | SimklSearchType | str | None
    ) -> SimklMediaKind:
        if kind in {SimklMediaKind.MOVIES, SimklSearchType.MOVIE, "movie", "movies"}:
            return SimklMediaKind.MOVIES
        if kind in {SimklMediaKind.ANIME, SimklSearchType.ANIME, "anime"}:
            return SimklMediaKind.ANIME
        return SimklMediaKind.SHOWS

    def _get_mutation_kind(
        self,
        media: SimklMedia,
        fallback_kind: SimklMediaKind | SimklSearchType | str | None,
    ) -> SimklMediaKind:
        if media.anime_type == "movie":
            return SimklMediaKind.MOVIES
        return self._get_media_kind(media.endpoint_type or media.type or fallback_kind)

    def _get_list_status(
        self,
        status: SimklListStatus | str | None,
        default: SimklListStatus | None = None,
    ) -> SimklListStatus | None:
        if status is None:
            return default
        try:
            return SimklListStatus(status)
        except ValueError:
            return default

    def _get_media_kind_from_media(self, media: SimklMedia) -> SimklMediaKind:
        return self._get_media_kind(media.endpoint_type or media.type)

    def _removed_changed(
        self, kind: SimklMediaKind, activities: SimklActivities
    ) -> bool:
        previous_group = self._get_activity_group(self._list_activities, kind)
        current_group = self._get_activity_group(activities, kind)
        if previous_group is None or current_group is None:
            return False
        return previous_group.removed_from_list != current_group.removed_from_list

    def _has_relevant_group_changes(
        self, kind: SimklMediaKind, activities: SimklActivities
    ) -> bool:
        previous_group = self._get_activity_group(self._list_activities, kind)
        current_group = self._get_activity_group(activities, kind)
        if previous_group is None or current_group is None:
            return False
        return (
            self._list_changed(previous_group, current_group)
            or self._ratings_changed(previous_group, current_group)
            or previous_group.removed_from_list != current_group.removed_from_list
        )

    def _list_changed(
        self, previous_group: SimklActivityGroup, current_group: SimklActivityGroup
    ) -> bool:
        return any(
            getattr(previous_group, field) != getattr(current_group, field)
            for field in _LIST_ACTIVITY_FIELDS
        )

    def _ratings_changed(
        self, previous_group: SimklActivityGroup, current_group: SimklActivityGroup
    ) -> bool:
        return previous_group.rated_at != current_group.rated_at

    def _get_activity_group(
        self, activities: SimklActivities | None, kind: SimklMediaKind
    ) -> SimklActivityGroup | None:
        if activities is None:
            return None
        return getattr(activities, _ACTIVITY_FIELD_BY_KIND[kind])

    async def _refresh_user(self, activities: SimklActivities) -> None:
        previous_settings = (
            self._list_activities.settings if self._list_activities else None
        )
        current_settings = activities.settings
        if current_settings is None:
            return
        if self._list_activities is None and self.user is not None:
            return
        if self.user is not None and previous_settings == current_settings:
            return
        self.user = await self.get_user()
        timezone_name = self.user.account.timezone
        if timezone_name:
            try:
                self.user_timezone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                self.user_timezone = UTC
        else:
            self.user_timezone = UTC

    def _format_datetime(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _rating_to_simkl(self, value: int | None) -> int | None:
        if value is None:
            return None
        score = round(value / 10)
        if score <= 0:
            return None
        return min(max(score, 1), 10)

    async def _make_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        body: dict | None = None,
        expected_statuses: Sequence[int] = (200, 201, 204),
    ) -> dict | list | None:
        if method.upper() == "GET":
            await self._read_limiter.acquire(asynchronous=True)
        else:
            await self._write_limiter.acquire(asynchronous=True)

        session = await self._get_session()
        request_params: dict[str, str | int] = {
            "client_id": self.client_id,
            "app-name": "anibridge-simkl-provider",
            "app-version": importlib.metadata.version("anibridge-simkl-provider"),
        }
        if params:
            request_params.update(params)

        async with session.request(
            method.upper(),
            self.API_URL + path,
            params=request_params,
            json=body,
        ) as response:
            if response.status not in expected_statuses:
                text = await response.text()
                raise aiohttp.ClientError(
                    f"Simkl {method.upper()} {path} failed with "
                    f"{response.status}: {text}"
                )
            if response.status == 204:
                return None
            text = await response.text()
            if not text:
                return None
            return msgspec.json.decode(text)
