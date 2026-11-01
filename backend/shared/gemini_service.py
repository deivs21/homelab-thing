"""Google Gemini service wrapper for the Dual-Hub homelab.

Provides:
* Gemini 1.5 Flash client with runtime override to 1.5 Pro.
* Exponential backoff with jitter for HTTP 429 (waits 30-60s, up to 3 retries).
* Strict emoji sanitizer.
* Vector embeddings via text-embedding-004 (768 dims).
* Multi-modal ingestion: handwritten image OCR, bilingual (Latvian + English terms) audio transcription.
* Per-call API roundtrip latency telemetry in ms.
* Daily automated key/quota healthcheck.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "\U000024C2-\U0001F251"
    "\U0001F004-\U0001F0CF"
    "\U0000FE00-\U0000FE0F"
    "\U0001F3FB-\U0001F3FF"
    "]+",
    flags=re.UNICODE,
)

DEFAULT_MODEL = os.getenv("DEFAULT_AI_MODEL", "gemini-1.5-flash")
EMBEDDING_MODEL = "text-embedding-004"
EMBEDDING_DIM = 768


def sanitize_emojis(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    return _EMOJI_RE.sub("", text)


class GeminiService:
    def __init__(self, api_key: Optional[str] = None, default_model: Optional[str] = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            logger.warning("GEMINI_API_KEY not set; Gemini calls will fail.")
        self._client = genai.Client(api_key=self.api_key) if self.api_key else None
        self._default_model = default_model or DEFAULT_MODEL
        self._current_model = self._default_model
        self._last_latency_ms: float = 0.0
        self._max_retries = 3

    @property
    def current_model(self) -> str:
        return self._current_model

    def set_model(self, model: str) -> None:
        self._current_model = model
        logger.info("Gemini model overridden to %s", model)

    def reset_model(self) -> None:
        self._current_model = self._default_model

    @staticmethod
    def _is_rate_limit(exc: BaseException) -> bool:
        code = getattr(exc, "code", None)
        if code is None:
            code = getattr(exc, "status_code", None)
        if code == 429:
            return True
        msg = str(exc).lower()
        return ("429" in msg) or ("rate limit" in msg) or ("resource_exhausted" in msg) or ("quota" in msg and "exceed" in msg)

    async def _with_backoff(self, func, *args, **kwargs):
        last_exc = None
        for attempt in range(self._max_retries):
            start = time.perf_counter()
            try:
                result = await asyncio.to_thread(func, *args, **kwargs)
                latency = (time.perf_counter() - start) * 1000.0
                self._last_latency_ms = latency
                return result, latency
            except Exception as exc:
                last_exc = exc
                if (not self._is_rate_limit(exc)) or attempt >= self._max_retries - 1:
                    raise
                wait = random.uniform(30.0, 60.0)
                logger.warning(
                    "Gemini 429 rate limit (attempt %d/%d); backing off %.1fs",
                    attempt + 1, self._max_retries, wait,
                )
                await asyncio.sleep(wait)
        raise last_exc

    def _ensure_client(self):
        if self._client is None:
            key = self.api_key or os.getenv("GEMINI_API_KEY")
            if not key:
                raise RuntimeError("GEMINI_API_KEY is not configured")
            self.api_key = key
            self._client = genai.Client(api_key=key)
        return self._client
    async def generate_text(
        self,
        prompt: str,
        model: Optional[str] = None,
        system_instruction: Optional[str] = None,
        temperature: float = 0.3,
        max_output_tokens: int = 2048,
    ) -> Dict[str, Any]:
        prompt = sanitize_emojis(prompt) or ""
        sys_instr = sanitize_emojis(system_instruction)
        model = model or self._current_model
        client = self._ensure_client()

        def _call():
            config = types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                system_instruction=sys_instr,
            )
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
            return getattr(resp, "text", None) or ""

        raw, latency = await self._with_backoff(_call)
        text = sanitize_emojis(raw) or ""
        return {
            "text": text,
            "model": model,
            "latency_ms": round(latency, 2),
        }

    async def embed_text(self, text: str) -> Dict[str, Any]:
        text = sanitize_emojis(text) or ""
        client = self._ensure_client()

        def _call():
            resp = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=text,
            )
            embeddings = getattr(resp, "embeddings", None)
            if embeddings:
                vals = embeddings[0].values
            else:
                vals = resp.embedding.values if hasattr(resp, "embedding") else []
            return list(vals)

        vec, latency = await self._with_backoff(_call)
        if len(vec) != EMBEDDING_DIM:
            logger.warning("Embedding dim %d != %d", len(vec), EMBEDDING_DIM)
        return {
            "embedding": vec,
            "dimensions": len(vec),
            "model": EMBEDDING_MODEL,
            "latency_ms": round(latency, 2),
        }

    async def ocr_image(
        self,
        image_bytes: bytes,
        mime_type: str = "image/png",
        prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        client = self._ensure_client()
        if prompt is None:
            prompt = (
                "Extract all handwritten and printed text from this image. "
                "Preserve layout and line breaks. Return only the extracted text."
            )
        prompt = sanitize_emojis(prompt) or ""

        def _call():
            part_img = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
            part_txt = types.Part(text=prompt)
            resp = client.models.generate_content(
                model=self._current_model,
                contents=[part_img, part_txt],
            )
            return getattr(resp, "text", None) or ""

        raw, latency = await self._with_backoff(_call)
        text = sanitize_emojis(raw) or ""
        return {
            "text": text,
            "kind": "ocr",
            "model": self._current_model,
            "latency_ms": round(latency, 2),
        }

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        mime_type: str = "audio/wav",
        prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        client = self._ensure_client()
        if prompt is None:
            prompt = (
                "Transcribe this audio. The speech is primarily in Latvian, but may "
                "contain English technical terms. Preserve both languages accurately. "
                "Return only the transcription."
            )
        prompt = sanitize_emojis(prompt) or ""

        def _call():
            part_audio = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
            part_txt = types.Part(text=prompt)
            resp = client.models.generate_content(
                model=self._current_model,
                contents=[part_audio, part_txt],
            )
            return getattr(resp, "text", None) or ""

        raw, latency = await self._with_backoff(_call)
        text = sanitize_emojis(raw) or ""
        return {
            "text": text,
            "kind": "transcription",
            "model": self._current_model,
            "latency_ms": round(latency, 2),
        }

    async def healthcheck(self) -> Dict[str, Any]:
        client = self._ensure_client()
        start = time.perf_counter()
        try:
            def _call():
                resp = client.models.generate_content(
                    model=self._current_model,
                    contents="Reply with the single word: OK",
                )
                return getattr(resp, "text", None) or ""

            raw, latency = await self._with_backoff(_call)
            ok = "OK" in (raw or "").upper()
            return {
                "status": "healthy" if ok else "degraded",
                "model": self._current_model,
                "api_key_present": bool(self.api_key),
                "roundtrip_ms": round(latency, 2),
                "message": sanitize_emojis(raw),
            }
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000.0
            logger.exception("Gemini healthcheck failed")
            return {
                "status": "unhealthy",
                "model": self._current_model,
                "api_key_present": bool(self.api_key),
                "roundtrip_ms": round(latency, 2),
                "error": sanitize_emojis(str(exc)),
            }

    @property
    def last_latency_ms(self) -> float:
        return self._last_latency_ms


_singleton = None


def get_gemini_service() -> "GeminiService":
    global _singleton
    if _singleton is None:
        _singleton = GeminiService()
    return _singleton


__all__ = [
    "GeminiService",
    "get_gemini_service",
    "sanitize_emojis",
    "DEFAULT_MODEL",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
]

