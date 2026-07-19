"""
Gemini Service — satu-satunya titik komunikasi ke Google Generative Language API.

Menggantikan pemanggilan `requests` yang sebelumnya tersebar di rag_service.py dengan satu
class terpusat, supaya:
- Model embedding & model generation bisa dikonfigurasi lewat environment variable
  (tidak hardcode "text-embedding-004" / "gemini-1.5-flash" yang ternyata sudah tidak
  tersedia di beberapa API key — lihat hasil `find_embedding_model()` di notebook yang
  justru menemukan "gemini-embedding-001").
- Retry + backoff untuk rate limit (429) konsisten di satu tempat.
- rag_service.py, main.py, atau script indexing offline bisa memakai ulang class ini.

Environment variables yang dibaca:
- GOOGLE_API_KEY           : wajib, API key Gemini.
- GEMINI_EMBED_MODEL       : opsional, default "gemini-embedding-001".
- GEMINI_GENERATION_MODEL  : opsional, default "gemini-3.5-flash".
"""
import os
import time
import logging
from typing import List, Tuple

import numpy as np
import requests

logger = logging.getLogger("gemini_service")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Fallback list dicoba berurutan kalau model utama tidak tersedia / error.
# Urutan ini mengikuti model yang terbukti ada di daftar `models.list` pada notebook.
EMBEDDING_MODEL_FALLBACKS = ["gemini-embedding-001", "text-embedding-004", "embedding-001"]
GENERATION_MODEL_FALLBACKS = ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.0-flash"]


class GeminiServiceError(RuntimeError):
    """Dilempar kalau semua model fallback gagal, atau API key tidak valid."""


class GeminiService:
    def __init__(self, api_key: str = None, embed_model: str = None, generation_model: str = None,
                 timeout: int = 15, max_retries: int = 3):
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY", "")
        if not self.api_key:
            raise GeminiServiceError("GOOGLE_API_KEY tidak ditemukan (env var atau parameter).")

        self.embed_model = embed_model or os.environ.get("GEMINI_EMBED_MODEL", EMBEDDING_MODEL_FALLBACKS[0])
        self.generation_model = generation_model or os.environ.get(
            "GEMINI_GENERATION_MODEL", GENERATION_MODEL_FALLBACKS[0]
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self._embedding_dim = None  # diisi otomatis setelah embed pertama berhasil

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------
    def _embed_one(self, text: str, task_type: str, model: str) -> List[float]:
        url = f"{BASE_URL}/models/{model}:embedContent?key={self.api_key}"
        payload = {"content": {"parts": [{"text": text}]}, "taskType": task_type}

        last_error = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json()["embedding"]["values"]
                if resp.status_code == 429:
                    wait = 3 * (attempt + 1)
                    logger.warning("Rate limited saat embed (model=%s). Tunggu %ss...", model, wait)
                    time.sleep(wait)
                    continue
                last_error = GeminiServiceError(f"Embed API error {resp.status_code}: {resp.text[:200]}")
                break
            except requests.exceptions.Timeout:
                last_error = GeminiServiceError(f"Timeout saat embed dengan model {model}")
        raise last_error or GeminiServiceError("Embedding gagal tanpa alasan spesifik.")

    def embed(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT",
              allow_fallback: bool = True) -> np.ndarray:
        """Embed satu atau banyak teks. task_type: RETRIEVAL_DOCUMENT (untuk chunk SOP)
        atau RETRIEVAL_QUERY (untuk query pencarian).

        allow_fallback=False WAJIB dipakai saat query ke index FAISS yang sudah jadi:
        index dibangun dengan satu model embedding tertentu, dan vektor dari model lain
        hidup di ruang berbeda — fallback diam-diam akan membuat hasil retrieval ngawur
        tanpa error. Fallback hanya aman saat MEMBANGUN index dari nol."""
        if allow_fallback:
            models_to_try = [self.embed_model] + [m for m in EMBEDDING_MODEL_FALLBACKS if m != self.embed_model]
        else:
            models_to_try = [self.embed_model]

        for model in models_to_try:
            try:
                vectors = [self._embed_one(t, task_type, model) for t in texts]
                if model != self.embed_model:
                    logger.info("Beralih ke embedding model fallback: %s", model)
                    self.embed_model = model  # cache pilihan yang berhasil untuk panggilan berikutnya
                arr = np.array(vectors, dtype="float32")
                self._embedding_dim = arr.shape[1]
                return arr
            except GeminiServiceError as e:
                logger.warning("Embedding model %s gagal (%s), coba fallback berikutnya...", model, e)
                continue

        raise GeminiServiceError(
            f"Semua model embedding gagal dicoba: {models_to_try}. Cek API key / kuota."
        )

    # ------------------------------------------------------------------
    # Generation (report dari Gemini)
    # ------------------------------------------------------------------
    def generate_content(self, prompt: str, model: str = None, timeout: int = 60) -> str:
        models_to_try = [model or self.generation_model] + [
            m for m in GENERATION_MODEL_FALLBACKS if m != (model or self.generation_model)
        ]

        last_error = None
        for m in models_to_try:
            url = f"{BASE_URL}/models/{m}:generateContent?key={self.api_key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            try:
                resp = requests.post(url, json=payload, timeout=timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if m != self.generation_model:
                        logger.info("Beralih ke generation model fallback: %s", m)
                        self.generation_model = m
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                last_error = GeminiServiceError(
                    f"Generation API error {resp.status_code} (model={m}): {resp.text[:300]}"
                )
            except requests.exceptions.Timeout:
                last_error = GeminiServiceError(f"Timeout (>{timeout}s) saat generate dengan model {m}")
                break  # timeout tidak perlu dicoba ulang ke model lain, biasanya masalah jaringan

        raise last_error or GeminiServiceError("Generation gagal tanpa alasan spesifik.")

    # ------------------------------------------------------------------
    # Util
    # ------------------------------------------------------------------
    def list_available_models(self) -> Tuple[List[str], List[str]]:
        """Mengembalikan (embedding_models, generation_models) yang benar-benar aktif
        untuk API key ini — berguna untuk debugging kalau model default gagal."""
        url = f"{BASE_URL}/models?key={self.api_key}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        models = resp.json().get("models", [])
        embed_models = [m["name"].replace("models/", "") for m in models
                        if "embedContent" in m.get("supportedGenerationMethods", [])]
        gen_models = [m["name"].replace("models/", "") for m in models
                      if "generateContent" in m.get("supportedGenerationMethods", [])]
        return embed_models, gen_models
