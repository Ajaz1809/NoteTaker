import logging
import os
from contextlib import contextmanager
from typing import Generator

import httpx
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from config.settings import DATABASE_URL, NOTETAKER_DATABASE_URL

# Base configuration
Base = declarative_base()

# Database connection pool configuration for better performance
# pool_size: Number of connections to maintain persistently
# max_overflow: Maximum number of connections beyond pool_size
# pool_timeout: Seconds to wait before giving up on getting a connection
# pool_recycle: Recycle connections after this many seconds (prevents stale connections)
# pool_pre_ping: Verify connections before using them (recommended for production)
DB_POOL_SIZE = 20  # Maintain 20 persistent connections
DB_MAX_OVERFLOW = 10  # Allow up to 10 additional connections under load
DB_POOL_TIMEOUT = 30  # Wait up to 30 seconds for a connection
DB_POOL_RECYCLE = 3600  # Recycle connections after 1 hour

# Main DB (for users, agents, etc.)
engine = create_engine(
    DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,
    pool_pre_ping=True,  # Verify connections before using
)
SessionLocal = sessionmaker(bind=engine)

# Notetaker DB (separate storage)
NoteTakerBase = declarative_base()
note_engine = create_engine(
    NOTETAKER_DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,
    pool_pre_ping=True,
)
NoteTakerSessionLocal = sessionmaker(bind=note_engine)

# Conference DB (external database)
ConferenceBase = declarative_base()
conference_engine = create_engine(
    DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,
    pool_pre_ping=True,
)
ConferenceSessionLocal = sessionmaker(bind=conference_engine)

# Security configuration
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


def get_db() -> Generator[Session, None, None]:
    """
    Provides a database session for dependency injection.
    Ensures proper cleanup after use.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_notetaker_db() -> Generator[Session, None, None]:
    """Notetaker DB session with proper cleanup."""
    db = NoteTakerSessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_conference_db() -> Generator[Session, None, None]:
    """
    Conference DB session for dependency injection.
    Ensures proper cleanup after use.
    """
    db = ConferenceSessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_remote_user_data(token: str):
    """
    Context manager to get user data from remote API.
    Yields user data or None if request fails.
    """

    import requests

    url = os.getenv("EXTERNAL_URL", 'https://ucaas.webvio.in/backend/api/user')
    headers = {'Authorization': f'Bearer {token}'}
    payload = {}
    try:
        return requests.request("GET", url, headers=headers, data=payload).json()
    except Exception:
        return None


def ensure_user_workspace(db: Session, user) -> None:
    """
    Ensures that a user has a workspace, workspace settings, and workspace member record.
    Creates them if they don't exist.
    """
    from models.models import Workspace, WorkspaceSettings, WorkspaceMember
    from datetime import datetime, timezone

    # Check if user already has a workspace
    workspace = db.query(Workspace).filter(Workspace.owner_id == user.id).first()
    if not workspace:
        # Create workspace for the user
        workspace = Workspace(
            name=f"{user.username}_workspace",
            description="Default workspace",
            owner_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(workspace)
        db.flush()  # Get ID without committing

    # Check if workspace settings exist
    settings = db.query(WorkspaceSettings).filter(WorkspaceSettings.workspace_id == workspace.id).first()
    if not settings:
        settings = WorkspaceSettings(
            workspace_id=workspace.id,
            default_model="gpt-4",
            default_voice="echo",
            temperature=1,
        )
        db.add(settings)

    # Check if workspace member exists
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id,
        WorkspaceMember.user_id == user.id
    ).first()
    if not member:
        member = WorkspaceMember(
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner"
        )
        db.add(member)

    # Commit all changes together
    db.commit()


def create_or_update_user_from_remote_data(db: Session, user_data: dict):
    """
    Creates or updates a user based on remote API data.
    Returns the user object.
    """
    from models.models import User

    email = user_data.get('data', {}).get('email')
    username = user_data.get('data', {}).get('username')
    if not email:
        raise HTTPException(status_code=401, detail="Email not found in user data")
    if not username:
        raise HTTPException(status_code=401, detail="username not found in user data")

    # Try to find existing user
    user = db.query(User).filter(User.email == email, User.username == username).first()
    if not email:
        raise HTTPException(status_code=401, detail="Email not found in user data")

    # Try to find existing user
    from sqlalchemy import or_

    # This will return the first user that matches either the email or the username
    user = db.query(User).filter(
        or_(
            User.email == email,
            User.username == user_data.get('data', {}).get('username', email)
        )
    ).first()
    if not user:
        # Create new user
        user = User(
            email=email,
            username=user_data.get('data', {}).get('username', email),
            full_name=user_data.get('data', {}).get('name', ''),
            is_ucaas_user=True,
            hashed_password='',
        )
        db.add(user)
        db.commit()  # Flush to get user ID
        db.refresh(user)  # Flush to get user ID
        db.flush()  # Flush to get user ID
    else:
        # Update existing user if needed
        user.full_name = user_data.get('data', {}).get('name', user.full_name)
        user.username = user_data.get('data', {}).get('username', user.username)
        user.is_ucaas_user = True
    # Ensure workspace and related records exist
    ensure_user_workspace(db, user)
    return user


def get_current_user(
        token: str = Depends(oauth2_scheme),
        db: Session = Depends(get_db)
) -> object:
    """
    Authenticates user using either local JWT validation or remote API validation.
    Returns the authenticated user object.
    """
    from models.models import User
    from utils.security import decode_token


    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated: No token provided")

    logging.info(f"Incoming token: {token[:10]}..." if len(token) > 10 else f"Incoming token: {token}")



    # Step 1: Try local JWT validation first (faster)
    try:
        payload = decode_token(token)
        email = payload.get("email")
        if email:
            user = db.query(User).filter(User.email == email).first()
            if user:
                logging.info(f"User authenticated via JWT: {email}")
                # Ensure user has required workspace records
                ensure_user_workspace(db, user)
                return user
    except HTTPException:
        # JWT decode failed (expired/invalid), continue to remote API
        pass
    except Exception as e:
        logging.warning(f"JWT validation error: {e}")

    print(token)

    # Step 2: Try remote API validation (UCAAS)
    remote_data = get_remote_user_data(token)
    if remote_data:
        user = create_or_update_user_from_remote_data(db, remote_data)
        logging.info(f"User authenticated via remote API: {remote_data.get('data', {}).get('email')}")
        return user

    # Step 3: Both validations failed
    raise HTTPException(status_code=401, detail="Invalid token or user not found")


# ... remaining content of the file without the conflicting sections ...