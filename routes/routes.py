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
from sqlalchemy.orm import Session, joinedload
import uuid
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
    WorkspaceCreate,
    WorkspaceOut,
    InviteMember,
    WorkspaceSettingsUpdate,
    CallIDRequest,
    ReanalyzeRequest,
  
)
from utils.helpers import (
    format_response,
    validate_email,
    validate_password,
    format_datetime_ist,
)


from livekit.api import (
    LiveKitAPI,
    RoomParticipantIdentity
)
from utils.security import hash_password, verify_password, create_token
from starlette.config import Config
from utils.custom_voice import validate_voice_id
import re,logging
from db.database import engine
import asyncio
from models.models import NoteTakerReanalysis
from openai import OpenAI

import time
import traceback
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from livekit import api
from livekit.api import (
    RoomParticipantIdentity,
    ListParticipantsRequest,
)

import os
VALIDATION_API_URL = os.getenv("VALIDATION_API_URL")

def _coerce_list(value):
    """Convert value to list if it's not already a list"""
    if isinstance(value, list):
        return value
    elif isinstance(value, str):
        return [value]
    else:
        return []


def _safe_text(value):
    """Convert value to safe text string"""
    return str(value).strip() if value else ""


def dedup_list(items):
    """Remove duplicates from list while preserving order"""
    seen = set()
    result = []
    for item in items:
        item_lower = item.lower().strip()
        if item_lower not in seen and item_lower:
            seen.add(item_lower)
            result.append(item)
    return result

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


##################################################################################################new

@router.post("/create-room-token")
async def create_room_and_token(
    request: CreateRoomRequestSchema,
    db: Session = Depends(get_db),
    authorization: str = Header(..., description="Bearer token")
):
    try:
        if not authorization.startswith('Bearer '):
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        auth_token = authorization.split(' ')[1]  # Renamed to avoid conflict
        
        # Try UCAAS validation first
        try:
            url = os.getenv('EXTERNAL_URL', 'https://ucaas.webvio.in/backend/api/user')  # Fixed: os.getenv
            headers = {'Authorization': f'Bearer {auth_token}'}
            
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
                    payload = decode_token(auth_token)
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

        # Search for agent in user's workspaces
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
        
        # Create room
        lkapi = api.LiveKitAPI(
            url=LIVEKIT_URL, api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET
        )
        try:
            metadata_payload = {
                "meeting_id": str(request.meeting_id),
                "user_id": str(request.user_id),
                "conf_name": str(request.conf_name),
            }
            lkapi.room.create_room(
                api.CreateRoomRequest(
                    name=request.room_name,
                    empty_timeout=10 * 60,
                    max_participants=10,
                    metadata=json.dumps(metadata_payload) if request.metadata else None,
                )
            )
            print(f"  Room '{request.room_name}' created successfully. Meeting ID: {request.meeting_id}, User ID: {request.user_id}, Conf Name: {request.conf_name}\n")
        except Exception as e:
            if "already exists" not in str(e):
                raise HTTPException(status_code=500, detail=f"LiveKit Error: {e}")

        # Generate token
        jwt_token = (
            api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            .with_identity(request.participant_identity)
            .with_name(request.participant_name)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=request.room_name,
                )
            )
        ).to_jwt()
        
        return jwt_token

    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")


@router.post("/create-dispatch")
async def create_dispatch(request: DispatchRequest):
    async with api.LiveKitAPI() as lkapi:
        try:
            print(f"Attempting to dispatch agent '{request.agent_name}' to room: {request.room_name} with meeting_id: {request.meeting_id}, user_id: {request.user_id}, conf_name: {request.conf_name}")
            metadata_payload = {
                "meeting_id": str(request.meeting_id),
                "user_id": str(request.user_id),
                "conf_name": str(request.conf_name),
            }
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name="note-taker-agent",
                    room=request.room_name,
                    metadata=json.dumps(metadata_payload),
                )
            )
            print(f"  Successfully dispatched agent 'note-taker-agent' to room: {request.room_name} with meeting_id: {request.meeting_id}, user_id: {request.user_id}, conf_name: {request.conf_name}\n")
            return {
                "status": "success",
                "dispatch_id": dispatch.id,
                "room": dispatch.room,
                "agent_name": dispatch.agent_name,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


############### Notetaker-specific routes (with DB integration)

@router.post("/notetaker-dispatch-try")
async def notetaker_dispatch_check(  # Renamed function
    request: DispatchRequest,
    db: Session = Depends(get_notetaker_db),
):
    async with LiveKitAPI() as lkapi:
        try:
            res = await lkapi.room.get_participant(
                RoomParticipantIdentity(
                    room=request.room_name,
                    identity=request.agent_name,
                )
            )
            if res and res.identity:
                return {
                    "status": False,
                    "message": "Agent already exists",
                    "data": res.identity,
                }
        except Exception:
            return {
                "status": True,
                "message": "Agent not found",
                "data": None,
            }



    
###################redis flag changes are here with new dispatcher changes###############

import redis

# Redis connection
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD", ""),
    decode_responses=True
)


