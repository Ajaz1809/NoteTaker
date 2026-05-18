import asyncio, json, re
import  time
import os
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    RoomInputOptions,
    RoomIO,
    RoomOutputOptions,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    llm,
    utils,
)
from livekit.plugins import deepgram, silero
from openai import OpenAI
from livekit.agents import JobRequest
from typing import Optional
import aio_pika
import redis
from logs import logw 
import random



def uniqueID():
    timestamp = int(time.time() * 1000)
    random_part = random.randint(1000, 9999)
    return f"Unique ID: {timestamp}.{random_part}"
    
# Redis connection
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD", ""),
    decode_responses=True
)


load_dotenv()
# logger = logging.getLogger("transcriber")
client = OpenAI()

class Transcriber(Agent):
    def __init__(self, *, participant_identity: str, transcript_collector: list,room_name: str,meeting_start_time_ms: int):
        stt_engine = None
        retry_count = 0
        max_retries = 3
        self.meeting_start_time_ms = meeting_start_time_ms
        while stt_engine is None and retry_count < max_retries:
            try:
                stt_engine = deepgram.STT(
                    model="nova-3",
                    language="multi",
                    interim_results=True,
                    endpointing_ms=400,
                    no_delay=True,
                    punctuate=True,
                    smart_format=True,
                    numerals=True,
                    sample_rate=16000,
                    filler_words=False,
                    profanity_filter=False,
                    mip_opt_out=True,
                )
            except Exception as e:
                retry_count += 1
                logw("error", f" Failed to init Deepgram STT (attempt {retry_count}/{max_retries}): {e}\n")
                if retry_count < max_retries:
                    time.sleep(1)  # Wait before retrying
                else:
                    stt_engine = None
                    
        if stt_engine is None:
            logw("error", " Could not initialize STT after all retries\n")
            # Fallback: use a simple STT or raise
            
        super().__init__(instructions="Silent observer", stt=stt_engine)
        self.participant_identity = participant_identity
        self.transcript_collector = transcript_collector
        self.room_name = room_name
        
        
    async def on_user_turn_completed(self, chat_ctx: llm.ChatContext, new_message: llm.ChatMessage):
        # Check if this callback is outdated (newer user input detected)
        if self._is_outdated():
            logw("info", f"Skipping outdated turn for {self.participant_identity}")
            return
            
        try:
            user_transcript = new_message.text_content
            if not user_transcript:
                return
                
            current_time_ms = int(time.time() * 1000)
            timestamp = current_time_ms - self.meeting_start_time_ms
            
            logw("info", f"Transcript from {self.participant_identity} at + {timestamp}ms: {user_transcript}\n")
            self.transcript_collector.append(
                f"{self.participant_identity}: {user_transcript} : {timestamp}"
            )
            
            # flag_key = f"transcription:active:{self.room_name}"
            # redis_client.setex(flag_key, 120, "1")
            # logw("info", f" Redis: [{flag_key}] = 1 | Transcription detected!\n")
        except Exception as e:
            logw("error", f" Error during transcript collection: {e}\n")

    def _is_outdated(self) -> bool:
        """Check if this callback is from an outdated user turn"""
        activity = getattr(self, '_get_activity_or_raise', None)
        if activity:
            try:
                activity_obj = activity()
                current_task = asyncio.current_task()
                user_turn_task = getattr(activity_obj, '_user_turn_completed_atask', None)
                if user_turn_task and user_turn_task != current_task:
                    return True
            except Exception as e:
                logw("error", f"Error in _is_outdated: {e}")
                pass
        return False

 
