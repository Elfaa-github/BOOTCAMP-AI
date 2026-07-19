"""
Backend orchestrator (FastAPI) — thin layer that wires Frontend <-> Model Service <-> RAG Service.
Run with: uvicorn main:app --host 0.0.0.0 --port 8000

Path model & RAG disesuaikan dengan struktur folder:
    models/segformer_best.pt
    rag/sop_faiss.index
    rag/sop_chunks.json
    services/model_service.py, rag_service.py, gemini_service.py, pdf_service.py
"""
import base64
import logging
import os
import time

import cv2
import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse

from services.model_service import ModelService
from services.rag_service import RagService

# Baca file .env di root project (kalau ada) supaya GOOGLE_API_KEY, dsb.
# tidak perlu di-set manual lewat $env: di setiap sesi terminal baru.
load_dotenv()

# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

# ----------------------------------------------------------------------
# Konfigurasi (semua lewat environment variable, tidak ada yang hardcode)
# ----------------------------------------------------------------------
MODEL_DIR = os.environ.get("MODEL_DIR", "models")
RAG_DIR = os.environ.get("RAG_DIR", "rag")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
CHECKPOINT_NAME = os.environ.get("CHECKPOINT_NAME", "segformer_best.pt")
# Asumsi ground sample distance (meter/piksel) untuk estimasi luas area — sesuaikan dengan
# resolusi citra satelit/drone yang sebenarnya dipakai.
GSD_METERS_PER_PIXEL = float(os.environ.get("GSD_METERS_PER_PIXEL", "0.3"))

app = FastAPI(title="Post-Disaster Damage Assessment API", version="1.1.0")

# ----------------------------------------------------------------------
# Load services sekali saat startup (bukan per-request)
# ----------------------------------------------------------------------
logger.info("Memuat ModelService dari %s/%s ...", MODEL_DIR, CHECKPOINT_NAME)
try:
    model_service = ModelService(checkpoint_path=f"{MODEL_DIR}/{CHECKPOINT_NAME}")
    logger.info("ModelService siap.")
except Exception:
    logger.exception("Gagal memuat ModelService — API tidak akan bisa melayani /analyze.")
    raise

rag_service = None
if GOOGLE_API_KEY:
    try:
        rag_service = RagService(
            faiss_index_path=f"{RAG_DIR}/sop_faiss.index",
            chunks_path=f"{RAG_DIR}/sop_chunks.json",
            google_api_key=GOOGLE_API_KEY,
        )
        logger.info("RagService siap.")
    except Exception:
        logger.exception("Gagal memuat RagService — RAG akan dinonaktifkan, /analyze tetap jalan tanpa laporan AI.")
        rag_service = None
else:
    logger.warning("GOOGLE_API_KEY tidak diset — RAG service dinonaktifkan.")


# ----------------------------------------------------------------------
# Helper: encoding gambar hasil jadi base64 PNG untuk dikirim ke frontend
# ----------------------------------------------------------------------
def _encode_png_base64(arr: np.ndarray) -> str:
    """Encode numpy image array (grayscale atau BGR) menjadi base64 PNG string."""
    ok, buf = cv2.imencode(".png", arr)
    if not ok:
        raise ValueError("Gagal encode array gambar ke format PNG.")
    return base64.b64encode(buf.tobytes()).decode("utf-8")


def _mask_to_visual(mask: np.ndarray) -> np.ndarray:
    """Ubah mask biner (0/1) menjadi gambar hitam-putih yang enak dilihat."""
    return (mask * 255).astype(np.uint8)


def _diff_to_visual(diff: np.ndarray) -> np.ndarray:
    """Ubah difference map (0=background, 1=utuh, 2=rusak) menjadi gambar BGR berwarna."""
    vis = np.zeros((*diff.shape, 3), dtype=np.uint8)
    vis[diff == 1] = (0, 255, 0)   # hijau = bangunan utuh
    vis[diff == 2] = (0, 0, 255)   # merah = bangunan rusak (OpenCV pakai urutan BGR)
    return vis


def _count_buildings(binary_mask: np.ndarray, min_area_px: int = 20) -> int:
    """Hitung jumlah komponen terhubung sebagai proxy jumlah bangunan pada mask biner.
    Ini estimasi kasar (connected components), bukan instance segmentation sungguhan.
    Blob lebih kecil dari min_area_px diabaikan supaya noise beberapa piksel
    tidak ikut terhitung sebagai bangunan."""
    n_labels, _, comp_stats, _ = cv2.connectedComponentsWithStats(binary_mask.astype(np.uint8))
    # baris 0 = background; kolom cv2.CC_STAT_AREA = luas blob dalam piksel
    return int(np.sum(comp_stats[1:, cv2.CC_STAT_AREA] >= min_area_px))


