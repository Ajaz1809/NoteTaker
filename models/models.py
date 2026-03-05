from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    Text,
    Float,
    JSON,
    BigInteger,
    ForeignKey,
    func,
    UniqueConstraint,
    Index
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from datetime import datetime
from db.database import Base, ConferenceBase
from sqlalchemy.dialects.postgresql import JSONB
import uuid, enum, time
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.mutable import MutableList


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, unique=False,nullable=True)
    username = Column(String, unique=True, nullable=False)
    full_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    retall_api_key = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    is_active = Column(Boolean, default=True)
    is_ucaas_user = Column(Boolean, default=False)

    workspaces = relationship(
        "Workspace", back_populates="owner", cascade="all, delete", passive_deletes=True
    )
    memberships = relationship("WorkspaceMember", back_populates="user")
    edited_keys = relationship("APIKey", back_populates="user")


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    owner_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    owner = relationship("User", back_populates="workspaces", passive_deletes=True)
    members = relationship(
        "WorkspaceMember", back_populates="workspace", cascade="all, delete"
    )
    knowledge_bases = relationship(
        "KnowledgeBase", back_populates="workspace", cascade="all, delete"
    )
    settings = relationship(
        "WorkspaceSettings",
        back_populates="workspace",
        uselist=False,
        cascade="all, delete",
    )
    api_keys = relationship(
        "APIKey", back_populates="workspace", cascade="all, delete-orphan"
    )

class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    role = Column(String, default="member")

    workspace = relationship("Workspace", back_populates="members")
    user = relationship("User", back_populates="memberships")

    # Composite index for common query pattern (user_id + workspace_id)
    __table_args__ = (
        Index('idx_workspace_member_user_workspace', 'user_id', 'workspace_id'),
    )


class WorkspaceSettings(Base):
    __tablename__ = "workspace_settings"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id"), unique=True, nullable=False
    )
    default_voice = Column(String, default="echo")
    default_model = Column(String, default="gpt-4")
    temperature = Column(Integer, default=1)

    workspace = relationship("Workspace", back_populates="settings")


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id = Column(
        String, primary_key=True, index=True, default=lambda: f"{uuid.uuid4().hex[:16]}"
    )
    name = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    enable_auto_refresh = Column(Boolean, default=False, nullable=False)
    auto_refresh_interval = Column(Integer, default=24, nullable=False)  # hours
    last_refreshed = Column(DateTime, nullable=True)
    knowledge_files = relationship(
        "KnowledgeFile", back_populates="knowledge_base", cascade="all, delete-orphan"
    )

    workspace = relationship("Workspace", back_populates="knowledge_bases")


class FileStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class SourceStatus(str, enum.Enum):
    document = "document"
    url = "url"
    text = "text"


