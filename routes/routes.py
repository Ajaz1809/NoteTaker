import os, shutil, fitz, boto3, io
from docx import Document  # pip install pymupdf python-docx
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from livekit.protocol.models import ListUpdate
from openai import OpenAI
import httpx
from fastapi import (
    Depends,
    Query,
    HTTPException,
    Form,
    UploadFile,
    File,
    Body,
    APIRouter,
    Header,
    Request,
)
from utils.helpers import is_user_in_workspace, generate_api_key
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import select
import uuid
from fastapi.responses import JSONResponse, StreamingResponse
import requests
from bs4 import BeautifulSoup
import time, json
from livekit import api
from typing import List, Optional
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from sqlalchemy.orm import sessionmaker, Session
from db.database import get_db, get_current_user, get_notetaker_db
from models.models import (
    User,
    Workspace,
    WorkspaceSettings,
    WorkspaceMember,
    KnowledgeBase,
    KnowledgeFile,
    FileStatus,
    SourceStatus,
    PBXLLM,
    NoteTakerCall,
)
from models.schemas import (
    CallListRequest,
    UserSignup,
    UserLogin,
    UpdateUser,
    DispatchRequest,
    CreateRoomRequestSchema,
    GetPBXLLMOut,
    WorkspaceCreate,
    WorkspaceOut,
    InviteMember,
    WorkspaceSettingsUpdate,
    PBXLLMCreate,
    PBXLLMOut,
    CreateChatRequest,
    CreateChatResponse,
    VoiceOut,
    VoiceCreate,
    APIResponse,
    CallIDRequest,
  
   
)
from utils.helpers import (
    format_response,
    validate_email,
    validate_password,
    format_datetime_ist,
)

from livekit.protocol.sip import (
    ListSIPDispatchRuleRequest, 
    ListSIPInboundTrunkRequest, 
    CreateSIPInboundTrunkRequest, 
    CreateSIPDispatchRuleRequest, 
    DeleteSIPTrunkRequest, 
    ListSIPTrunkRequest,
    ListSIPOutboundTrunkRequest,
    CreateSIPOutboundTrunkRequest,
)
from livekit.api import (
    LiveKitAPI,
    ListSIPInboundTrunkRequest,
    CreateSIPInboundTrunkRequest,
    SIPInboundTrunkInfo,
    ListSIPDispatchRuleRequest,
    CreateSIPDispatchRuleRequest,
    RoomParticipantIdentity
)
from utils.security import hash_password, verify_password, create_token

from starlette.config import Config

from utils.custom_voice import validate_voice_id
import re,logging
from db.database import engine

logger = logging.getLogger("routes")
router = APIRouter()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads")).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
# Initialize OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


@router.get("/check-username")
def check_username_availability(
    username: str = Query(..., min_length=5, max_length=50),
    db: Session = Depends(get_db),
):
    try:
        existing_user = db.query(User).filter(User.username == username).first()
        if existing_user:
            return format_response(
                status=False,
                message="Username already taken",
                errors=[
                    {"field": "username", "message": "This username is already in use"}
                ],
            )
        return format_response(status=True, message="Username is available")

    except Exception as e:
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )

@router.post("/signup")
def signup(user: UserSignup, db: Session = Depends(get_db)):
    try:
        # Validate inputs
        validate_email(user.email)
        validate_password(user.password)

        # Check if user already exists
        if db.query(User).filter(User.username == user.username).first():
            return format_response(
                status=False,
                message="Username already exists",
                errors=[{"field": "username", "message": "Username already exists"}],
            )

        if db.query(User).filter(User.email == user.email).first():
            return format_response(
                status=False,
                message="Email already exists",
                errors=[{"field": "email", "message": "Email already exists"}],
            )

        # Create user
        new_user = User(
            username=user.username,
            email=user.email,
            full_name=user.full_name,
            hashed_password=hash_password(user.password),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        # Create default workspace
        workspace = Workspace(
            name=f"{user.username}_workspace",
            description="Default workspace",
            owner_id=new_user.id,
            created_at=datetime.utcnow(),
        )
        db.add(workspace)
        db.commit()
        db.refresh(workspace)

        # Add user as member and create workspace settings
        settings = WorkspaceSettings(
            workspace_id=workspace.id,
            default_model="gpt-4",
            default_voice="echo",
            temperature=1,
        )
        member = WorkspaceMember(
            user_id=new_user.id, workspace_id=workspace.id, role="owner"
        )
        db.add_all([settings, member])
        db.commit()

        return format_response(
            status=True,
            message="Signup successful",
            data={"workspace_id": workspace.id, "workspace_name": workspace.name},
        )

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )

@router.post("/login")
async def login(user: UserLogin, db: Session = Depends(get_db)):
    try:
        # Check if user exists in the database
        db_user = db.query(User).filter(User.email == user.email).first()
        
        # Verify credentials
        if not db_user or not verify_password(user.password, db_user.hashed_password):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # Generate JWT token
        token = create_token(data={"email": db_user.email})

        return format_response(
            status=True, message="Login successful", data={"token": "Bearer " + token}
        )

    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")


