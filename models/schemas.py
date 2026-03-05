from pydantic import BaseModel, Field, EmailStr, field_validator, ConfigDict, model_validator, ConfigDict
from datetime import datetime
from typing import Optional, List, Literal, Dict, Any, Union
from enum import Enum
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


class KnowledgeFileOut(BaseModel):
    id: int
    filename: str
    file_path: str
    kb_id: str
    uploaded_at: datetime
    extract_data: Optional[str] = None

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


class PhoneNumberOut(BaseModel):
    id: str
    phone_number: str
    phone_number_type: Optional[str] = None
    phone_number_pretty: Optional[str] = None
    inbound_agent_id: Optional[str] = None
    outbound_agent_id: Optional[str] = None
    inbound_agent_version: Optional[str] = None
    outbound_agent_version: Optional[str] = None
    area_code: Optional[str] = None
    nickname: Optional[str] = None
    inbound_webhook_url: Optional[str] = None
    last_modification_timestamp: Optional[str] = None

    class Config:
        from_attributes = True  # For Pydantic v2

    @field_validator(
        "id",
        "inbound_agent_version",
        "outbound_agent_version",
        "area_code",
        "last_modification_timestamp",
        mode="before",
    )
    def convert_to_str(cls, v: Any) -> Optional[str]:
        return str(v) if v is not None else None


class PostCallAnalysis(BaseModel):
    type: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    example: Optional[str] = None


class UserDTMFOptions(BaseModel):
    digit_limit: Optional[int] = None
    termination_key: Optional[str] = None
    timeout_ms: Optional[int] = None


class AgentCreate(BaseModel):
    workspace_id: int
    agent_name: Optional[str] = None
    version: Optional[int] = 0
    voice_id: Optional[str] = None
    voice_model: Optional[str] = None
    fallback_voice_ids: Optional[List[str]] = None
    voice_temperature: Optional[float] = 1.0
    voice_speed: Optional[float] = 1.0
    volume: Optional[float] = 1.0
    responsiveness: Optional[float] = 1.0
    interruption_sensitivity: Optional[float] = 1.0
    enable_backchannel: Optional[bool] = False
    backchannel_frequency: Optional[float] = 0.8
    backchannel_words: Optional[List[str]] = None
    reminder_trigger_ms: Optional[int] = 10000
    reminder_max_count: Optional[int] = 1
    ambient_sound: Optional[str] = None
    ambient_sound_volume: Optional[float] = 1.0
    language: Optional[str] = "en-US"
    webhook_url: Optional[str] = None
    boosted_keywords: Optional[str] = None 
    opt_out_sensitive_data_storage: Optional[bool] = False
    opt_in_signed_url: Optional[bool] = True
    pronunciation_dictionary: Optional[List[Pronunciation]] = None
    normalize_for_speech: Optional[bool] = True
    end_call_after_silence_ms: Optional[int] = 600000
    max_call_duration_ms: Optional[int] = 3600000
    voicemail_option: Optional[VoicemailOption] = None
    post_call_analysis_data: Optional[List[PostCallAnalysis]] = None
    post_call_analysis_model: Optional[str] = "gpt-4o-mini"
    begin_message_delay_ms: Optional[int] = 1000
    ring_duration_ms: Optional[int] = 30000
    stt_mode: Optional[str] = "fast"
    vocab_specialization: Optional[str] = "general"
    allow_user_dtmf: Optional[bool] = True
    user_dtmf_options: Optional[UserDTMFOptions] = None
    denoising_mode: Optional[str] = "noise-cancellation"
    response_engine: Optional[ResponseEngine] = None


