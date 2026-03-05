import httpx
import os
from dotenv import load_dotenv
load_dotenv()


ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")

async def validate_voice_id(voice_id: str) -> bool:
    """
    Validate ElevenLabs voice by making a minimal TTS request.
    Returns True if the voice ID is valid.
    Returns False if invalid.
    """

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"

    headers = {
        "xi-api-key": str(ELEVEN_API_KEY),
        "Content-Type": "application/json"
    }

    payload = {
        "text": "test",
        "model_id": "eleven_multilingual_v2"
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, headers=headers, json=payload)

    # SUCCESS

    if response.status_code == 200:
        return True

    # VOICE ID NOT FOUND OR OTHER ERROR
    return False
