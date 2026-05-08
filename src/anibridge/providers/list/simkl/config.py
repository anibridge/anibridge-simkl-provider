"""Simkl provider configuration."""

from typing import Annotated

import msgspec


class SimklListProviderConfig(msgspec.Struct, kw_only=True):
    """Configuration for the Simkl list provider."""

    token: Annotated[
        str,
        msgspec.Meta(description="Simkl access token."),
    ]
    client_id: Annotated[
        str,
        msgspec.Meta(description="Simkl client_id for authenticated API requests."),
    ] = "966b70652bf3ebbe46556dde9aa5a88e903790ae448c30b9866584743a6fc39e"
    rate_limit: (
        Annotated[
            int,
            msgspec.Meta(
                ge=1,
                description=(
                    "Maximum number of read requests per minute. "
                    "Use null to rely on the shared global default limit."
                ),
            ),
        ]
        | None
    ) = None
