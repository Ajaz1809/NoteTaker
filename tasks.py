import asyncio
import json
import logging
import time
from celery_app import celery
from openai import OpenAI, RateLimitError
from db.database import NoteTakerSessionLocal
from models.models import NoteTakerCall
import tiktoken

logger = logging.getLogger("tasks")
client = OpenAI()

# ----------------- CONFIG -----------------
MODEL_FAST = "gpt-4o-mini"     # for chunk analysis (fast + cheap)
MODEL_FINAL = "gpt-4o"         # for final merge (high quality)
MODEL_MAX_TOKENS = 25000
CHUNK_TOKEN_LIMIT = 1000       # <<< REDUCED FROM 10000 -> 1000 as requested
SAFETY_MARGIN_TOKENS = 500
MAX_PARALLEL_CHUNKS = 3         # send 3 requests at once

# ----------------- TOKEN COUNTER -----------------
def get_token_counter(model_name: str):
    try:
        try:
            enc = tiktoken.encoding_for_model(model_name)
        except Exception:
            enc = tiktoken.get_encoding("cl100k_base")
        return lambda text: len(enc.encode(text or ""))
    except Exception:
        return lambda text: max(1, int(len(text or "") / 4))

num_tokens = get_token_counter(MODEL_FAST)

# ----------------- HELPERS -----------------
def chunk_lines_by_tokens(lines: list[str], max_tokens: int) -> list[list[str]]:
    chunks, cur, cur_tokens = [], [], 0
    for line in lines:
        line_tokens = num_tokens(line + "\n")
        if cur and (cur_tokens + line_tokens > max_tokens):
            chunks.append(cur)
            cur, cur_tokens = [line], line_tokens
        else:
            cur.append(line)
            cur_tokens += line_tokens
    if cur:
        chunks.append(cur)
    return chunks

async def chat_completion(model: str, prompt: str) -> str:
    """
    Run non-streaming chat completion and return the raw JSON string
    (caller is responsible for parsing).
    """
    try:
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                response_format={"type": "json_object"},  # ✅ enforce JSON output
            ),
        )

        # Extract content
        content = response.choices[0].message.content
        if not content:
            logger.warning("⚠️ Empty response from model")
            return ""

        return content  # let caller parse JSON

    except Exception as e:
        logger.exception(f"❌ Chat completion request failed: {e}")
        return ""

async def analyze_chunk(chunk_text: str) -> dict:
    """Analyze one chunk using gpt-4o-mini."""
    user_prompt = f"""
    You are an intelligent meeting assistant.
    Analyze the following transcript chunk and return a JSON object.

    Transcript chunk:
    {chunk_text}

    JSON schema:
    - summary: one paragraph summary
    - purpose: meeting goal
    - key_points: list of points
    - users_tasks: map of ALL users mentioned in the transcript to their tasks.
      * If a user has no clear task, include them with ["no task"]
    - next_steps: actionable follow-ups
    - transcript_dict: transcript lines (exactly as given)
    """
    content = await chat_completion(MODEL_FAST, user_prompt)
    try:
        if content:
            return json.loads(content) 
        else:
            logger.warning("⚠️ Empty response for chunk analysis")
            return {
                "summary": "Error analyzing chunk",
                "purpose": "",
                "key_points": [],
                "users_tasks": {},
                "next_steps": [],
                "transcript_dict": chunk_text.split("\n"),
                "error": "empty",
            }
       
    except Exception as e:
        logger.exception(f"❌ Failed to parse JSON for chunk: {e}")
        return {"error": str(e), "transcript_dict": chunk_text.split("\n")}