class AgentUpdate(BaseModel):
    agent_name: Optional[str] = None
    response_engine: Optional[Dict[str, Any]] = None
    language: Optional[str] = None
    opt_out_sensitive_data_storage: Optional[bool] = None
    opt_in_signed_url: Optional[bool] = None
    end_call_after_silence_ms: Optional[int] = None
    version: Optional[str] = None
    is_published: Optional[bool] = None
    post_call_analysis_model: Optional[str] = None
    voice_id: Optional[str] = None
    fallback_voice_ids: Optional[List[str]] = None
    voice_model: Optional[str] = None
    voice_temperature: Optional[float] = None
    voice_speed: Optional[float] = None
    volume: Optional[float] = None
    enable_backchannel: Optional[bool] = None
    backchannel_frequency: Optional[float] = None
    backchannel_words: Optional[List[str]] = None
    reminder_trigger_ms: Optional[int] = None
    reminder_max_count: Optional[int] = None
    max_call_duration_ms: Optional[int] = None
    interruption_sensitivity: Optional[float] = None
    ambient_sound: Optional[str] = None
    ambient_sound_volume: Optional[float] = None
    responsiveness: Optional[float] = None
    normalize_for_speech: Optional[bool] = None
    begin_message_delay_ms: Optional[int] = None
    ring_duration_ms: Optional[int] = None
    stt_mode: Optional[str] = None
    allow_user_dtmf: Optional[bool] = None
    user_dtmf_options: Optional[Dict[str, Any]] = None
    denoising_mode: Optional[str] = None
    webhook_url: Optional[str] = None
    boosted_keywords: Optional[str] = None
    pronunciation_dictionary: Optional[List[Pronunciation]] = None
    voicemail_option: Optional[VoicemailOption] = None
    post_call_analysis_data: Optional[List[PostCallAnalysis]] = None
    vocab_specialization: Optional[str] = None

    class Config:
        from_attributes = True


class AgentOut(AgentCreate):
    agent_id: str = Field(..., alias="id")
    agent_name: str = Field(..., alias="name")
    version: Optional[int] = 0
    is_published: Optional[bool]
    last_modification_timestamp: Optional[int]

    class Config:
        from_attributes = True  # for Pydantic v2
        validate_by_name = True


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


class KnowledgeFileOut(BaseModel):
    id: int
    kb_id: str
    filename: str
    file_path: Optional[str]
    extract_data: Optional[str]
    status: str
    source_type: str
    embedding: Optional[List[float]]
    uploaded_at: datetime

    class Config:
        from_attributes = True


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


# import phone
class PhoneNumberCreate(BaseModel):
    phone_number: str
    phone_number_type: str
    sip_trunk_auth_username: Optional[str] = None
    sip_trunk_auth_password: Optional[str] = None
    inbound_agent_id: Optional[str] = None
    outbound_agent_id: Optional[str] = None
    inbound_agent_version: Optional[int] = None
    outbound_agent_version: Optional[int] = None
    nickname: Optional[str] = None
    inbound_webhook_url: Optional[str] = None


class PhoneNumberUpdate(BaseModel):
    phone_number_type: Optional[str] = None
    phone_number_pretty: Optional[str] = None
    inbound_agent_id: Optional[str] = None
    outbound_agent_id: Optional[str] = None
    inbound_agent_version: Optional[str] = None
    outbound_agent_version: Optional[str] = None
    area_code: Optional[str] = None
    nickname: Optional[str] = None
    inbound_webhook_url: Optional[str] = None


class APIResponse(BaseModel):
    status: bool
    message: str
    data: Optional[List[VoiceOut]] = None
    errors: List[dict] = []


# -----------------------------web call--------------------------------
class WebCallCreateRequest(BaseModel):
    agent_id: str
    agent_version: Optional[int] = None
    call_metadata: Optional[Dict[str, Any]] = None
    llm_dynamic_variables: Optional[Dict[str, str]] = None


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


