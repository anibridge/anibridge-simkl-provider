"""Simkl list provider for AniBridge."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from anibridge.list import (
    ListEntry,
    ListMedia,
    ListMediaType,
    ListProvider,
    ListStatus,
    ListTarget,
    ListUser,
)
from anibridge.utils.types import ProviderLogger

from anibridge.providers.list.simkl.client import SimklClient
from anibridge.providers.list.simkl.config import SimklListProviderConfig
from anibridge.providers.list.simkl.models import (
    SimklListEntryState,
    SimklListItem,
    SimklListStatus,
    SimklMedia,
    SimklMediaKind,
)

__all__ = ["SimklListProvider"]

_SIMKL_TO_LIST = {
    SimklListStatus.COMPLETED: ListStatus.COMPLETED,
    SimklListStatus.WATCHING: ListStatus.CURRENT,
    SimklListStatus.DROPPED: ListStatus.DROPPED,
    SimklListStatus.HOLD: ListStatus.PAUSED,
    SimklListStatus.PLANTOWATCH: ListStatus.PLANNING,
}

_LIST_TO_SIMKL = {
    ListStatus.COMPLETED: SimklListStatus.COMPLETED,
    ListStatus.CURRENT: SimklListStatus.WATCHING,
    ListStatus.DROPPED: SimklListStatus.DROPPED,
    ListStatus.PAUSED: SimklListStatus.HOLD,
    ListStatus.PLANNING: SimklListStatus.PLANTOWATCH,
    ListStatus.REPEATING: SimklListStatus.WATCHING,
}


def _is_movie_like(media: SimklMedia, kind: SimklMediaKind) -> bool:
    return kind is SimklMediaKind.MOVIES or media.anime_type == "movie"


class SimklListProvider(ListProvider):
    """List provider implementation backed by Simkl's REST API."""

    NAMESPACE = "simkl"
    MAPPING_PROVIDERS = frozenset({"simkl", "anidb", "tmdb_movie", "tvdb_show"})

    def __init__(self, *, logger: ProviderLogger, config: dict | None = None) -> None:
        """Initialize the Simkl list provider."""
        super().__init__(logger=logger, config=config)
        self.parsed_config = SimklListProviderConfig.model_validate(config or {})
        self._client = SimklClient(
            client_id=self.parsed_config.client_id,
            token=self.parsed_config.token,
            logger=self.log,
            rate_limit=self.parsed_config.rate_limit,
        )
        self._user: ListUser | None = None

    async def initialize(self) -> None:
        """Initialize the underlying Simkl client and cache the current user."""
        self.log.debug("Initializing Simkl provider client")
        await self._client.initialize()
        if self._client.user is None:
            raise RuntimeError(
                "Failed to fetch Simkl user during provider initialization"
            )
        self._user = ListUser(
            key=str(self._client.user.account.id),
            title=self._client.user.user.name,
        )
        self.log.debug("Simkl provider initialized for user id=%s", self._user.key)

    async def backup_list(self) -> str:
        """Return a JSON backup of the current Simkl list."""
        return await self._client.backup_list()

    async def restore_list(self, backup: str) -> None:
        """Restore the Simkl list from a JSON backup."""
        await self._client.restore_list(backup)

    async def delete_entry(self, key: str) -> None:
        """Delete a Simkl entry by media key."""
        media = await self._client.get_media(int(key))
        if media is None:
            return
        kind = (
            self._client.get_media_kind(int(key))
            or media.endpoint_type
            or SimklMediaKind.SHOWS
        )
        await self._client.delete_media_entry(media, kind)

    async def get_entry(self, key: str) -> SimklListEntry | None:
        """Return the current entry state for a Simkl media key."""
        media = await self._client.get_media(int(key))
        if media is None:
            return None
        entry_state = self._build_entry_state(
            media, self._client.get_list_entry(int(key))
        )
        return SimklListEntry(self, media=media, entry_state=entry_state)

    async def resolve_mapping_descriptors(
        self, descriptors: Sequence[tuple[str, str, str | None]]
    ) -> Sequence[ListTarget]:
        """Resolve external mapping descriptors into Simkl media keys."""
        resolved: list[ListTarget] = []
        for provider, entry_id, scope in descriptors:
            media_key = await self._client.resolve_media_id(provider, entry_id, scope)
            if media_key is None:
                continue
            resolved.append(
                ListTarget(descriptor=(provider, entry_id, scope), media_key=media_key)
            )
        return resolved

    async def search(self, query: str) -> Sequence[SimklListEntry]:
        """Search Simkl and adapt the results into AniBridge entries."""
        media_results = await self._client.search_media(query)
        return [
            SimklListEntry(
                self,
                media=media,
                entry_state=self._build_entry_state(
                    media,
                    self._client.get_list_entry(media.ids.canonical_simkl_id or 0),
                ),
            )
            for media in media_results
        ]

    async def update_entry(self, key: str, entry: ListEntry) -> SimklListEntry | None:
        """Update a Simkl entry using the normalized AniBridge state."""
        current = await self.get_entry(key)
        if current is None:
            return None

        updated_entry = cast(SimklListEntry, entry)
        entry_state = SimklListEntryState(
            media_id=current._entry_state.media_id,
            kind=current._entry_state.kind,
            status=_LIST_TO_SIMKL.get(updated_entry.status)
            if updated_entry.status is not None
            else None,
            progress=updated_entry.progress,
            user_rating=updated_entry.user_rating,
            started_at=updated_entry.started_at,
            finished_at=(
                updated_entry.finished_at
                if _is_movie_like(current.media()._media, current._entry_state.kind)
                else None
            ),
            review=updated_entry.review,
        )
        await self._client.update_media_entry(current.media()._media, entry_state)
        return await self.get_entry(key)

    def user(self) -> ListUser | None:
        """Return the authenticated Simkl user, if initialized."""
        return self._user

    async def clear_cache(self) -> None:
        """Clear any cached Simkl state held by the provider."""
        self._client.clear_cache()
        self.log.debug("Cleared Simkl provider cache")

    async def close(self) -> None:
        """Close the underlying Simkl client session."""
        await self._client.close()
        self.log.debug("Closed Simkl provider client")

    def _build_entry_state(
        self, media: SimklMedia, entry: SimklListItem | None
    ) -> SimklListEntryState:
        simkl_id = media.ids.canonical_simkl_id
        if simkl_id is None:
            raise ValueError("Simkl media is missing a simkl id")
        kind = (
            media.endpoint_type
            or self._client.get_media_kind(simkl_id)
            or SimklMediaKind.SHOWS
        )
        progress = 0
        rating = None
        started_at = None
        finished_at = None
        status = None
        if entry is not None:
            status = entry.status
            rating = entry.user_rating * 10 if entry.user_rating is not None else None
            started_at = self._coerce_datetime(entry.added_to_watchlist_at)
            if _is_movie_like(media, kind):
                finished_at = self._coerce_finished_at(entry.last_watched_at)
            if kind is SimklMediaKind.MOVIES:
                progress = 1 if entry.status is SimklListStatus.COMPLETED else 0
            else:
                progress = entry.watched_episodes_count or 0

        return SimklListEntryState(
            media_id=simkl_id,
            kind=kind,
            status=status,
            progress=progress,
            user_rating=rating,
            started_at=started_at,
            finished_at=finished_at,
            review=None,
        )

    def _coerce_datetime(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(self._client.user_timezone)

    def _coerce_finished_at(self, value: datetime | None) -> datetime | None:
        coerced = self._coerce_datetime(value)
        if coerced == datetime(1970, 1, 1, tzinfo=UTC):
            return None
        return coerced


class SimklListMedia(ListMedia):
    """Simkl list media implementation."""

    def __init__(self, provider: SimklListProvider, media: SimklMedia) -> None:
        self._provider = provider
        self._media = media
        simkl_id = media.ids.canonical_simkl_id
        if simkl_id is None:
            raise ValueError("Simkl media is missing a simkl id")
        self._key = str(simkl_id)
        self._title = media.title

    @property
    def external_url(self) -> str | None:
        if self._media.url:
            return "https://simkl.com" + self._media.url
        simkl_id = self._media.ids.canonical_simkl_id
        slug = self._media.ids.slug or ""
        if simkl_id is None:
            return None
        match self._media.endpoint_type:
            case SimklMediaKind.MOVIES:
                return f"https://simkl.com/movies/{simkl_id}/{slug}".rstrip("/")
            case SimklMediaKind.ANIME:
                return f"https://simkl.com/anime/{simkl_id}/{slug}".rstrip("/")
            case _:
                return f"https://simkl.com/tv/{simkl_id}/{slug}".rstrip("/")

    @property
    def labels(self) -> Sequence[str]:
        labels: list[str] = []
        if self._media.year is not None:
            labels.append(str(self._media.year))
        if self._media.anime_type:
            labels.append(self._media.anime_type.title())
        elif self._media.endpoint_type is SimklMediaKind.MOVIES:
            labels.append("Movie")
        else:
            labels.append("TV")
        if self._media.status:
            labels.append(self._media.status.title())
        return labels

    @property
    def media_type(self) -> ListMediaType:
        return (
            ListMediaType.MOVIE
            if self._media.endpoint_type is SimklMediaKind.MOVIES
            else ListMediaType.TV
        )

    @property
    def total_units(self) -> int | None:
        if self.media_type == ListMediaType.MOVIE:
            return 1
        return self._media.total_episodes or self._media.ep_count

    @property
    def poster_image(self) -> str | None:
        if not self._media.poster:
            return None
        return f"https://wsrv.nl/?url=https://simkl.in/posters/{self._media.poster}_ca.webp"

    def provider(self) -> SimklListProvider:
        return self._provider


class SimklListEntry(ListEntry):
    """Simkl list entry implementation."""

    def __init__(
        self,
        provider: SimklListProvider,
        media: SimklMedia,
        entry_state: SimklListEntryState,
    ) -> None:
        self._provider = provider
        self._media = SimklListMedia(provider, media)
        self._entry_state = entry_state
        self._key = str(entry_state.media_id)
        self._title = media.title

    @property
    def progress(self) -> int | None:
        return self._entry_state.progress

    @progress.setter
    def progress(self, value: int | None) -> None:
        if value is None:
            self._entry_state.progress = None
            return
        if value < 0:
            raise ValueError("Progress cannot be negative.")
        self._entry_state.progress = value

    @property
    def repeats(self) -> int | None:
        return None

    @repeats.setter
    def repeats(self, value: int | None) -> None:
        return

    @property
    def review(self) -> str | None:
        return self._entry_state.review

    @review.setter
    def review(self, value: str | None) -> None:
        self._entry_state.review = value

    @property
    def status(self) -> ListStatus | None:
        if self._entry_state.status is None:
            return None
        return _SIMKL_TO_LIST.get(self._entry_state.status)

    @status.setter
    def status(self, value: ListStatus | None) -> None:
        if value is None:
            self._entry_state.status = None
            return
        if value not in _LIST_TO_SIMKL:
            raise ValueError(f"Unsupported list status: {value}")
        self._entry_state.status = _LIST_TO_SIMKL[value]

    @property
    def user_rating(self) -> int | None:
        return self._entry_state.user_rating

    @user_rating.setter
    def user_rating(self, value: int | None) -> None:
        if value is None:
            self._entry_state.user_rating = None
            return
        if value < 0 or value > 100:
            raise ValueError("Ratings must be between 0 and 100.")
        self._entry_state.user_rating = value

    @property
    def started_at(self) -> datetime | None:
        return self._entry_state.started_at

    @started_at.setter
    def started_at(self, value: datetime | None) -> None:
        if value is None:
            self._entry_state.started_at = None
            return
        if value.tzinfo is None:
            value = value.replace(tzinfo=self._provider._client.user_timezone)
        else:
            value = value.astimezone(self._provider._client.user_timezone)
        self._entry_state.started_at = value

    @property
    def finished_at(self) -> datetime | None:
        return self._entry_state.finished_at

    @finished_at.setter
    def finished_at(self, value: datetime | None) -> None:
        if not _is_movie_like(self._media._media, self._entry_state.kind):
            self._entry_state.finished_at = None
            return
        if value is None:
            self._entry_state.finished_at = None
            return
        if value.tzinfo is None:
            value = value.replace(tzinfo=self._provider._client.user_timezone)
        else:
            value = value.astimezone(self._provider._client.user_timezone)
        self._entry_state.finished_at = value

    def media(self) -> SimklListMedia:
        return self._media

    def provider(self) -> SimklListProvider:
        return self._provider