async def merge_analysis(all_chunk_results: list[dict], transcript_list: list[str]) -> dict:
    """Merge all chunk analyses with gpt-4o.

    Includes robust JSON-recovery logic when the model returns invalid/truncated JSON
    (e.g. unterminated string). Tries multiple heuristics before failing.
    """
    merge_prompt = f"""
        You are a meeting assistant. Multiple partial analyses are provided.
        Merge them into a single final JSON analysis.

        Partial analyses (JSON list):
        {json.dumps(all_chunk_results, ensure_ascii=False)}

        Instructions:
        - Combine summaries into one coherent summary
        - Merge purposes into one clear purpose
        - Deduplicate key points and next_steps
        - users_tasks:
            * Must include ALL users that appear in the transcript.
            * If a user has no assigned task, still list them with ["no task"].
        - transcript_dict must include the FULL transcript (all lines)

        Return ONLY a JSON object with fields:
        summary, purpose, key_points, users_tasks, next_steps, transcript_dict
        """

    def _try_extract_balanced_json(s: str) -> str | None:
        """Attempt to extract a balanced JSON object substring from s.

        Strategy:
        - Find each opening brace '{' or '[' and scan forward keeping a stack counter.
        - When the counter returns to zero, attempt to parse that substring as JSON.
        - Return the first successfully parsed JSON text (string) or None.
        """
        starts = []
        for i, ch in enumerate(s):
            if ch in '{[':
                starts.append((i, ch))
        for start_idx, start_ch in starts:
            stack = []
            open_ch = start_ch
            close_ch = '}' if open_ch == '{' else ']'
            for j in range(start_idx, len(s)):
                if s[j] == open_ch:
                    stack.append(open_ch)
                elif s[j] == close_ch:
                    if stack:
                        stack.pop()
                    if not stack:
                        candidate = s[start_idx:j+1]
                        # quick sanity checks
                        if len(candidate) < 5:
                            continue
                        try:
                            json.loads(candidate)
                            return candidate
                        except Exception:
                            # try next possible closing
                            continue
        return None

    def _local_merge(all_chunks: list[dict], transcript_list: list[str]) -> dict:
        """
        Fallback merge when the LLM returns invalid JSON.
        We combine the partial analyses directly in Python.
        """
        summaries = []
        purposes = []
        key_points: list[str] = []
        next_steps: list[str] = []
        users_tasks: dict[str, set[str]] = {}

        for ch in all_chunks:
            if not isinstance(ch, dict):
                continue

            # Summaries
            s = ch.get("summary")
            if isinstance(s, str) and s.strip():
                summaries.append(s.strip())

            # Purpose
            p = ch.get("purpose")
            if isinstance(p, str) and p.strip():
                purposes.append(p.strip())

            # Key points
            kp = ch.get("key_points") or []
            if isinstance(kp, list):
                for item in kp:
                    if isinstance(item, str) and item.strip() and item not in key_points:
                        key_points.append(item)

            # Next steps
            ns = ch.get("next_steps") or []
            if isinstance(ns, list):
                for item in ns:
                    if isinstance(item, str) and item.strip() and item not in next_steps:
                        next_steps.append(item)

            # Users tasks
            ut = ch.get("users_tasks") or {}
            if isinstance(ut, dict):
                for user, tasks in ut.items():
                    if not isinstance(user, str) or not user.strip():
                        continue
                    if user not in users_tasks:
                        users_tasks[user] = set()
                    if isinstance(tasks, list):
                        for t in tasks:
                            if isinstance(t, str) and t.strip():
                                users_tasks[user].add(t.strip())

        # Deduplicate purposes but keep order
        seen_purposes = set()
        merged_purposes = []
        for p in purposes:
            if p not in seen_purposes:
                seen_purposes.add(p)
                merged_purposes.append(p)
        merged_purpose = " | ".join(merged_purposes) if merged_purposes else ""

        merged_summary = "\n\n".join(summaries) if summaries else ""

        # Convert users_tasks sets back to lists
        users_tasks_list = {
            user: sorted(list(tasks)) if tasks else ["no task"]
            for user, tasks in users_tasks.items()
        }

        return {
            "summary": merged_summary or "Summary not available (local merge fallback).",
            "purpose": merged_purpose,
            "key_points": key_points,
            "users_tasks": users_tasks_list,
            "next_steps": next_steps,
            "transcript_dict": transcript_list,
            "merge_mode": "local_fallback",
        }

    retries = 3
    last_content = None
    for attempt in range(1, retries + 1):
        try:
            content = await chat_completion(MODEL_FINAL, merge_prompt)
            last_content = content
            if not content:
                logger.warning(f"⚠️ Empty response on merge attempt {attempt}/{retries}")
            else:
                try:
                    result = json.loads(content)
                    result["transcript_dict"] = transcript_list
                    return result
                except Exception as e:
                    # Log the parse error and attempt recovery heuristics
                    logger.error(f"❌ Failed to parse final merge JSON (attempt {attempt}/{retries}): {e}")
                    logger.debug(f"Raw model response (truncated 32k): {repr(content[:32768])}")

                    # Heuristic 1: try to extract a balanced JSON substring from the model response
                    recovered = _try_extract_balanced_json(content)
                    if recovered:
                        try:
                            result = json.loads(recovered)
                            result["transcript_dict"] = transcript_list
                            logger.info(f"✅ Successfully recovered JSON from model response on attempt {attempt}")
                            return result
                        except Exception as e2:
                            logger.exception(f"❌ Recovered substring still failed to parse: {e2}")

                    # Heuristic 2: try simple fixes for unterminated string errors
                    if isinstance(e, json.JSONDecodeError) and 'Unterminated string' in str(e):
                        # Attempt to close open quotes by appending a closing '"' and closing brace
                        alt = content + '"}'
                        try:
                            result = json.loads(alt)
                            result["transcript_dict"] = transcript_list
                            logger.info(f"✅ Fixed JSON by appending closing characters on attempt {attempt}")
                            return result
                        except Exception:
                            logger.debug("⚠️ Quick append fix did not work")

                    # If parsing failed, allow retry (the for-loop will continue)

        except RateLimitError as r:
            logger.exception(f"❌ OpenAI quota exceeded: {r}")
            return {
                "summary": "OpenAI limit exceed",
                "purpose": "",
                "key_points": [],
                "users_tasks": {},
                "next_steps": [],
                "transcript_dict": transcript_list,
            }
        except Exception as e:
            logger.exception(f"❌ Unexpected error during merge (attempt {attempt}/{retries}): {e}")

        # Small delay before retrying
        if attempt < retries:
            await asyncio.sleep(2 * attempt)

    # After retries, attempt one final recovery from last_content if available
    if last_content:
        logger.warning("⚠️ All merge attempts failed — attempting final recovery from last response")
        recovered = _try_extract_balanced_json(last_content)
        if recovered:
            try:
                result = json.loads(recovered)
                result["transcript_dict"] = transcript_list
                logger.info("✅ Final recovery succeeded")
                return result
            except Exception as e:
                logger.exception(f"❌ Final recovery parse failed: {e}")

                # If still can't parse, fall back to local merge
        logger.error("❌ Merge failed after all retries and recoveries — falling back to local merge.")
        return _local_merge(all_chunk_results, transcript_list)

    logger.error("❌ Merge failed after all retries — falling back to local merge.")
    return _local_merge(all_chunk_results, transcript_list)


