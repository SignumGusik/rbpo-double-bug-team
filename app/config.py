from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    seed_password: str
    access_token_minutes: int


def _required_secret(name: str, minimum_length: int) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    if len(value) < minimum_length:
        raise RuntimeError(f"{name} must contain at least {minimum_length} characters")
    return value


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./campus_events.db"),
        jwt_secret=_required_secret("JWT_SECRET", 32),
        seed_password=_required_secret("SEED_PASSWORD", 10),
        access_token_minutes=int(os.getenv("ACCESS_TOKEN_MINUTES", "30")),
    )
