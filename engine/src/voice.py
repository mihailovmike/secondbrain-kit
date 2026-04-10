"""Voice note pipeline: audio → Gemini transcription → structured note → inbox.

Gemini 2.5 Pro handles audio natively — no Whisper needed.
Raw transcription never enters the graph (W2: value gate applies).
"""

import json
import logging
import os
import tempfile
from pathlib import Path

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-pro")
VAULT_PATH = os.getenv("VAULT_PATH", "/app/vault")
INBOX_DIR_NAME = os.getenv("INBOX_DIR_NAME", "_inbox")

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


def transcribe_and_structure(audio_path: str, source: str = "voice") -> dict | None:
    """Transcribe audio and extract structured knowledge.

    Returns: {text: str, summary: str} or None if no valuable content.
    Audio is sent directly to Gemini (supports mp3, ogg, wav, m4a, etc.)
    """
    path = Path(audio_path)
    if not path.exists():
        logger.warning(f"Audio file not found: {audio_path}")
        return None

    audio_bytes = path.read_bytes()
    mime_map = {
        ".ogg": "audio/ogg", ".oga": "audio/ogg",
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
        ".wav": "audio/wav", ".webm": "audio/webm",
    }
    mime = mime_map.get(path.suffix.lower(), "audio/ogg")

    prompt = """Listen to this voice message and:
1. Transcribe the speech accurately.
2. Extract the key facts, decisions, and insights.
3. Structure the content as a clean note (NOT raw transcription).
4. Remove filler words, repetitions, and "uhm"s.
5. If the speaker asks to "structure and save" or "remember" — follow that instruction.

Return JSON:
{
  "text": "Structured note content in the speaker's language",
  "summary": "One-sentence summary of what was said",
  "has_value": true/false (does this contain long-term knowledge?)
}

If the audio is unclear or contains no meaningful content, set has_value to false."""

    try:
        resp = _get_client().models.generate_content(
            model=LLM_MODEL,
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime),
                prompt,
            ],
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )
        result = json.loads(resp.text)
        if not result.get("has_value", False):
            logger.info(f"Voice note has no long-term value, skipping: {audio_path}")
            return None
        return result
    except Exception as e:
        logger.error(f"Voice transcription failed: {e}")
        return None


def process_voice(audio_path: str, source: str = "voice") -> str | None:
    """Full pipeline: audio → structure → write to inbox.

    Returns path to created inbox file, or None.
    """
    result = transcribe_and_structure(audio_path, source)
    if not result:
        return None

    text = result["text"]
    summary = result.get("summary", "")

    # Write structured note to inbox (daemon will process it)
    inbox = Path(VAULT_PATH) / INBOX_DIR_NAME
    inbox.mkdir(parents=True, exist_ok=True)

    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"voice-{source}-{ts}.md"
    filepath = inbox / filename

    filepath.write_text(text, encoding="utf-8")
    logger.info(f"Voice note saved to inbox: {filename} ({summary})")

    return str(filepath)