# ----------------------------------------------------------------------
# Endpoint utama
# ----------------------------------------------------------------------
@app.post("/analyze")
async def analyze(pre_image: UploadFile = File(...), post_image: UploadFile = File(...)):
    request_start = time.time()
    logger.info("Request diterima: pre=%s, post=%s", pre_image.filename, post_image.filename)

    # --- 1) Baca & decode gambar, dengan validasi ---
    try:
        pre_np = np.frombuffer(await pre_image.read(), np.uint8)
        post_np = np.frombuffer(await post_image.read(), np.uint8)
        pre_bgr = cv2.imdecode(pre_np, cv2.IMREAD_COLOR)
        post_bgr = cv2.imdecode(post_np, cv2.IMREAD_COLOR)
    except Exception as e:
        logger.error("Gagal membaca file upload: %s", e)
        return JSONResponse(status_code=400, content={"error": f"Gagal membaca file upload: {e}"})

    if pre_bgr is None or post_bgr is None:
        logger.error("Salah satu gambar tidak valid / corrupt / bukan format gambar yang didukung.")
        return JSONResponse(
            status_code=400,
            content={"error": "Salah satu gambar tidak valid atau gagal dibaca. "
                              "Pastikan format PNG/JPG dan file tidak corrupt."},
        )

    MAX_SIDE = 8192  # batas dimensi supaya gambar raksasa tidak bikin backend kehabisan memori
    if max(pre_bgr.shape[:2]) > MAX_SIDE or max(post_bgr.shape[:2]) > MAX_SIDE:
        return JSONResponse(
            status_code=400,
            content={"error": f"Dimensi gambar melebihi batas {MAX_SIDE}px. Perkecil gambar terlebih dahulu."},
        )

    if pre_bgr.shape[:2] != post_bgr.shape[:2]:
        logger.warning("Ukuran pre (%s) dan post (%s) berbeda.", pre_bgr.shape[:2], post_bgr.shape[:2])
        return JSONResponse(
            status_code=400,
            content={"error": f"Ukuran gambar pre {pre_bgr.shape[:2]} dan post {post_bgr.shape[:2]} berbeda. "
                              "Gunakan pasangan citra pre/post dengan dimensi yang sama."},
        )

    # --- 2) Model Service: segmentasi + difference map + damage stats ---
    try:
        t0 = time.time()
        mask_pre, mask_post, diff, stats = model_service.analyze(pre_bgr, post_bgr)
        model_time = round(time.time() - t0, 2)
        logger.info("Model inference selesai dalam %.2fs", model_time)
    except Exception as e:
        logger.exception("Model inference gagal")
        return JSONResponse(status_code=500, content={"error": f"Model inference gagal: {e}"})

    # --- 3) Enrich stats: estimasi jumlah bangunan & luas area ---
    try:
        n_total_buildings = _count_buildings(mask_pre)
        n_damaged_buildings = _count_buildings((diff == 2).astype(np.uint8))
        n_safe_buildings = max(n_total_buildings - n_damaged_buildings, 0)
        area_m2 = round(stats.get("total_building_pixels", 0) * (GSD_METERS_PER_PIXEL ** 2), 1)
        stats.update({
            "buildings_total": n_total_buildings,
            "buildings_damaged": n_damaged_buildings,
            "buildings_safe": n_safe_buildings,
            "area_m2": area_m2,
            "gsd_meters_per_pixel": GSD_METERS_PER_PIXEL,
        })
    except Exception as e:
        logger.warning("Gagal menghitung estimasi jumlah bangunan/area (non-fatal): %s", e)

    # --- 4) RAG Service: retrieval + Gemini report generation ---
    rag_time = None
    if rag_service:
        try:
            t0 = time.time()
            report_text, retrieved = rag_service.generate_report(stats)
            rag_time = round(time.time() - t0, 2)
            stats["ai_report"] = report_text
            stats["rag_sources_used"] = [chunk[:120] for chunk, _score in retrieved]
            logger.info("RAG + Gemini report selesai dalam %.2fs", rag_time)
        except Exception as e:
            logger.exception("RAG/Gemini report gagal (non-fatal, analisis CV tetap dikembalikan)")
            stats["ai_report"] = f"Gagal membuat laporan AI: {e}"
            stats["rag_sources_used"] = []
    else:
        stats["ai_report"] = "GOOGLE_API_KEY not set — RAG service disabled."
        stats["rag_sources_used"] = []

    total_time = round(time.time() - request_start, 2)
    stats["inference_time"] = {
        "model_seconds": model_time,
        "rag_seconds": rag_time,
        "total_seconds": total_time,
    }
    stats["confidence_note"] = (
        "confidence = rata-rata probabilitas softmax model pada piksel citra post-disaster "
        "yang diprediksi sebagai 'building' (bukan angka tetap)."
    )
    logger.info("Request selesai dalam %.2fs (model=%.2fs, rag=%s)", total_time, model_time, rag_time)

    # --- 5) Encode visual outputs sebagai base64 PNG agar bisa dirender langsung di frontend ---
    try:
        payload = {
            "stats": stats,
            "mask_pre": _encode_png_base64(_mask_to_visual(mask_pre)),
            "mask_post": _encode_png_base64(_mask_to_visual(mask_post)),
            "difference_map": _encode_png_base64(_diff_to_visual(diff)),
        }
    except Exception as e:
        logger.exception("Gagal encode gambar hasil ke base64")
        # stats tetap dikembalikan walau gambar gagal di-encode, supaya frontend tidak kehilangan semua info
        return JSONResponse(
            status_code=500,
            content={"error": f"Gagal encode gambar hasil: {e}", "stats": stats},
        )

    return JSONResponse(payload)


# ----------------------------------------------------------------------
# Endpoint pendukung: health, version, model-info
# ----------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "rag_enabled": rag_service is not None}


@app.get("/version")
async def version():
    return {"version": app.version, "service": "post-disaster-damage-assessment-api"}


@app.get("/model-info")
async def model_info():
    return {
        "model": "SegFormer-B0 (fine-tuned, binary building segmentation)",
        "checkpoint": f"{MODEL_DIR}/{CHECKPOINT_NAME}",
        "rag_enabled": rag_service is not None,
        "gsd_meters_per_pixel": GSD_METERS_PER_PIXEL,
    }
