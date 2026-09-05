"""GET /api/tts/voices, POST /api/tts (ElevenLabs) — the two routes that
stayed directly on `app` since Task 4's router split (see
TASK4_SPLIT_ROUTERS.md §1: this POST /api/tts is permanently shadowed by
sessions.py's Sarvam-backed /api/tts, which is registered first and always
wins — dead code, kept only because there was no behavior reason to spend
a whole extraction pass on it at the time). Moved here now as part of
extracting src/diffdx/main.py, which should only do app assembly, not
routes.
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

router = APIRouter(tags=["tts"])

_ELEVENLABS_VOICES = [
    {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel",  "description": "Calm, clear female — ideal for clinical explanations"},
    {"id": "ErXwobaYiN019PkySvjV", "name": "Antoni",  "description": "Warm, professional male"},
    {"id": "TxGEqnHWrfWFTfGW9XjX", "name": "Josh",    "description": "Deep, authoritative male"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "name": "Bella",   "description": "Soft, reassuring female"},
    {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam",    "description": "Neutral, clear male"},
    {"id": "AZnzlk1XvdvUeBnXmlld", "name": "Domi",    "description": "Strong, confident female"},
]


@router.get("/api/tts/voices")
async def list_tts_voices():
    """Return available ElevenLabs voice options."""
    return {"voices": _ELEVENLABS_VOICES}


@router.post("/api/tts")
async def text_to_speech(request: Request):
    """Proxy ElevenLabs TTS. Returns audio/mpeg stream."""
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ElevenLabs API key not configured. Set ELEVENLABS_API_KEY.")
    body = await request.json()
    text = (body.get("text") or "").strip()
    voice_id = body.get("voice_id") or "21m00Tcm4TlvDq8ikWAM"
    if not text:
        raise HTTPException(status_code=400, detail="text is required.")
    # Truncate to avoid large bills on very long messages
    if len(text) > 1000:
        text = text[:997] + "…"
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {"stability": 0.55, "similarity_boost": 0.75},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"ElevenLabs error: {r.status_code}")
        return Response(content=r.content, media_type="audio/mpeg",
                        headers={"Cache-Control": "no-store"})
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="TTS request timed out.")
