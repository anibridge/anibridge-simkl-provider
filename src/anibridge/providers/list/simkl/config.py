"""Simkl provider configuration."""

from pydantic import BaseModel, Field


class SimklListProviderConfig(BaseModel):
    """Configuration for the Simkl list provider."""

    token: str = Field(default=..., description="Simkl access token.")
    client_id: str = Field(
        default="966b70652bf3ebbe46556dde9aa5a88e903790ae448c30b9866584743a6fc39e",
        description="Simkl client_id for authenticated API requests.",
    )
    rate_limit: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Maximum number of read requests per minute. "
            "Use null to rely on the shared global default limit."
        ),
    )
