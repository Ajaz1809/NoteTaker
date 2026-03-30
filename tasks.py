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
CHUNK_TOKEN_LIMIT = 1500       # <<< FURTHER REDUCED from 2000 to avoid large merge context
SAFETY_MARGIN_TOKENS = 500
MAX_PARALLEL_CHUNKS = 5        # send 5 parallel requests for faster processing

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
        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)
        if not content:
            logger.warning("⚠️ Empty response from model")
            return ""

        return content  # let caller parse JSON

    except Exception as e:
        error_str = str(e)
        # Check for context length exceeded error
        if "context_length_exceeded" in error_str or "maximum context length" in error_str:
            logger.exception(
                f"❌ Context length exceeded. Prompt is too long. "
                f"Consider reducing chunk size or using hierarchical merging. Error: {e}"
            )
        else:
            logger.exception(f"❌ Chat completion request failed: {e}")
        return ""

def _coerce_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return []


def _safe_text(value):
    if isinstance(value, str):
        return value.strip()
    return ""


async def analyze_chunk(chunk_text: str) -> dict:
    """Analyze one chunk using gpt-4o-mini."""
    user_prompt = f"""
You are an expert AI meeting assistant trained to generate structured, business-quality meeting summaries.

Analyze the following meeting transcript and return a structured JSON output.

Transcript:
{chunk_text}

Instructions:
- Understand context even if sentences are incomplete or noisy
- Convert conversational language into clear professional insights
- Focus on decisions, problems, solutions, and responsibilities
- Avoid generic or vague statements
- Do NOT copy transcript text directly

Extract the following:

1. Meeting Purpose
- A clear 1-line reason why the meeting was conducted

2. Key Takeaways
- 3 to 6 most important insights, updates, or conclusions

3. Decisions
- Explicit decisions finalized during this part of the meeting

4. Topics
- Group discussions into meaningful business topics
- Each topic must include:
    - title: short and meaningful
    - problem: issue discussed (null if none)
    - solution: outcome or conclusion
    - rationale: why this decision/solution was chosen
    - postponed_items: only if explicitly postponed, else []

5. Action Items
- Extract tasks with clear ownership
- Only include real actionable tasks (avoid unnecessary "no task")

6. Blockers
- Any risks, issues, or delays affecting progress

7. Next Steps
- Immediate follow-ups or future actions

8. Meeting Tone & Observations
- Overall tone and atmosphere of the meeting
- Key behavioral observations (e.g., accountability focus, performance concerns)
- Communication style insights

9. Transcript Snapshot (Cleaned Highlights)
- 3-5 most representative or impactful quotes from the transcript
- Clean and professional language
- Include speaker attribution
- Focus on commitments, issues, or key statements

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

Rules:
- Keep output concise but meaningful
- Do not repeat information
- Use professional, clean language
- Ensure valid JSON only (no extra text)
"""
    content = await chat_completion(MODEL_FAST, user_prompt)
    logger.info(f"🤖 AI response for chunk: {content[:500]}...")
    if not content:
        logger.warning("⚠️ Empty response for chunk analysis")
        return {
            "meeting_purpose": "",
            "key_takeaways": [],
            "decisions": [],
            "topics": [],
            "action_items": [],
            "blockers": [],
            "next_steps": [],
            "meeting_tone": [],
            "transcript_highlights": [],
            "transcript_dict": chunk_text.split("\n"),
            "error": "empty",
        }
    try:
        result = json.loads(content)

        # Normalize output structure
        def _normalize_topics(raw_topics):
            out = []
            if isinstance(raw_topics, list):
                for t in raw_topics:
                    if isinstance(t, dict):
                        out.append({
                            "title": _safe_text(t.get("title") or t.get("topic") or "Untitled Topic"),
                            "problem": t.get("problem") if t.get("problem") or t.get("problem") == "" else None,
                            "solution": _safe_text(t.get("solution") or t.get("resolve") or ""),
                            "rationale": _safe_text(t.get("rationale") or t.get("why") or "") or None,
                            "postponed_items": _coerce_list(t.get("postponed_items") or t.get("deferred") or []),
                        })
                    elif isinstance(t, str) and t.strip():
                        out.append({
                            "title": t.strip(),
                            "problem": None,
                            "solution": "",
                            "rationale": None,
                            "postponed_items": [],
                        })
            return out

        def _normalize_action_items(raw):
            out = []
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        owner = _safe_text(item.get("owner") or item.get("assignee") or "Unknown")
                        task = _safe_text(item.get("task") or item.get("action") or "")
                        priority = _safe_text(item.get("priority") or "") or None
                        deadline = _safe_text(item.get("deadline") or item.get("due") or "") or None
                        if owner or task:
                            out.append({"owner": owner or "Unknown", "task": task or "no task", "priority": priority, "deadline": deadline})
                    elif isinstance(item, str) and item.strip():
                        out.append({"owner": "Unknown", "task": item.strip(), "priority": None, "deadline": None})
            elif isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, list) and v:
                        for task in v:
                            if isinstance(task, str):
                                out.append({"owner": _safe_text(k), "task": task.strip(), "priority": None, "deadline": None})
                    elif isinstance(v, str):
                        out.append({"owner": _safe_text(k), "task": v.strip(), "priority": None, "deadline": None})
            return out

        normalized = {
            "meeting_purpose": _safe_text(result.get("meeting_purpose") or result.get("purpose") or ""),
            "key_takeaways": _coerce_list(result.get("key_takeaways") or result.get("key_points") or []),
            "decisions": _coerce_list(result.get("decisions") or []),
            "topics": _normalize_topics(result.get("topics") or []),
            "action_items": _normalize_action_items(result.get("action_items") or result.get("users_tasks") or []),
            "blockers": _coerce_list(result.get("blockers") or result.get("negative_points") or []),
            "next_steps": _coerce_list(result.get("next_steps") or []),
            "transcript_dict": _coerce_list(result.get("transcript_dict") or chunk_text.split("\n")),
        }
        return normalized
    except Exception as e:
        logger.exception(f"❌ Failed to parse JSON for chunk: {e}")
        return {
            "meeting_purpose": "",
            "key_takeaways": [],
            "decisions": [],
            "topics": [],
            "action_items": [],
            "blockers": [],
            "next_steps": [],
            "transcript_dict": chunk_text.split("\n"),
            "error": str(e),
        }

