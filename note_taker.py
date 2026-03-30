import asyncio, json, re
import logging, time
from sqlalchemy import func
from dotenv import load_dotenv
from datetime import datetime
from livekit import rtc
from tasks import analyze_transcript  # import at top
# from openai._exceptions import APITimeoutError
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    RoomInputOptions,
    RoomIO,
    RoomOutputOptions,
    StopResponse,
    WorkerOptions,
    cli,
    llm,
    utils,
)
from livekit.plugins import deepgram, silero
from openai import OpenAI
from db.database import NoteTakerSessionLocal
from models.models import NoteTakerCall
from livekit.agents import JobRequest
from typing import Optional
import os

load_dotenv()
logger = logging.getLogger("transcriber")
client = OpenAI()

class Transcriber(Agent):
    def __init__(self, *, participant_identity: str, transcript_collector: list):
        try:
            stt_engine = deepgram.STT(
            model="nova-3",
            language="multi",
            # CRITICAL: Disable interim results so we only get high-confidence final sentences
            interim_results=True, 
            # CRITICAL: Increase endpointing to 1 second so pauses don't break sentences
            endpointing_ms=100,
            # CRITICAL: Disable no_delay to allow the AI to use context for better grammar
            no_delay=True,
            punctuate=True,
            smart_format=True,
            # CRITICAL: Enable numerals for better readability of numbers in transcripts
            numerals=True,
            # CRITICAL: Set sample rate for optimal audio processing
            sample_rate=16000,
            # Fathom removes filler words ("um", "uh") for a professional transcript
            filler_words=True, 
            # Fathom's profanity filter is aggressive and can break non-English sentences, so we disable it for multilingual support
            profanity_filter=False,
            # CRITICAL: Opt out of MIP (Multi-Input Processing) for better control
            mip_opt_out=True, 
                            )
        except Exception as e:
            logger.error(f"❌ Failed to init Deepgram STT: {e}")
            # Fallback: Disable STT, still run Agent
            stt_engine = None
            self.stt_error = str(e)
        else:
            self.stt_error = None

        # super().__init__(instructions="not-needed", stt=stt_engine)
        super().__init__(instructions="Silent observer", stt=stt_engine)
        self.participant_identity = participant_identity
        self.transcript_collector = transcript_collector
        
    async def clean_transcript_with_llm(self, raw_text: str):
        """
        Multi-language aware cleaning.
        """
        try:
            # We use GPT-4o-mini because it is natively multilingual
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system", 
                        "content": (
                            "You are a transcript polisher for multilingual meetings. "
                            "Fix punctuation and spelling for the language provided. "
                            "DO NOT translate the text. If it is in Hindi, keep it in Hindi. "
                            "If it is in English, keep it in English. "
                            "Only fix errors and formatting. Keep it 100% authentic to the speaker's intent."
                        )
                    },
                    {"role": "user", "content": raw_text}
                ],
                max_tokens=500,
                temperature=0
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"LLM Cleaning failed: {e}")
            return raw_text

    async def on_user_turn_completed(
        self, chat_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ):
        try:
            user_transcript = new_message.text_content
            timestamp = int(time.time() * 1000)
            logger.info(
                f"{self.participant_identity} -> {user_transcript} at {timestamp}"
            )
            self.transcript_collector.append(
                f"{self.participant_identity}: {user_transcript} : {timestamp}"
            )
        except Exception as e:
            logger.error(f"⚠️ Error during transcript collection: {e}")
            self.transcript_collector.append(f"ERROR: {str(e)}")
        finally:
            raise StopResponse()

