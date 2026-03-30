import httpx,os
import logging
from typing import Optional, Tuple
from datetime import datetime

from sqlalchemy.orm import Session

# Helper: strip Bearer prefix
def strip_bearer_prefix(token: Optional[str]) -> Optional[str]:
    if token and token.startswith('Bearer '):
        return token[7:]
    return token


def is_ucaas_token_format(token: Optional[str]) -> bool:
    """
    Heuristic: a UCAAS 'pipe' token may include '|'. Keep as heuristic only —
    DO NOT use this to accept a token without calling the UCAAS endpoint.
    """
    return bool(token and '|' in token)


def fetch_ucaas_profile(token: str):
    url = f"{os.getenv('EXTERNAL_URL')}/user"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    with httpx.Client(timeout=10.0) as client:
        resp = client.get(url, headers=headers)

        if resp.status_code == 200:
            return True, resp.json()

        return False, None



def upsert_user_from_ucaas(db: Session, ucaas_data: dict, fallback_username: Optional[str] = None):
    # unchanged from your implementation, slightly defensive
    from models.models import User, Workspace, WorkspaceMember

    data = (ucaas_data or {}).get('data', {}) if isinstance(ucaas_data, dict) else {}
    email = data.get('email')
    username = data.get('username') or fallback_username or (email or '').split('@')[0] or 'ucaas_user'
    full_name = data.get('name') or username
    # ---- NEW: extract account_id from UCAAS payload ----
    raw_account_id = data.get('account_id')
    account_id: Optional[int] = None
    if raw_account_id is not None:
        try:
            account_id = int(raw_account_id)
        except (TypeError, ValueError):
            logging.warning(f"Invalid account_id in UCAAS data: {raw_account_id!r}")

    if not email:
        # last-resort synthetic email
        email = f"{username}@ucaas.local"

    user = db.query(User).filter(User.email == email).first()
    if not user:
        try:
            user = User(
                email=email,
                account_id=account_id,
                username=username,
                full_name=full_name,
                is_ucaas_user=True,
                hashed_password='',
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        except Exception as e:
            db.rollback()
            logging.error(f"Creating UCAAS user failed: {e}")
            raise
        else:
            try:
                # Always safe to update these
                user.full_name = full_name or user.full_name
                user.is_ucaas_user = True

                if account_id is not None:
                    user.account_id = account_id

                # ✅ SAFE username update (prevents duplicate error)
                if username and username != user.username:
                    existing = (
                        db.query(User)
                        .filter(User.username == username, User.id != user.id)
                        .first()
                    )
                    if existing:
                        logging.warning(
                            f"Username '{username}' already exists. "
                            f"Keeping existing username '{user.username}' for user {user.id}"
                        )
                    else:
                        user.username = username

                user.updated_at = datetime.utcnow()
                db.commit()
                db.refresh(user)

            except Exception as e:
                db.rollback()
                logging.error(f"Updating UCAAS user failed: {e}")
                raise

    # Ensure default workspace exists
    try:
        owned = db.query(Workspace).filter(Workspace.owner_id == user.id).first()
        if not owned:
            default_ws = Workspace(
                name=f"{user.username}'s Workspace",
                description=f"Auto-created workspace for {user.username}",
                owner_id=user.id,
            )
            db.add(default_ws)
            db.commit()
            db.refresh(default_ws)
            owner_membership = WorkspaceMember(
                workspace_id=default_ws.id,
                user_id=user.id,
                role='owner',
            )
            db.add(owner_membership)
            db.commit()
    except Exception as e:
        db.rollback()
        logging.error(f"Ensuring default workspace failed for user {user.id}: {e}")

    return user


def authenticate_any_token(db: Session, raw_token: Optional[str]):
    """
    Global auth entrypoint:
      1) Try UCAAS endpoint — if UCAAS validates, upsert & return user.
      2) Else try local JWT decode.
    Important: do NOT accept tokens just because they contain '|'.
    """
    from utils.security import decode_token  # local JWT
    from models.models import User

    token = strip_bearer_prefix(raw_token)
    if not token:
        raise Exception("No token provided")

    # First: always attempt to verify token with UCAAS endpoint.
    ok, profile = fetch_ucaas_profile(token)
    if ok and profile:
        # Optionally derive fallback_username only if needed:
        fallback_username = token.split('|', 1)[0] if is_ucaas_token_format(token) else None
        logging.info("Token verified by UCAAS; upserting user.")
        return upsert_user_from_ucaas(db, profile, fallback_username=fallback_username)

    # If UCAAS didn't validate the token, do NOT accept it based on format alone.
    logging.info("Token not validated by UCAAS; attempting local JWT fallback.")

    # Fallback to local JWT
    try:
        payload = decode_token(token)
        email = (payload or {}).get('email')
        if not email:
            raise Exception('JWT missing email')
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise Exception('User not found for JWT')
        return user
    except Exception as e:
        logging.error(f"Local JWT fallback failed: {e}")
        raise
