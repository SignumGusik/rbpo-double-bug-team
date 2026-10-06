from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.security import decode_access_token


bearer_scheme = HTTPBearer(auto_error=False)
DbSession = Annotated[Session, Depends(get_db)]
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


def _user_from_token(token: str, db: Session) -> User:
    user_id, token_role = decode_access_token(token)
    user = db.get(User, user_id)
    if user is None or not user.is_active or user.role != token_role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication context is no longer valid",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_current_user(credentials: BearerCredentials, db: DbSession) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _user_from_token(credentials.credentials, db)


def get_optional_user(credentials: BearerCredentials, db: DbSession) -> User | None:
    if credentials is None:
        return None
    return _user_from_token(credentials.credentials, db)


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