class KnowledgeFile(Base):
    __tablename__ = "knowledge_files"
    id = Column(Integer, primary_key=True, index=True)
    kb_id = Column(String, ForeignKey("knowledge_bases.id"), nullable=False, index=True)
    filename = Column(String, nullable=False)
    file_path = Column(String, nullable=True)
    extract_data = Column(Text, nullable=True)
    status = Column(SqlEnum(FileStatus), default=FileStatus.pending, nullable=False)
    source_type = Column(
        SqlEnum(SourceStatus), default=SourceStatus.document, nullable=False
    )
    embedding = Column(ARRAY(Float), nullable=True)
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())
    knowledge_base = relationship("KnowledgeBase", back_populates="knowledge_files")

    knowledge_base = relationship("KnowledgeBase")


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    name = Column(String, nullable=False)
    key_value = Column(String, unique=True, nullable=False)
    is_webhook_key = Column(Boolean, default=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace = relationship("Workspace", back_populates="api_keys")
    user = relationship("User", back_populates="edited_keys")


# Agent Models
class pbx_ai_agent(Base):
    __tablename__ = "pbx_ai_agent"

    id = Column(String, primary_key=True, index=True, default=lambda: f"{uuid.uuid4().hex[:16]}")
    account_id = Column(Integer, unique=False,nullable=True)
    version = Column(Integer, default=0)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    name = Column(String, nullable=True)
    voice_id = Column(String)
    voice_model = Column(String, nullable=True)
    fallback_voice_ids = Column(JSON, nullable=True)
    voice_temperature = Column(Float, default=1.0)
    voice_speed = Column(Float, default=1.0)
    volume = Column(Float, default=1.0)
    responsiveness = Column(Float, default=1.0)
    interruption_sensitivity = Column(Float, default=1.0)
    enable_backchannel = Column(Boolean, default=False)
    backchannel_frequency = Column(Float, default=0.8)
    backchannel_words = Column(JSON, nullable=True)
    reminder_trigger_ms = Column(Integer, default=10000)
    reminder_max_count = Column(Integer, default=1)
    ambient_sound = Column(String, nullable=True)
    ambient_sound_volume = Column(Float, default=1.0)
    language = Column(String, default="en-US")
    webhook_url = Column(String, nullable=True)
    boosted_keywords = Column(JSON, nullable=True)
    opt_out_sensitive_data_storage = Column(Boolean, default=False)
    opt_in_signed_url = Column(Boolean, default=True)
    pronunciation_dictionary = Column(JSON, nullable=True)
    normalize_for_speech = Column(Boolean, default=True)
    end_call_after_silence_ms = Column(Integer, default=600000)
    max_call_duration_ms = Column(Integer, default=3600000)
    voicemail_option = Column(JSON, nullable=True)
    post_call_analysis_data = Column(JSON, nullable=True)
    post_call_analysis_model = Column(String, default="gpt-4o-mini")
    begin_message_delay_ms = Column(Integer, default=1000)
    ring_duration_ms = Column(Integer, default=30000)
    stt_mode = Column(String, default="fast")
    vocab_specialization = Column(String, default="general")
    allow_user_dtmf = Column(Boolean, default=True)
    user_dtmf_options = Column(JSON, nullable=True)
    denoising_mode = Column(String, default="noise-cancellation")
    last_modification_timestamp = Column(
        BigInteger, default=lambda: int(time.time() * 1000)
    )
    is_published = Column(Boolean, default=False)
    response_engine = Column(JSON)

    phone_number = Column(String, unique=True, nullable=False)


# PBX LLm
class PBXLLM(Base):
    __tablename__ = "pbx_llms"

    id = Column(String, primary_key=True, default=lambda: f"{uuid.uuid4().hex[:24]}")
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)

    version = Column(Integer, default=0)
    model = Column(String, nullable=True)
    s2s_model = Column(String, nullable=True)
    model_temperature = Column(Float, default=0.0)
    model_high_priority = Column(Boolean, default=False)
    tool_call_strict_mode = Column(Boolean, default=False)

    general_prompt = Column(Text, nullable=True)  # Prefer `Text` for long prompts
    general_tools = Column(JSON, nullable=True)
    states = Column(JSON, nullable=True)
    starting_state = Column(String, nullable=True)
    begin_message = Column(String, nullable=True)
    default_dynamic_variables = Column(JSON, nullable=True)
    knowledge_base_ids = Column(MutableList.as_mutable(JSON), nullable=True)

    is_published = Column(Boolean, default=False)
    last_modification_timestamp = Column(
        BigInteger, default=lambda: int(time.time() * 1000)
    )


# Chat-room
class ChatSession(Base):
    __tablename__ = "chat_sessions"

    chat_id = Column(
        String, primary_key=True, default=lambda: f"{uuid.uuid4().hex[:24]}"
    )
    agent_id = Column(String, nullable=False)
    agent_version = Column(Integer, default=0)
    chat_status = Column(String, default="ongoing")  # ongoing, ended, error
    llm_dynamic_variables = Column(JSONB, nullable=True)
    collected_dynamic_variables = Column(JSONB, nullable=True)
    start_timestamp = Column(BigInteger, nullable=True)
    end_timestamp = Column(BigInteger, nullable=True)
    transcript = Column(String, nullable=True)
    message_with_tool_calls = Column(JSONB, nullable=True)
    chat_metadata = Column(
        "metadata", JSONB, nullable=True
    )  # renamed to avoid SQLAlchemy conflict
    chat_cost = Column(JSONB, nullable=True)
    chat_analysis = Column(JSONB, nullable=True)


