"""Build FAISS index dari SEMUA PDF SOP di rag/sop_docs/ (bukan cuma satu file).

Jalankan sekali secara offline setiap kali koleksi SOP berubah:
    python build_index.py

Butuh GOOGLE_API_KEY (di .env atau env var). Hasil: rag/sop_faiss.index + rag/sop_chunks.json,
langsung dipakai RagService di main.py tanpa perubahan apa pun.
"""
import glob
import json
import os

import faiss
from dotenv import load_dotenv

from services.gemini_service import GeminiService
from services.pdf_service import clean_pdf_text, extract_raw_text, split_text

load_dotenv()
RAG_DIR = os.environ.get("RAG_DIR", "rag")

pdfs = sorted(glob.glob(os.path.join(RAG_DIR, "sop_docs", "*.pdf")))
if not pdfs:
    raise SystemExit(f"Tidak ada PDF di {RAG_DIR}/sop_docs — taruh dokumen SOP di sana dulu.")

all_chunks = []
for path in pdfs:
    text = clean_pdf_text(extract_raw_text(path))
    chunks = split_text(text)
    print(f"{os.path.basename(path)}: {len(chunks)} chunk")
    all_chunks.extend(chunks)

print(f"\nTotal {len(all_chunks)} chunk dari {len(pdfs)} dokumen. Embedding via Gemini "
      "(satu panggilan API per chunk — kalau kena rate limit 429, retry otomatis)...")

gemini = GeminiService()
embeddings = gemini.embed(all_chunks, task_type="RETRIEVAL_DOCUMENT")

faiss.normalize_L2(embeddings)
index = faiss.IndexFlatIP(embeddings.shape[1])
index.add(embeddings)

faiss.write_index(index, os.path.join(RAG_DIR, "sop_faiss.index"))
with open(os.path.join(RAG_DIR, "sop_chunks.json"), "w", encoding="utf-8") as f:
    json.dump(all_chunks, f, ensure_ascii=False, indent=2)

print(f"Selesai: {len(all_chunks)} chunk, embedding dim {embeddings.shape[1]}, "
      f"model {gemini.embed_model} -> {RAG_DIR}/sop_faiss.index")
print("Catatan: set GEMINI_EMBED_MODEL ke model yang sama saat menjalankan backend, "
      "karena query wajib memakai model embedding yang identik dengan index.")
