from datetime import UTC, datetime

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.models import CancelReason, EventStatus, RegistrationStatus, Role


def _require_future(value: datetime | None) -> datetime | None:
    if value is not None and value <= datetime.now(UTC):
        raise ValueError("starts_at must be in the future")
    return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=120)
    description: str = Field(default="", max_length=4000)
    location: str = Field(min_length=1, max_length=200)
    starts_at: AwareDatetime
    capacity: int = Field(ge=1, le=10000)

    @field_validator("starts_at")
    @classmethod
    def starts_in_future(cls, value: datetime) -> datetime:
        return _require_future(value)


class EventUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=3, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    location: str | None = Field(default=None, min_length=1, max_length=200)
    starts_at: AwareDatetime | None = None
    capacity: int | None = Field(default=None, ge=1, le=10000)

    @field_validator("starts_at")
    @classmethod
    def starts_in_future(cls, value: datetime | None) -> datetime | None:
        return _require_future(value)

    @model_validator(mode="after")
    def no_explicit_nulls(self) -> "EventUpdate":
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    role: Role


class EventPublic(BaseModel):
    id: int
    title: str
    description: str
    location: str
    starts_at: datetime
    status: EventStatus
    capacity: int
    free_places: int


class EventOrganizerRead(EventPublic):
    owner_id: int
    reserved: int
    created_at: datetime
    updated_at: datetime


class ParticipantRead(BaseModel):
    registration_id: int
    participant_id: int
    email: str
    display_name: str
    registered_at: datetime


class RegistrationRead(BaseModel):
    id: int
    event_id: int
    event_title: str
    event_starts_at: datetime
    event_status: EventStatus
    status: RegistrationStatus
    created_at: datetime
    cancelled_at: datetime | None
    cancel_reason: CancelReason | None


class AuditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: str
    actor_id: int
    registration_id: int | None
    created_at: datetime


class HealthResponse(BaseModel):
    status: str