# voice
class LLMVoice(Base):
    __tablename__ = "voices"
    voice_id = Column(
        String, primary_key=True, index=True
    )  # Use voice_id as primary key
    voice_name = Column(String)
    provider = Column(String)
    gender = Column(String)
    accent = Column(String)
    age = Column(String)
    preview_audio_url = Column(String)


# import phone
class ImportedPhoneNumber(Base):
    __tablename__ = "imported_phone_numbers"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, nullable=False, index=True)
    phone_number_type = Column(String, default="retell-twilio")
    phone_number_pretty = Column(String)
    inbound_agent_id = Column(String, nullable=True)
    outbound_agent_id = Column(String, nullable=True)
    inbound_agent_version = Column(Integer, nullable=True)
    outbound_agent_version = Column(Integer, nullable=True)
    area_code = Column(Integer)
    nickname = Column(String, nullable=True)
    inbound_webhook_url = Column(String, nullable=True)
    last_modification_timestamp = Column(BigInteger)


# ---------------web call model-----------------------
class WebCall(Base):
    __tablename__ = "web_calls"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    call_type = Column(String, default="web_call", nullable=False)
    access_token = Column(Text, nullable=False)
    call_id = Column(String, unique=True, nullable=False)
    agent_id = Column(String, ForeignKey("pbx_ai_agent.id", ondelete="SET NULL"), nullable=True)
    agent = relationship("pbx_ai_agent", backref="web_calls") 
    agent_version = Column(Integer, nullable=True)
    call_status = Column(String, nullable=False, default="registered")
    call_metadata = Column(JSON, nullable=True)
    llm_dynamic_variables = Column(JSON, nullable=True)
    collected_dynamic_variables = Column(JSON, nullable=True)
    custom_sip_headers = Column(JSON, nullable=True)
    opt_out_sensitive_data_storage = Column(Boolean, default=False)
    opt_in_signed_url = Column(Boolean, default=True)
    start_timestamp = Column(BigInteger, nullable=True)
    end_timestamp = Column(BigInteger, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    transcript = Column(Text, nullable=True)
    transcript_object = Column(JSON, nullable=True)
    transcript_with_tool_calls = Column(JSON, nullable=True)
    recording_url = Column(Text, nullable=True)
    public_log_url = Column(Text, nullable=True)
    knowledge_base_retrieved_contents_url = Column(Text, nullable=True)
    latency = Column(JSON, nullable=True)
    disconnection_reason = Column(String, nullable=True)
    call_analysis = Column(JSON, nullable=True)
    call_cost = Column(JSON, nullable=True)
    llm_token_usage = Column(JSON, nullable=True)
    created_at = Column(BigInteger, default=lambda: int(time.time() * 1000))
    updated_at = Column(BigInteger, default=lambda: int(time.time() * 1000))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    user = relationship("User", backref="web_calls")

    # Composite index for common query pattern (user_id + start_timestamp)
    __table_args__ = (
        Index('idx_webcall_user_start_timestamp', 'user_id', 'start_timestamp'),
    )


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"

    id = Column(Integer, primary_key=True, autoincrement=True)  # <-- FIXED
    phone_number = Column(String)
    phone_number_type = Column(String)
    phone_number_pretty = Column(String)
    inbound_agent_id = Column(String)
    outbound_agent_id = Column(String)
    inbound_agent_version = Column(String)
    outbound_agent_version = Column(String)
    area_code = Column(Integer)
    nickname = Column(String)
    inbound_webhook_url = Column(String)
    last_modification_timestamp = Column(Float)
    
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

class PhoneCall(Base):
    __tablename__ = "phone_calls"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # Add this line
    call_type = Column(String, nullable=False)  # phone_call/web_call etc.
    from_number = Column(String, nullable=False)
    to_number = Column(String, nullable=False)
    direction = Column(String, nullable=False)
    dispatch_rule_id = Column(String, ForeignKey("sip_dispatch_rules.id"), nullable=True)
    agent_id = Column(String, ForeignKey("pbx_ai_agent.id"), nullable=False)

    telephony_identifier = Column(JSON, nullable=True)
    call_id = Column(String, unique=True, nullable=False)

    # agent_id = Column(String, nullable=False)
    agent_version = Column(Integer, nullable=True)

    call_status = Column(String, nullable=False, default="registered")
    call_metadata = Column(JSON, nullable=True)

    llm_dynamic_variables = Column(JSON, nullable=True)
    collected_dynamic_variables = Column(JSON, nullable=True)
    custom_sip_headers = Column(JSON, nullable=True)

    opt_out_sensitive_data_storage = Column(Boolean, default=False)
    opt_in_signed_url = Column(Boolean, default=True)

    start_timestamp = Column(BigInteger, nullable=True)
    end_timestamp = Column(BigInteger, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    transcript = Column(Text, nullable=True)
    transcript_object = Column(JSON, nullable=True)
    transcript_with_tool_calls = Column(JSON, nullable=True)

    recording_url = Column(Text, nullable=True)
    public_log_url = Column(Text, nullable=True)
    knowledge_base_retrieved_contents_url = Column(Text, nullable=True)

    latency = Column(JSON, nullable=True)
    disconnection_reason = Column(String, nullable=True)
    call_analysis = Column(JSON, nullable=True)
    call_cost = Column(JSON, nullable=True)
    llm_token_usage = Column(JSON, nullable=True)

    created_at = Column(BigInteger, default=lambda: int(time.time() * 1000))
    updated_at = Column(BigInteger, default=lambda: int(time.time() * 1000))

class NoteTakerCall(Base):
    __tablename__ = "notetaker_calls"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    call_type = Column(String, default="notetaker_calls", nullable=False)
    call_id = Column(String, unique=False, nullable=False)
    call_status = Column(String, nullable=False, default="active")
    start_timestamp = Column(BigInteger, nullable=True)
    end_timestamp = Column(BigInteger, nullable=True)
    duration_ms = Column(BigInteger, nullable=True)
    recording_url = Column(Text, nullable=True)
    call_analysis = Column(JSON, nullable=True)
    created_at = Column(BigInteger, default=lambda: int(time.time() * 1000))
    updated_at = Column(BigInteger, default=lambda: int(time.time() * 1000))
    
    # New field
    meeting_id = Column(String, nullable=True)   # or meeting_link if we want it
    conf_name = Column(String(1024), nullable=True)
    user_id = Column(String(255), nullable=True)


# ------------------ AGENT ------------------


class ConversationalFlow(Base):
    __tablename__ = "conversational_flows"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, nullable=True)
    name = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(BigInteger, default=lambda: int(time.time() * 1000))
    updated_at = Column(BigInteger, default=lambda: int(time.time() * 1000))
    nodes = relationship("FlowNode", back_populates="flow", cascade="all, delete-orphan")
    edges = relationship("FlowEdge", back_populates="flow", cascade="all, delete-orphan")
    settings = relationship("FlowGlobalSetting", back_populates="flow", cascade="all, delete-orphan")

# ------------------ FLOW NODE ------------------
class FlowNode(Base):
    __tablename__ = "flow_nodes"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, nullable=True, unique=True)
    flow_id = Column(Integer, ForeignKey("conversational_flows.id", ondelete="CASCADE"))

    type = Column(String, nullable=True)  # e.g., callBegin, conversation, pressDigit, etc.
    label = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    prompt = Column(Text, nullable=True)
    position_x = Column(Float, nullable=True)
    position_y = Column(Float, nullable=True)
    settings = Column(JSON, default={})

    flow = relationship("ConversationalFlow", back_populates="nodes")
    subnodes = relationship("FlowSubNode", back_populates="parent_node", cascade="all, delete-orphan")