class MultiUserTranscriber:
    def __init__(self, ctx: JobContext, meeting_metadata: Optional[dict] = None):
        self.room_name = ctx.room.name if ctx.room else "unknown"
        self.ctx = ctx
        self._sessions: dict[str, AgentSession] = {}
        self._tasks: set[asyncio.Task] = set()
        self.transcript_data: list[str] = []
        self.participants_remaining: set[str] = set()
        # NEW: Track all participants who ever joined (including those who left)
        self.all_participants: set[str] = set()
        self.room_ended = False
        self.retry_mode = False
        self._is_shutting_down = False
        self._published = False
        self.start_time_ms = int(time.time() * 1000)
        self.meeting_start_time_ms = self.start_time_ms
        # Store meeting metadata for RabbitMQ
        self.meeting_metadata = meeting_metadata or {}
        self.meeting_id = self.meeting_metadata.get("meeting_id", "N/A")
        self.user_id = self.meeting_metadata.get("user_id", "N/A")
        self.conf_name = self.meeting_metadata.get("conf_name", "N/A")
        logw("info", f" Initialized with Meeting ID: {self.meeting_id}  || User ID: {self.user_id} || Conference: {self.conf_name}\n")

    def set_retry_mode(self):
        """Called when we detect RTC error and plan to rejoin."""
        self.retry_mode = True

    def start(self):
        self.ctx.room.on("participant_connected", self.on_participant_connected)
        self.ctx.room.on("participant_disconnected", self.on_participant_disconnected)
        self.ctx.room.on("disconnected", self.on_room_disconnected)

    async def publish_to_rabbitmq(self):
        """Publish final transcript to RabbitMQ"""
        if self._published or not self.transcript_data:
            logw("info", "Skipping publish: No data or already published.")
            return False
        
        try:
            # Connect to RabbitMQ
            connection = await aio_pika.connect_robust(
                host=os.getenv("RABBITMQ_HOST", "rabbitmq.webvio.in"),
                port=int(os.getenv("RABBITMQ_PORT", 5672)),
                login=os.getenv("RABBITMQ_USERNAME", "AjazDev"),
                password=os.getenv("RABBITMQ_PASSWORD", "Dev@123"),
                timeout=10
            )

            async with connection:
                channel = await connection.channel()
                # Declare queue (durable so messages survive broker restart)
                await channel.declare_queue("final.transcripts", durable=True)
                end_time_ms = int(time.time() * 1000)
                message_payload = {
                    "meeting_id": self.meeting_id,
                    "user_id": self.user_id,
                    "conf_name": self.conf_name,
                    "room_name": self.room_name,
                    "transcript": self.transcript_data,
                    "timestamp": end_time_ms,
                    "duration_ms": max(0, end_time_ms - self.start_time_ms),
                    "remaining_participants_count": len(self.participants_remaining),
                    # I want to give the list all pariticipants count in the who joined the call , make sure participants not repeated .
                    "all_participants_count": len(list(self.all_participants)),
                    "participants_list": list(self.all_participants),
                    
                    
                    "status": "ended" if self.room_ended else "active"
                }                   
                   

                # Publish message
                await channel.default_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps(message_payload).encode(),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        content_type='application/json'
                    ),
                    routing_key="final.transcripts",
                )
                
            self._published = True
            logw("info", f"->>> Published meeting {self.meeting_id} to RabbitMQ with {len(self.transcript_data)} transcript entries")
            return True
        except Exception as e:
            logw("error", f" RabbitMQ publish error: {e}")
            return False

    async def aclose(self):
        """Clean shutdown with RabbitMQ publish"""
        if self._is_shutting_down:
            return

        self._is_shutting_down = True
        logw("info", "Closing room and cleaning up sessions...")

        # Wait for pending transcripts if participants are still active
        if self._sessions and not self.retry_mode:
            logw("info", f"⏳ Waiting for {len(self._sessions)} active sessions to complete...")
            await asyncio.sleep(1.0)  # Wait for final transcripts
            await asyncio.sleep(0)    # Process pending callbacks

        # Cancel running tasks
        try:
            if self._tasks:
                await utils.aio.cancel_and_wait(*self._tasks)
        except Exception as e:
            logw("error", f"Task cancellation failed: {e}")

        # Close all sessions
        try:
            sessions = list(self._sessions.values())
            results = await asyncio.gather(
                *(self._close_session(s) for s in sessions),
                return_exceptions=True
            )
            for result in results:
                if isinstance(result, Exception):
                    logw("error", f" Session close failed: {result}")
        except Exception as e:
            logw("error", f" Error during session cleanup: {e}")

        # Extra buffer for last-moment transcripts
        if self._sessions and not self.retry_mode:
            await asyncio.sleep(0.5)

        # Publish to RabbitMQ (only once)
        if not self._published and not self.retry_mode:
            await self.publish_to_rabbitmq()
        elif self.retry_mode:
            logw("info", "Retry mode: Skipping final publish, will continue in new session")
        else:
            logw("info", "Transcript already published")

        # Remove event listeners
        self.ctx.room.off("participant_connected", self.on_participant_connected)
        self.ctx.room.off("participant_disconnected", self.on_participant_disconnected)
        self.ctx.room.off("disconnected", self.on_room_disconnected)
         # DELETE Redis flag when agent leaves room
        flag_key = f"transcription:active:{self.room_name}"
        redis_client.delete(flag_key)
        logw("info", f"Redis deleted: [{flag_key}]")

    def on_room_disconnected(self, *args, **kwargs):
        logw("info", "Room 'disconnected' event received")
        self.room_ended = True

    def on_participant_connected(self, participant: rtc.RemoteParticipant):
        if participant.identity in self._sessions:
            return

        logw("info", f" Connected: {participant.identity}")
        self.participants_remaining.add(participant.identity)
        self.all_participants.add(participant.identity)
        task = asyncio.create_task(self._start_session(participant))
        self._tasks.add(task)

        def on_task_done(task: asyncio.Task):
            try:
                self._sessions[participant.identity] = task.result()
            finally:
                self._tasks.discard(task)

        task.add_done_callback(on_task_done)

    def on_participant_disconnected(self, participant: rtc.RemoteParticipant):
        session = self._sessions.pop(participant.identity, None)
        if session is None:
            return

        logw("info", f"<> Disconnected <>: {participant.identity}")
        self.participants_remaining.discard(participant.identity)
        task = asyncio.create_task(self._close_session(session))
        self._tasks.add(task)
        task.add_done_callback(lambda _: self._tasks.discard(task))

        # If everyone left, end the session
        if not self.participants_remaining:
            logw("info", " All participants left, ending session.")
            self.room_ended = True
            asyncio.create_task(self.aclose())

    async def _start_session(self, participant: rtc.RemoteParticipant) -> AgentSession:
        if participant.identity in self._sessions:
            return self._sessions[participant.identity]

        session = AgentSession(
            vad=self.ctx.proc.userdata["vad"],
            turn_handling=TurnHandlingOptions(
                interruption={"mode": "vad"}  # ← ADD THIS
            )
        )
    
        room_io = RoomIO(
            agent_session=session,
            room=self.ctx.room,
            participant=participant,
            input_options=RoomInputOptions(
                text_enabled=False, close_on_disconnect=False
            ),
            output_options=RoomOutputOptions(
                transcription_enabled=False, audio_enabled=False
            ),
        )
        await room_io.start()
        await session.start(
            agent=Transcriber(
                participant_identity=participant.identity,
                transcript_collector=self.transcript_data,
                room_name=self.room_name,
                meeting_start_time_ms=self.meeting_start_time_ms,
            ),
            record=False
        )
        return session

    async def _close_session(self, sess: AgentSession):
        if not sess:
            return

        try:
            await asyncio.wait_for(sess.drain(), timeout=5.0)
            await asyncio.sleep(0.1)
        except (RuntimeError, asyncio.TimeoutError) as e:
            logw("warning", f"Session drain timeout/error: {e}")
            await asyncio.sleep(0)

        await sess.aclose()