@router.patch("/user/update")
def update_user(
    user_data: UpdateUser,
    db: Session = Depends(get_db),
    authorization: str = Header(..., description="Bearer token")
):
    try:
        if not authorization.startswith('Bearer '):
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(' ')[1]
        
        # Try UCAAS validation first
        try:
            url = 'https://testing.webvio.in/backend/api/user'
            headers = {'Authorization': f'Bearer {token}'}
            
            with httpx.Client() as client:
                response = client.get(url, headers=headers)
                if response.status_code == 200:
                    ucaas_data = response.json()
                    email = ucaas_data.get('data', {}).get('email')
                    if not email:
                        raise HTTPException(status_code=401, detail="Email not found in UCAAS response")
                        
                    user = db.query(User).filter(User.email == email).first()
                    if not user:
                        user = User(
                            email=email,
                            username=ucaas_data.get('data', {}).get('username', email),
                            full_name=ucaas_data.get('data', {}).get('name', ''),
                            is_ucaas_user=True,
                            hashed_password='',
                        )
                        db.add(user)
                        db.commit()
                        db.refresh(user)
                else:
                    # If UCAAS fails, try JWT
                    from utils.security import decode_token
                    payload = decode_token(token)
                    user = db.query(User).filter(User.email == payload.get("email")).first()
                    if not user:
                        raise HTTPException(status_code=401, detail="User not found")
        except Exception as e:
            raise HTTPException(status_code=401, detail="Invalid token")

        existing_user = user
        if not existing_user:
            return format_response(
                status=False,
                message="User not found",
                errors=[{"field": "user", "message": "User not found"}],
            )

        updated = False
        errors = []
        # Update username
        if user_data.username and user_data.username != existing_user.username:
            username_exists = (
                db.query(User).filter(User.username == user_data.username).first()
            )
            if username_exists:
                errors.append(
                    {"field": "username", "message": "Username already taken"}
                )
            else:
                existing_user.username = user_data.username
                updated = True

        # Update email
        if user_data.email and user_data.email != existing_user.email:
            try:
                validate_email(user_data.email)
            except Exception as e:
                errors.append({"field": "email", "message": str(e)})
            else:
                email_exists = (
                    db.query(User).filter(User.email == user_data.email).first()
                )
                if email_exists:
                    errors.append(
                        {"field": "email", "message": "Email already registered"}
                    )
                else:
                    existing_user.email = user_data.email
                    updated = True

        # Update password
        if user_data.password:
            try:
                validate_password(user_data.password)
            except Exception as e:
                errors.append({"field": "password", "message": str(e)})
            else:
                existing_user.hashed_password = hash_password(user_data.password)
                updated = True

        if errors:
            return format_response(
                status=False, message="Validation or conflict error", errors=errors
            )

        if not updated:
            return format_response(
                status=False,
                message="No valid fields provided for update",
                errors=[{"field": "update", "message": "Nothing to update"}],
            )

        # Update the timestamp
        existing_user.updated_at = datetime.utcnow()

        db.commit()
        user_response = {
            "id": existing_user.id,
            "username": existing_user.username,
            "email": existing_user.email,
            "full_name": existing_user.full_name,
            # Add other fields you want to return here
        }

        return format_response(
            status=True, message="User details updated successfully", data=user_response
        )

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )


@router.post("/create-room-token")
async def create_room_and_token(
    request: CreateRoomRequestSchema,
    db: Session = Depends(get_db),
    authorization: str = Header(..., description="Bearer token")
):
    try:
        if not authorization.startswith('Bearer '):
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(' ')[1]
        
        # Try UCAAS validation first
        try:
            url = os.environ('EXTERNAL_URL', 'https://ucaas.webvio.in/backend/api/user')
            headers = {'Authorization': f'Bearer {token}'}
            
            with httpx.Client() as client:
                response = client.get(url, headers=headers)
                if response.status_code == 200:
                    ucaas_data = response.json()
                    email = ucaas_data.get('data', {}).get('email')
                    if not email:
                        raise HTTPException(status_code=401, detail="Email not found in UCAAS response")
                        
                    user = db.query(User).filter(User.email == email).first()
                    if not user:
                        user = User(
                            email=email,
                            username=ucaas_data.get('data', {}).get('username', email),
                            full_name=ucaas_data.get('data', {}).get('name', ''),
                            is_ucaas_user=True,
                            hashed_password='',
                        )
                        db.add(user)
                        db.commit()
                        db.refresh(user)
                else:
                    # If UCAAS fails, try JWT
                    from utils.security import decode_token
                    payload = decode_token(token)
                    user = db.query(User).filter(User.email == payload.get("email")).first()
                    if not user:
                        raise HTTPException(status_code=401, detail="User not found")
        except Exception as e:
            raise HTTPException(status_code=401, detail="Invalid token")

        LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
        LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
        LIVEKIT_URL = os.getenv("LIVEKIT_URL")

        if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_URL):
            raise HTTPException(
                status_code=500, detail="LiveKit credentials not configured"
            )

        workspace_ids = [
            membership.workspace_id for membership in user.memberships
        ]

        if not workspace_ids:
            raise HTTPException(
                status_code=403, detail="User is not a member of any workspace"
            )

        # 🔥 Step 2: Search for agent in user's workspaces
        agent = (
            db.query(pbx_ai_agent)
            .filter(
                pbx_ai_agent.name == request.agent_name,
                pbx_ai_agent.workspace_id.in_(workspace_ids),
            )
            .first()
        )
        pbxllm = db.query(PBXLLM).filter(PBXLLM.workspace_id.in_(workspace_ids)).first()

        if not agent and pbxllm:
            raise HTTPException(
                status_code=404,
                detail="No LLM configuration & Agent not found for this user",
            )

        # 🔥 Step 2: Create room (optional - LiveKit auto-creates)
        lkapi = api.LiveKitAPI(
            url=LIVEKIT_URL, api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET
        )
        try:
            lkapi.room.create_room(
                api.CreateRoomRequest(
                    name=request.room_name,
                    empty_timeout=10 * 60,  # 10 minutes timeout
                    max_participants=10,
                    metadata=json.dumps(request.metadata) if request.metadata else None,
                )
            )
            print(f"Room '{request.room_name}' created successfully.")
        except Exception as e:
            if "already exists" not in str(e):
                raise HTTPException(status_code=500, detail=f"LiveKit Error: {e}")

        # 🔥 Step 3: Generate token
        token = (
            api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            .with_identity(request.participant_identity)
            .with_name(request.participant_name)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=request.room_name,
                )
            )
        )
        jwt_token = token.to_jwt()
        return jwt_token

    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")