class WebCallResponse(BaseModel):
    call_type: str
    user_id: int
    access_token: str
    call_id: str
    agent_id: str
    agent_name: Optional[str] = None
    agent_version: Optional[int] = None
    call_status: str
    call_metadata: Optional[Dict[str, Any]] = {}
    llm_dynamic_variables: Optional[Dict[str, str]] = {}
    collected_dynamic_variables: Optional[Dict[str, Any]] = {}
    custom_sip_headers: Optional[Dict[str, str]] = {}
    opt_out_sensitive_data_storage: bool
    opt_in_signed_url: bool
    start_timestamp: Optional[int] = None
    end_timestamp: Optional[int] = None
    duration_ms: Optional[int] = None
    transcript: Optional[str] = None
    transcript_object: Optional[List[Dict[str, Any]]] = {}
    transcript_with_tool_calls: Optional[List[Dict[str, Any]]] = []
    recording_url: Optional[str] = None
    public_log_url: Optional[str] = None
    knowledge_base_retrieved_contents_url: Optional[str] = None
    latency: Optional[Dict[str, Any]] = {}
    disconnection_reason: Optional[str] = None
    call_analysis: Optional[CallAnalysis] = None
    call_cost: Optional[CallCost] = None
    llm_token_usage: Optional[Dict[str, Any]] = {}

    class Config:
        from_attributes = True


class UpdateCallMetadata(BaseModel):
    metadata: Dict[str, Any]
    opt_out_sensitive_data_storage: Optional[bool] = None


# Pydantic models for request and response
class PhoneNumberRequest(BaseModel):
    area_code: int
    inbound_agent_id: str | None = None
    inbound_agent_name: str | None = None
    inbound_agent_version: int | None = None
    inbound_webhook_url: str | None = None
    nickname: str
    number_provider: str
    outbound_agent_id: str | None = None


class Response(BaseModel):
    status: bool
    message: str
    data: PhoneNumberOut | None = None
    errors: list[dict] | None = None

class NumberAssignRequest(BaseModel):
    phone_number: str
    termination_uri: str


class PhoneCallCreateRequest(BaseModel):
    call_type: str
    from_number: str
    to_number: str
    direction: str
    telephony_identifier: Optional[Dict[str, Any]] = None
    call_id: str
    agent_id: str
    agent_version: Optional[int] = None
    call_status: str
    metadata: Optional[Dict[str, Any]] = {}
    llm_dynamic_variables: Optional[Dict[str, Any]] = {}
    collected_dynamic_variables: Optional[Dict[str, Any]] = {}
    custom_sip_headers: Optional[Dict[str, str]] = {}
    opt_out_sensitive_data_storage: bool = False
    opt_in_signed_url: bool = True
    start_timestamp: Optional[int] = None
    end_timestamp: Optional[int] = None
    duration_ms: Optional[int] = None
    transcript: Optional[str] = None
    transcript_object: Optional[List[Dict[str, Any]]] = []
    transcript_with_tool_calls: Optional[List[Dict[str, Any]]] = []
    recording_url: Optional[str] = None
    public_log_url: Optional[str] = None
    knowledge_base_retrieved_contents_url: Optional[str] = None
    latency: Optional[Dict[str, Any]] = {}
    disconnection_reason: Optional[str] = None
    call_analysis: Optional[Dict[str, Any]] = None
    call_cost: Optional[Dict[str, Any]] = None
    llm_token_usage: Optional[Dict[str, Any]] = None


class PhoneCallResponse(PhoneCallCreateRequest):
    id: str

    class Config:
        from_attributes = True


class PhoneCallCreateRequestMinimal(BaseModel):
    to_number: str


class UpdatePhoneNumberPayload(BaseModel):
    phone_number: Optional[str] = None
    inbound_agent_id: Optional[str] = None
    outbound_agent_id: Optional[str] = None
    nickname: Optional[str] = None


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


# ---------------- Conversation Flow Schemas (independent) ----------------

class FlowSubNodeCreate(BaseModel):
    id: Optional[str] = None
    parentId: Optional[str] = None
    value: Optional[str] = None
    handleId: Optional[str] = None


# ---------- Field ----------
class FlowField(BaseModel):
    id: str
    value: str


