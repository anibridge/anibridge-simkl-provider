"""Shared test doubles for the Simkl provider tests."""

import json
from dataclasses import dataclass, field
from datetime import UTC

from anibridge.providers.list.simkl.models import (
    SimklAccount,
    SimklListEntryState,
    SimklListItem,
    SimklMedia,
    SimklMediaKind,
    SimklUser,
    SimklUserSettings,
)


@dataclass
class FakeSimklClient:
    """Minimal asynchronous Simkl client stand-in used by provider tests."""

    medias: dict[int, SimklMedia] = field(default_factory=dict)
    entries: dict[int, SimklListItem] = field(default_factory=dict)
    search_results: list[SimklMedia] = field(default_factory=list)
    user: SimklUserSettings = field(
        default_factory=lambda: SimklUserSettings(
            user=SimklUser(name="Test User"),
            account=SimklAccount(id=99, timezone="UTC", type="free"),
        )
    )

    def __post_init__(self) -> None:
        self.user_timezone = UTC
        self.updated_entry_states: list[SimklListEntryState] = []
        self.deleted_media_ids: list[tuple[int, SimklMediaKind]] = []
        self.resolved: dict[tuple[str, str, str | None], str | None] = {}
        self.last_restore: str | None = None
        if not self.search_results:
            self.search_results = list(self.medias.values())

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def clear_cache(self) -> None:
        return None

    async def backup_list(self) -> str:
        payload = []
        for simkl_id, media in self.medias.items():
            item = self.entries.get(simkl_id)
            payload.append(
                {
                    "media": media.model_dump(mode="json", exclude_none=True),
                    "kind": media.endpoint_type,
                    "status": item.status if item else None,
                    "progress": item.watched_episodes_count if item else 0,
                    "user_rating": (
                        item.user_rating * 10
                        if item and item.user_rating is not None
                        else None
                    ),
                    "started_at": item.added_to_watchlist_at if item else None,
                }
            )
        return json.dumps(payload)

    async def restore_list(self, backup: str) -> None:
        self.last_restore = backup

    async def get_media(self, simkl_id: int) -> SimklMedia | None:
        return self.medias.get(simkl_id)

    def get_list_entry(self, simkl_id: int) -> SimklListItem | None:
        return self.entries.get(simkl_id)

    def get_media_kind(self, simkl_id: int) -> SimklMediaKind | None:
        media = self.medias.get(simkl_id)
        return media.endpoint_type if media else None

    async def search_media(self, query: str, *, limit: int = 10) -> list[SimklMedia]:
        lowered = query.lower()
        return [
            media for media in self.search_results if lowered in media.title.lower()
        ][:limit]

    async def resolve_media_id(
        self, provider: str, entry_id: str, scope: str | None = None
    ) -> str | None:
        return self.resolved.get((provider, entry_id, scope))

    async def update_media_entry(
        self, media: SimklMedia, entry_state: SimklListEntryState
    ) -> None:
        self.updated_entry_states.append(entry_state)
        item = self.entries.get(entry_state.media_id)
        if item is not None:
            item.status = entry_state.status
            item.watched_episodes_count = entry_state.progress
            item.added_to_watchlist_at = entry_state.started_at
            item.user_rating = (
                None
                if entry_state.user_rating is None
                else round(entry_state.user_rating / 10)
            )

    async def delete_media_entry(self, media: SimklMedia, kind: SimklMediaKind) -> None:
        simkl_id = media.ids.canonical_simkl_id
        if simkl_id is None:
            return
        self.deleted_media_ids.append((simkl_id, kind))
        self.entries.pop(simkl_id, None)