@router.post("/notetaker-dispatch")
async def notetaker_dispatch(
    request: DispatchRequest,
    db: Session = Depends(get_notetaker_db),
):
    try:
        LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
        LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
        LIVEKIT_URL = os.getenv("LIVEKIT_URL")

        if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_URL):
            raise HTTPException(status_code=500, detail="LiveKit credentials not configured")

        async with api.LiveKitAPI(url=LIVEKIT_URL, api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET) as lkapi:
            
            flag_key = f"transcription:active:{request.room_name}"
            
            # ============================================
            # STEP 1: Check if already transcribing
            # ============================================
            current_flag = redis_client.get(flag_key)
            if current_flag == "1":
                print(f"🎉 Agent already transcribing!")
                return {
                    "status": "success",
                    "room": request.room_name,
                    "message": "Agent already transcribing"
                }

            # ============================================
            # STEP 2: Remove old agent + Ensure room exists
            # ============================================
            try:
                await lkapi.room.remove_participant(
                    api.RoomParticipantIdentity(
                        room=request.room_name,
                        identity=f"note-taker-{request.room_name}"
                    )
                )
                print(f"🗑️ Old agent removed")
                await asyncio.sleep(3)
            except:
                pass

            try:
                await lkapi.room.create_room(
                    api.CreateRoomRequest(
                        name=request.room_name,
                        empty_timeout=30 * 60,
                        max_participants=20,
                    )
                )
                print(f"✅ Room: {request.room_name}")
            except:
                pass

            # ============================================
            # STEP 3: Database
            # ============================================
            try:
                existing_call = db.query(NoteTakerCall).filter(
                    NoteTakerCall.call_id == request.room_name,
                    NoteTakerCall.call_status == "active"
                ).first()
                if not existing_call:
                    new_call = NoteTakerCall(
                        call_id=request.room_name,
                        start_timestamp=int(time.time() * 1000),
                        meeting_id=request.meeting_id,
                        conf_name=request.conf_name,
                        user_id=request.user_id,
                        call_status="active"
                    )
                    db.add(new_call)
                    db.commit()
            except:
                db.rollback()

            # ============================================
            # STEP 4: Store metadata in Redis
            # ============================================
            metadata_dict = {
                "meeting_id": str(request.meeting_id),
                "user_id": str(request.user_id),
                "conf_name": str(request.conf_name),
            }
            redis_client.setex(f"meeting:meta:{request.room_name}", 300, json.dumps(metadata_dict))

            # ============================================
            # STEP 5: Delete old flag + Dispatch
            # ============================================
            redis_client.delete(flag_key)
            
            metadata_payload = {
                "meeting_id": str(request.meeting_id),
                "user_id": str(request.user_id),
                "conf_name": str(request.conf_name),
            }

            print(f"🚀 Dispatching to: {request.room_name}")
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name="note-taker-agent",
                    room=request.room_name,
                    metadata=json.dumps(metadata_payload)
                )
            )
            print(f"   Dispatch ID: {dispatch.id}")

            # ============================================
            # STEP 6: Wait for Redis flag = 1
            # ============================================
            MAX_RETRIES = 10
            
            for attempt in range(1, MAX_RETRIES + 1):
                print(f"\n{'─'*40}")
                print(f"🔄 Attempt {attempt}/{MAX_RETRIES}")
                
                for wait_count in range(15):
                    await asyncio.sleep(2)
                    
                    flag_value = redis_client.get(flag_key)
                    
                    if flag_value == "1":
                        print(f"\n🎉 [{wait_count*2}s] FLAG=1!")
                        return {
                            "status": "success",
                            "dispatch_id": dispatch.id,
                            "room": request.room_name,
                            "attempts": attempt,
                            "message": "Agent transcribing"
                        }
                    
                    print(f"   [{wait_count*2}s] Flag: {flag_value}")
                
                # Re-dispatch
                if redis_client.get(flag_key) != "1":
                    print(f"   🔄 Re-dispatching...")
                    
                    try:
                        await lkapi.room.remove_participant(
                            api.RoomParticipantIdentity(
                                room=request.room_name,
                                identity=f"note-taker-{request.room_name}"
                            )
                        )
                        await asyncio.sleep(3)
                    except:
                        pass
                    
                    redis_client.delete(flag_key)
                    
                    dispatch = await lkapi.agent_dispatch.create_dispatch(
                        api.CreateAgentDispatchRequest(
                            agent_name="note-taker-agent",
                            room=request.room_name,
                            metadata=json.dumps(metadata_payload)
                        )
                    )
                    print(f"   🚀 Re-dispatched: {dispatch.id}")

            return {
                "status": "failed",
                "room": request.room_name,
                "message": "Failed after max attempts"
            }

    except Exception as e:
        print("ERROR:", str(e))
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


#####old code has been commented out for reference, new code above has improved logic with retries and flag checks
# @router.get("/notetaker/call-list")
# def get_calls(user_id: Optional[str] = None, db: Session = Depends(get_notetaker_db)):
#     try:
#         query = db.query(NoteTakerCall)
#         if user_id:
#             query = query.filter(NoteTakerCall.user_id == user_id)

#         calls = query.all()
#         if not calls:
#             return format_response(
#                 status=False,
#                 message="No calls found",
#                 errors=[{"field": "calls", "message": "No calls found"}],
#             )

#         return format_response(
#             status=True,
#             message="Calls retrieved successfully",
#             data=[
#                 {
#                     "id": c.id,
#                     "call_type": c.call_type,
#                     "call_id": c.call_id,
#                     "call_status": c.call_status,
#                     "start_timestamp": c.start_timestamp,
#                     "end_timestamp": c.end_timestamp,
#                     "duration_ms": c.duration_ms,
#                     "recording_url": c.recording_url,
#                     "call_analysis": c.call_analysis,
#                     "created_at": c.created_at,
#                     "updated_at": c.updated_at,
#                     "meeting_id": c.meeting_id,
#                     "conf_name": c.conf_name,
#                     "user_id": c.user_id,   # NEW
#                 }
#                 for c in calls
#             ],
#         )


