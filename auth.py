import json
import os
import secrets
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

load_dotenv()

DATA_DIR = Path(os.getenv("POCKETSMART_DATA_DIR", Path(__file__).parent / "data"))
USERS_FILE = DATA_DIR / "users.json"
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token", auto_error=False)
password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_users_lock = threading.RLock()


def _read_users() -> dict[str, dict[str, Any]]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with USERS_FILE.open("r", encoding="utf-8") as file:
            users = json.load(file)
            return users if isinstance(users, dict) else {}
    except FileNotFoundError:
        return {}


def _write_users(users: dict[str, dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(dir=DATA_DIR, prefix="users-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(users, file, ensure_ascii=True, indent=2)
        os.replace(temporary_path, USERS_FILE)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _username_key(username: str) -> str:
    return username.strip().casefold()


def register_user(username: str, email: str, full_name: str, password: str) -> dict[str, Any]:
    key = _username_key(username)
    with _users_lock:
        users = _read_users()
        if key in users:
            raise HTTPException(status_code=409, detail="That username is already registered.")
        if any(user.get("email", "").casefold() == email.casefold() for user in users.values()):
            raise HTTPException(status_code=409, detail="That email address is already registered.")
        user = {
            "username": username.strip(),
            "email": email.strip().casefold(),
            "full_name": full_name.strip(),
            "hashed_password": password_context.hash(password),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "session_data": {},
            "financial_profile": {},
        }
        users[key] = user
        _write_users(users)
        return {field: user[field] for field in ("username", "email", "full_name", "created_at")}


def authenticate_user(username: str, password: str) -> dict[str, Any] | None:
    with _users_lock:
        users = _read_users()
        user = users.get(_username_key(username))
    if not user or not password_context.verify(password, user["hashed_password"]):
        return None
    return user


def save_session_data(username: str, data: dict[str, Any]) -> None:
    with _users_lock:
        users = _read_users()
        key = _username_key(username)
        user = users.get(key)
        if user is not None:
            user["session_data"] = data
            _write_users(users)


def get_financial_profile(username: str) -> dict[str, Any]:
    with _users_lock:
        user = _read_users().get(_username_key(username), {})
        profile = user.get("financial_profile", {})
        return profile if isinstance(profile, dict) else {}


def save_financial_profile(username: str, profile: dict[str, Any]) -> None:
    with _users_lock:
        users = _read_users()
        key = _username_key(username)
        user = users.get(key)
        if user is not None:
            user["financial_profile"] = profile
            users[key] = user
            _write_users(users)


def create_access_token(subject: str, session_id: str, expires_delta: timedelta | None = None) -> str:
    expires = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return jwt.encode({"sub": subject, "jti": session_id, "exp": expires}, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_active_user(
    bearer_token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias="access_token"),
) -> dict[str, Any]:
    token = bearer_token or cookie_token
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Please sign in to continue.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_error
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username, session_id = payload.get("sub"), payload.get("jti")
    except JWTError:
        raise credentials_error from None
    if not username or not session_id:
        raise credentials_error

    from app import get_session_user

    user = get_session_user(session_id, username)
    if user is None:
        raise credentials_error
    return user


def valid_secret_key() -> bool:
    import re

    return bool(os.getenv("SECRET_KEY")) and not re.search(r"change.?me|example|your.?secret", SECRET_KEY, re.I) and len(SECRET_KEY) >= 32


def new_session_id() -> str:
    return secrets.token_urlsafe(24)