# ----------------- MAIN ANALYSIS -----------------
async def _analyze_chunked(transcript_list: list[str]) -> dict:
    transcript_list = [str(x) for x in transcript_list]
    chunks = chunk_lines_by_tokens(transcript_list, CHUNK_TOKEN_LIMIT)
    logger.info(f"📊 Total chunks to process: {len(chunks)}")

    all_chunk_results = []
    start_time = time.time()

    # Process chunks in parallel batches
    for i in range(0, len(chunks), MAX_PARALLEL_CHUNKS):
        batch = chunks[i:i + MAX_PARALLEL_CHUNKS]
        batch_texts = ["\n".join(c) for c in batch]

        logger.info(f"⏳ Processing batch {i//MAX_PARALLEL_CHUNKS + 1} with {len(batch)} chunks...")
        results = await asyncio.gather(*[analyze_chunk(ct) for ct in batch_texts])
        all_chunk_results.extend(results)

    logger.info(f"✅ All chunks analyzed in {time.time() - start_time:.2f}s. Merging...")

    # Merge with gpt-4o
    final_result = await merge_analysis(all_chunk_results, transcript_list)
    logger.info(f"✅ Final merge complete in {time.time() - start_time:.2f}s.")
    return final_result

# ----------------- CELERY TASK -----------------
@celery.task(name="tasks.analyze_transcript")
def analyze_transcript(call_db_id: str):
    """Celery worker entrypoint (sync wrapper)."""
    # Ensure event loop is available
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    db = NoteTakerSessionLocal()
    try:
        # Fetch the call row by unique DB id
        note_call = db.query(NoteTakerCall).filter_by(id=call_db_id).first()
        if not note_call:
            logger.warning(f"⚠️ No call found with db id {call_db_id}")
            return {"error": "call not found"}

        # Extract raw transcript list (saved earlier)
        transcript_list = []
        if note_call.call_analysis and "transcript_dict" in note_call.call_analysis:
            transcript_list = note_call.call_analysis["transcript_dict"]

        if not transcript_list:
            logger.warning(f"⚠️ No transcript found for call {call_db_id}")
            return {"error": "no transcript"}

        # Run async analysis
        result = loop.run_until_complete(_analyze_chunked(transcript_list))

        # Ensure JSON serializable
        try:
            result_json = json.loads(json.dumps(result))
        except Exception as e:
            logger.exception(f"❌ Result not JSON serializable: {e}")
            result_json = {"error": "not JSON serializable", "raw": str(result)}

        # Update DB row
        note_call.call_analysis = result_json  # ✅ overwrite "pending"
        db.add(note_call)
        db.commit()
        logger.info(f"✅ Analysis saved for call {call_db_id}")

        return result_json

    except Exception as e:
        logger.exception(f"❌ Error analyzing call {call_db_id}: {e}")
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()