# ---------- Node Data ----------
class FlowNodeData(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    fields: Optional[List[FlowField]] = []
    subNodes: Optional[List[FlowSubNodeCreate]] = []
    settings: Optional[Dict[str, Any]] = {}


# ---------- Node ----------
class FlowNodeCreate(BaseModel):
    id: str
    type: str
    position: Dict[str, float]
    data: Optional[FlowNodeData] = None

    @model_validator(mode="after")
    def validate_node(cls, values):
        if not values.id or not values.type:
            raise ValueError("Node must include 'id' and 'type'")

        pos = values.position or {}
        if "x" not in pos or "y" not in pos:
            raise ValueError(f"Node {values.id} must have valid position (x, y)")

        # Validate conversation-type node
        if values.type == "conversation":
            fields_data = values.data.fields if values.data else []
            if not fields_data or len(fields_data) == 0:
                raise ValueError(f"Node {values.id} (conversation) must have at least one field")

        return values


# ---------- Edge ----------
class FlowEdgeCreate(BaseModel):
    id: str
    source: Optional[str] = None
    target: Optional[str] = None
    sourceHandle: Optional[str] = None
    type: Optional[str] = None
    animated: Optional[bool] = False
    subNodeConnection: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_edge(cls, values):
        if not all([values.id, values.source, values.target]):
            raise ValueError("Edge must include id, source, and target")
        return values


# ---------- Global Settings ----------
class PostCallAnalysisItem(BaseModel):
    type: str
    name: str
    description: Optional[str] = None
    example: Optional[str] = None


class FlowGlobalSettingCreate(BaseModel):
    
    model_config = ConfigDict(extra="forbid")

    # ---------- Core voice & model settings ----------
    language: Optional[str] = Field(default=None, description="Language code, e.g. en-US")
    voice_id: Optional[str] = Field(default=None, description="Voice identifier for TTS")
    voice_speed: Optional[float] = Field(default=None, description="Speed of the voice playback")
    voice_model: Optional[str] = Field(default=None, description="Voice model used for TTS")
    voice_temperature: Optional[float] = Field(default=None, description="Temperature for TTS variation")
    volume: Optional[float] = Field(default=None, description="Overall volume level")

    model: Optional[str] = Field(default=None, description="LLM model name, e.g. gpt-4o-mini")
    model_temperature: Optional[float] = Field(default=None, description="Temperature for LLM response randomness")

    # ---------- Knowledge base ----------
    knowledge_base_ids: Optional[List[str]] = Field(default_factory=list, description="List of linked knowledge base IDs")

    # ---------- Ambient sound ----------
    ambient_sound: Optional[str] = Field(default=None, description="Ambient sound type, e.g. coffee-shop")
    ambient_sound_volume: Optional[float] = Field(default=None, description="Volume level for ambient sound")

    # ---------- Behavioral controls ----------
    responsiveness: Optional[float] = Field(default=None, description="Responsiveness tuning parameter")
    interruption_sensitivity: Optional[float] = Field(default=None, description="How sensitive to user interruptions")
    enable_backchannel: Optional[bool] = Field(default=None, description="Whether AI should give small backchannel responses")

    stt_mode: Optional[str] = Field(default=None, description="STT mode, e.g. accurate or fast")

    # ---------- Advanced settings ----------
    normalize_for_speech: Optional[bool] = Field(default=None, description="Normalize audio for speech")
    enable_transcription_formatting: Optional[bool] = Field(default=None, description="Enable formatted transcription")
    enable_voicemail_detection: Optional[bool] = Field(default=None, description="Enable voicemail detection")

    boosted_keywords: Optional[str] = Field(default=None, description="Boosted speech keywords")
    reminder_trigger_ms: Optional[str] = Field(default=None, description="Reminder trigger time in milliseconds")
    end_call_after_silence_ms: Optional[str] = Field(default=None, description="End call after silence in milliseconds")
    max_call_duration_ms: Optional[str] = Field(default=None, description="Max duration for call in milliseconds")

    post_call_analysis_model: Optional[str] = Field(default=None, description="Model for post-call analysis")

    post_call_analysis_data: Optional[List[PostCallAnalysisItem]] = Field(
        default_factory=list,
        description="List of post-call analysis metadata fields",
    )


# class FlowGlobalSettingCreate(BaseModel):
#     language: Optional[str] = None
#     voice_id: Optional[str] = None
#     voice_speed: Optional[float] = None
#     voice_model: Optional[str] = None
#     voice_temperature: Optional[float] = None
#     volume: Optional[float] = None
#     model: Optional[str] = None
#     model_temperature: Optional[float] = None
#     knowledge_base_ids: Optional[List[str]] = []
#     ambient_sound: Optional[str] = None
#     ambient_sound_volume: Optional[float] = None
#     responsiveness: Optional[float] = None
#     interruption_sensitivity: Optional[float] = None
#     enable_backchannel: Optional[bool] = None
#     stt_mode: Optional[str] = None


# ---------- Main Flow ----------
class ConversationalFlowCreate(BaseModel):
    uuid: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    nodes: List[FlowNodeCreate]
    edges: List[FlowEdgeCreate]
    globalSettings: Optional[FlowGlobalSettingCreate] = None

    @model_validator(mode="after")
    def validate_flow(cls, values):
        # 1️⃣ Basic existence
        if not values.nodes or not isinstance(values.nodes, list):
            raise ValueError("At least one node is required.")

        if not values.edges or not isinstance(values.edges, list):
            raise ValueError("At least one edge is required.")

        # 2️⃣ Collect used source handles
        used_handles = {edge.sourceHandle for edge in values.edges if edge.sourceHandle}

        # 3️⃣ Validate subNode linkage
        missing_links = []
        for node in values.nodes:
            subnodes = node.data.subNodes if node.data else []
            for sub in subnodes:
                if sub.handleId and sub.handleId not in used_handles:
                    missing_links.append(
                        f"SubNode '{sub.value}' (handleId: {sub.handleId}) in node '{node.id}' "
                        f"is not linked to any edge.sourceHandle."
                    )

        if missing_links:
            raise ValueError("SubNode linkage validation failed:\n" + "\n".join(missing_links))

        # 4️⃣ Node type rules
        for node in values.nodes:
            node_type = node.type.lower()
            data = node.data or FlowNodeData()
            subnodes = data.subNodes or []

            # --- (a) callEnd nodes ---
            if node_type == "callend":
                if subnodes:
                    raise ValueError(f"Node '{node.id}' of type 'callEnd' must not contain subNodes.")
                outgoing_edges = [e for e in values.edges if e.source == node.id]
                if outgoing_edges:
                    raise ValueError(f"Node '{node.id}' of type 'callEnd' cannot have outgoing edges.")

            # --- (b) pressDigit nodes ---
            if node_type == "pressdigit":
                if not subnodes:
                    raise ValueError(
                        f"Node '{node.id}' of type 'pressDigit' must contain at least one subNode."
                    )

                for sub in subnodes:
                    # Check non-empty value
                    if not sub.value or not str(sub.value).strip():
                        raise ValueError(
                            f"SubNode in node '{node.id}' (pressDigit) must include a non-empty 'value'."
                        )

                    # Ensure it's a single digit 0–9
                    if not str(sub.value).isdigit() or not (0 <= int(sub.value) <= 9):
                        raise ValueError(
                            f"SubNode in node '{node.id}' has invalid value '{sub.value}'. "
                            f"PressDigit values must be digits from 0 to 9 only."
                        )

            # --- (c) callTransfer nodes ---
            if node_type == "calltransfer":
                if not subnodes:
                    pass

            # --- (d) conversation nodes ---
            if node_type == "conversation":
                if not data.prompt or not data.prompt.strip():
                    raise ValueError(
                        f"Node '{node.id}' of type 'conversation' must include a non-empty 'prompt'."
                    )
                for sub in subnodes:
                    if not sub.value or not sub.value.strip():
                        raise ValueError(
                            f"SubNode in node '{node.id}' must include a non-empty 'value'."
                        )

        # 5️⃣ callBegin must have outgoing edge
        all_sources = {edge.source for edge in values.edges if edge.source}
        for node in values.nodes:
            if node.type.lower() == "callbegin" and node.id not in all_sources:
                raise ValueError(
                    f"Node '{node.id}' of type 'callBegin' must have an outgoing edge (be a source in edges)."
                )

        return values