# ------------------ FLOW SUBNODE ------------------
class FlowSubNode(Base):
    __tablename__ = "flow_subnodes"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, nullable=True)
    parent_node_id = Column(Integer, ForeignKey("flow_nodes.id", ondelete="CASCADE"))

    value = Column(String, nullable=True)
    handle_id = Column(String, nullable=True)

    parent_node = relationship("FlowNode", back_populates="subnodes")


# ------------------ FLOW EDGE ------------------
class FlowEdge(Base):
    __tablename__ = "flow_edges"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, nullable=True)
    flow_id = Column(Integer, ForeignKey("conversational_flows.id", ondelete="CASCADE"))

    source_node_id = Column(String, ForeignKey("flow_nodes.uuid", ondelete="CASCADE"), nullable=True)
    target_node_id = Column(String, ForeignKey("flow_nodes.uuid", ondelete="CASCADE"), nullable=True)

    source_handle = Column(String, nullable=True)
    subnode_connection = Column(JSON, default={})
    animated = Column(Boolean, default=False)
    type = Column(String, default="customEdge")

    flow = relationship("ConversationalFlow", back_populates="edges")


# ------------------ FLOW GLOBAL SETTINGS ------------------
class FlowGlobalSetting(Base):
    __tablename__ = "flow_global_settings"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, nullable=False, index=True)
    flow_id = Column(Integer, ForeignKey("conversational_flows.id", ondelete="CASCADE"))

    settings = Column(JSON, nullable=False)

    flow = relationship("ConversationalFlow", back_populates="settings")


