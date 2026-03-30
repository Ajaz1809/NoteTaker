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



    
    