@router.post("/create-dispatch")
async def create_dispatch(request: DispatchRequest):
    lkapi = api.LiveKitAPI()
    try:
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=request.agent_name,
                room=request.room_name,
                metadata=json.dumps(request.metadata),
            )
        )
        return {
            "status": "success",
            "dispatch_id": dispatch.id,
            "room": dispatch.room,
            "agent_name": dispatch.agent_name,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await lkapi.aclose()

###############Begin Notetaker-specific routes (with DB integration)

@router.post("/notetaker-dispatch-try")
async def create_dispatch(
    request: DispatchRequest,
    db: Session = Depends(get_notetaker_db),
):
    async with LiveKitAPI() as lkapi:

        # List participants in room
        # res = await lkapi.room.list_participants(ListParticipantsRequest(
        #     room=request.room_name
        # ))
        res = await lkapi.room.get_participant(
            RoomParticipantIdentity(
                room=request.room_name,
                identity=request.agent_name,
            )
        )
        if res.identity:
            # If participant exists, return their identity
            return {
                "status": False,
                "message": "Agent already exists",
                "data": res.identity,
            }


@router.post("/notetaker-dispatch")
async def create_dispatch(
    request: DispatchRequest,
    db: Session = Depends(get_notetaker_db),
):

    lkapi = api.LiveKitAPI()
    try:
        # check if participant already exists
        res = await lkapi.room.get_participant(
            RoomParticipantIdentity(
                room=request.room_name,
                identity=request.agent_name,
            )
        )
        if res.identity:
            # If participant exists, return their identity
            return {
                "status": False,
                "message": "Agent already exists",
                "data": res.identity,
            }
    except Exception as e:
        # Handle 'participant does not exist' gracefully
        if "participant does not exist" in str(e).lower():
            # Check if there's an active call (by call_id == room_name)
            existing_call = (
                db.query(NoteTakerCall)
                .filter(
                    NoteTakerCall.call_id == request.room_name,
                    NoteTakerCall.call_status == "active",
                )
                .first()
            )

            if not existing_call:
                new_call = NoteTakerCall(
                    call_id=request.room_name,
                    start_timestamp=int(time.time() * 1000),
                    meeting_id=request.meeting_id,
                    conf_name=request.conf_name,
                    user_id=request.user_id,   # NEW
                )
                db.add(new_call)
                db.commit()
                db.refresh(new_call)
            else:
                updated = False
                if request.user_id and existing_call.user_id != request.user_id:
                    existing_call.user_id = request.user_id
                    updated = True
                if request.meeting_id and existing_call.meeting_id != request.meeting_id:
                    existing_call.meeting_id = request.meeting_id
                    updated = True
                if request.conf_name and existing_call.conf_name != request.conf_name:
                    existing_call.conf_name = request.conf_name
                    updated = True
                if updated:
                    db.add(existing_call)
                    db.commit()
                    db.refresh(existing_call)

            # Dispatch the note-taker agent
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=request.agent_name,
                    room=request.room_name,
                )
            )

            return {
                "status": "success",
                "dispatch_id": dispatch.id,
                "room": dispatch.room,
                "agent_name": dispatch.agent_name,
            }

        # For any other error
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/notetaker/call-list")
def get_calls(user_id: Optional[str] = None, db: Session = Depends(get_notetaker_db)):
    try:
        query = db.query(NoteTakerCall)
        if user_id:
            query = query.filter(NoteTakerCall.user_id == user_id)

        calls = query.all()
        if not calls:
            return format_response(
                status=False,
                message="No calls found",
                errors=[{"field": "calls", "message": "No calls found"}],
            )

        return format_response(
            status=True,
            message="Calls retrieved successfully",
            data=[
                {
                    "id": c.id,
                    "call_type": c.call_type,
                    "call_id": c.call_id,
                    "call_status": c.call_status,
                    "start_timestamp": c.start_timestamp,
                    "end_timestamp": c.end_timestamp,
                    "duration_ms": c.duration_ms,
                    "recording_url": c.recording_url,
                    "call_analysis": c.call_analysis,
                    "created_at": c.created_at,
                    "updated_at": c.updated_at,
                    "meeting_id": c.meeting_id,
                    "conf_name": c.conf_name,
                    "user_id": c.user_id,   # NEW
                }
                for c in calls
            ],
        )


    except Exception as e:
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )
  
@router.post("/notetaker/call-list")
def get_calls(payload: CallListRequest, db: Session = Depends(get_notetaker_db)):
    try:
        query = db.query(NoteTakerCall)

        if payload.summary_id:  # ✅ If summary_id is passed, filter by id
            query = query.filter(NoteTakerCall.id == payload.summary_id)
        elif payload.user_id:  # ✅ Otherwise, filter by user_id
            query = query.filter(NoteTakerCall.user_id == payload.user_id)

        calls = query.all()
        if not calls:
            return format_response(
                status=False,
                message="No calls found",
                errors=[{"field": "calls", "message": "No calls found"}],
            )

        return format_response(
            status=True,
            message="Calls retrieved successfully",
            data=[
                {
                    "id": c.id,
                    "call_type": c.call_type,
                    "call_id": c.call_id,
                    "call_status": c.call_status,
                    "start_timestamp": c.start_timestamp,
                    "end_timestamp": c.end_timestamp,
                    "duration_ms": c.duration_ms,
                    "recording_url": c.recording_url,
                    "call_analysis": c.call_analysis,
                    "created_at": c.created_at,
                    "updated_at": c.updated_at,
                    "meeting_id": c.meeting_id,
                    "conf_name": c.conf_name,
                    "user_id": c.user_id,
                }
                for c in calls
            ],
        )

    except Exception as e:
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )
        


@router.post("/notetaker/calls")
def get_calls_by_call_id(
    request: CallIDRequest, db: Session = Depends(get_notetaker_db)
):
    query = db.query(NoteTakerCall)

    if request.roomid:
        query = query.filter(NoteTakerCall.call_id == request.roomid)
    if request.user_id:
        query = query.filter(NoteTakerCall.user_id == request.user_id)

    call_records = query.all()

    if not call_records:
        raise HTTPException(
            status_code=404, detail="No calls found with the given filters."
        )

    return {
        "status": True,
        "message": "Successfully retrieved notes!",
        "data": [
            {
                "id": c.id,
                "call_type": c.call_type,
                "call_id": c.call_id,
                "call_status": c.call_status,
                "start_timestamp": c.start_timestamp,
                "end_timestamp": c.end_timestamp,
                "duration_ms": c.duration_ms,
                "recording_url": c.recording_url,
                "call_analysis": c.call_analysis,
                "created_at": c.created_at,
                "updated_at": c.updated_at,
                "meeting_id": c.meeting_id,
                "conf_name": c.conf_name,
                "user_id": c.user_id,   # NEW
            }
            for c in call_records
        ],
    }


