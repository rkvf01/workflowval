"""The auth config file (FR-2). It names env vars and never holds a secret value (NFR-7)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from workflowval.schemas._base import Strict

EnvVarName = Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]*$")]


class ApiKeyAuth(Strict):
    """An API key sent in a header or query parameter."""

    type: Literal["api_key"]
    name: str = Field(min_length=1, description="Header or query parameter name")
    location: Literal["header", "query"]
    value_env: EnvVarName


class BearerAuth(Strict):
    """A bearer token."""

    type: Literal["bearer"]
    token_env: EnvVarName


class BasicAuth(Strict):
    """HTTP basic auth."""

    type: Literal["basic"]
    username_env: EnvVarName
    password_env: EnvVarName


class LoginAuth(Strict):
    """Call a login operation, extract a token, reuse it and refresh it on 401.

    Leave `token_header` and `token_prefix` out and the agent works out how to send the token, from
    the spec's security section, then the docs, then the login response and what the API accepts.
    Values set here always win. The agent never sees the secret values.
    """

    type: Literal["login"]
    op: str = Field(min_length=1, description="Login operationId")
    body_env: dict[str, EnvVarName] = Field(
        description="Request body field to env var name. `user.email` nests the field.",
    )
    token_path: str = Field(pattern=r"^\$", description="JSONPath of the token in the response")
    token_header: str | None = Field(default=None, description="Override, e.g. Cookie")
    token_prefix: str | None = Field(default=None, description="Override, e.g. token=")
    refresh_on_401: bool = True


AuthScheme = Annotated[
    ApiKeyAuth | BearerAuth | BasicAuth | LoginAuth,
    Field(discriminator="type"),
]


class AuthConfig(Strict):
    """Top level of the auth config file."""

    scheme: AuthScheme