class MultiUserTranscriber:
    def __init__(self, ctx: JobContext, existing_call_id: Optional[str] = None):
        self.ctx = ctx
        self._sessions: dict[str, AgentSession] = {}
        self._tasks: set[asyncio.Task] = set()
        self.transcript_data: list[str] = []
        self.participants_remaining: set[str] = set()
        self.note_call_id = existing_call_id
        self.room_ended = False  # track whether the room actually ended
        self.retry_mode = False  # 👈 new: when True, this was an RTC crash / retry

    def set_retry_mode(self):
        """
        Called from entrypoint when we detect a critical RTC error and plan to rejoin.
        In this mode, aclose() MUST NOT mark the call as ended or start analysis.
        """
        self.retry_mode = True

    def start(self):
        self.ctx.room.on("participant_connected", self.on_participant_connected)
        self.ctx.room.on("participant_disconnected", self.on_participant_disconnected)
        # listen for room-level disconnect (room destroyed / closed)
        self.ctx.room.on("disconnected", self.on_room_disconnected)

    async def aclose(self):
        # Cancel running tasks and close sessions
        await utils.aio.cancel_and_wait(*self._tasks)
        await asyncio.gather(
            *[self._close_session(session) for session in self._sessions.values()]
        )

        # Remove room event listeners
        self.ctx.room.off("participant_connected", self.on_participant_connected)
        self.ctx.room.off("participant_disconnected", self.on_participant_disconnected)
        # detach the room disconnected handler
        self.ctx.room.off("disconnected", self.on_room_disconnected)

        end_call_time = int(time.time() * 1000)
        error_msg = None

        if not self.room_ended:
            try:
                if not self.ctx.room.remote_participants:
                    logger.info(
                        "🏁 No remote participants at shutdown; inferring room has ended."
                    )
                    self.room_ended = True
            except Exception:
                # If anything goes wrong reading remote_participants, don't crash saving.
                logger.warning(
                    "⚠️ Could not inspect remote_participants; leaving room_ended as-is."
                )

        try:
            # ✅ Only save if transcripts exist
            if self.transcript_data:
                new_transcript_entry = list(map(str, self.transcript_data))
                db = NoteTakerSessionLocal()
                try:
                    # 1. Fetch by stored ID or search for an ACTIVE one if ID is missing
                    note_call = None
                    if self.note_call_id:
                        note_call = (
                            db.query(NoteTakerCall)
                            .filter_by(id=self.note_call_id)
                            .first()
                        )
                        if not note_call:
                            note_call = (
                                db.query(NoteTakerCall)
                                .filter_by(
                                    call_id=self.ctx.room.name, call_status="active"
                                )
                                .order_by(NoteTakerCall.start_timestamp.desc())
                                .first()
                            )
                    else:
                        note_call = (
                            db.query(NoteTakerCall)
                            .filter_by(call_id=self.ctx.room.name, call_status="active")
                            .order_by(NoteTakerCall.start_timestamp.desc())
                            .first()
                        )

                    # SPECIAL CASE: RETRY MODE (RTC ERROR REJOIN)
                    if self.retry_mode and note_call:
                        # just append transcripts and keep call ACTIVE, no ending, no analysis
                        if note_call.call_analysis is None:
                            note_call.call_analysis = {
                                "status": "initializing",
                                "transcript_dict": [],
                            }

                        existing_transcripts = note_call.call_analysis.get(
                            "transcript_dict", []
                        )
                        existing_transcripts.extend(new_transcript_entry)

                        note_call.call_analysis = {
                            "status": "pending",
                            "transcript_dict": existing_transcripts,
                        }

                        # Ensure it stays active (meeting still ongoing)
                        note_call.call_status = "active"
                        note_call.updated_at = end_call_time

                        db.commit()
                        self.note_call_id = note_call.id
                        logger.info(
                            f"✅ [RETRY MODE] Buffered transcript flushed to DB (id={self.note_call_id}) "
                            f"for room '{self.ctx.room.name}', keeping call ACTIVE."
                        )
                        return  # do NOT fall through to normal end/analysis logic

                    if note_call:
                        # Append new transcript lines
                        if note_call.call_analysis is None:
                            note_call.call_analysis = {
                                "status": "initializing",
                                "transcript_dict": [],
                            }

                        existing_transcripts = note_call.call_analysis.get(
                            "transcript_dict", []
                        )
                        existing_transcripts.extend(new_transcript_entry)

                        note_call.call_analysis = {
                            "status": "pending",
                            "transcript_dict": existing_transcripts,
                        }

                        # 🔚 IMPORTANT:
                        # Any non-retry aclose() means this note-taker session is finished.
                        # Mark the call as ENDED, regardless of room_ended / LiveKit state.
                        note_call.call_status = "ended"
                        note_call.end_timestamp = end_call_time

                        start_ts = note_call.start_timestamp or end_call_time
                        duration = end_call_time - start_ts
                        if duration < 0:
                            logger.warning(
                                f"⚠️ Negative duration detected: {duration}ms. Setting to 0."
                            )
                            duration = 0
                        note_call.duration_ms = duration

                        # always bump updated_at so we know this is the most recent row
                        note_call.updated_at = end_call_time

                        db.commit()
                        self.note_call_id = note_call.id
                        logger.info(
                            f"✅ Raw transcript UPDATED to DB (id={self.note_call_id}) for room '{self.ctx.room.name}'"
                        )
                        
                    else:
                        # 3. Create a new entry if none found
                        status = "ended" # if self.room_ended else "active"
                        start_ts = end_call_time
                        end_ts = end_call_time if self.room_ended else None
                        duration = end_call_time - start_ts if self.room_ended else 0

                        note_call = NoteTakerCall(
                            call_id=self.ctx.room.name,
                            start_timestamp=start_ts,
                            end_timestamp=end_ts,
                            duration_ms=duration,
                            call_status=status,
                            call_analysis={
                                "status": "pending",
                                "transcript_dict": new_transcript_entry,
                            },
                            updated_at=end_call_time,
                        )
                        db.add(note_call)
                        db.commit()
                        self.note_call_id = note_call.id
                        logger.info(
                            f"✅ Raw transcript CREATED in DB (id={self.note_call_id}) for room '{self.ctx.room.name}'"
                        )

                finally:
                    db.close()

                # analyze when this is a REAL end, not an RTC retry
                if self.note_call_id and not self.retry_mode:
                    logger.info(
                        f"🧠 Note-taker session ended, kicking off analysis for id={self.note_call_id}"
                    )
                    analyze_transcript.delay(self.note_call_id)
                else:
                    logger.info(
                        "ℹ️ Retry mode / non-final end; skipping analysis for now."
                    )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ Error during aclose: {error_msg}")

            db = NoteTakerSessionLocal()
            try:
                note_call = None
                if self.note_call_id:
                    note_call = (
                        db.query(NoteTakerCall).filter_by(id=self.note_call_id).first()
                    )
                if not note_call:
                    note_call = (
                        db.query(NoteTakerCall)
                        .filter_by(call_id=self.ctx.room.name)
                        .order_by(NoteTakerCall.start_timestamp.desc())
                        .first()
                    )

                if note_call:
                    if self.room_ended:
                        note_call.call_status = "ended"
                        note_call.end_timestamp = end_call_time
                    else:
                        note_call.call_status = "error"

                    note_call.call_analysis = {
                        "status": "error",
                        "summary": error_msg,
                        "transcript_dict": list(map(str, self.transcript_data)),
                    }
                    if self.room_ended and note_call.start_timestamp:
                        note_call.duration_ms = (
                            end_call_time - note_call.start_timestamp
                        )

                    note_call.updated_at = end_call_time  # 👈 added

                    db.commit()
                    logger.info(
                        f"⚠️ Error details saved in DB for call id={note_call.id}"
                    )
                else:
                    logger.warning(
                        f"⚠️ No NoteTakerCall found to log error for room '{self.ctx.room.name}'"
                    )
            finally:
                db.close()

    # event handlers

    def on_room_disconnected(self, *args, **kwargs):
        # Fired when LiveKit disconnects this room (e.g. explicitly closed).
        logger.info("🏁 Room 'disconnected' event received; marking room as ended.")
        self.room_ended = True

    def on_participant_connected(self, participant: rtc.RemoteParticipant):
        if participant.identity in self._sessions:
            return

        logger.info(f"🟢 Connected: {participant.identity}")
        self.participants_remaining.add(participant.identity)
        task = asyncio.create_task(self._start_session(participant))
        self._tasks.add(task)

        def on_task_done(task: asyncio.Task):
            try:
                self._sessions[participant.identity] = task.result()
            finally:
                self._tasks.discard(task)

        task.add_done_callback(on_task_done)

    def on_participant_disconnected(self, participant: rtc.RemoteParticipant):
        if (session := self._sessions.pop(participant.identity)) is None:
            return

        logger.info(f"🔴 Disconnected: {participant.identity}")
        self.participants_remaining.discard(participant.identity)
        task = asyncio.create_task(self._close_session(session))
        self._tasks.add(task)
        task.add_done_callback(lambda _: self._tasks.discard(task))

        # If everyone disconnected, shutdown and mark room as ended
        if not self.participants_remaining:
            logger.info("👋 All participants left, marking room as ended.")
            self.room_ended = True
            asyncio.create_task(self.aclose())

    async def _start_session(self, participant: rtc.RemoteParticipant) -> AgentSession:
        if participant.identity in self._sessions:
            return self._sessions[participant.identity]

        session = AgentSession(vad=self.ctx.proc.userdata["vad"])
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

        # BEY avatar functionality removed, only transcription sessions are used
        await session.start(
            agent=Transcriber(
                participant_identity=participant.identity,
                transcript_collector=self.transcript_data,
            ),
            record=False
        )
        return session

    async def _close_session(self, sess: AgentSession):
        if not sess:
            return

        try:
            await sess.drain()
        except RuntimeError as e:
            if "isn't running" not in str(e):
                raise

        await sess.aclose()