@router.get("/user/status")
def check_user_status(
    db: Session = Depends(get_db),
    authorization: str = Header(..., description="Bearer token")
):
    try:
        if not authorization.startswith('Bearer '):
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(' ')[1]
        
        # Try UCAAS validation first
        try:
            url = os.environ('EXTERNAL_URL', 'https://ucaas.webvio.in/backend/api/user')
            headers = {'Authorization': f'Bearer {token}'}
            
            with httpx.Client() as client:
                response = client.get(url, headers=headers)
                if response.status_code == 200:
                    ucaas_data = response.json()
                    email = ucaas_data.get('data', {}).get('email')
                    if not email:
                        raise HTTPException(status_code=401, detail="Email not found in UCAAS response")
                        
                    user = db.query(User).filter(User.email == email).first()
                    if not user:
                        user = User(
                            email=email,
                            username=ucaas_data.get('data', {}).get('username', email),
                            full_name=ucaas_data.get('data', {}).get('name', ''),
                            is_ucaas_user=True,
                            hashed_password='',
                        )
                        db.add(user)
                        db.commit()
                        db.refresh(user)
                else:
                    # If UCAAS fails, try JWT
                    from utils.security import decode_token
                    payload = decode_token(token)
                    user = db.query(User).filter(User.email == payload.get("email")).first()
                    if not user:
                        raise HTTPException(status_code=401, detail="User not found")
        except Exception as e:
            raise HTTPException(status_code=401, detail="Invalid token")

        if not user:
            return format_response(
                status=False,
                message="User not found",
                errors=[{"field": "user", "message": "User not found"}],
            )

        return format_response(
            status=True,
            message="User is active" if user.is_active else "User is inactive",
            data={"is_active": user.is_active},
        )

    except HTTPException as he:
        return format_response(
            status=False,
            message=str(he.detail),
            errors=[{"field": "authorization", "message": str(he.detail)}],
        )
    except Exception as e:
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )


@router.post("/workspaces")
def create_workspace(
    data: WorkspaceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        workspace = Workspace(
            name=data.name, description=data.description, owner_id=current_user.id
        )
        db.add(workspace)
        db.commit()
        db.refresh(workspace)

        settings = WorkspaceSettings(
            workspace_id=workspace.id,
            default_model="gpt-4",
            default_voice="echo",
            temperature=1,
        )
        db.add(settings)

        member = WorkspaceMember(
            user_id=current_user.id, workspace_id=workspace.id, role="owner"
        )
        db.add(member)
        db.commit()

        return format_response(
            status=True,
            message="Workspace created",
            data=WorkspaceOut(
                id=workspace.id,
                name=workspace.name,
                description=workspace.description,
                created_at=format_datetime_ist(workspace.created_at),
                owner_username=current_user.username,
            ).dict(),
        )

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )



@router.get("/workspaces")
def list_workspaces(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    try:
        # Eager load owner relationship to avoid N+1 queries
        workspaces = (
            db.query(Workspace)
            .join(WorkspaceMember)
            .filter(WorkspaceMember.user_id == current_user.id)
            .options(joinedload(Workspace.owner))
            .all()
        )

        data = [
            WorkspaceOut(
                id=w.id,
                name=w.name,
                created_at=format_datetime_ist(w.created_at),
                owner_username=w.owner.username,
            ).dict()
            for w in workspaces
        ]

        return format_response(
            status=True, message="Workspaces retrieved successfully", data=data
        )

    except Exception as e:
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )

@router.post("/workspaces/{workspace_id}/invite")
def invite_user(
    workspace_id: int,
    invite: InviteMember,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if not workspace:
            return format_response(
                status=False,
                message="Workspace not found",
                errors=[{"field": "workspace_id", "message": "Workspace not found"}],
            )

        if workspace.owner_id != current_user.id:
            return format_response(
                status=False,
                message="Only the owner can invite members",
                errors=[
                    {"field": "permission", "message": "Only the owner can invite"}
                ],
                status_code=403,
            )

        user = db.query(User).filter(User.username == invite.username).first()
        if not user:
            return format_response(
                status=False,
                message="User not found",
                errors=[{"field": "username", "message": "User not found"}],
            )

        existing = (
            db.query(WorkspaceMember)
            .filter_by(workspace_id=workspace_id, user_id=user.id)
            .first()
        )
        if existing:
            return format_response(
                status=False,
                message="User already a member",
                errors=[{"field": "username", "message": "Already a member"}],
                status_code=400,
            )

        new_member = WorkspaceMember(
            workspace_id=workspace_id, user_id=user.id, role=invite.role
        )
        db.add(new_member)
        db.commit()

        return format_response(
            status=True, message=f"{invite.username} invited to workspace"
        )

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )


@router.delete("/workspaces/{workspace_id}")
def delete_workspace(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if not workspace:
            return format_response(
                status=False,
                message="Workspace not found",
                errors=[{"field": "workspace_id", "message": "Workspace not found"}],
                status_code=404,
            )

        if workspace.owner_id != current_user.id:
            return format_response(
                status=False,
                message="Only the owner can delete the workspace",
                errors=[{"field": "permission", "message": "Unauthorized"}],
                status_code=403,
            )

        # Delete knowledge base folders from filesystem
        for kb in workspace.knowledge_bases:
            kb_path = UPLOAD_DIR / str(workspace.id) / str(kb.id)
            if kb_path.exists():
                import shutil

                shutil.rmtree(kb_path)

        # Delete workspace (with cascading)
        db.delete(workspace)
        db.commit()

        return format_response(status=True, message="Workspace deleted successfully")

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )


@router.post("/workspaces/knowledge-bases")
def create_knowledge_base(
    workspace_id: int = Form(...),
    name: str = Form(...),
    files: List[UploadFile] = File(default=[]),
    urls: Optional[List[str]] = Form(default=[]),
    text_filenames: Optional[List[str]] = Form(default=[]),
    text_contents: Optional[List[str]] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        # Validate workspace
        workspace = (
            db.query(Workspace)
            .filter(Workspace.id == workspace_id, Workspace.owner_id == current_user.id)
            .first()
        )
        if not workspace:
            return format_response(
                status=False,
                message="Workspace not found",
                errors=[{"field": "workspace_id", "message": "Workspace not found"}],
                status_code=404,
            )

        # Validate name
        if not name.strip():
            return format_response(
                status=False,
                message="Validation error",
                errors=[
                    {"field": "name", "message": "Knowledge base name is required"}
                ],
                status_code=400,
            )

        # Create knowledge base folder and metadata
        kb_id = f"knowledge_base_{uuid.uuid4().hex[:16]}"
        kb_folder = UPLOAD_DIR / f"workspace_{workspace_id}" / kb_id
        kb_folder.mkdir(parents=True, exist_ok=True)
        now_utc = datetime.utcnow()

        # Create KnowledgeBase entry
        kb = KnowledgeBase(
            id=kb_id,
            name=name,
            file_path=str(kb_folder),
            workspace_id=workspace_id,
            enable_auto_refresh=True,
            auto_refresh_interval=24,
            last_refreshed=now_utc,
        )
        db.add(kb)
        db.flush()  # Get kb.id before commit

        sources = []

        # Process uploaded files
        for file in files:
            if file and file.filename:
                filename = f"{uuid.uuid4().hex}_{file.filename}"
                file_path = kb_folder / filename
                with open(file_path, "wb") as f:
                    f.write(file.file.read())

                db.add(
                    KnowledgeFile(
                        kb_id=kb.id,
                        filename=filename,
                        file_path=str(file_path),
                        source_type=SourceStatus.file,
                        status=FileStatus.pending,
                    )
                )

                sources.append(
                    {
                        "type": "document",
                        "source_id": kb.id,
                        "filename": filename,
                        "file_url": str(file_path),
                    }
                )

        # Process web URLs
        for url in urls or []:
            if url.strip():
                filename = f"web_{uuid.uuid4().hex}.txt"
                file_path = kb_folder / filename
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(f"Crawled content from {url}")  # placeholder

                db.add(
                    KnowledgeFile(
                        kb_id=kb.id,
                        filename=filename,
                        file_path=str(file_path),
                        source_type=SourceStatus.web_page,
                        status=FileStatus.pending,
                    )
                )

                sources.append(
                    {
                        "type": "web_page",
                        "source_id": kb.id,
                        "filename": filename,
                        "file_url": str(file_path),
                    }
                )

        # Process text entries
        for title, content in zip(text_filenames or [], text_contents or []):
            if title.strip() and content.strip():
                filename = f"{uuid.uuid4().hex}_{title}.txt"
                file_path = kb_folder / filename
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

                db.add(
                    KnowledgeFile(
                        kb_id=kb.id,
                        filename=filename,
                        file_path=str(file_path),
                        source_type=SourceStatus.text,
                        status=FileStatus.pending,
                    )
                )

                sources.append(
                    {
                        "type": "text",
                        "source_id": kb.id,
                        "filename": filename,
                        "file_url": str(file_path),
                    }
                )

        if not sources:
            return format_response(
                status=False,
                message="No valid sources provided",
                errors=[
                    {
                        "field": "sources",
                        "message": "Provide at least one file, URL, or text",
                    }
                ],
            )

        db.commit()
        db.refresh(kb)

        return format_response(
            status=True,
            message="Knowledge base created successfully",
            data={
                "knowledge_base_id": kb.id,
                "knowledge_base_name": kb.name,
                "status": "in_progress",
                "knowledge_base_sources": sources,
                "enable_auto_refresh": kb.enable_auto_refresh,
                "last_refreshed_timestamp": int(now_utc.timestamp() * 1000),
            },
        )

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )


@router.post("/workspaces/{workspace_id}/create-knowledge-base")
async def create_or_update_knowledge_base(
    workspace_id: int,
    kb_id: Optional[str] = Form(None),
    knowledge_base_name: Optional[str] = Form(None),
    knowledge_base_files: List[UploadFile] = File(default=[]),
    knowledge_base_urls: Optional[str] = Form(default=None),
    knowledge_base_texts: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        s3_client = boto3.client(
            "s3",
            region_name=os.getenv("AWS_DEFAULT_REGION"),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )

        AWS_BUCKET = os.getenv("AWS_BUCKET")
        AWS_REGION = os.getenv("AWS_DEFAULT_REGION")
        if knowledge_base_urls:
            try:
                knowledge_base_urls = json.loads(knowledge_base_urls)
            except json.JSONDecodeError:
                return format_response(
                    status=False,
                    message="Invalid JSON for knowledge_base_urls",
                    errors=[
                        {
                            "field": "knowledge_base_urls",
                            "message": "Invalid JSON format",
                        }
                    ],
                    status_code=400,
                )
        else:
            knowledge_base_urls = []
        if knowledge_base_texts:
            try:
                knowledge_base_texts = json.loads(knowledge_base_texts)
            except json.JSONDecodeError:
                return format_response(
                    status=False,
                    message="Invalid JSON for knowledge_base_texts",
                    errors=[
                        {
                            "field": "knowledge_base_texts",
                            "message": "Invalid JSON format",
                        }
                    ],
                    status_code=400,
                )
        else:
            knowledge_base_texts = []

        # Check workspace ownership
        workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if not workspace:
            return format_response(
                status=False,
                message="Workspace not found",
                errors=[{"field": "workspace_id", "message": "Workspace not found"}],
                status_code=404,
            )

        # Check if KB exists
        if kb_id:
            # Try to fetch existing KB by kb_id
            kb = (
                db.query(KnowledgeBase)
                .filter_by(workspace_id=workspace_id, id=kb_id)
                .first()
            )
        else:
            kb = None

        now_utc = datetime.utcnow()

        if not kb:
            # Create new KB
            kb_id = f"{uuid.uuid4().hex[:16]}"
            kb_folder = UPLOAD_DIR / f"workspace_{workspace_id}" / kb_id
            kb_folder.mkdir(parents=True, exist_ok=True)

            kb = KnowledgeBase(
                id=kb_id,
                name=knowledge_base_name,
                file_path=str(kb_folder),
                workspace_id=workspace_id,
                enable_auto_refresh=True,
                auto_refresh_interval=24,
                last_refreshed=now_utc,
            )
            db.add(kb)
            db.flush()
        else:
            # Existing KB folder
            kb_folder = Path(kb.file_path)
            kb_folder.mkdir(parents=True, exist_ok=True)

        sources = []
        kb_files_to_add = []  # Collect all KnowledgeFile objects for batch processing

        # Process uploaded files
        for file in knowledge_base_files:
            if file and file.filename:
                filename = f"{uuid.uuid4().hex}_{file.filename}"
                # file_path = kb_folder / filename
                s3_key = f"ai_agent/knowledge_base_files/workspace_{workspace_id}/kb_{kb.id}/{filename}"
                content = await file.read()

                # Upload to S3
                s3_client.put_object(Bucket=AWS_BUCKET, Key=s3_key, Body=content)

                # content = await file.read()
                # with open(file_path, "wb") as f_out:
                #     f_out.write(content)

                # Extract text for embedding
                ext = Path(filename).suffix.lower()
                extract_text = ""
                if ext == ".pdf":
                    with fitz.open(stream=content, filetype="pdf") as doc:
                        extract_text = "\n".join(p.get_text() for p in doc)
                    # with fitz.open(file_path) as doc:
                    #     extract_text = "\n".join(p.get_text() for p in doc)
                elif ext == ".docx":
                    doc = Document(io.BytesIO(content))
                    extract_text = "\n".join(p.text for p in doc.paragraphs)
                    # extract_text = "\n".join(
                    #     p.text for p in Document(file_path).paragraphs
                    # )
                elif ext == ".txt":
                    extract_text = content.decode("utf-8")
                else:
                    extract_text = ""  # fallback for unsupported

                # Truncate and embed
                truncated = extract_text[:30000]
                embedding_response = openai_client.embeddings.create(
                    model="text-embedding-3-small", input=truncated
                )
                embedding_vector = embedding_response.data[0].embedding
                file_url = (
                    f"https://{AWS_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
                )

                kb_file = KnowledgeFile(
                    kb_id=kb.id,
                    filename=filename,
                    file_path=file_url,
                    extract_data=extract_text,
                    embedding=embedding_vector,
                    status=FileStatus.completed,
                    source_type=SourceStatus.document,  # ✅ updated to new enum
                )
                kb_files_to_add.append(("document", kb_file, filename, file_url))

        # Process web URLs (using async HTTP for better performance)
        async with httpx.AsyncClient(timeout=10.0) as client:
            for url in knowledge_base_urls or []:
                if url.strip():
                    filename = "none"

                    res = await client.get(
                        url, headers={"User-Agent": "Mozilla/5.0"}
                    )
                    res.raise_for_status()
                    soup = BeautifulSoup(res.text, "html.parser")
                    for tag in soup(["script", "style"]):
                        tag.decompose()
                    extract_text = soup.get_text(separator="\n", strip=True)

                    truncated = extract_text[:30000]
                    embedding_response = openai_client.embeddings.create(
                        model="text-embedding-3-small", input=truncated
                    )
                    embedding_vector = embedding_response.data[0].embedding

                    kb_file = KnowledgeFile(
                        kb_id=kb.id,
                        filename=filename,
                        file_path=url,
                        extract_data=extract_text,
                        embedding=embedding_vector,
                        status=FileStatus.completed,
                        source_type=SourceStatus.url,  # ✅ updated to new enum
                    )
                    kb_files_to_add.append(("url", kb_file, filename, url))

        for entry in knowledge_base_texts or []:
            title = entry.get("title", "").strip()
            content = entry.get("text", "").strip()
            if title and content:
                filename = f"{uuid.uuid4().hex}_{title}.txt"
                s3_key = f"ai_agent/knowledge_base_texts/workspace_{workspace_id}/kb_{kb.id}/{filename}"
                #  Upload content to S3
                s3_client.put_object(
                    Bucket=AWS_BUCKET,
                    Key=s3_key,
                    Body=content.encode("utf-8"),
                    ContentType="text/plain",
                )

                # file_path = kb_folder / filename
                # file_path.write_text(content, encoding="utf-8")

                truncated = content[:30000]
                embedding_response = openai_client.embeddings.create(
                    model="text-embedding-3-small", input=truncated
                )
                embedding_vector = embedding_response.data[0].embedding
                file_url = (
                    f"https://{AWS_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
                )

                kb_file = KnowledgeFile(
                    kb_id=kb.id,
                    filename=filename,
                    file_path=file_url,
                    extract_data=content,
                    embedding=embedding_vector,
                    status=FileStatus.completed,
                    source_type=SourceStatus.text,  # ✅ updated to new enum
                )
                kb_files_to_add.append(("text", kb_file, filename, file_url))

        # Batch add all KnowledgeFile objects to session
        for source_type, kb_file, filename, file_url in kb_files_to_add:
            db.add(kb_file)
        
        # Single flush to get all IDs in one database round trip
        if kb_files_to_add:
            db.flush()
            
            # Build sources list with IDs after flush
            for source_type, kb_file, filename, file_url in kb_files_to_add:
                sources.append(
                    {
                        "type": source_type,
                        "source_id": kb_file.id,
                        "filename": filename,
                        "file_url": file_url,
                    }
                )

        if not sources:
            return format_response(
                status=False,
                message="No valid sources provided",
                errors=[
                    {
                        "field": "sources",
                        "message": "Provide at least one file, URL, or text",
                    }
                ],
            )

        kb.last_refreshed = now_utc
        db.commit()
        db.refresh(kb)

        return format_response(
            status=True,
            message="Knowledge base updated successfully",
            data={
                "knowledge_base_id": kb.id,
                "knowledge_base_name": kb.name,
                "status": "in_progress",
                "knowledge_base_sources": sources,
                "enable_auto_refresh": kb.enable_auto_refresh,
                "last_refreshed_timestamp": int(now_utc.timestamp() * 1000),
            },
        )

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
            status_code=500,
        )


@router.get("/workspaces/{workspace_id}/list-knowledge-bases")
def list_knowledge_bases(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        # Check workspace ownership or membership
        workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()

        if not workspace:
            return format_response(
                status=False,
                message="Workspace not found",
                errors=[{"field": "workspace_id", "message": "Workspace not found"}],
                status_code=404,
            )

        # Fetch all KnowledgeBases for workspace with eager loading of knowledge_files
        kbs = (
            db.query(KnowledgeBase)
            .filter_by(workspace_id=workspace_id)
            .options(joinedload(KnowledgeBase.knowledge_files))
            .all()
        )

        response_data = []
        for kb in kbs:
            # Use preloaded knowledge_files instead of querying separately
            sources = [
                {
                    "type": kf.source_type,
                    "source_id": kf.id,
                    "filename": kf.filename,
                    "file_url": kf.file_path,
                }
                for kf in kb.knowledge_files
            ]

            response_data.append(
                {
                    "knowledge_base_id": kb.id,
                    "knowledge_base_name": kb.name,
                    "status": "completed",  # You can make dynamic if needed
                    "knowledge_base_sources": sources,
                    "enable_auto_refresh": kb.enable_auto_refresh,
                    "last_refreshed_timestamp": (
                        int(kb.last_refreshed.timestamp() * 1000)
                        if kb.last_refreshed
                        else None
                    ),
                }
            )

        return format_response(
            status=True,
            message="Knowledge bases fetched successfully",
            data=response_data,
        )

    except Exception as e:
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
            status_code=500,
        )