#     except Exception as e:
#         return format_response(
#             status=False,
#             message="Internal Server Error",
#             errors=[{"field": "server", "message": str(e)}],
#         )
  
  

#new changes here for filtering by summary_id and status, also added error handling for invalid status values and no calls found scenarios


from fastapi import APIRouter, Depends, Query,Request
from sqlalchemy.orm import Session
from typing import Optional
import math

@router.get("/notetaker/call-list")
def get_calls(
    request: Request,
    user_id: Optional[str] = None,
    order: str = Query("desc", enum=["asc", "desc"]),
    search: Optional[str] = None,
    call_type: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    db: Session = Depends(get_notetaker_db),
):
    token = request.headers.get("Authorization")
    # print(f"\n\nSSSSSSSSSSSSSSSSStoken : {token}\n\n")
    # url="https://meeting.webvio.in/backend-php/api/user"
    headers = {
        "Authorization": f"Bearer {token}"
    }
    response = requests.get(VALIDATION_API_URL, headers=headers)
    status_code = response.status_code
    resp_message = response.json().get("message", response.json().get("error"))  
    print(f"status_code: {status_code} ||  Message: {resp_message}\n")
    ##Add here validation for if user id is blank or not provided then return response 401 unauthorized with message "User ID is required to fetch calls"
    if not user_id:
        return JSONResponse(
        status_code=401,
        content=format_response(
            status=False,
            message="User ID is required to fetch calls",
            errors=[
                {
                    "field": "user_id",
                    "message": "User ID is required"
                }
            ],
        )
    ) 
        
    # in this scenario if have the status code !=200 then return response 401 unauthorized with message "Invalid or expired token"
    elif response.status_code != 200:
        return JSONResponse(status_code=status_code, content=format_response(
            status=False,
            message=resp_message or "Invalid or expired token",

        ))  
            
    try:
        query = db.query(NoteTakerCall)

        # Filter by user_id
        if user_id:
            # query = query.filter(NoteTakerCall.user_id == user_id)
            query = query.filter(
                NoteTakerCall.user_id == user_id,
                NoteTakerCall.call_analysis.isnot(None)
            )

        # Filter by call_type
        if call_type:
            query = query.filter(NoteTakerCall.call_type == call_type)

        # Search by meeting/conference name
        if search:
            query = query.filter(
                NoteTakerCall.conf_name.ilike(f"%{search}%")
            )

        # Ordering
        order = order.lower()
        if order == "asc":
            query = query.order_by(NoteTakerCall.created_at.asc())
        else:
            query = query.order_by(NoteTakerCall.created_at.desc())

        # Total count before pagination
        total_records = query.count()

        # Pagination calculations
        offset = (page - 1) * limit
        total_pages = math.ceil(total_records / limit)

        # Fetch paginated data
        calls = query.offset(offset).limit(limit).all()

        return format_response(
            status=True,
            message="Calls retrieved successfully",
            data={
                "pagination": {
                    "current_page": page,
                    "limit": limit,
                    "total_records": total_records,
                    "total_pages": total_pages,
                    "has_next": page < total_pages,
                    "has_previous": page > 1,
                },
                "records": [
                    {
                        "id": c.id,
                        "call_type": c.call_type,
                        "call_id": c.call_id,
                        "call_status": c.call_status,
                        "start_timestamp": c.start_timestamp,
                        "end_timestamp": c.end_timestamp,
                        "duration_ms": c.duration_ms,
                        # "recording_url": c.recording_url,
                        # "call_analysis": c.call_analysis,
                        "created_at": c.created_at,
                        "updated_at": c.updated_at,
                        "meeting_id": c.meeting_id,
                        "conf_name": c.conf_name,
                        "user_id": c.user_id,
                    }
                    for c in calls
                ],
            },
        )

    except Exception as e:
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{
                "field": "server",
                "message": str(e)
            }],
        )


@router.post("/notetaker/call-list")
def get_calls(payload: CallListRequest, db: Session = Depends(get_notetaker_db)):
    try:
    
        query = db.query(NoteTakerCall)

        if payload.summary_id:
            query = query.filter(NoteTakerCall.id == payload.summary_id)
        elif payload.user_id:
            query = query.filter(NoteTakerCall.user_id == payload.user_id)
        
        # ✅ Optional: Add status filter
        if payload.status:
            if payload.status not in ["active", "paused", "ended"]:
                return format_response(
                    status=False,
                    message="Invalid status value",
                    errors=[{"field": "status", "message": "Status must be 'active', 'paused', or 'ended'"}],
                )
            query = query.filter(NoteTakerCall.call_status == payload.status)

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
    
    
##In this i need to add new show-notes api end point in which we get call_id as path parameter then give return data same call-list also apply validation and autherization

