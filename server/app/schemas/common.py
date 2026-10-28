from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def normalize_uuid(value: object) -> str:
    """Validate JSON/header UUIDs while keeping SQLAlchemy String binds strings."""
    if not isinstance(value, str):
        raise ValueError("UUID must be a string")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid UUID") from exc


UUIDString = Annotated[str, BeforeValidator(normalize_uuid)]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ErrorBody(ApiModel):
    code: str
    message: str
    request_id: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    error: ErrorBody


class UserOut(ApiModel):
    id: str
    email: str
    display_name: str | None = None
    role: str
    status: str
    locale: str
    created_at: datetime
    last_login_at: datetime | None = None


class SettingsOut(ApiModel):
    history_retention_days: int | None
    learning_enabled: bool
    locale: str
    version: int


class DeviceOut(ApiModel):
    id: str
    installation_id: str
    name: str
    platform: str
    client_version: str
    last_seen_at: datetime
    created_at: datetime
    is_current: bool = False


class TokenPair(ApiModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = 900
    refresh_expires_at: datetime
    user: UserOut


class RegisterIn(ApiModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(ApiModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    client_type: str = "desktop"
    device: "DeviceIn | None" = None


class DeviceIn(ApiModel):
    installation_id: UUIDString
    name: str = Field(min_length=1, max_length=64)
    platform: str
    client_version: str = Field(min_length=1, max_length=32)


class RefreshIn(ApiModel):
    refresh_token: str = Field(min_length=16, max_length=256)


class DisplayNameIn(ApiModel):
    display_name: str | None = Field(default=None, max_length=64)


class SettingsPatchIn(ApiModel):
    version: int = Field(ge=1)
    history_retention_days: int | None = None
    learning_enabled: bool | None = None
    locale: str | None = None


LoginIn.model_rebuild()