@router.get("/get-knowledge-base/{kb_id}")
def get_knowledge_base(
    kb_id: str,  # changed from int to str
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        # Eager load knowledge_files to avoid N+1 query
        kb = (
            db.query(KnowledgeBase)
            .filter(KnowledgeBase.id == kb_id)
            .options(joinedload(KnowledgeBase.knowledge_files))
            .first()
        )
        if not kb:
            return format_response(
                status=False,
                message="Knowledge base not found",
                errors=[{"field": "kb_id", "message": "Knowledge base not found"}],
                status_code=404,
            )

        is_member = (
            db.query(WorkspaceMember)
            .filter_by(workspace_id=kb.workspace_id, user_id=current_user.id)
            .first()
        )

        if not is_member:
            return format_response(
                status=False,
                message="Access denied",
                errors=[{"field": "workspace", "message": "Access denied"}],
                status_code=403,
            )

        # Use preloaded knowledge_files instead of querying separately
        sources = [
            {
                "type": f.source_type,
                "source_id": f.id,
                "filename": f.filename,
                "file_url": f.file_path,
            }
            for f in kb.knowledge_files
        ]

        data = {
            "knowledge_base_id": kb.id,
            "knowledge_base_name": kb.name,
            "status": "in_progress",
            "knowledge_base_sources": sources,
            "enable_auto_refresh": kb.enable_auto_refresh,
            "last_refreshed_timestamp": (
                int(kb.last_refreshed.timestamp() * 1000) if kb.last_refreshed else None
            ),
        }

        return format_response(
            status=True, message="Knowledge base details fetched", data=data
        )

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
            status_code=500,
        )


from openai import OpenAI

config = Config(".env")

# Ensure you initialize OpenAI client
openai_client = OpenAI()


@router.post("/knowledge-bases/sources")
async def add_kb_source(
    kb_id: str = Form(...),
    source_type: str = Form(...),
    file: UploadFile = File(None),
    url: str = Form(None),
    text_filename: str = Form(None),
    text_content: str = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        # Validate KB and user membership
        kb = db.query(KnowledgeBase).filter_by(id=kb_id).first()
        if not kb:
            return format_response(
                False,
                "Knowledge base not found",
                errors=[{"field": "kb_id"}],
                status_code=404,
            )

        member = (
            db.query(WorkspaceMember)
            .filter_by(workspace_id=kb.workspace_id, user_id=current_user.id)
            .first()
        )
        if not member:
            return format_response(
                False, "Access denied", errors=[{"field": "workspace"}], status_code=403
            )

        # Directory setup
        kb_path = (
            UPLOAD_DIR / f"workspace_{kb.workspace_id}" / f"knowledge_base_{kb_id}"
        )
        kb_path.mkdir(parents=True, exist_ok=True)

        # Process content
        file_path, file_name, extract_text = None, None, ""

        if source_type == "file":
            if not file:
                return format_response(
                    False,
                    "File is required",
                    errors=[{"field": "file"}],
                    status_code=400,
                )

            ext = Path(file.filename).suffix.lower()
            file_name = f"{uuid4().hex}_{file.filename}"
            file_path = kb_path / file_name

            with open(file_path, "wb") as f_out:
                f_out.write(await file.read())

            if ext == ".pdf":
                with fitz.open(file_path) as doc:
                    extract_text = "\n".join(p.get_text() for p in doc)
            elif ext == ".docx":
                extract_text = "\n".join(p.text for p in Document(file_path).paragraphs)
            elif ext == ".txt":
                extract_text = file_path.read_text(encoding="utf-8")
            else:
                return format_response(False, "Unsupported file type", status_code=400)

        elif source_type == "web_page":
            if not url or not text_filename:
                return format_response(
                    False, "URL and filename required", status_code=400
                )
            res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            extract_text = soup.get_text(separator="\n", strip=True)
            file_name = text_filename
            file_path = url

        elif source_type == "txt":
            if not text_filename or not text_content:
                return format_response(
                    False, "Text filename and content required", status_code=400
                )
            file_name = f"{uuid4().hex}_{text_filename}.txt"
            file_path = kb_path / file_name
            file_path.write_text(text_content, encoding="utf-8")
            extract_text = text_content

        else:
            return format_response(False, "Invalid source type", status_code=400)

        clean_text = extract_text.replace("\n", " ").strip()
        truncated = clean_text[:3000]  # limit tokens for embedding

        embedding_response = openai_client.embeddings.create(
            model="text-embedding-3-small", input=truncated
        )
        embedding_vector = embedding_response.data[0].embedding

        # Save to DB
        kb_file = KnowledgeFile(
            kb_id=kb.id,
            filename=file_name,
            file_path=str(file_path),
            extract_data=extract_text,
            embedding=embedding_vector,  # ➕ store embedding
            status=FileStatus.completed,
            source_type=SourceStatus(source_type),
        )
        db.add(kb_file)
        db.commit()

        return format_response(
            True,
            "Source added with embedding",
            data={
                "source_id": kb_file.id,
                "filename": file_name,
                "file_url": str(file_path),
                "type": source_type,
            },
        )

    except Exception as e:
        db.rollback()
        return format_response(
            False,
            "Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
            status_code=500,
        )


@router.delete("/knowledge-bases/{kb_id}")
def delete_knowledge_base(
    kb_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        # Fetch knowledge base
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        if not kb:
            return format_response(
                status=False,
                message="Knowledge base not found",
                errors=[{"field": "kb_id", "message": "Knowledge base not found"}],
                status_code=404,
            )

        # Check workspace ownership
        workspace = db.query(Workspace).filter(Workspace.id == kb.workspace_id).first()
        if not workspace or workspace.owner_id != current_user.id:
            return format_response(
                status=False,
                message="Only the workspace owner can delete this knowledge base",
                errors=[{"field": "permission", "message": "Access denied"}],
                status_code=403,
            )

        # === 1. Delete related knowledge files from DB ===
        db.query(KnowledgeFile).filter(KnowledgeFile.kb_id == kb_id).delete()
        kbid = str(kb.id)
        # === 2. Remove kb.id from PBXLLM.knowledge_base_ids JSON ===
        pbx_llms = db.query(PBXLLM).filter(PBXLLM.workspace_id == workspace.id).all()
        for pbx in pbx_llms:
            if kbid in pbx.knowledge_base_ids:
                pbx.knowledge_base_ids.remove(kbid)
                db.add(pbx)
                db.commit()

        # === 3. Delete S3 folder and versions ===
        s3 = boto3.resource("s3")
        bucket = s3.Bucket(os.getenv("AWS_BUCKET"))
        prefix = f"ai_agent/knowledge_base_files/workspace_{workspace.id}/kb_{kb.id}/"

        # Delete all objects
        for obj in bucket.objects.filter(Prefix=prefix):
            obj.delete()

        # Delete all versions (if versioning enabled)
        for version in bucket.object_versions.filter(Prefix=prefix):
            version.delete()

        # === 4. Delete knowledge base from DB ===
        db.delete(kb)
        db.commit()

        return format_response(
            status=True, message="Knowledge base deleted successfully"
        )

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
            status_code=500,
        )




@router.delete("/knowledge-files/{file_id}")
def delete_knowledge_file(
    file_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        # Fetch the file entry
        file = db.query(KnowledgeFile).filter(KnowledgeFile.id == file_id).first()
        if not file:
            return format_response(
                status=False,
                message="File not found",
                errors=[{"field": "file_id", "message": "Knowledge file not found"}],
                status_code=404,
            )

        # Check if user has access to the knowledge base
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == file.kb_id).first()
        if not kb:
            return format_response(
                status=False,
                message="Knowledge base not found",
                errors=[
                    {"field": "kb_id", "message": "Associated knowledge base not found"}
                ],
                status_code=404,
            )

        membership = (
            db.query(WorkspaceMember)
            .filter_by(workspace_id=kb.workspace_id, user_id=current_user.id)
            .first()
        )

        if not membership:
            return format_response(
                status=False,
                message="Access denied",
                errors=[
                    {
                        "field": "workspace",
                        "message": "User is not a member of this workspace",
                    }
                ],
                status_code=403,
            )

        # Attempt to delete the file from the filesystem
        file_path = Path(file.file_path)
        if file_path.exists() and file_path.is_file():
            try:
                file_path.unlink()
            except Exception as e:
                # Log warning or handle non-fatal file deletion error if needed
                pass

        # Delete the file record from the database
        db.delete(file)
        db.commit()

        return format_response(
            status=True, message="Knowledge file deleted successfully"
        )

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
            status_code=500,
        )


