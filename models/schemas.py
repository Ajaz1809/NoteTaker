from pydantic import BaseModel, Field, EmailStr, field_validator
from datetime import datetime
from typing import Optional, List, Literal, Dict, Any, Union
import uuid

class UserSignup(BaseModel):
    username: str = Field(..., min_length=5)
    email: EmailStr
    full_name: str
    password: str
    retall_api_key: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str
    ucaas_token: Optional[str] = None


class UpdateUser(BaseModel):
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = None


class WorkspaceCreate(BaseModel):
    name: str
    description: Optional[str] = None


class WorkspaceOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    created_at: str
    owner_username: str

    class Config:
        from_attributes = True


class InviteMember(BaseModel):
    username: str
    role: Optional[str] = "member"


class DispatchRequest(BaseModel):
    room_name: str
    agent_name: str
    metadata: Optional[Dict] = None
    meeting_id: str = None   
    conf_name: Optional[str] = None
    user_id: Optional[int] = None   

class CallIDRequest(BaseModel):
    roomid: Optional[str] = None
    user_id: Optional[str] = None


class CreateRoomRequestSchema(BaseModel):
    agent_name: str
    room_name: str
    participant_identity: str
    participant_name: str
    metadata: Optional[Dict[str, Any]] = None


class KnowledgeBaseCreate(BaseModel):
    name: str
    workspace_id: int


class TokenRequest(BaseModel):
    room_name: str
    user_id: str
    agent_name: str


class KnowledgeBaseOut(BaseModel):
    id: str
    name: str
    file_path: str
    created_at: datetime
    workspace_id: int

    class Config:
        from_attributes = True
        json_encoders = {datetime: lambda v: v.isoformat()}


class APIKeyCreate(BaseModel):
    name: str


class WorkspaceSettingsUpdate(BaseModel):
    default_voice: Optional[str] = None
    default_model: Optional[str] = None
    temperature: Optional[float] = None

# Agent schemas----------------------------------------------


class ResponseEngine(BaseModel):
    type: Literal["pbx-llm", "custom-llm", "conversation-flow", "retell-llm"]
    config: Optional[dict] = None
    llm_id: Optional[str] = None
    version: Optional[int] = None
    llm_websocket_url: Optional[str] = None

    @field_validator("llm_id")
    @classmethod
    def validate_llm_id(cls, v, info):
        if info.data.get("type") == "pbx-llm" and not v:
            raise ValueError("llm_id is required for pbx-llm type")
        return v

    @field_validator("llm_websocket_url")
    @classmethod
    def validate_ws_url(cls, v, info):
        if info.data.get("type") == "custom-llm" and not v:
            raise ValueError("llm_websocket_url is required for custom-llm type")
        return v


class VoicemailAction(BaseModel):
    type: str
    text: str


class VoicemailOption(BaseModel):
    action: VoicemailAction


class Pronunciation(BaseModel):
    word: str
    alphabet: str
    phoneme: str

class GetPBXLLMOut(BaseModel):
    id: str
    workspace_id: int
    version: int
    model: Optional[str]
    s2s_model: Optional[str]
    model_temperature: float
    model_high_priority: bool
    tool_call_strict_mode: bool
    general_prompt: Optional[str]
    general_tools: Optional[List[Dict[str, Any]]]
    states: Optional[List[Dict[str, Any]]]
    starting_state: Optional[str]
    begin_message: Optional[str]
    default_dynamic_variables: Optional[Dict[str, Any]]
    knowledge_base_ids: Optional[List[str]]
    is_published: bool
    last_modification_timestamp: int

    class Config:
        from_attributes = True  # for Pydantic v2
        validate_by_name = True


# PBX LLM SCHEMAS----------------------------------------------

class Tool(BaseModel):
    type: str
    name: str
    description: str

class StateEdge(BaseModel):
    destination_state_name: str
    description: str


class LLMState(BaseModel):
    name: str
    state_prompt: str
    edges: Optional[List[StateEdge]] = []
    tools: Optional[List[Tool]] = []

class StartAgentRequest(BaseModel):
    room: str
    kb_id: str
    persona: str = "You are a helpful voice assistant."
    model_llm: str = "gpt-4o"
    model_tts: str = "tts-1"
    voice_tts: str = "nova"
    model_stt: str = "general"

