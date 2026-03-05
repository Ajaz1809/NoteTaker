import json
import numpy as np
import logging
from typing import List, Tuple, Dict, Any
from openai import OpenAI

# --- Import your DB setup ---
# Adjust these imports based on where your actual database.py and models.py are located
from db.database import SessionLocal 
from models.models import KnowledgeFile 

# Initialize Logger
logger = logging.getLogger("kb-utils")
client = OpenAI()

def get_embedding(text: str) -> List[float]:
    """
    Generates an embedding vector for the input text using OpenAI.
    """
    text = text.replace("\n", " ").strip()
    if not text:
        return []
    try:
        response = client.embeddings.create(model="text-embedding-3-small", input=[text])
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"Error generating embedding: {e}")
        return []

def cosine_similarity(a, b):
    """
    Calculates the cosine similarity between two vectors.
    """
    if a is None or b is None or len(a) == 0 or len(b) == 0:
        return 0.0
    
    # Ensure inputs are numpy arrays
    a = np.array(a)
    b = np.array(b)
    
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

def retrieve_kb_context(query: str, kb_ids: List[str], top_k: int = 3) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Retrieves the most relevant context chunks from the specified Knowledge Bases.
    
    Returns:
        Tuple[str, List[Dict]]: (Combined context string, List of top result objects)
    """
    if not query or not kb_ids:
        return "", []

    # 1. Generate embedding for the user's query
    query_embedding = get_embedding(query)
    if not query_embedding:
        return "", []

    session = SessionLocal()
    results = []
    
    try:
        # 2. Fetch all knowledge files belonging to the active KB IDs
        # Filter out files without embeddings at DB level for better performance
        kb_files = (
            session.query(KnowledgeFile)
            .filter(
                KnowledgeFile.kb_id.in_(kb_ids),
                KnowledgeFile.extract_data.isnot(None),
                KnowledgeFile.embedding.isnot(None)
            )
            .all()
        )

        # 3. Calculate Similarity for each file/chunk
        # Note: For production with millions of records, use pgvector in the DB.
        # For smaller datasets, Python-side calculation is fine.
        for file in kb_files:
            # extract_data and embedding are already filtered at DB level
            if not file.extract_data or not file.embedding:
                continue
            
            # Parse stored embedding (assuming it's stored as a JSON string or list)
            stored_embedding = file.embedding
            if isinstance(stored_embedding, str):
                try:
                    stored_embedding = json.loads(stored_embedding)
                except:
                    # Fallback for legacy formats or use eval if strictly necessary/safe
                    try:
                        stored_embedding = eval(stored_embedding) 
                    except:
                        continue

            # Calculate Score
            score = cosine_similarity(query_embedding, stored_embedding)
            
            results.append({
                "score": score,
                "content": file.extract_data.strip(),
                "file_path": getattr(file, "file_path", "unknown"),
                "kb_id": file.kb_id
            })

    except Exception as e:
        logger.error(f"Error during KB retrieval: {e}")
        return "", []
    
    finally:
        session.close()

    # 4. Sort by score (High to Low) and slice top_k
    top_chunks = sorted(results, key=lambda x: x["score"], reverse=True)[:top_k]

    # 5. Combine content into a single string
    # We add a small delimiter like "\n---\n" to separate distinct chunks
    combined_context = "\n---\n".join(chunk["content"] for chunk in top_chunks)

    logger.info(f"Retrieved {len(top_chunks)} chunks from KB. Top score: {top_chunks[0]['score'] if top_chunks else 0}")

    return combined_context, top_chunks