@router.get("/notetaker/show-notes/{call_id}")
def show_notes(
    request: Request,
    call_id: str,
    db: Session = Depends(get_notetaker_db),
):
    # Token authentication
    token = request.headers.get("Authorization")
    
    # Remove 'Bearer ' prefix if it exists
    if token and token.startswith("Bearer "):
        token = token.replace("Bearer ", "")
    
    if not token:
        return JSONResponse(
            status_code=401,
            content=format_response(
                status=False,
                message="Authorization token is required",
                errors=[{
                    "field": "Authorization",
                    "message": "Token is missing"
                }]
            )
        )
    
    headers = {
        "Authorization": f"Bearer {token}"  # Now token doesn't have 'Bearer ' prefix
    }
    response = requests.get(VALIDATION_API_URL, headers=headers)
    status_code = response.status_code
    resp_message = response.json().get("message", response.json().get("error"))  
    print(f"status_code: {status_code} ||  Message: {resp_message}\n")
    
    # Validate token response
    if response.status_code != 200:
        return JSONResponse(
            status_code=status_code, 
            content=format_response(
                status=False,
                message=resp_message or "Invalid or expired token",
            )
        )
    
    # Validate call_id (though FastAPI ensures it's not empty as path param)
    if not call_id or call_id.strip() == "":
        return JSONResponse(
            status_code=400,
            content=format_response(
                status=False,
                message="Call ID is required",
                errors=[{
                    "field": "call_id",
                    "message": "Call ID cannot be empty"
                }]
            )
        )
    
    try:
        # Fetch the call by call_id directly
        call = db.query(NoteTakerCall).filter(
            NoteTakerCall.id == call_id,  # Direct match with path parameter
            NoteTakerCall.call_analysis.isnot(None)
        ).first()
        
        # Check if call exists
        if not call:
            return JSONResponse(
                status_code=404,
                content=format_response(
                    status=False,
                    message="Call not found",
                    errors=[{
                        "field": "call_id",
                        "message": f"No call found with ID: {call_id}"
                    }]
                )
            )
        
        # Return the notes/call details
        return format_response(
            status=True,
            message="Notes retrieved successfully",
            data={
                "id": call.id,
                "call_type": call.call_type,
                "call_id": call.call_id,
                "call_status": call.call_status,
                "start_timestamp": call.start_timestamp,
                "end_timestamp": call.end_timestamp,
                "duration_ms": call.duration_ms,
                "recording_url": call.recording_url,
                "call_analysis": call.call_analysis,  # This contains the notes
                "created_at": call.created_at,
                "updated_at": call.updated_at,
                "meeting_id": call.meeting_id,
                "conf_name": call.conf_name,
                "user_id": call.user_id,
            },
        )
        
    except Exception as e:
        return format_response(
            status=False,
            message="Internal Server Error",
            errors=[{
                "field": "server",
                "message": str(e)
            }],
        )

@router.get("/notetaker/health")
def notetaker_health_check(
    db: Session = Depends(get_notetaker_db),
    authorization: str = Header(..., description="Bearer token")
):
    """Check if note-taker system is healthy"""
    try:
        # Check if there are any recent calls (last 5 minutes)
        five_min_ago = int((time.time() - 300) * 1000)
        recent_calls = db.query(NoteTakerCall).filter(
            NoteTakerCall.start_timestamp >= five_min_ago
        ).count()
        
        # Check for any stuck 'pending' calls (older than 1 hour)
        one_hour_ago = int((time.time() - 3600) * 1000)
        pending_calls = db.query(NoteTakerCall).filter(
            NoteTakerCall.call_analysis['status'].astext == 'pending',
            NoteTakerCall.start_timestamp < one_hour_ago
        ).count()
        
        return format_response(
            status=True,
            message="Note-taker system healthy",
            data={
                "recent_calls_5min": recent_calls,
                "stuck_pending_calls": pending_calls,
                "consumer_running": pending_calls == 0,  # Approximate
            }
        )
    except Exception as e:
        return format_response(
            status=False,
            message="Health check failed",
            errors=[{"field": "server", "message": str(e)}],
        )

# RabbitMQ connection and message processing would go here (not shown for brevity)

@router.post("/notetaker/webhook/{call_id}")
async def notetaker_webhook(
    call_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_notetaker_db),
):
    """
    Webhook endpoint that consumer can call when analysis is complete
    """
    try:
        call = db.query(NoteTakerCall).filter(NoteTakerCall.id == call_id).first()
        if not call:
            return format_response(status=False, message="Call not found")
        
        # Update call with webhook data
        call.call_analysis = payload.get("analysis", call.call_analysis)
        call.call_status = "completed"
        db.commit()
        
        # You can also trigger notifications here (email, SMS, etc.)
        
        return format_response(status=True, message="Webhook processed")
    except Exception as e:
        return format_response(status=False, message=str(e))


########################Add for analysis service integration############################