async def entrypoint(ctx: JobContext):
    """
    Note-taker with RabbitMQ only (no database)
    """
    max_retries = 3
    retry_count = 0
    flag_key = None
    transcription_happened = False 
    # Parse metadata from job
    meeting_metadata = {}
    try:
        if ctx.job.metadata:
            raw_metadata = ctx.job.metadata
            metadata = json.loads(raw_metadata)
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            meeting_metadata = metadata
            logw("info", f" Meeting metadata: {meeting_metadata}")
    except Exception as e:
        logw("warning", f"Could not parse metadata: {e}")

    while True:
        transcriber = MultiUserTranscriber(ctx, meeting_metadata=meeting_metadata)
        transcriber.start()

        try:
            await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
            logw("info", f" Connected to room: {ctx.room.name}")
            
             #### SET INITIAL FLAG - Agent room mein hai
            room_name = ctx.room.name
            flag_key = f"transcription:active:{room_name}"
            redis_client.setex(flag_key, 60, "1")  # 0 = room mein hai but abhi transcribe nahi kiya
            logw("info", f"🔴 Redis initial flag: {flag_key} = 1")
            
            # Connect to already-present participants
            for participant in ctx.room.remote_participants.values():
                if "note-taker" not in participant.identity.lower():
                    logw("info", f"👤 Connecting: {participant.identity}")
                    transcriber.on_participant_connected(participant)

            ctx.add_shutdown_callback(lambda: asyncio.create_task(transcriber.aclose()))

            # Wait until room ends
            while not transcriber.room_ended:
                await asyncio.sleep(1)
            
            # Normal exit
            await transcriber.aclose()
            return

        except Exception as e:
            error_str = str(e)
            logw("error", f"Error in main loop: {error_str}")
            
            # Handle RTC errors with retry
            if "Subscriber pc state failed" in error_str or "rtc_session" in error_str:
                logw("error", f" CRITICAL RTC ERROR: {error_str}")
                logw("info", " Initiating REJOIN sequence...")

                try:
                    transcriber.set_retry_mode()
                    await transcriber.aclose()
                except Exception as e2:
                    logw("error", f"Error flushing transcripts: {e2}")

                try:
                    await ctx.disconnect()
                except Exception as e:
                    logw("error", f"Error during disconnect, may already be disconnected: {e}")

                retry_count += 1
                if retry_count > max_retries:
                    logw("error", " Max retries reached. Giving up.")
                    raise

                logw("info", f" Retry {retry_count}/{max_retries} in 2s...")
                await asyncio.sleep(2)
                continue
            
            # Non-RTC errors
            logw("error", f" Non-retryable error: {error_str}")
            await transcriber.aclose()
            raise
        finally:
            #  DELETE FLAG when agent exits
            if flag_key and not transcription_happened:
                redis_client.delete(flag_key)
                logw("info", f" Redis deleted (no transcription): [{flag_key}]")

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()

async def request_fnc(req: JobRequest):
    await req.accept(name="agent-note-taker", identity=f"note-taker-{req.room.name}")

def main():
    opts = WorkerOptions(
        entrypoint_fnc=entrypoint,
        request_fnc=request_fnc,
        prewarm_fnc=prewarm,
        job_memory_warn_mb=2048,
        job_memory_limit_mb=3072,
        agent_name="note-taker-agent",
        num_idle_processes=1,
        port=8091,
    )
    cli.run_app(opts)

if __name__ == "__main__":
    main()