async def entrypoint(ctx: JobContext):
    """
    Note-taker entrypoint with automatic RTC error retry.

    Behavior:
    - On startup, find/create NoteTakerCall row for this room and keep its ID.
    - If we hit a known RTC error (Subscriber pc state failed / rtc_session),
      we flush buffer to DB WITHOUT ending the call, disconnect, and rejoin
      the SAME room with the SAME NoteTakerCall.id.
    """

    max_retries = 3
    retry_count = 0

    # 1. Find/create persistent call
    persistent_call_id: Optional[str] = None
    db = NoteTakerSessionLocal()
    try:
        # Pick the latest resumable call for this room (active/error),
        # or create a new one if none found.
        note_call = (
            db.query(NoteTakerCall)
            .filter(
                NoteTakerCall.call_id == ctx.room.name,
                NoteTakerCall.call_status.in_(["active", "error"]),
            )
            .order_by(NoteTakerCall.updated_at.desc())
            .first()
        )

        now_ms = int(time.time() * 1000)

        if note_call:
            persistent_call_id = note_call.id
            logger.info(
                f"🔄 Rejoining existing call session in DB with ID: {persistent_call_id} "
                f"(prev_status={note_call.call_status})"
            )
            note_call.call_status = "active"
            note_call.end_timestamp = None
            note_call.duration_ms = 0
            note_call.updated_at = now_ms
            db.commit()
        else:
            new_call = NoteTakerCall(
                call_id=ctx.room.name,
                start_timestamp=now_ms,
                end_timestamp=None,
                call_status="active",
                duration_ms=0,
                call_analysis={"status": "initializing", "transcript_dict": []},
                updated_at=now_ms,
            )
            db.add(new_call)
            db.commit()
            persistent_call_id = new_call.id
            logger.info(
                f"✨ Created NEW call session in DB with ID: {persistent_call_id}"
            )
    finally:
        db.close()

    # 2. Retry loop
    while True:
        transcriber = MultiUserTranscriber(ctx, existing_call_id=persistent_call_id)
        transcriber.start()

        try:
            await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
            # Connect to already-present participants
            for participant in ctx.room.remote_participants.values():
                transcriber.on_participant_connected(participant)

            ctx.add_shutdown_callback(lambda: transcriber.aclose())

            # Normal path: just return when LiveKit ends the job
            return

        except Exception as e:
            error_str = str(e)

            # 3. ERROR VALIDATION
            if "Subscriber pc state failed" in error_str or "rtc_session" in error_str:
                logger.error(f"🚨 CRITICAL RTC ERROR DETECTED: {error_str}")
                logger.info("♻️ Validated RTC error. Initiating REJOIN sequence...")

                try:
                    # Tell transcriber NOT to end/close the DB call, just flush transcripts
                    transcriber.set_retry_mode()
                    await transcriber.aclose()
                except Exception as e2:
                    logger.error(
                        f"⚠️ Error while flushing transcripts in retry mode: {e2}"
                    )

                # Try to disconnect cleanly to reset SDK state
                try:
                    await ctx.disconnect()
                except Exception:
                    pass

                retry_count += 1
                if retry_count > max_retries:
                    logger.error("❌ Max RTC retries reached. Giving up.")
                    raise  # bubble up to worker

                logger.info(f"🔁 Retry {retry_count}/{max_retries} in 2s...")
                await asyncio.sleep(2)  # async cooldown
                continue  # 🔄 RESTART LOOP with SAME persistent_call_id

            # Non-RTC errors → re-raise (handled as usual)
            raise

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()

async def request_fnc(req: JobRequest):

    await req.accept(
        name="note-taker-agent",
        identity="note-taker-agent",
    )


def main():
    opts = WorkerOptions(
        entrypoint_fnc=entrypoint,
        request_fnc=request_fnc,
        prewarm_fnc=prewarm,
        job_memory_warn_mb=1024,
        agent_name="note-taker-agent",
        port=8091,
    )
    cli.run_app(
        opts,
    )


if __name__ == "__main__":
    main()