# ==================== RE-ANALYSIS ENDPOINTS ====================
@router.post("/notetaker/reanalyze")
def reanalyze_transcript(
    request: ReanalyzeRequest,
    db: Session = Depends(get_notetaker_db)
):
    """
    Re-analyze transcript using OpenAI and store in separate table
    """
    max_version = int(os.getenv("MAX_REANALYSIS_VERSIONS", 5))
    existing_reanalyses_count = db.query(NoteTakerReanalysis).filter(
        NoteTakerReanalysis.original_call_id == request.call_id
    ).count()
    
    
    if existing_reanalyses_count >= max_version:
        return format_response(
            status=False,
            message=f"Limit reached: Maximum {max_version} re-analyses allowed per call. Please remove an older version to continue",
            errors=[{
                "field": "reanalysis_limit",
                "message": f"Limit: {max_version} versions per call",
                "current_versions": existing_reanalyses_count
            }]
        )
    
    start_time = time.time()
    print(f"Re-analysis request received for call_id: {request.call_id} by user_id: {request.user_id}")
    try:
        # Fetch original call
        original_call = db.query(NoteTakerCall).filter(
            NoteTakerCall.id == request.call_id
        ).first()
        participants_list = getattr(original_call, "participants_list", [])
        
        if not original_call:
            return format_response(
                status=False,
                message="Call not found",
                errors=[{"field": "call_id", "message": f"No call found with id: {request.call_id}"}]
            )
        
        # Verify user access
        if request.user_id and original_call.user_id != request.user_id:
            return format_response(
                status=False,
                message="Unauthorized",
                errors=[{"field": "user_id", "message": "You don't have access to this call"}]
            )
        
        # Extract raw transcript
        if not original_call.call_analysis:
            return format_response(
                status=False,
                message="No analysis data found",
                errors=[{"field": "transcript", "message": "Call analysis data is missing"}]
            )
        
        raw_transcript = original_call.call_analysis.get("transcript_dict", [])
        print(f"Original transcript length: {len(raw_transcript)} lines")
        if not raw_transcript:
            return format_response(
                status=False,
                message="No transcript found",
                errors=[{"field": "transcript", "message": "Transcript data is missing"}]
            )
        
        # Get next version number
        last_reanalysis = db.query(NoteTakerReanalysis).filter(
            NoteTakerReanalysis.original_call_id == request.call_id
        ).order_by(NoteTakerReanalysis.reanalysis_version.desc()).first()
        
        next_version = (last_reanalysis.reanalysis_version + 1) if last_reanalysis else 1
        
        # Convert transcript to text
        transcript_text = "\n".join(raw_transcript)
        
        # Call OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        # UPDATED PROMPT with stronger action item grouping rules and JSON formatting
        prompt = f"""
You are an expert AI meeting assistant trained to generate structured, business-quality meeting summaries.

Analyze the following meeting transcript and return a structured JSON output.

Transcript:
{transcript_text}

Instructions:
- Understand context even if sentences are incomplete or noisy
- Convert conversational language into clear professional insights
- Focus only on meaningful business outcomes (decisions, problems, solutions, ownership)
- Avoid repetition across sections (no duplicate insights anywhere)
- Be concise, precise, and high-signal
- Do NOT copy transcript text directly

Extraction Rules (STRICT):

1. Meeting Purpose
- Must be a single, clear, unambiguous sentence
- Clearly answer: why the meeting happened and what outcome was expected
- Avoid generic phrases like "discussion" or "catch-up"
- Extract this SOLELY from the transcript content - do not invent or use placeholder values

2. Key Takeaways
- 3 to 5 unique insights only
- Each point must add new information
- Do NOT repeat information from decisions, topics, blockers, or next steps
- Each takeaway must be high-level and outcome-focused, not task-level
- Extract these SOLELY from the transcript content

3. Decisions
- Include only finalized and high-impact decisions
- Exclude discussions without clear outcomes
- Avoid repeating takeaways or topics
- Each decision must have clear business impact
- Extract these SOLELY from the transcript content

4. Topics (CRITICAL - Must include person/team ownership)
- Group discussions from the transcript into meaningful business topics
- Topics must be outcome-focused (what was discussed, not just who discussed)
- Avoid using only team names as titles unless necessary
- Topic titles must be derived from actual discussion subjects in the transcript

Each topic must include:
    - title: short, specific, and outcome-focused (derive from what was actually discussed)
    - problem: issue discussed (include person/team name if mentioned in transcript, else null)
    - solution: outcome or conclusion (include who owns/contributes based on transcript)
    - rationale: why this solution/decision was chosen (based on transcript discussion)
    - postponed_items: include only explicitly postponed items mentioned in transcript, else []

5. Action Items (CRITICAL RULES - No person left behind):
 **You have the all participants list {participants_list}, mentioned all update into action item do not missed any one to update into action item if they have any task to do, and make sure every participant appears once in action item if they have task to do .**
- Identify EVERY person mentioned in the transcript who has assigned tasks or responsibilities
- Create ONE action item per person (owner)
- If a person has MULTIPLE tasks mentioned in transcript, combine ALL their tasks into a SINGLE action item
- The 'task' field must contain a comprehensive list of ALL responsibilities for that owner from the transcript
- Use format: comma separated within the single task field
- DO NOT create separate action items for the same owner
- Include owners even if they have only one task
- If owner name is not mentioned in transcript, use their role or "unassigned"
- Priority assignment rules (based on transcript context):
    - high → urgent / blockers / critical path / impacts delivery date (as mentioned in transcript)
    - medium → important but not urgent / standard feature work
    - low → minor optimizations / nice-to-have / no clear deadline mentioned
- Include deadline ONLY if explicitly mentioned in transcript (date or relative like "tomorrow")

Action item structure:
{{
    "owner": "person's name or role from transcript",
    "task": "Task from transcript, Another task from transcript",
    "priority": "high | medium | low",
    "deadline": "due date if mentioned in transcript, else null"
}}

6. Blockers
- Only include issues from transcript that impact delivery, timelines, or dependencies
- Exclude minor or already resolved concerns
- Each blocker must include implicit or explicit impact if mentioned in transcript
- Format as clear, actionable statements extracted from transcript

7. Next Steps (CRITICAL - Must follow exact Name: Content format):
- Include only the MOST IMPORTANT forward-looking, project-level actions mentioned in transcript (maximum 5-6 items)
- Define what happens next based on transcript (sequence or directional guidance)
- Do NOT repeat action items or tasks from action_items section
- **FORMAT REQUIREMENT**: Each next step must be written as "Name: Content"
- Name should be person's name or team name as mentioned in transcript (e.g., name from transcript + colon)
- Content should clearly state the action and timeline if mentioned in transcript
- If multiple people responsible in transcript, combine as "Person1 & Person2: Content"
- If no person mentioned in transcript, infer from context or use team name
- If multiple steps for same person from transcript, merge into one "Name: Content" entry with semicolons
- Do NOT use simple dash lists - enforce "Name: Content" format strictly
- Extract these SOLELY from the transcript - do not invent next steps

8. Meeting Tone & Observations
- Keep it short and insightful (2–4 points max)
- Focus on behavior observed in transcript: accountability, urgency, alignment, concerns, decision quality
- Include positive observations if warranted based on transcript (e.g., "clear ownership", "decisive resolution")
- Derive these from conversational cues in the transcript

9. Transcript Snapshot (Cleaned Highlights)
- 3–5 impactful, rephrased statements from transcript
- Include speaker attribution as mentioned in transcript (e.g., "Name from transcript: Statement")
- Focus on commitments, risks, or key statements from transcript
- Do NOT duplicate content from other sections
- Rephrase into professional language without changing meaning

10. Transcript Dict (Array of Key Statements)
- Include 2-3 important raw or lightly cleaned statements per major topic from transcript
- Preserve speaker attribution as in transcript
- Keep original meaning intact
- Useful for fallback context

JSON Schema:
{{
"meeting_purpose": "string",
"key_takeaways": ["..."],
"decisions": ["..."],
"topics": [
    {{
    "title": "string",
    "problem": "string or null",
    "solution": "string",
    "rationale": "string",
    "postponed_items": ["..."]
    }}
],
"action_items": [
    {{
    "owner": "string",
    "task": "string",
    "priority": "high | medium | low",
    "deadline": "string or null"
    }}
],
"blockers": ["..."],
"next_steps": ["..."],
"meeting_tone": ["..."],
"transcript_highlights": ["..."],
"transcript_dict": ["..."]
}}

CRITICAL JSON FORMATTING RULES (MUST FOLLOW):
- All string values MUST escape double quotes with backslash: He said \"hello\"
- No literal newlines inside string values - use \\n instead
- No control characters in strings
- No trailing commas in arrays or objects
- All property names MUST be double-quoted
- ALL strings MUST be properly terminated with closing quotes
- Do NOT include markdown code blocks or any text outside the JSON object

Final Rules (MUST FOLLOW):
- NO hardcoded or static example values from these instructions - extract EVERYTHING from the transcript
- No repeated or paraphrased information across any sections
- Each section must add unique value based on transcript content
- Merge duplicate insights intelligently (by meaning, not just wording)
- Prefer clarity over quantity
- Keep output concise and structured (no fluff)
- Output valid JSON only (no extra text, no markdown, no explanation)
- ENSURE every person with tasks from transcript gets an action item - no one is left out
- ENSURE next_steps array has each element in EXACT "Name: Content" format only
- ENSURE topics include person/team ownership from transcript in problem and solution fields
- ENSURE no generic titles like "Discussion" or "Updates" - use specific outcome-focused titles from transcript
- ENSURE rationale explains WHY based on transcript discussion, not just WHAT

Quality Checklist Before Output:
[ ] All content extracted from transcript - no invented or placeholder values
[ ] Meeting purpose answers "why" and "expected outcome" based on transcript
[ ] Key takeaways don't repeat decisions or topics
[ ] Each topic has title, problem, solution, rationale, postponed_items from transcript
[ ] Action items have one owner per entry with all tasks combined from transcript
[ ] Next steps follow "Name: Content" format strictly and come from transcript
[ ] No duplicate information across sections
[ ] Valid JSON only with proper escaping
"""
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Fixed model name
            messages=[
                {"role": "system", "content": "You are a meeting analyst. Always return valid JSON only. CRITICAL: Escape all double quotes inside strings with backslash. No markdown, no extra text, just pure JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_completion_tokens=2000,
            response_format={"type": "json_object"}
        )
        
        # Get raw response
        raw_response = response.choices[0].message.content
        
        # Function to clean and parse JSON
        def clean_and_parse_json(raw_json_str):
            """Attempt to clean and parse malformed JSON from LLM"""
            import re
            
            # Remove markdown code blocks if present
            raw_json_str = re.sub(r'^```json\s*', '', raw_json_str)
            raw_json_str = re.sub(r'^```\s*', '', raw_json_str)
            raw_json_str = re.sub(r'\s*```$', '', raw_json_str)
            
            # Remove any non-JSON text before/after
            json_match = re.search(r'\{.*\}', raw_json_str, re.DOTALL)
            if json_match:
                raw_json_str = json_match.group()
            
            # Remove control characters
            cleaned = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', raw_json_str)
            
            # Try direct parse first
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass
            
            # Fix common JSON issues
            try:
                # Fix unterminated strings by adding missing quotes
                lines = cleaned.split('\n')
                fixed_lines = []
                in_string = False
                for line in lines:
                    new_line = []
                    i = 0
                    while i < len(line):
                        char = line[i]
                        if char == '"' and (i == 0 or line[i-1] != '\\'):
                            in_string = not in_string
                            new_line.append(char)
                        elif char == '\n' and in_string:
                            new_line.append('\\n')
                        elif char == '"' and in_string and (i + 1 < len(line) and line[i+1] not in [',', '}', ']', ':', ' ']):
                            # Escaping unescaped quotes
                            new_line.append('\\"')
                        else:
                            new_line.append(char)
                        i += 1
                    fixed_lines.append(''.join(new_line))
                
                cleaned = '\n'.join(fixed_lines)
                
                # Fix odd number of quotes
                if cleaned.count('"') % 2 != 0:
                    # Find last position and add quote
                    cleaned = cleaned + '"'
                
                return json.loads(cleaned)
            except json.JSONDecodeError as e:
                # Try json_repair if available (optional dependency)
                try:
                    import json_repair
                    return json_repair.repair_json(cleaned, return_objects=True)
                except ImportError:
                    pass
                except Exception:
                    pass
                
                raise e
        
        # Parse response with cleaning
        try:
            result = clean_and_parse_json(raw_response)
        except json.JSONDecodeError as e:
            # Log error details for debugging
            print(f"Raw response (first 500 chars): {raw_response[:500]}")
            print(f"Raw response (last 500 chars): {raw_response[-500:]}")
            print(f"JSON parse error: {str(e)}")
            return format_response(
                status=False,
                message=f"Failed to parse AI response: {str(e)}",
                errors=[{"field": "llm", "message": str(e)}]
            )
        
        # Process action items with improved grouping and deduplication
        action_items = []
        raw_actions = result.get("action_items", [])
        
        if isinstance(raw_actions, list):
            # First, collect all action items by owner
            owner_map = {}
            
            for item in raw_actions:
                if isinstance(item, dict):
                    owner = _safe_text(item.get("owner", "unassigned")).strip().lower()
                    task = _safe_text(item.get("task", ""))
                    priority = _safe_text(item.get("priority", "medium"))
                    deadline = _safe_text(item.get("deadline")) if item.get("deadline") else None
                    
                    if owner not in owner_map:
                        owner_map[owner] = {
                            "owner": _safe_text(item.get("owner", "unassigned")),  # Keep original case
                            "tasks": [],
                            "priority": priority,
                            "deadline": deadline,
                            "original_priority": priority
                        }
                    
                    # Add task to the owner's task list
                    if task:
                        owner_map[owner]["tasks"].append(task)
                    
                    # Update priority to highest if multiple tasks have different priorities
                    if priority == "high" or owner_map[owner]["priority"] == "high":
                        owner_map[owner]["priority"] = "high"
                    elif priority == "medium" and owner_map[owner]["priority"] not in ["high"]:
                        owner_map[owner]["priority"] = "medium"
            
            # Convert owner_map to final action items list
            for owner_key, owner_data in owner_map.items():
                # Combine all tasks into one task field with numbering
                if len(owner_data["tasks"]) > 1:
                    combined_tasks = []
                    for idx, task in enumerate(owner_data["tasks"], 1):
                        combined_tasks.append(f"{idx}. {task}")
                    final_task = "; ".join(combined_tasks)
                else:
                    final_task = owner_data["tasks"][0] if owner_data["tasks"] else ""
                
                action_items.append({
                    "owner": owner_data["owner"],
                    "task": final_task,
                    "priority": owner_data["priority"],
                    "deadline": owner_data["deadline"]
                })
            
            # If no action items found from AI, try to extract from transcript as fallback
            if not action_items:
                print("No action items found in AI response, attempting fallback extraction")
                action_items = extract_action_items_fallback(transcript_text)
        
        # Process topics with new structure
        topics_list = result.get("topics", [])
        
        if not isinstance(topics_list, list):
            topics_list = []
        
        # Validate and clean each topic object with new fields
        validated_topics = []
        for topic in topics_list:
            if isinstance(topic, dict):
                validated_topic = {
                    "title": _safe_text(topic.get("title", "")),
                    "problem": _safe_text(topic.get("problem")) if topic.get("problem") else None,
                    "solution": _safe_text(topic.get("solution", "")),
                    "rationale": _safe_text(topic.get("rationale", "")),
                    "postponed_items": topic.get("postponed_items", []) if isinstance(topic.get("postponed_items", []), list) else []
                }
                validated_topics.append(validated_topic)
        
        # Create reanalysis data with updated structure
        reanalysis_data = {
            "meeting_purpose": _safe_text(result.get("meeting_purpose") or result.get("purpose") or ""),
            "key_takeaways": dedup_list(_coerce_list(result.get("key_takeaways") or result.get("key_points") or [])),
            "decisions": dedup_list(_coerce_list(result.get("decisions") or [])),
            "topics": validated_topics,
            "action_items": action_items,  # Now properly grouped by owner
            "blockers": dedup_list(_coerce_list(result.get("blockers") or result.get("negative_points") or [])),
            "next_steps": dedup_list(_coerce_list(result.get("next_steps") or [])),
            "meeting_tone": dedup_list(_coerce_list(result.get("meeting_tone") or [])),
            "transcript_highlights": dedup_list(_coerce_list(result.get("transcript_highlights") or [])),
            "transcript_dict": raw_transcript,
        }
        
        # Calculate processing time
        processing_time_ms = int((time.time() - start_time) * 1000)
        
        # Save ONLY to reanalysis table
        reanalysis_record = NoteTakerReanalysis(
            original_call_id=original_call.id,
            user_id=request.user_id or original_call.user_id,
            reanalysis_data=reanalysis_data,
            reanalysis_version=next_version,
            reanalysis_reason=request.reanalysis_reason,
            reanalysis_triggered_at=int(time.time() * 1000),
            status="completed",
            analysis_params={"model": "gpt-4o-mini", "temperature": 0.3},
            processing_time_ms=processing_time_ms
        )
        
        db.add(reanalysis_record)
        db.commit()
        db.refresh(reanalysis_record)
        
        return format_response(
            status=True,
            message="Re-analysis completed successfully",
            data={
                "reanalysis_id": reanalysis_record.id,
                "original_call_id": original_call.id,
                "reanalysis_version": next_version,
                "processing_time_ms": processing_time_ms,
                "analysis": reanalysis_data,
                "action_items_count": len(action_items),
                "unique_owners": [item["owner"] for item in action_items]
            }
        )
        
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI response: {str(e)}")
        return format_response(
            status=False,
            message=f"Failed to parse AI response: {str(e)}",
            errors=[{"field": "llm", "message": str(e)}]
        )
    except Exception as e:
        logger.error(f"Re-analysis failed: {str(e)}")
        return format_response(
            status=False,
            message="Failed to re-analyze transcript",
            errors=[{"field": "server", "message": str(e)}]
        )



def extract_action_items_fallback(transcript_text: str) -> list:
    """
    Fallback function to extract action items using a simpler prompt
    when the main extraction doesn't find any action items
    """
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        fallback_prompt = f"""
        Extract ALL action items from this transcript.
        
        CRITICAL RULES:
        1. Identify EVERY person mentioned with assigned tasks
        2. Create ONE action item per person
        3. Combine ALL tasks for the same person into ONE action item
        4. Format tasks as numbered list within the task field
        
        Transcript:
        {transcript_text[:8000]}  # Limit length for fallback
        
        Return JSON with action_items array only:
        {{
            "action_items": [
                {{
                    "owner": "person name",
                    "task": "1. First task; 2. Second task",
                    "priority": "high/medium/low",
                    "deadline": "date or null"
                }}
            ]
        }}
        """
        
        response = client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[
                {"role": "system", "content": "Extract action items per person. One action item per owner only."},
                {"role": "user", "content": fallback_prompt}
            ],
            temperature=0.3,
            max_completion_tokens=2500,
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        return result.get("action_items", [])
        
    except Exception as e:
        print(f"Fallback extraction failed: {str(e)}")
        return []


# Helper function to safely coerce values (add this if not already present)
def _coerce_list(value):
    """Convert value to list if it's not already"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return list(value) if hasattr(value, '__iter__') else [value]


@router.get("/notetaker/reanalysis/history/{call_id}")
def get_reanalysis_history(
    call_id: str,
    limit: int = 10,
    db: Session = Depends(get_notetaker_db)
):
    """Get all re-analysis versions for a call"""
    reanalyses = db.query(NoteTakerReanalysis).filter(
        NoteTakerReanalysis.original_call_id == call_id
    ).order_by(NoteTakerReanalysis.reanalysis_version.desc()).limit(limit).all()
    
    if not reanalyses:
        return format_response(
            status=True,
            message="No reanalysis history found",
            data={"total": 0, "reanalyses": []}
        )
    
    return format_response(
        status=True,
        message="History retrieved successfully",
        data={
            "total": len(reanalyses),
            "reanalyses": [
                {
                    "id": r.id,
                    "version": r.reanalysis_version,
                    "reason": r.reanalysis_reason,
                    "created_at": r.created_at,
                    "status": r.status,
                    "summary": r.reanalysis_data.get("meeting_purpose", "")[:100],
                    "key_takeaways_count": len(r.reanalysis_data.get("key_takeaways", [])),
                    "action_items_count": len(r.reanalysis_data.get("action_items", [])),
                    "owners": [item.get("owner") for item in r.reanalysis_data.get("action_items", [])]
                }
                for r in sorted(reanalyses, key=lambda r: r.reanalysis_version)
            ]
        }
    )


@router.get("/notetaker/reanalysis/details/{reanalysis_id}")
def get_reanalysis_details(
    reanalysis_id: int,
    db: Session = Depends(get_notetaker_db)
):
    """Get complete re-analysis details by ID"""
    reanalysis = db.query(NoteTakerReanalysis).filter(
        NoteTakerReanalysis.id == reanalysis_id
    ).first()
    
    if not reanalysis:
        return format_response(
            status=False,
            message="Reanalysis record not found",
            errors=[{"field": "reanalysis_id", "message": "Invalid ID"}]
        )
    
    return format_response(
        status=True,
        message="Details retrieved successfully",
        data={
            "id": reanalysis.id,
            "original_call_id": reanalysis.original_call_id,
            "version": reanalysis.reanalysis_version,
            "reason": reanalysis.reanalysis_reason,
            "created_at": reanalysis.created_at,
            "status": reanalysis.status,
            "processing_time_ms": reanalysis.processing_time_ms,
            "analysis_params": reanalysis.analysis_params,
            "analysis": reanalysis.reanalysis_data,
            "action_items_summary": {
                "total": len(reanalysis.reanalysis_data.get("action_items", [])),
                "by_owner": [
                    {
                        "owner": item.get("owner"),
                        "task_count": item.get("task", "").count(";") + 1 if item.get("task") else 0
                    }
                    for item in reanalysis.reanalysis_data.get("action_items", [])
                ]
            }
        }
    )
#####################end analyzer integration##############


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

