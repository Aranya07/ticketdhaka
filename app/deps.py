import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Role, User
from app.security import decode_token, verify_password

bearer = HTTPBearer(auto_error=False)
docs_basic = HTTPBasic(auto_error=False)
DOCS_REALM = 'Basic realm="TicketDhaka API docs"'


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        user_id = decode_token(creds.credentials, "access")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


def require_organizer(user: User = Depends(get_current_user)) -> User:
    if user.role not in (Role.organizer, Role.admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organizer role required")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user


def require_docs_admin(
    creds: HTTPBasicCredentials | None = Depends(docs_basic),
    db: Session = Depends(get_db),
) -> User:
    """Guards /docs and /openapi.json. A browser can't attach a bearer token when you just open a
    URL, so these use HTTP Basic: the browser shows its own login box and you sign in with the
    admin email + password. Every failure gives the same 401, so the box doesn't reveal whether
    an email exists or isn't an admin."""
    denied = HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Admin login required", headers={"WWW-Authenticate": DOCS_REALM}
    )
    if creds is None:
        raise denied
    user = db.execute(select(User).where(User.email == creds.username.lower())).scalar_one_or_none()
    if user is None or user.role != Role.admin or not verify_password(creds.password, user.hashed_password):
        raise denied
    return user