def _strip_chunk_results(chunk_results: list[dict]) -> list[dict]:
    """
    Strip transcript_dict from chunk results to reduce token count.
    Keep only analysis fields needed for merging.
    """
    stripped = []
    for ch in chunk_results:
        if not isinstance(ch, dict):
            continue
        stripped_ch = {
            "meeting_purpose": ch.get("meeting_purpose", ""),
            "key_takeaways": ch.get("key_takeaways", []),
            "decisions": ch.get("decisions", []),
            "topics": ch.get("topics", []),
            "action_items": ch.get("action_items", []),
            "blockers": ch.get("blockers", []),
            "next_steps": ch.get("next_steps", []),
            "meeting_tone": ch.get("meeting_tone", []),
            "transcript_highlights": ch.get("transcript_highlights", []),
        }
        stripped.append(stripped_ch)
    return stripped

def _estimate_tokens(text: str) -> int:
    """Quick token estimation (1 token ≈ 4 characters)."""
    return int(len(text) / 4) + 500  # add buffer for JSON overhead

async def merge_analysis(all_chunk_results: list[dict], transcript_list: list[str]) -> dict:
    """Merge all chunk analyses with gpt-4o.

    Includes robust JSON-recovery logic when the model returns invalid/truncated JSON
    (e.g. unterminated string). Tries multiple heuristics before failing.
    """
    # 🔥 STRIP REDUNDANT DATA TO REDUCE TOKEN COUNT
    stripped_results = _strip_chunk_results(all_chunk_results)

    merge_prompt = f"""
You are a senior AI meeting analyst.

You are given multiple partial meeting analyses.
Merge them into ONE final, clean, structured output.

Partial analyses:
{json.dumps(stripped_results, ensure_ascii=False)}

Instructions:

1. Meeting Purpose
- Combine into ONE clear and concise statement

2. Key Takeaways
- Merge and deduplicate
- Keep only the most important 5–6 points
- Do not repeat any point

3. Decisions
- Combine all decisions
- Remove duplicates strictly
- Keep only clear, finalized outcomes

4. Topics
- Merge similar topics together
- Ensure each topic is meaningful and not repeated
- Deduplicate by title - if two topics have the same title, merge their content
- Improve clarity of:
    - problem
    - solution
    - rationale
- Keep topics business-focused, not generic

5. Action Items
- Merge all tasks
- Remove duplicates strictly - same owner and task should appear only once
- Ensure each task has:
    - clear owner
    - meaningful description
- Assign priority:
    - high → urgent / blockers / bugs
    - medium → important but not urgent
    - low → minor improvements
- Keep deadline if mentioned

6. Blockers
- Combine all risks/issues
- Keep only meaningful blockers
- Remove duplicates

7. Next Steps
- Merge and deduplicate
- Keep actionable future steps
- Do not repeat

8. Meeting Tone & Observations
- Combine observations about meeting atmosphere and communication style
- Focus on key behavioral insights (e.g., accountability, collaboration, concerns)
- Keep 3-5 most representative observations
- Remove duplicates

9. Transcript Snapshot (Cleaned Highlights)
- Select 4-6 most impactful quotes from across all chunks
- Clean and professionalize the language
- Include speaker attribution
- Focus on commitments, issues, decisions, or key statements
- Ensure diversity across speakers if possible

Quality Rules:
- No repetition in any section
- No vague statements
- No filler content
- Use clear, professional language
- Ensure logical grouping

Return ONLY valid JSON:

{{
  "meeting_purpose": "...",
  "key_takeaways": ["..."],
  "decisions": ["..."],
  "topics": [...],
  "action_items": [...],
  "blockers": ["..."],
  "next_steps": ["..."],
  "meeting_tone": ["..."],
  "transcript_highlights": ["..."],
  "transcript_dict": ["..."]
}}
"""
    # 🔥 TOKEN CHECK: Estimate if the prompt exceeds safe limit
    estimated_tokens = _estimate_tokens(merge_prompt)
    max_safe_tokens = 100000  # Leave buffer before 128k limit
    
    if estimated_tokens > max_safe_tokens:
        logger.warning(
            f"⚠️ Merge prompt too large ({estimated_tokens} est. tokens). "
            f"Falling back to local merge to save computation."
        )
        return _local_merge(all_chunk_results, transcript_list)

    def _normalize_topic_item(item: dict) -> dict:
        return {
            "title": _safe_text(item.get("title") or item.get("topic") or "Untitled Topic"),
            "problem": item.get("problem") if item.get("problem") is not None else None,
            "solution": _safe_text(item.get("solution") or item.get("outcome") or ""),
            "rationale": _safe_text(item.get("rationale") or item.get("why") or "") or None,
            "postponed_items": _coerce_list(item.get("postponed_items") or []),
        }

    def _normalize_topics(raw) -> list:
        topics = []
        if isinstance(raw, list):
            for t in raw:
                if isinstance(t, dict):
                    topics.append(_normalize_topic_item(t))
                elif isinstance(t, str) and t.strip():
                    topics.append({"title": t.strip(), "problem": None, "solution": "", "rationale": None, "postponed_items": []})
        return topics

    def _normalize_action_items(raw) -> list:
        out = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    owner = _safe_text(item.get("owner") or item.get("assignee") or "Unknown")
                    task = _safe_text(item.get("task") or item.get("action") or "no task")
                    priority = _safe_text(item.get("priority") or "") or None
                    deadline = _safe_text(item.get("deadline") or item.get("due") or "") or None
                    out.append({"owner": owner or "Unknown", "task": task, "priority": priority, "deadline": deadline})
                elif isinstance(item, str) and item.strip():
                    out.append({"owner": "Unknown", "task": item.strip(), "priority": None, "deadline": None})
        elif isinstance(raw, dict):
            # fallback old map format
            for owner, tasks in raw.items():
                if isinstance(tasks, list):
                    for task in tasks:
                        if isinstance(task, str):
                            out.append({"owner": _safe_text(owner), "task": task.strip(), "priority": None, "deadline": None})
                elif isinstance(tasks, str):
                    out.append({"owner": _safe_text(owner), "task": tasks.strip(), "priority": None, "deadline": None})
        return out

    def _ensure_list(value):
        return value if isinstance(value, list) else []

    def _normalize_final_result(result: dict, transcript_list: list[str]) -> dict:
        if not isinstance(result, dict):
            result = {}
        
        # deduplicate lists
        def dedup_list(lst):
            seen = set()
            out = []
            for item in lst:
                if isinstance(item, str) and item.strip() and item not in seen:
                    out.append(item.strip())
                    seen.add(item)
            return out
        
        # deduplicate topics by title
        raw_topics = _ensure_list(result.get("topics") or [])
        topics_dict = {}
        for t in raw_topics:
            if isinstance(t, dict):
                norm_t = _normalize_topic_item(t)
                title = norm_t["title"]
                if title not in topics_dict:
                    topics_dict[title] = norm_t
        
        # deduplicate action_items by (owner, task)
        raw_action_items = _ensure_list(result.get("action_items") or result.get("users_tasks") or [])
        action_items = []
        action_set = set()
        for ai in _normalize_action_items(raw_action_items):
            key = (ai["owner"], ai["task"])
            if key not in action_set:
                action_items.append(ai)
                action_set.add(key)
        
        final = {
            "meeting_purpose": _safe_text(result.get("meeting_purpose") or result.get("purpose") or ""),
            "key_takeaways": dedup_list(_coerce_list(result.get("key_takeaways") or result.get("key_points") or [])),
            "decisions": dedup_list(_coerce_list(result.get("decisions") or [])),
            "topics": list(topics_dict.values()),
            "action_items": action_items,
            "blockers": dedup_list(_coerce_list(result.get("blockers") or result.get("negative_points") or [])),
            "next_steps": dedup_list(_coerce_list(result.get("next_steps") or [])),
            "meeting_tone": dedup_list(_coerce_list(result.get("meeting_tone") or [])),
            "transcript_highlights": dedup_list(_coerce_list(result.get("transcript_highlights") or [])),
            "transcript_dict": transcript_list,
        }

        # ensure required shape for topics
        if not final["topics"]:
            final["topics"] = []

        # ensure required shape for action_items
        if not final["action_items"]:
            final["action_items"] = []

        return final

    def _try_extract_balanced_json(s: str) -> str | None:
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
                        if len(candidate) < 5:
                            continue
                        try:
                            json.loads(candidate)
                            return candidate
                        except Exception:
                            continue
        return None

    def _local_merge(all_chunks: list[dict], transcript_list: list[str]) -> dict:
        meeting_purpose = ""
        key_takeaways = []
        decisions = []
        topics = []
        action_items = []
        blockers = []
        next_steps = []
        meeting_tone = []
        transcript_highlights = []

        topics_dict = {}  # deduplicate by title
        action_set = set()  # deduplicate by (owner, task)

        for ch in all_chunks:
            if not isinstance(ch, dict):
                continue
            if not meeting_purpose:
                meeting_purpose = _safe_text(ch.get("meeting_purpose") or ch.get("purpose") or "")
            for item in _coerce_list(ch.get("key_takeaways") or ch.get("key_points") or []):
                if isinstance(item, str) and item.strip() and item not in key_takeaways:
                    key_takeaways.append(item.strip())
            for item in _coerce_list(ch.get("decisions") or []):
                if isinstance(item, str) and item.strip() and item not in decisions:
                    decisions.append(item.strip())
            for item in _coerce_list(ch.get("blockers") or ch.get("negative_points") or []):
                if isinstance(item, str) and item.strip() and item not in blockers:
                    blockers.append(item.strip())
            for item in _coerce_list(ch.get("next_steps") or []):
                if isinstance(item, str) and item.strip() and item not in next_steps:
                    next_steps.append(item.strip())
            for item in _coerce_list(ch.get("meeting_tone") or []):
                if isinstance(item, str) and item.strip() and item not in meeting_tone:
                    meeting_tone.append(item.strip())
            for item in _coerce_list(ch.get("transcript_highlights") or []):
                if isinstance(item, str) and item.strip() and item not in transcript_highlights:
                    transcript_highlights.append(item.strip())
            raw_topics = ch.get("topics") or []
            if isinstance(raw_topics, list):
                for t in raw_topics:
                    if isinstance(t, dict):
                        norm_t = _normalize_topic_item(t)
                        title = norm_t["title"]
                        if title not in topics_dict:
                            topics_dict[title] = norm_t
                        else:
                            # merge if needed, but for simplicity, keep first
                            pass
                    elif isinstance(t, str) and t.strip():
                        title = t.strip()
                        if title not in topics_dict:
                            topics_dict[title] = {"title": title, "problem": None, "solution": "", "rationale": None, "postponed_items": []}
            raw_action_items = ch.get("action_items") or ch.get("users_tasks") or []
            for ai in _normalize_action_items(raw_action_items):
                key = (ai["owner"], ai["task"])
                if key not in action_set:
                    action_items.append(ai)
                    action_set.add(key)

        return {
            "meeting_purpose": meeting_purpose or "Meeting purpose not available (local fallback).",
            "key_takeaways": key_takeaways,
            "decisions": decisions,
            "topics": list(topics_dict.values()),
            "action_items": action_items,
            "blockers": blockers,
            "next_steps": next_steps,
            "meeting_tone": meeting_tone,
            "transcript_highlights": transcript_highlights,
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
                    normalized = _normalize_final_result(result, transcript_list)
                    logger.info(f"✅ Final merge successful: key_points={len(normalized.get('key_points', []))} items")
                    return normalized
                except Exception as e:
                    # Log the parse error and attempt recovery heuristics
                    logger.error(f"❌ Failed to parse final merge JSON (attempt {attempt}/{retries}): {e}")
                    logger.debug(f"Raw model response (truncated 32k): {repr(content[:32768])}")

                    # Heuristic 1: try to extract a balanced JSON substring from the model response
                    recovered = _try_extract_balanced_json(content)
                    if recovered:
                        try:
                            result = json.loads(recovered)
                            normalized = _normalize_final_result(result, transcript_list)
                            logger.info(f"✅ Successfully recovered JSON from model response on attempt {attempt}")
                            return normalized
                        except Exception as e2:
                            logger.exception(f"❌ Recovered substring still failed to parse: {e2}")

                    # Heuristic 2: try simple fixes for unterminated string errors
                    if isinstance(e, json.JSONDecodeError) and 'Unterminated string' in str(e):
                        # Attempt to close open quotes by appending a closing '"' and closing brace
                        alt = content + '"}'
                        try:
                            result = json.loads(alt)
                            normalized = _normalize_final_result(result, transcript_list)
                            logger.info(f"✅ Fixed JSON by appending closing characters on attempt {attempt}")
                            return normalized
                        except Exception:
                            logger.debug("⚠️ Quick append fix did not work")

                    # If parsing failed, allow retry (the for-loop will continue)

        except RateLimitError as r:
            logger.exception(f"❌ OpenAI quota exceeded: {r}")
            return {
                "meeting_purpose": "",
                "key_takeaways": [],
                "decisions": [],
                "topics": [],
                "action_items": [],
                "blockers": [],
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
                normalized = _normalize_final_result(result, transcript_list)
                logger.info("✅ Final recovery succeeded")
                return normalized
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
        logger.info(f"📊 Analysis result: meeting_purpose={result.get('meeting_purpose')} key_takeaways={len(result.get('key_takeaways', []))} decisions={len(result.get('decisions', []))}")

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
