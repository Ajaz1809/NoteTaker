#!/usr/bin/env python3
"""
Final Saver Consumer Service - Optimized for direct insertion
Consumes transcripts from RabbitMQ, writes NEW records to DB, triggers Celery
"""

import json
import logging
import time
import signal
import sys
from typing import Optional, Dict, Any, Union
from rmq_generics import RabbitMQHandler
from db.database import NoteTakerSessionLocal
from models.models import NoteTakerCall
from tasks import analyze_transcript
import os
import re

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("final-saver")

class FinalTranscriptConsumer:
    def __init__(self):
        self.rabbitmq = None
        self.running = True
        self.max_retries = 3
        self.retry_delay = 5
        
    def connect(self):
        try:
            self.rabbitmq = RabbitMQHandler(
                queue="final.transcripts",
                exchange="transcripts.exchange",
                exchange_type="direct"
            )
            logger.info("Final Saver Consumer connected to RabbitMQ")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to RabbitMQ: {e}")
            return False

    def insert_new_record(self, message: dict, retry_count: int = 0) -> Optional[int]:
        """Directly inserts a new call record into the database for every message"""
        
        print(f"\n\n################################# Data received: {message}\n\n")
        db = NoteTakerSessionLocal()
        try:
            meeting_id = message.get("meeting_id")
            room_name = message.get("room_name")
            transcript = message.get("transcript", [])
            participants = message.get("participants", [])
            start_timestamp = message.get("start_timestamp")
            end_timestamp = message.get("end_timestamp")
            duration_ms = message.get("duration_ms", 0)
            user_id = message.get("user_id")
            conf_name = message.get("conf_name")
            remain_participants = message.get("remaining_participants_count", 0)
            all_participants = message.get("all_participants_count", 0)
            participants_list = message.get("participants_list", [])
            participants_list = [re.sub(r'-\d+$', '', item).strip() for item in participants_list]
            json_participants_list = json.dumps(participants_list) if participants_list else None
            
            
            print(f"remaining_participants_count: type={type(remain_participants)} | value={remain_participants}")
            print(f"all_participants_count: type={len(participants_list)} | value={participants_list}")
            
            # Use status from message if provided, otherwise derive it
            # This fixes the bug where records were unnecessarily marked as 'active'
            incoming_status = message.get("status")
            if not incoming_status:
                incoming_status = "ended" if (end_timestamp or duration_ms > 0) else "active"

            logger.info(f"✨ Creating NEW call record for meeting_id: {meeting_id} with status: {incoming_status}")
            
            if not meeting_id:
                logger.error(f"Cannot create call record: missing meeting_id")
                raise ValueError("meeting_id is required")
            
            # Calculate duration if possible
            final_duration = duration_ms
            if end_timestamp and start_timestamp:
                final_duration = max(duration_ms, end_timestamp - start_timestamp)

            # ALWAYS INSERT NEW RECORD
            note_call = NoteTakerCall(
                call_id=room_name,
                meeting_id=meeting_id,
                start_timestamp=start_timestamp or int(time.time() * 1000),
                end_timestamp=end_timestamp,
                duration_ms=final_duration,
                call_status=incoming_status,
                user_id=user_id,
                conf_name=conf_name,
                remain_participants_count=remain_participants,
                all_participants_count=all_participants,
                participants_list=json_participants_list,
                call_analysis={
                    "status": "pending",
                    "transcript_dict": transcript,
                    "participants": participants,
                    "room_name": room_name,
                    "received_at": int(time.time() * 1000),
                    "update_count": 0
                },
                updated_at=int(time.time() * 1000)
            )
            
            db.add(note_call)
            db.commit()
            db.refresh(note_call)
            
            logger.info(f"✅ Inserted record ID: {note_call.id} | Meeting: {meeting_id} | Status: {note_call.call_status}")
            return note_call.id
                
        except Exception as e:
            logger.exception(f"Database error: {e}")
            db.rollback()
            if retry_count < self.max_retries:
                time.sleep(self.retry_delay)
                return self.insert_new_record(message, retry_count + 1)
            raise
        finally:
            db.close()
    
    def process_message(self, message: dict) -> bool:
        try:
            call_db_id = self.insert_new_record(message)
            if call_db_id:
                analyze_transcript.delay(str(call_db_id))
            return True
        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            return False
    
    def run(self):
        if not self.connect():
            sys.exit(1)
        
        def signal_handler(signum, frame):
            self.running = False
            if self.rabbitmq: self.rabbitmq.close()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            self.rabbitmq._ensure_connection()
            def callback(ch, method, properties, body):
                if not self.running: return
                try:
                    data = json.loads(body.decode('utf-8'))
                    if self.process_message(data):
                        ch.basic_ack(delivery_tag=method.delivery_tag)
                    else:
                        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                except Exception:
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            
            self.rabbitmq.channel.basic_qos(prefetch_count=1)
            self.rabbitmq.channel.basic_consume(queue=self.rabbitmq.queue, on_message_callback=callback)
            self.rabbitmq.channel.start_consuming()
        finally:
            if self.rabbitmq: self.rabbitmq.close()

if __name__ == "__main__":
    FinalTranscriptConsumer().run()
