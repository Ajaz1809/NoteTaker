import re
import uuid
import pytz
from typing import Optional, Any, List, Dict
from datetime import datetime
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_
from models.models import WorkspaceMember, User, Workspace


def validate_email(email: str):
    if not re.match(r"^[^@]+@[^@]+\.(com)$", email):
        raise HTTPException(status_code=400, detail="Invalid email format")


def validate_password(password: str):
    if not re.match(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[\W_]).{8,}$", password):
        raise HTTPException(status_code=400, detail="Password too weak")


def format_response(
    status: bool,
    message: str,
    data: Optional[Any] = None,
    errors: Optional[List[Dict[str, str]]] = None,
    status_code: int = 201,
) -> dict:
    response = {"status": status, "message": message, "data": data}
    if errors is not None:
        response["errors"] = errors
    return response


def format_datetime_ist(dt: datetime) -> str:
    ist = pytz.timezone("Asia/Kolkata")
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    dt_ist = dt.astimezone(ist)
    return dt_ist.strftime("%Y-%m-%d %H:%M:%S")


def generate_api_key() -> str:
    return f"key_{uuid.uuid4().hex}"


def is_user_in_workspace(user_id: int, workspace_id: int, db: Session) -> bool:
    workspace = (
        db.query(Workspace)
        .outerjoin(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .filter(
            Workspace.id == workspace_id,
            or_(
                Workspace.owner_id == user_id,
                WorkspaceMember.user_id == user_id,
            ),
        )
        .first()
    )
    return workspace is not None


def create_default_workspace(db: Session, user: User) -> Workspace:
    workspace = Workspace(
        name="Default Workspace",
        owner_id=user.id,
        # set other default fields if required
    )
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return workspace