"""Minimal Simkl API models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SimklModel(BaseModel):
    """Base model for Simkl API payloads."""

    model_config = ConfigDict(extra="ignore")


class SimklMediaKind(StrEnum):
    """Normalized Simkl list buckets used by sync endpoints."""

    SHOWS = "shows"
    ANIME = "anime"
    MOVIES = "movies"


class SimklSearchType(StrEnum):
    """Search endpoint types returned by Simkl lookup APIs."""

    ANIME = "anime"
    MOVIE = "movie"
    TV = "tv"


class SimklListStatus(StrEnum):
    """List statuses accepted by Simkl list mutation endpoints."""

    COMPLETED = "completed"
    WATCHING = "watching"
    DROPPED = "dropped"
    HOLD = "hold"
    PLANTOWATCH = "plantowatch"


class SimklIds(SimklModel):
    """Identifiers returned by or sent to Simkl."""

    simkl: int | None = None
    simkl_id: int | None = None
    slug: str | None = None
    imdb: str | None = None
    tmdb: int | str | None = None
    tvdb: int | str | None = None
    mal: int | str | None = None
    anidb: int | str | None = None
    anilist: int | str | None = None

    @property
    def canonical_simkl_id(self) -> int | None:
        """Return the canonical Simkl id regardless of response shape."""
        return self.simkl or self.simkl_id

    def request_ids(self) -> SimklIds:
        """Return request-friendly ids using the Simkl field name."""
        return SimklIds(
            simkl=self.canonical_simkl_id,
            slug=self.slug,
            imdb=self.imdb,
            tmdb=self.tmdb,
            tvdb=self.tvdb,
            mal=self.mal,
            anidb=self.anidb,
            anilist=self.anilist,
        )


class SimklUser(SimklModel):
    """Authenticated user details."""

    name: str = ""


class SimklAccount(SimklModel):
    """Authenticated account details."""

    id: int
    timezone: str | None = None
    type: str | None = None


class SimklUserSettings(SimklModel):
    """Response from /users/settings."""

    user: SimklUser
    account: SimklAccount


class SimklActivityGroup(SimklModel):
    """Activity timestamps for one media bucket."""

    all: datetime | None = None
    rated_at: datetime | None = None
    playback: datetime | None = None
    plantowatch: datetime | None = None
    watching: datetime | None = None
    completed: datetime | None = None
    hold: datetime | None = None
    dropped: datetime | None = None
    removed_from_list: datetime | None = None


class SimklSettingsActivity(SimklModel):
    """Settings activity timestamps."""

    all: datetime | None = None


class SimklActivities(SimklModel):
    """Response from /sync/activities."""

    all: datetime | None = None
    settings: SimklSettingsActivity | None = None
    tv_shows: SimklActivityGroup | None = None
    anime: SimklActivityGroup | None = None
    movies: SimklActivityGroup | None = None


class SimklMedia(SimklModel):
    """Common media shape used across search and sync responses."""

    title: str = ""
    year: int | None = None
    poster: str | None = None
    ids: SimklIds = Field(default_factory=SimklIds)
    url: str | None = None
    endpoint_type: SimklMediaKind | None = None
    type: SimklSearchType | None = None
    anime_type: str | None = None
    status: str | None = None
    total_episodes: int | None = None
    ep_count: int | None = None
    runtime: int | None = None


class SimklListItem(SimklModel):
    """Entry returned by /sync/all-items."""

    added_to_watchlist_at: datetime | None = None
    last_watched_at: datetime | None = None
    user_rated_at: datetime | None = None
    user_rating: int | None = None
    status: SimklListStatus | None = None
    last_watched: str | None = None
    next_to_watch: str | None = None
    watched_episodes_count: int | None = None
    total_episodes_count: int | None = None
    not_aired_episodes_count: int | None = None
    anime_type: str | None = None
    show: SimklMedia | None = None
    movie: SimklMedia | None = None


class SimklAllItems(SimklModel):
    """Response from /sync/all-items."""

    shows: list[SimklListItem] = Field(default_factory=list)
    anime: list[SimklListItem] = Field(default_factory=list)
    movies: list[SimklListItem] = Field(default_factory=list)


class SimklMemo(SimklModel):
    """Optional memo payload."""

    text: str
    is_private: bool = False


class SimklEpisodePayload(SimklModel):
    """Episode payload for history updates."""

    number: int
    watched_at: datetime | None = None


class SimklItemPayload(SimklModel):
    """Movie/show payload for Simkl mutation endpoints."""

    title: str | None = None
    year: int | str | None = None
    ids: SimklIds
    to: SimklListStatus | None = None
    status: SimklListStatus | None = None
    added_at: datetime | None = None
    watched_at: datetime | None = None
    rated_at: datetime | None = None
    rating: int | None = None
    memo: SimklMemo | None = None
    episodes: list[SimklEpisodePayload] | None = None


class SimklMutationBody(SimklModel):
    """Request body for Simkl list mutations."""

    shows: list[SimklItemPayload] = Field(default_factory=list)
    movies: list[SimklItemPayload] = Field(default_factory=list)
    episodes: list[SimklEpisodePayload] = Field(default_factory=list)


class SimklMutationStatusResponse(SimklModel):
    """Per-item status returned by mutation endpoints."""

    status: SimklListStatus | None = None
    simkl_type: str | None = None
    anime_type: str | None = None


class SimklMutationStatus(SimklModel):
    """Per-item mutation result."""

    request: SimklItemPayload | None = None
    response: SimklMutationStatusResponse | None = None


class SimklMutationAdded(SimklModel):
    """Mutation result summary."""

    movies: list[SimklItemPayload] | int | None = None
    shows: list[SimklItemPayload] | int | None = None
    episodes: list[SimklEpisodePayload] | int | None = None
    statuses: list[SimklMutationStatus] = Field(default_factory=list)


class SimklMutationNotFound(SimklModel):
    """Mutation not-found section."""

    movies: list[SimklItemPayload] = Field(default_factory=list)
    shows: list[SimklItemPayload] = Field(default_factory=list)
    episodes: list[SimklEpisodePayload] = Field(default_factory=list)


class SimklMutationResponse(SimklModel):
    """Mutation response envelope."""

    added: SimklMutationAdded | None = None
    not_found: SimklMutationNotFound | None = None


class SimklListEntryState(SimklModel):
    """Provider-facing normalized entry state."""

    media_id: int
    kind: SimklMediaKind
    status: SimklListStatus | None = None
    progress: int | None = None
    repeats: int | None = None
    user_rating: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    review: str | None = None


class SimklListBackupEntry(SimklModel):
    """Serialized backup entry."""

    media: SimklMedia
    kind: SimklMediaKind
    status: SimklListStatus | None = None
    progress: int | None = None
    repeats: int | None = None
    user_rating: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    review: str | None = None
