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
    APIKey,
    FileStatus,
    SourceStatus,
    pbx_ai_agent,
    PBXLLM,
    ChatSession,
    LLMVoice,
    NoteTakerCall,
    ConversationalFlow,
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
    AgentCreate,
    PBXLLMCreate,
    PBXLLMOut,
    CreateChatRequest,
    CreateChatResponse,
    VoiceOut,
    VoiceCreate,
    APIResponse,
    AgentUpdate,
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

from models.schemas import PhoneNumberOut
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


#  API KEYS
@router.post("/workspaces/{workspace_id}/api-keys")
def create_api_key(
    workspace_id: int,
    payload: dict = Body(...),  # expecting: { "name": "my key name" }
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_user_in_workspace(current_user.id, workspace_id, db):
        raise HTTPException(
            status_code=403, detail="You do not have access to this workspace"
        )

    workspace = db.query(Workspace).filter_by(id=workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    key_value = generate_api_key()
    api_key = APIKey(
        workspace_id=workspace.id,
        name=payload["name"],
        key_value=key_value,
        created_by=current_user.id,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    return format_response(
        status=True,
        message="API Key created successfully",
        data={
            "id": api_key.id,
            "name": api_key.name,
            "key_value": api_key.key_value,
            "last_edited_by": current_user.username,
            "updated_at": api_key.updated_at.isoformat(),
        },
    )


@router.get("/workspaces/{workspace_id}/api-keys")
def list_api_keys(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_user_in_workspace(current_user.id, workspace_id, db):
        raise HTTPException(
            status_code=403, detail="You do not have access to this workspace"
        )

    keys = db.query(APIKey).filter_by(workspace_id=workspace_id).all()
    data = [
        {
            "id": key.id,
            "name": key.name,
            "key_value": key.key_value,
            "last_edited_by": current_user.username,
            "updated_at": key.updated_at.isoformat(),
            "is_webhook_key": key.is_webhook_key,
        }
        for key in keys
    ]

    return format_response(
        status=True, message="API keys fetched successfully", data=data
    )


@router.delete("/workspaces/{workspace_id}/api-keys/{key_id}")
def delete_api_key(
    workspace_id: int,
    key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Check if the user is authorized in this workspace
    if not is_user_in_workspace(current_user.id, workspace_id, db):
        raise HTTPException(
            status_code=403, detail="You do not have access to this workspace"
        )

    # Fetch the specific API key under that workspace
    key = db.query(APIKey).filter_by(id=key_id, workspace_id=workspace_id).first()
    if not key:
        raise HTTPException(
            status_code=404, detail="API key not found in this workspace"
        )

    if key.is_webhook_key:
        raise HTTPException(
            status_code=403,
            detail="Cannot delete webhook key. Create a new key and set as webhook key first before deleting this key.",
        )

    db.delete(key)
    db.commit()

    return format_response(status=True, message="API key deleted successfully")


@router.put("/api-keys/{key_id}/rename")
def update_api_key_name(
    key_id: int,
    name: str = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    key = db.query(APIKey).filter_by(id=key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")

    key.name = name
    key.updated_at = datetime.utcnow()
    key.created_by = current_user.id
    db.commit()
    return {"message": "API key renamed"}


@router.put("/api-keys/{key_id}/set-webhook")
def set_webhook_api_key(
    key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    key = db.query(APIKey).filter_by(id=key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")

    # Clear existing webhook key
    db.query(APIKey).filter(
        APIKey.workspace_id == key.workspace_id, APIKey.is_webhook_key == True
    ).update({"is_webhook_key": False})

    key.is_webhook_key = True
    db.commit()
    return {"message": "Webhook API key set"}


__all__ = ["router"]


@router.post("/agents")
async def create_agent(
    payload: AgentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_user_in_workspace(current_user.id, payload.workspace_id, db):
        raise HTTPException(
            status_code=403, detail="You do not have access to this workspace"
        )
    is_valid = await validate_voice_id(payload.voice_id)
    # print(f"is_valid: {is_valid}")
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail="Voice ID not found or invalid in ElevenLabs"
        )


    new_agent = pbx_ai_agent(
        workspace_id=payload.workspace_id,
        name=payload.agent_name,
        voice_id=payload.voice_id,
        voice_model=payload.voice_model,
        fallback_voice_ids=payload.fallback_voice_ids,
        voice_temperature=payload.voice_temperature,
        voice_speed=payload.voice_speed,
        volume=payload.volume,
        responsiveness=payload.responsiveness,
        interruption_sensitivity=payload.interruption_sensitivity,
        enable_backchannel=payload.enable_backchannel,
        backchannel_frequency=payload.backchannel_frequency,
        backchannel_words=payload.backchannel_words,
        reminder_trigger_ms=payload.reminder_trigger_ms,
        reminder_max_count=payload.reminder_max_count,
        ambient_sound=payload.ambient_sound,
        ambient_sound_volume=payload.ambient_sound_volume,
        language=payload.language,
        webhook_url=payload.webhook_url,
        boosted_keywords=payload.boosted_keywords,
        opt_out_sensitive_data_storage=payload.opt_out_sensitive_data_storage,
        opt_in_signed_url=payload.opt_in_signed_url,
        pronunciation_dictionary=(
            [p.model_dump() for p in payload.pronunciation_dictionary]
            if payload.pronunciation_dictionary
            else None
        ),
        normalize_for_speech=payload.normalize_for_speech,
        end_call_after_silence_ms=payload.end_call_after_silence_ms,
        max_call_duration_ms=payload.max_call_duration_ms,
        post_call_analysis_data=(
            [a.model_dump() for a in payload.post_call_analysis_data]
            if payload.post_call_analysis_data
            else None
        ),
        post_call_analysis_model=payload.post_call_analysis_model,
        begin_message_delay_ms=payload.begin_message_delay_ms,
        ring_duration_ms=payload.ring_duration_ms,
        stt_mode=payload.stt_mode,
        vocab_specialization=payload.vocab_specialization,
        allow_user_dtmf=payload.allow_user_dtmf,
        user_dtmf_options=(
            payload.user_dtmf_options.model_dump()
            if payload.user_dtmf_options
            else None
        ),
        denoising_mode=payload.denoising_mode,
        response_engine=(
            payload.response_engine.model_dump() if payload.response_engine else None
        ),
        version=payload.version,
        last_modification_timestamp=int(time.time() * 1000),
        voicemail_option=(
            payload.voicemail_option.model_dump() if payload.voicemail_option else None
        ),
    )

    db.add(new_agent)
    db.commit()
    db.refresh(new_agent)

    response_data = {
        "agent_id": str(new_agent.id),
        "last_modification_timestamp": new_agent.last_modification_timestamp,
        "agent_name": new_agent.name,
        "response_engine": new_agent.response_engine,
        "language": new_agent.language,
        "opt_out_sensitive_data_storage": new_agent.opt_out_sensitive_data_storage,
        "opt_in_signed_url": new_agent.opt_in_signed_url,
        "end_call_after_silence_ms": new_agent.end_call_after_silence_ms,
        "version": new_agent.version,
        "is_published": new_agent.is_published,
        "post_call_analysis_model": new_agent.post_call_analysis_model,
        "voice_id": new_agent.voice_id,
        "voice_model": new_agent.voice_model,
        "fallback_voice_ids": new_agent.fallback_voice_ids,
        "voice_temperature": new_agent.voice_temperature,
        "voice_speed": new_agent.voice_speed,
        "volume": new_agent.volume,
        "enable_backchannel": new_agent.enable_backchannel,
        "backchannel_frequency": new_agent.backchannel_frequency,
        "backchannel_words": new_agent.backchannel_words,
        "reminder_trigger_ms": new_agent.reminder_trigger_ms,
        "reminder_max_count": new_agent.reminder_max_count,
        "max_call_duration_ms": new_agent.max_call_duration_ms,
        "interruption_sensitivity": new_agent.interruption_sensitivity,
        "ambient_sound": new_agent.ambient_sound,
        "ambient_sound_volume": new_agent.ambient_sound_volume,
        "responsiveness": new_agent.responsiveness,
        "normalize_for_speech": new_agent.normalize_for_speech,
        "begin_message_delay_ms": new_agent.begin_message_delay_ms,
        "ring_duration_ms": new_agent.ring_duration_ms,
        "stt_mode": new_agent.stt_mode,
        "vocab_specialization": new_agent.vocab_specialization,
        "allow_user_dtmf": new_agent.allow_user_dtmf,
        "user_dtmf_options": new_agent.user_dtmf_options,
        "denoising_mode": new_agent.denoising_mode,
        "webhook_url": new_agent.webhook_url,
        "boosted_keywords": new_agent.boosted_keywords,
        "pronunciation_dictionary": new_agent.pronunciation_dictionary,
        "voicemail_option": new_agent.voicemail_option,
        "post_call_analysis_data": new_agent.post_call_analysis_data,
    }

    return {"status": True, "data": response_data}


@router.delete("/agent/delete-agent/{agent_id}")
def delete_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = db.query(pbx_ai_agent).filter(pbx_ai_agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not is_user_in_workspace(current_user.id, agent.workspace_id, db):
        raise HTTPException(
            status_code=403, detail="You do not have access to this workspace"
        )

    # 🗑️ Delete the agent
    db.delete(agent)
    db.commit()

    return {"status": True, "message": f"Agent deleted successfully", "data": None}


import httpx


async def get_elevenlabs_voice_name(voice_id: str) -> str:
    """Fetch ElevenLabs voice name by ID"""
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
    }
    url = f"{ELEVENLABS_VOICE_URL}/{voice_id}"

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)

    if response.status_code != 200:
        return "Unknown Voice"

    data = response.json()
    return data.get("name", "Unknown Voice")

# listing conv flow agent
@router.get("/all-agents/{workspace_id}")
async def list_my_agents(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_user_in_workspace(current_user.id, workspace_id, db):
        raise HTTPException(
            status_code=403, detail="You do not have access to this workspace"
        )
    try:
        from sqlalchemy import cast, String, or_

        # Get all workspace IDs the user is a member of
        workspace_ids = (
            select(WorkspaceMember.workspace_id)
            .filter(WorkspaceMember.user_id == current_user.id)
        )

        # Fetch agents in those workspaces
        agents = (
            db.query(pbx_ai_agent)
            .filter(pbx_ai_agent.workspace_id.in_(workspace_ids))
            .all()
        )

        # Helper: parse response_engine into dict safely
        def _parse_response_engine(val):
            if val is None:
                return {}
            if isinstance(val, dict):
                return val
            if isinstance(val, str):
                import json
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    return {}
            return {}

        # Helper: canonical flow id preferring uuid else conversation_flow_<id>
        def _canonical_flow_id(flow):
            if not flow:
                return None
            if getattr(flow, "uuid", None):
                return str(flow.uuid)
            if getattr(flow, "id", None) is not None:
                return f"conversation_flow_{flow.id}"
            return None

        # Build base agent list (unchanged behaviour)
        agent_list = []
        # track conversation_flow_ids already present directly in agent.response_engine
        seen_flow_ids = set()

        for agent in agents:
            response_engine_value = _parse_response_engine(agent.response_engine)

            # capture conversation_flow_id if agent already references it
            if isinstance(response_engine_value, dict):
                for key in ("conversation_flow_id", "conversationFlowId", "conversationFlow", "conversation_flow", "flow_id"):
                    if key in response_engine_value and response_engine_value.get(key):
                        seen_flow_ids.add(str(response_engine_value.get(key)))
                        break

            voice_name = await get_elevenlabs_voice_name(agent.voice_id)

            agent_list.append(
                {
                    "agent_id": agent.id,
                    "version": agent.version,
                    "is_published": agent.is_published,
                    "response_engine": response_engine_value or {},
                    "agent_name": agent.name,
                    "voice_id": agent.voice_id,
                    "voice_name": voice_name,
                    "voice_model": agent.voice_model,
                    "fallback_voice_ids": agent.fallback_voice_ids or [],
                    "voice_temperature": agent.voice_temperature,
                    "voice_speed": agent.voice_speed,
                    "volume": agent.volume,
                    "responsiveness": agent.responsiveness,
                    "interruption_sensitivity": agent.interruption_sensitivity,
                    "enable_backchannel": agent.enable_backchannel,
                    "backchannel_frequency": agent.backchannel_frequency,
                    "backchannel_words": agent.backchannel_words or [],
                    "reminder_trigger_ms": agent.reminder_trigger_ms,
                    "reminder_max_count": agent.reminder_max_count,
                    "ambient_sound": agent.ambient_sound,
                    "ambient_sound_volume": agent.ambient_sound_volume,
                    "language": agent.language,
                    "webhook_url": agent.webhook_url,
                    "boosted_keywords": agent.boosted_keywords or [],
                    "opt_out_sensitive_data_storage": agent.opt_out_sensitive_data_storage,
                    "opt_in_signed_url": agent.opt_in_signed_url,
                    "pronunciation_dictionary": agent.pronunciation_dictionary or [],
                    "normalize_for_speech": agent.normalize_for_speech,
                    "end_call_after_silence_ms": agent.end_call_after_silence_ms,
                    "max_call_duration_ms": agent.max_call_duration_ms,
                    "voicemail_option": agent.voicemail_option or {},
                    "post_call_analysis_data": agent.post_call_analysis_data or [],
                    "post_call_analysis_model": agent.post_call_analysis_model,
                    "begin_message_delay_ms": agent.begin_message_delay_ms,
                    "ring_duration_ms": agent.ring_duration_ms,
                    "stt_mode": agent.stt_mode,
                    "vocab_specialization": agent.vocab_specialization,
                    "allow_user_dtmf": agent.allow_user_dtmf,
                    "user_dtmf_options": agent.user_dtmf_options or {},
                    "denoising_mode": agent.denoising_mode,
                    "last_modification_timestamp": agent.last_modification_timestamp,
                }
            )

        # --- Load conversational flows that are active ---
        try:
            flows_q = db.query(ConversationalFlow).filter(ConversationalFlow.is_active == True)
            # if flows are workspace-scoped
            if hasattr(ConversationalFlow, "workspace_id"):
                flows_q = flows_q.filter(ConversationalFlow.workspace_id == workspace_id)
            flows = flows_q.all()
        except Exception:
            flows = []

        # For each flow, attempt to resolve a real agent via 3 strategies:
        # 1) flow.agent_id / flow.agent_uuid
        # 2) agent.response_engine contains flow.uuid or conversation_flow_<id> (text search)
        # 3) fallback: agent.name == flow.name (last resort)
        added_pairs = set()  # (agent_id, canonical_flow_id) to avoid duplicates

        for flow in flows:
            canonical = _canonical_flow_id(flow)
            if canonical and str(canonical) in seen_flow_ids:
                # agent already directly references this flow -> skip adding duplicate
                continue

            matched_agent = None

            # Strategy 1: flow stores agent reference fields
            for fld in ("agent_id", "agent_uuid"):
                try:
                    aid = getattr(flow, fld, None)
                    if aid:
                        candidate = db.query(pbx_ai_agent).filter(pbx_ai_agent.id == str(aid)).one_or_none()
                        if candidate:
                            matched_agent = candidate
                            break
                except Exception:
                    continue
            if matched_agent:
                pass

            # Strategy 2: search agent.response_engine string for flow identifiers
            if not matched_agent and canonical:
                try:
                    pattern1 = f"%{canonical}%"
                    # also search by flow.uuid explicitly if it exists
                    pattern2 = f"%{getattr(flow,'uuid', '')}%"
                    candidate = (
                        db.query(pbx_ai_agent)
                        .filter(
                            or_(
                                cast(pbx_ai_agent.response_engine, String).ilike(pattern1),
                                cast(pbx_ai_agent.response_engine, String).ilike(pattern2),
                            )
                        )
                        .first()
                    )
                    if candidate:
                        matched_agent = candidate
                except Exception:
                    matched_agent = None

            # Strategy 3: fallback name match (only if flow.name exists)
            if not matched_agent and getattr(flow, "name", None):
                try:
                    candidate = db.query(pbx_ai_agent).filter(pbx_ai_agent.name == flow.name).first()
                    if candidate:
                        matched_agent = candidate
                except Exception:
                    matched_agent = None

            # If still no match, skip this flow
            if not matched_agent:
                continue

            # avoid duplicates
            flow_key = canonical or str(getattr(flow, "id", ""))
            pair = (str(matched_agent.id), flow_key)
            if pair in added_pairs:
                continue
            added_pairs.add(pair)

            # Build conversation-flow payload using matched_agent metadata (so agent_name is correct)
            conv_re = {"type": "conversation-flow", "version": getattr(matched_agent, "version", 0)}
            if flow_key:
                conv_re["conversation_flow_id"] = flow_key

            convo_payload = {
                "agent_id": matched_agent.id,
                "channel": getattr(matched_agent, "channel", None) or "chat",
                "last_modification_timestamp": getattr(flow, "updated_at", None) or getattr(flow, "created_at", None),
                "agent_name": matched_agent.name,
                "response_engine": conv_re,
                "language": getattr(matched_agent, "language", None) or getattr(flow, "language", None) or "en-US",
                "data_storage_setting": getattr(matched_agent, "data_storage_setting", None) or getattr(matched_agent, "data_storage", "everything"),
                "opt_in_signed_url": matched_agent.opt_in_signed_url if hasattr(matched_agent, "opt_in_signed_url") else False,
                "version": matched_agent.version or 0,
                "is_published": matched_agent.is_published,
                "post_call_analysis_model": getattr(matched_agent, "post_call_analysis_model", None),
                "pii_config": getattr(matched_agent, "pii_config", None) or {"mode": "post_call", "categories": []},
                "post_chat_analysis_model": getattr(matched_agent, "post_chat_analysis_model", None),
            }

            # Append if not already existing in agent_list (match by agent_id + conversation_flow_id)
            exists = False
            for ex in agent_list:
                try:
                    if str(ex.get("agent_id")) == str(convo_payload["agent_id"]):
                        ex_re = ex.get("response_engine", {})
                        if ex_re and ex_re.get("conversation_flow_id") == conv_re.get("conversation_flow_id"):
                            exists = True
                            break
                except Exception:
                    continue
            if not exists:
                agent_list.append(convo_payload)

        return format_response(status=True, message="Agents fetched successfully", data=agent_list)

    except Exception as e:
        db.rollback()
        return format_response(status=False, message=f"Internal Server Error: {str(e)}", data=None)


@router.patch("/agent/update-agent/{agent_id}")
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,  # ✅ using optional fields schema
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = db.query(pbx_ai_agent).filter(pbx_ai_agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not is_user_in_workspace(current_user.id, agent.workspace_id, db):
        raise HTTPException(
            status_code=403, detail="You do not have access to this workspace"
        )

    update_data = payload.dict(exclude_unset=True)
    # Map `agent_name` in payload to `name` in DB model
    if "agent_name" in update_data:
        update_data["name"] = update_data.pop("agent_name")
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided for update")
    
    if "voice_id" in update_data:
        is_valid = await validate_voice_id(update_data["voice_id"])
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail="Invalid voice_id: Voice not found or not supported by ElevenLabs",
            )

    for field, value in update_data.items():
        if hasattr(agent, field):
            setattr(agent, field, value)
        else:
            print(f"⚠️ Skipped unknown field: {field}")

    agent.last_modification_timestamp = int(time.time() * 1000)

    db.add(agent)  # ✅ Ensure it's tracked
    db.commit()
    db.refresh(agent)

    response_data = {
        "agent_id": str(agent.id),
        "last_modification_timestamp": agent.last_modification_timestamp,
        "agent_name": agent.name,
        "response_engine": agent.response_engine,
        "language": agent.language,
        "opt_out_sensitive_data_storage": agent.opt_out_sensitive_data_storage,
        "opt_in_signed_url": agent.opt_in_signed_url,
        "end_call_after_silence_ms": agent.end_call_after_silence_ms,
        "version": agent.version,
        "is_published": agent.is_published,
        "post_call_analysis_model": agent.post_call_analysis_model,
        "voice_id": agent.voice_id,
        "voice_model": agent.voice_model,
        "fallback_voice_ids": agent.fallback_voice_ids,
        "voice_temperature": agent.voice_temperature,
        "voice_speed": agent.voice_speed,
        "volume": agent.volume,
        "enable_backchannel": agent.enable_backchannel,
        "backchannel_frequency": agent.backchannel_frequency,
        "backchannel_words": agent.backchannel_words,
        "reminder_trigger_ms": agent.reminder_trigger_ms,
        "reminder_max_count": agent.reminder_max_count,
        "max_call_duration_ms": agent.max_call_duration_ms,
        "interruption_sensitivity": agent.interruption_sensitivity,
        "ambient_sound": agent.ambient_sound,
        "ambient_sound_volume": agent.ambient_sound_volume,
        "responsiveness": agent.responsiveness,
        "normalize_for_speech": agent.normalize_for_speech,
        "begin_message_delay_ms": agent.begin_message_delay_ms,
        "ring_duration_ms": agent.ring_duration_ms,
        "stt_mode": agent.stt_mode,
        "vocab_specialization": agent.vocab_specialization,
        "allow_user_dtmf": agent.allow_user_dtmf,
        "user_dtmf_options": agent.user_dtmf_options,
        "denoising_mode": agent.denoising_mode,
        "webhook_url": agent.webhook_url,
        "boosted_keywords": agent.boosted_keywords,
        "pronunciation_dictionary": agent.pronunciation_dictionary,
        "voicemail_option": agent.voicemail_option,
        "post_call_analysis_data": agent.post_call_analysis_data,
    }

    return {
        "status": True,
        "message": "Agent updated successfully",
        "data": response_data,
    }



@router.post("/pbx-llms", response_model=PBXLLMOut)
def create_pbx_llm(
    payload: PBXLLMCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Validate workspace access
    if not is_user_in_workspace(current_user.id, payload.workspace_id, db):
        raise HTTPException(
            status_code=403, detail="You do not have access to this workspace"
        )

    workspace = db.query(Workspace).filter(Workspace.id == payload.workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    llm = PBXLLM(
        workspace_id=payload.workspace_id,
        version=payload.version,
        model=payload.model,
        s2s_model=payload.s2s_model,
        model_temperature=payload.model_temperature,
        model_high_priority=payload.model_high_priority,
        tool_call_strict_mode=payload.tool_call_strict_mode,
        general_prompt=payload.general_prompt,
        general_tools=payload.general_tools,
        states=payload.states,
        starting_state=payload.starting_state,
        begin_message=payload.begin_message,
        default_dynamic_variables=payload.default_dynamic_variables,
        knowledge_base_ids=payload.knowledge_base_ids,
        last_modification_timestamp=int(time.time() * 1000),
    )
    db.add(llm)
    db.commit()
    db.refresh(llm)
    return PBXLLMOut(status=True, llm_id=f"{llm.id}")


@router.delete("/delete-pbx-llm/{llm_id}")
def delete_pbx_llm(
    llm_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Fetch the PBXLLM
    llm = db.query(PBXLLM).filter(PBXLLM.id == llm_id).first()
    if not llm:
        return format_response(
            status=False,
            message="PBXLLM not found",
            errors=[{"field": "llm_id", "message": "PBXLLM not found"}],
            status_code=404,
        )

    # Validate workspace access
    if not is_user_in_workspace(current_user.id, llm.workspace_id, db):
        return format_response(
            status=False,
            message="Access denied",
            errors=[
                {
                    "field": "workspace",
                    "message": "You do not have access to this workspace",
                }
            ],
            status_code=403,
        )

    # Delete PBXLLM
    db.delete(llm)
    db.commit()

    return format_response(status=True, message="LLM deleted successfully", data=None)


@router.get("/pbx-llm/{llm_id}")
def get_pbx_llm(
    llm_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Fetch PBXLLM by id
    llm = db.query(PBXLLM).filter(PBXLLM.id == llm_id).first()

    if not llm:
        # Return 404 if not found
        return JSONResponse(
            status_code=404,
            content={
                "status": False,
                "message": f"LLM with id {llm_id} not found",
                "data": None,
            },
        )

    # Build response data
    llm_data = {
        "llm_id": llm.id,
        "version": llm.version,
        "model": llm.model,
        "s2s_model": llm.s2s_model,
        "model_temperature": llm.model_temperature,
        "model_high_priority": llm.model_high_priority,
        "tool_call_strict_mode": llm.tool_call_strict_mode,
        "general_prompt": llm.general_prompt,
        "general_tools": llm.general_tools,
        "states": llm.states,
        "starting_state": llm.starting_state,
        "begin_message": llm.begin_message,
        "default_dynamic_variables": llm.default_dynamic_variables,
        "knowledge_base_ids": llm.knowledge_base_ids,
        "last_modification_timestamp": llm.last_modification_timestamp,
        "is_published": llm.is_published,
    }

    # Return wrapped response
    return JSONResponse(
        status_code=200, content={"status": True, "message": "", "data": llm_data}
    )





# Constants you requested (change if needed)
TWILIO_ADDRESS = "ucaas-testing.pstn.twilio.com"
TWILIO_AUTH_USER = "natty"
TWILIO_AUTH_PASS = "Passw0rd@123"


def _normalize_e164(n: str) -> str:
    """Return normalized +E164 string (leading '+')."""
    if not n:
        return n
    # Keep digits and leading plus only
    cleaned = re.sub(r"[^\d+]", "", n)
    if cleaned.startswith("+"):
        return cleaned
    return f"+{cleaned}"


def _pick_id(trunk_obj) -> Optional[str]:
    """
    Try multiple attribute names for trunk id across SDK versions / response wraps.
    Handles:
      - trunk_obj.sip_outbound_trunk_id
      - trunk_obj.trunk_id
      - trunk_obj.id
      - trunk_obj.trunk.sip_outbound_trunk_id
      - trunk_obj.trunk (object)
    """
    if trunk_obj is None:
        return None
    # direct properties
    for attr in ("sip_outbound_trunk_id", "trunk_id", "id"):
        val = getattr(trunk_obj, attr, None)
        if val:
            return val
    # nested shapes
    nested = getattr(trunk_obj, "trunk", None)
    if nested:
        for attr in ("sip_outbound_trunk_id", "trunk_id", "id"):
            val = getattr(nested, attr, None)
            if val:
                return val
    return None


async def ensure_outbound_trunk_for_number(phone_number: str) -> str:
    """
    Ensure a LiveKit SIP outbound trunk is available that includes the provided phone_number.
    Returns the trunk id that now contains the number.
    """
    desired = _normalize_e164(phone_number)
    lkapi = api.LiveKitAPI()
    try:
        resp = await lkapi.sip.list_sip_outbound_trunk(ListSIPOutboundTrunkRequest())
        trunks = getattr(resp, "items", []) or []

        # 1) If any trunk already contains the number, return its id
        for t in trunks:
            existing_numbers: List[str] = getattr(t, "numbers", []) or []
            normalized = {_normalize_e164(x) for x in existing_numbers if x}
            if desired in normalized:
                tid = _pick_id(t)
                if tid:
                    logging.info(f"Found existing trunk {tid} containing {desired}; reusing.")
                    return tid

        # 2) Prefer to add to a trunk that has the TWILIO_ADDRESS or name 'transfer trunk'
        candidate = None
        by_name = None
        for t in trunks:
            address = getattr(t, "address", "") or ""
            name = getattr(t, "name", "") or ""
            if address == TWILIO_ADDRESS:
                candidate = t
                break
            if name.lower() == "transfer trunk":
                by_name = t
        if candidate is None:
            candidate = by_name

        if candidate:
            tid = _pick_id(candidate)
            if tid:
                logging.info(f"Adding {desired} to existing trunk {tid} (candidate).")
                await lkapi.sip.update_sip_outbound_trunk_fields(
                    trunk_id=tid,
                    numbers=ListUpdate(add=[desired], remove=[]),
                )
                return tid

        # 3) Create a new trunk
        unique_name = f"transfer-trunk-{uuid.uuid4().hex[:8]}"
        trunk_info = api.SIPOutboundTrunkInfo(
            name=unique_name,
            address=TWILIO_ADDRESS,
            numbers=[desired],
            auth_username=TWILIO_AUTH_USER,
            auth_password=TWILIO_AUTH_PASS,
        )

        created = await lkapi.sip.create_sip_outbound_trunk(CreateSIPOutboundTrunkRequest(trunk=trunk_info))

        # try to pick id from response
        new_id = _pick_id(created) or _pick_id(getattr(created, "trunk", None))
        if not new_id:
            # fallback: re-list and find by name we created
            rel = await lkapi.sip.list_sip_outbound_trunk(ListSIPOutboundTrunkRequest())
            for t in getattr(rel, "items", []) or []:
                if getattr(t, "name", "") == unique_name:
                    new_id = _pick_id(t)
                    break

        if not new_id:
            raise RuntimeError("Could not determine id of newly created trunk")

        logging.info(f"Created new trunk {new_id} with name {unique_name} for number {desired}")
        return new_id

    finally:
        await lkapi.aclose()


async def remove_number_from_trunk(phone_number: str) -> Optional[str]:
    """
    Remove the given number from any trunk that contains it. Returns trunk id removed from, or None.
    """
    desired = _normalize_e164(phone_number)
    lkapi = api.LiveKitAPI()
    try:
        resp = await lkapi.sip.list_sip_outbound_trunk(ListSIPOutboundTrunkRequest())
        trunks = getattr(resp, "items", []) or []
        for t in trunks:
            existing_numbers: List[str] = getattr(t, "numbers", []) or []
            normalized = {_normalize_e164(x) for x in existing_numbers if x}
            if desired in normalized:
                tid = _pick_id(t)
                if not tid:
                    continue
                await lkapi.sip.update_sip_outbound_trunk_fields(
                    trunk_id=tid,
                    numbers=ListUpdate(add=[], remove=[desired]),
                )
                logging.info(f"Removed {desired} from trunk {tid}")
                return tid
        return None
    finally:
        await lkapi.aclose()

# === Updated endpoint (async) ===
@router.patch("/pbx-llms/update-llm/{llm_id}")
async def update_pbx_llm(
    llm_id: str,
    payload: PBXLLMCreate,  # Or PBXLLMUpdate where fields are Optional
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Fetch PBXLLM by ID
    llm = db.query(PBXLLM).filter(PBXLLM.id == llm_id).first()
    if not llm:
        raise HTTPException(status_code=404, detail=f"LLM with id {llm_id} not found")

    # Check workspace access
    if not is_user_in_workspace(current_user.id, llm.workspace_id, db):
        raise HTTPException(status_code=403, detail="You do not have access to this workspace")

    # Apply only provided fields
    update_data = payload.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(llm, field, value)

    # If general_tools updated (or present), process transfer_call tool
    try:
        tools_candidate = update_data.get("general_tools", None)
        # If not provided in update payload, use existing llm.general_tools
        tools_candidate = tools_candidate if tools_candidate is not None else llm.general_tools or []

        # some code paths store general_tools as tuple/list
        if isinstance(tools_candidate, tuple):
            tools_candidate = tools_candidate[0]

        transfer_tool = next((t for t in (tools_candidate or []) if t.get("type") == "transfer_call"), None)

        # read previous transfer number (if we stored it previously); try to detect old number in llm.general_tools too
        previous_number = None
        try:
            existing_tools = llm.general_tools or []
            if isinstance(existing_tools, tuple):
                existing_tools = existing_tools[0]
            prev_tool = next((t for t in (existing_tools or []) if t.get("type") == "transfer_call"), None)
            previous_dest = (prev_tool.get("transfer_destination") or {}) if prev_tool else {}
            previous_number = previous_dest.get("number") or previous_dest.get("phone") or None
        except Exception:
            previous_number = None

        # If transfer tool present and has a number -> ensure trunk contains it
        if transfer_tool:
            dest = transfer_tool.get("transfer_destination") or {}
            new_number = dest.get("number") or dest.get("phone") or ""
            new_number = new_number.strip() if isinstance(new_number, str) else ""
            if new_number:
                try:
                    trunk_id = await ensure_outbound_trunk_for_number(new_number)
                    # optionally save trunk id on llm for future reference if model has that column
                    if hasattr(llm, "sip_outbound_trunk_id"):
                        try:
                            llm.sip_outbound_trunk_id = trunk_id
                        except Exception:
                            # ignore if DB model doesn't support or mapping differs
                            pass
                except Exception as e:
                    # Do not fail the whole update if trunk provisioning fails; log and continue
                    logging.exception(f"Failed to ensure trunk for number {new_number}: {e}")
        else:
            # transfer tool removed -> remove previous number from any trunk
            if previous_number:
                try:
                    removed_from = await remove_number_from_trunk(previous_number)
                    logging.info(f"Removed {previous_number} from trunk {removed_from}")
                except Exception:
                    logging.exception(f"Failed to remove previous transfer number {previous_number} from trunk")

    except Exception:
        # never block DB update on trunk operations; log and continue
        logging.exception("Error while provisioning outbound trunk for transfer_call tool")

    # Update timestamp & commit
    llm.last_modification_timestamp = int(time.time() * 1000)
    db.commit()
    db.refresh(llm)

    # Build response
    updated_llm_data = {
        "llm_id": llm.id,
        "version": llm.version,
        "model": llm.model,
        "s2s_model": llm.s2s_model,
        "model_temperature": llm.model_temperature,
        "model_high_priority": llm.model_high_priority,
        "tool_call_strict_mode": llm.tool_call_strict_mode,
        "general_prompt": llm.general_prompt,
        "general_tools": llm.general_tools,
        "states": llm.states,
        "starting_state": llm.starting_state,
        "begin_message": llm.begin_message,
        "default_dynamic_variables": llm.default_dynamic_variables,
        "knowledge_base_ids": llm.knowledge_base_ids,
        "last_modification_timestamp": llm.last_modification_timestamp,
        "is_published": llm.is_published,
        # optionally expose trunk id:
        "sip_outbound_trunk_id": getattr(llm, "sip_outbound_trunk_id", None),
    }

    return JSONResponse(
        status_code=200,
        content={
            "status": True,
            "message": "LLM updated successfully",
            "data": updated_llm_data,
        },
    )

@router.get("/all-pbx-llms/{workspace_id}")
def get_pbx_llm(
    workspace_id=int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        # Fetch the PBX LLM for the user's workspace
        pbx_llm = db.query(PBXLLM).filter(PBXLLM.workspace_id == workspace_id).all()

        if not pbx_llm:
            return format_response(
                status=False,
                message="No LLM configuration found for this workspace",
                data=None,
                errors=[
                    {
                        "field": "workspace_id",
                        "message": "No PBX LLM found for workspace",
                    }
                ],
                status_code=404,
            )

        # Validate and serialize with Pydantic
        pbx_llm_out_list = [GetPBXLLMOut.from_orm(llm).model_dump() for llm in pbx_llm]

        return format_response(
            status=True,
            message="LLM configuration fetched successfully",
            data=pbx_llm_out_list,
        )

    except Exception as e:
        return format_response(
            status=False,
            message="Failed to fetch LLM configuration",
            errors=[{"field": "server", "message": str(e)}],
            status_code=500,
        )


# Chat room APIs--------------------------------------------------
@router.post("/create-chat", response_model=CreateChatResponse)
def create_chat(payload: CreateChatRequest, db: Session = Depends(get_db)):
    try:
        chat = ChatSession(
            agent_id=payload.agent_id,
            agent_version=payload.agent_version or 0,
            chat_status=payload.chat_status,
            llm_dynamic_variables=payload.llm_dynamic_variables,
            collected_dynamic_variables={},
            chat_metadata=payload.metadata,
            start_timestamp=payload.start_timestamp,
            end_timestamp=payload.end_timestamp,
            transcript=payload.transcript or "null",
            message_with_tool_calls=[],  # ensure it's a list of dicts or JSON-serializable
            chat_cost=payload.chat_cost.model_dump() if payload.chat_cost else None,
            chat_analysis=(
                payload.chat_analysis.model_dump() if payload.chat_analysis else None
            ),
        )

        db.add(chat)
        db.commit()
        db.refresh(chat)

        return CreateChatResponse(
            chat_id=chat.chat_id,
            agent_id=chat.agent_id,
            chat_status=chat.chat_status,
            llm_dynamic_variables=chat.llm_dynamic_variables,
            collected_dynamic_variables=chat.collected_dynamic_variables,
            start_timestamp=chat.start_timestamp,
            end_timestamp=chat.end_timestamp,
            transcript=chat.transcript,
            message_with_tool_calls=chat.message_with_tool_calls,
            metadata=chat.chat_metadata,
            chat_cost=chat.chat_cost,
            chat_analysis=chat.chat_analysis,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# voice
@router.post("/llm-voice", response_model=VoiceOut)
def create_voice(
    payload: VoiceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),  # optional if you want auth
):
    existing = db.query(LLMVoice).filter_by(voice_id=payload.voice_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Voice ID already exists")

    voice = LLMVoice(**payload.dict())
    db.add(voice)
    db.commit()
    db.refresh(voice)
    return voice


@router.get("/llm-voice/{voice_id}", response_model=VoiceOut)
def get_voice(
    voice_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    voice = db.query(LLMVoice).filter_by(voice_id=voice_id).first()
    if not voice:
        raise HTTPException(status_code=404, detail="Voice not found")

    # Inject dynamic preview URL
    voice.preview_audio_url = (
        f"https://ai.webvio.in/backend-py/llm-voice/preview/{voice_id}"
    )

    return voice



ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
ELEVENLABS_VOICE_URL = "https://api.elevenlabs.io/v1/voices"
# ELEVENLABS_CREATE_VOICE_URL = "https://api.elevenlabs.io/v1/voices/add"

@router.get("/all-voices", response_model=APIResponse)
async def list_voices(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    try:
        headers = {
            "xi-api-key": ELEVEN_API_KEY,
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(ELEVENLABS_VOICE_URL, headers=headers)

        if response.status_code != 200:
            raise Exception(f"ElevenLabs API error: {response.text}")

        data = response.json()
        voices_data = data.get("voices", [])

        # Map ElevenLabs voice to your schema
        formatted_voices = []
        for voice in voices_data:
            formatted_voices.append(
                {
                    "voice_id": voice["voice_id"],
                    "voice_name": voice["name"],
                    "provider": "elevenlabs",
                    "gender": voice.get("labels", {}).get("gender", "unknown"),
                    "accent": voice.get("labels", {}).get("accent", "unknown"),
                    "age": voice.get("labels", {}).get("age", "unknown"),
                    "preview_audio_url": voice.get("preview_url", ""),
                }
            )

        # Validate and convert to VoiceOut
        try:
            voices = [VoiceOut.model_validate(voice) for voice in formatted_voices]
        except ValueError as validation_error:
            return APIResponse(
                status=False,
                message="Validation error",
                data=None,
                errors=[{"field": "voice_data", "message": str(validation_error)}],
            )

        return APIResponse(
            status=True, message="Voices fetched successfully", data=voices, errors=[]
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "status": False,
                "message": "Internal Server Error",
                "data": None,
                "errors": [{"field": "server", "message": str(e)}],
            },
        )

@router.get("/llm-voice/preview/{voice_id}")
async def generate_preview(
    voice_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Optional: ensure voice exists
    voice = db.query(LLMVoice).filter_by(voice_id=voice_id).first()
    if not voice:
        raise HTTPException(status_code=404, detail="Voice not found")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

    payload = {
        "text": "Hello! This is a preview of my cloned voice.",
        "model_id": "eleven_multilingual_v2"
    }

    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"ElevenLabs Error: {resp.text}"
        )

    return StreamingResponse(
        iter([resp.content]),
        media_type="audio/mpeg"
    )