@router.get("/workspaces/{workspace_id}/settings")
def get_workspace_settings(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        membership = (
            db.query(WorkspaceMember)
            .filter_by(workspace_id=workspace_id, user_id=current_user.id)
            .first()
        )
        if not membership:
            return format_response(
                status=False,
                message="Access denied",
                errors=[
                    {
                        "field": "workspace_id",
                        "message": "User is not a member of this workspace",
                    }
                ],
                status_code=403,
            )

        settings = (
            db.query(WorkspaceSettings).filter_by(workspace_id=workspace_id).first()
        )
        if not settings:
            return format_response(
                status=False,
                message="Settings not found",
                errors=[
                    {"field": "workspace_id", "message": "Workspace settings not found"}
                ],
                status_code=404,
            )

        return format_response(
            status=True,
            message="Workspace settings retrieved",
            data={
                "default_voice": settings.default_voice,
                "default_model": settings.default_model,
                "temperature": settings.temperature,
            },
        )

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )


@router.put("/workspaces/{workspace_id}/settings")
def update_workspace_settings(
    workspace_id: int,
    updates: WorkspaceSettingsUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        workspace = db.query(Workspace).filter_by(id=workspace_id).first()
        if not workspace or workspace.owner_id != current_user.id:
            return format_response(
                status=False,
                message="Only owner can update settings",
                errors=[
                    {
                        "field": "permission",
                        "message": "Only the workspace owner can update settings",
                    }
                ],
                status_code=403,
            )

        settings = (
            db.query(WorkspaceSettings).filter_by(workspace_id=workspace_id).first()
        )
        if not settings:
            return format_response(
                status=False,
                message="Settings not found",
                errors=[
                    {"field": "workspace_id", "message": "Workspace settings not found"}
                ],
                status_code=404,
            )

        if updates.default_voice is not None:
            settings.default_voice = updates.default_voice
        if updates.default_model is not None:
            settings.default_model = updates.default_model
        if updates.temperature is not None:
            settings.temperature = updates.temperature

        db.commit()

        return format_response(status=True, message="Settings updated")

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
        )


@router.get("/list-knowledge-bases")
def list_knowledge_bases(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    try:
        # Join KnowledgeBase -> Workspace -> WorkspaceMember with eager loading
        knowledge_bases = (
            db.query(KnowledgeBase)
            .join(Workspace, KnowledgeBase.workspace)  # join workspace from KB
            .outerjoin(WorkspaceMember, Workspace.id == WorkspaceMember.workspace_id)
            .filter(
                (Workspace.owner_id == current_user.id)
                | (WorkspaceMember.user_id == current_user.id)
            )
            .options(joinedload(KnowledgeBase.knowledge_files))
            .all()
        )

        result = []

        for kb in knowledge_bases:
            # Use preloaded knowledge_files instead of querying separately
            file_sources = [
                {
                    "type": "document",
                    "source_id": str(file.id),
                    "filename": file.filename,
                    "file_url": file.file_url,
                }
                for file in kb.knowledge_files
            ]

            result.append(
                {
                    "knowledge_base_id": f"knowledge_base_{kb.id}",
                    "knowledge_base_name": kb.name,
                    "status": getattr(kb, "status", "in_progress"),
                    "knowledge_base_sources": file_sources,
                    "enable_auto_refresh": kb.enable_auto_refresh,
                    "last_refreshed_timestamp": (
                        int(kb.last_refreshed.timestamp() * 1000)
                        if kb.last_refreshed
                        else 0
                    ),
                }
            )

        return format_response(
            status=True, message="Knowledge bases retrieved successfully", data=result
        )

    except Exception as e:
        db.rollback()
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{"field": "server", "message": str(e)}],
            status_code=500,
        )