class PBXLLMCreate(BaseModel):
    workspace_id: int
    version: Optional[int] = 0
    model: Optional[str] = None
    s2s_model: Optional[str] = None
    model_temperature: Optional[float] = 0.0
    model_high_priority: Optional[bool] = False
    tool_call_strict_mode: Optional[bool] = False
    general_prompt: Optional[str] = None
    general_tools: Optional[List[Dict[str, Any]]] = None
    states: Optional[List[Dict[str, Any]]] = None
    starting_state: Optional[str] = None
    begin_message: Optional[str] = None
    default_dynamic_variables: Optional[Dict[str, str]] = None
    knowledge_base_ids: Optional[List[str]] = None


class PBXLLMOut(BaseModel):
    status: bool
    llm_id: str


#  Chat room -----------------------------------------------

class MessageWithToolCall(BaseModel):
    message_id: str
    role: str
    content: str
    created_timestamp: int

class ProductCost(BaseModel):
    product: str
    unitPrice: float
    cost: float

class ChatCost(BaseModel):
    product_costs: List[ProductCost]
    combined_cost: float


class ChatAnalysis(BaseModel):
    chat_summary: str
    user_sentiment: str
    chat_successful: bool
    custom_analysis_data: Optional[Dict[str, Any]] = None


class CreateChatRequest(BaseModel):
    agent_id: str
    chat_status: str
    agent_version: Optional[int] = 0
    metadata: Optional[Dict[str, Any]] = None
    llm_dynamic_variables: Optional[Dict[str, str]] = None
    start_timestamp: Optional[int] = None
    end_timestamp: Optional[int] = None
    transcript: Optional[str] = None
    chat_cost: Optional[ChatCost] = None
    chat_analysis: Optional[ChatAnalysis] = None


class CreateChatResponse(BaseModel):
    chat_id: str
    agent_id: str
    chat_status: str
    llm_dynamic_variables: Optional[Dict[str, str]] = None
    collected_dynamic_variables: Optional[Dict[str, str]] = None
    start_timestamp: Optional[int] = None
    end_timestamp: Optional[int] = None
    transcript: Optional[str] = None
    message_with_tool_calls: Optional[List[MessageWithToolCall]] = None
    metadata: Optional[Dict[str, Any]] = None
    chat_cost: Optional[ChatCost] = None
    chat_analysis: Optional[ChatAnalysis] = None


# voice


class VoiceBase(BaseModel):
    voice_id: str
    voice_name: str
    provider: Literal["elevenlabs", "openai", "deepgram"]
    gender: Literal["male", "female"]
    accent: Optional[str] = None
    age: Optional[str] = None
    preview_audio_url: Optional[str] = None


class VoiceCreate(VoiceBase):
    pass


class VoiceOut(BaseModel):
    voice_id: str
    voice_name: str
    provider: str
    gender: str
    accent: str
    age: str
    preview_audio_url: Optional[str] = None

    class Config:
        from_attributes = True


class VoiceListResponse(BaseModel):
    status: bool
    data: List[VoiceOut]

class APIResponse(BaseModel):
    status: bool
    message: str
    data: Optional[List[VoiceOut]] = None
    errors: List[dict] = []

class LatencyMetrics(BaseModel):
    p50: int
    p90: int
    p95: int
    p99: int
    max: int
    min: int
    num: int
    values: List[int]

class CallAnalysis(BaseModel):
    call_summary: str
    in_voicemail: bool
    user_sentiment: str
    call_successful: bool
    custom_analysis_data: Dict[str, Any]

class CallCost(BaseModel):
    product_costs: List[Dict[str, Any]]
    total_duration_seconds: int
    total_duration_unit_price: int
    total_one_time_price: int
    combined_cost: int


class UpdateCallMetadata(BaseModel):
    metadata: Dict[str, Any]
    opt_out_sensitive_data_storage: Optional[bool] = None

class NumberAssignRequest(BaseModel):
    phone_number: str
    termination_uri: str

# ////////////////////////////////////
class PlayPauseRequest(BaseModel):
    meeting_id: str
    pause: bool

class PlayPauseResponse(BaseModel):
    meeting_id: str
    paused: bool
    message: str
    success: bool
    
class CallListRequest(BaseModel):
    user_id: Optional[str] = None
    summary_id: Optional[str] = None    
    
class NoteTakerCallResponse(BaseModel):
    id: str
    call_type: str
    call_id: str
    call_status: str
    start_timestamp: Optional[int]
    end_timestamp: Optional[int]
    duration_ms: Optional[int]
    recording_url: Optional[str]
    call_analysis: Optional[dict]
    created_at: int
    updated_at: int
    meeting_id: Optional[str]
    conf_name: Optional[str]
    user_id: Optional[str]

    class Config:
        from_attributes = True
        
class Attendee(BaseModel):
    name: str
    email: EmailStr
    timeZone: Optional[str] = None
    phoneNumber: Optional[str] = None
    language: Optional[str] = None