# ///////////////////////////

class ConferenceSummary(Base):
    __tablename__ = "conference_summaries"

    id = Column(BigInteger, primary_key=True, index=True)
    conference_id = Column(BigInteger, nullable=False)
    call_analysis = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    
    
    
    
    
    
    
# dispatch and trunk related tables
class SIPTrunk(Base):
    __tablename__ = "sip_trunks"
    # e.g. LiveKit trunk id "ST_gD9P..."
    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    trunk_id = Column(String, unique=True, nullable=False)  # livekit trunk id
    provider = Column(String, nullable=True)                 # e.g. "twilio"
    address = Column(String, nullable=True)                  # e.g. "natty-ai.pstn.twilio.com"
    auth_username = Column(String, nullable=True)
    auth_password = Column(String, nullable=True)
    region = Column(String, nullable=True)
    created_at = Column(BigInteger, default=lambda: int(time.time() * 1000))
    updated_at = Column(BigInteger, default=lambda: int(time.time() * 1000))


class SIPDispatchRuleRow(Base):
    __tablename__ = "sip_dispatch_rules"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    livekit_rule_id = Column(String, nullable=True, index=True)
    phone_number = Column(String, nullable=False)
    agent_id = Column(String, ForeignKey("pbx_ai_agent.id"), nullable=False)
    agent_name = Column(String, nullable=True)
    trunk_id = Column(String, ForeignKey("sip_trunks.trunk_id"), nullable=True)
    metadata_json = Column(JSON, nullable=True)  # ✅ renamed from metadata
    rule_name = Column(String, nullable=True)
    room_prefix = Column(String, nullable=True)
    active = Column(Boolean, default=True, nullable=False)
    last_response = Column(JSON, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(BigInteger, default=lambda: int(time.time() * 1000))
    updated_at = Column(BigInteger, default=lambda: int(time.time() * 1000))

    __table_args__ = (
        UniqueConstraint("phone_number", name="uq_sip_dispatch_phone"),
        Index("ix_sip_dispatch_phone_active", "phone_number", "active"),
    )
