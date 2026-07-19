import json
import os

import numpy as np
import faiss

from services.gemini_service import GeminiService


class RagService:
    def __init__(self, faiss_index_path, chunks_path, google_api_key=None, gemini_service: GeminiService = None):
        self.index = faiss.read_index(faiss_index_path)
        with open(chunks_path, encoding="utf-8") as f:
            self.chunks = json.load(f)
        # boleh pakai instance GeminiService yang sudah ada (dishare dari main.py),
        # atau bikin baru kalau dipanggil berdiri sendiri.
        self.gemini = gemini_service or GeminiService(api_key=google_api_key)
        # Kalau ada metadata index (ditulis build_index.py / notebook), pakai model embedding
        # yang MEMBANGUN index itu — query wajib satu model dengan index, apa pun default env.
        meta_path = os.path.join(os.path.dirname(faiss_index_path) or ".", "sop_index_meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("embed_model"):
                self.gemini.embed_model = meta["embed_model"]

    def retrieve(self, query, top_k=3):
        # allow_fallback=False: query WAJIB pakai model embedding yang sama dengan
        # yang membangun index — model lain menghasilkan vektor di ruang berbeda
        # dan retrieval-nya jadi ngawur tanpa error.
        q_emb = self.gemini.embed([query], task_type="RETRIEVAL_QUERY", allow_fallback=False)
        if q_emb.shape[1] != self.index.d:
            raise ValueError(
                f"Dimensi embedding query ({q_emb.shape[1]}) != dimensi index FAISS ({self.index.d}). "
                f"Index dibangun dengan model embedding berbeda dari '{self.gemini.embed_model}' — "
                "rebuild index atau set GEMINI_EMBED_MODEL ke model yang dipakai saat build."
            )
        faiss.normalize_L2(q_emb)
        scores, idxs = self.index.search(q_emb, top_k)
        return [(self.chunks[i], float(scores[0][j])) for j, i in enumerate(idxs[0]) if i != -1]

    def generate_report(self, stats: dict, top_k=3):
        # Query dibangun dari seluruh konteks kejadian, bukan cuma priority + damage%,
        # supaya retrieval-nya berbeda antar kasus (tidak degenerate ke 4 query yang sama).
        logistics = ", ".join(stats.get("required_logistics", []))
        event = ""
        if stats.get("disaster_type") or stats.get("location"):
            event = (f"Bencana {stats.get('disaster_type', '')} "
                     f"di {stats.get('location', 'lokasi tidak diketahui')}. ").replace("  ", " ")
        query = (f"{event}Prioritas {stats['priority']}, kerusakan bangunan {stats['damage_percentage']}%, "
                 f"{stats.get('buildings_damaged', '?')} dari {stats.get('buildings_total', '?')} bangunan rusak, "
                 f"estimasi luas terdampak {stats.get('area_m2', '?')} m2, "
                 f"radius evakuasi {stats.get('evacuation_radius_km', '?')} km. "
                 f"Prosedur evakuasi, alokasi sumber daya, dan kebutuhan logistik: {logistics}.")
        retrieved = self.retrieve(query, top_k=top_k)
        context = "\n".join(f"- {chunk}" for chunk, _score in retrieved)

        prompt = f"""
Anda adalah analis triase bencana AI. Buat laporan penilaian kerusakan singkat dalam Bahasa Indonesia.

ATURAN:
1. Semua angka HARUS berasal dari JSON di bawah. Jangan mengarang angka.
2. Gunakan KONTEKS SOP di bawah untuk mendasari rekomendasi Anda.
3. Sebutkan recommended_action, evacuation_radius_km, dan required_logistics secara eksplisit.

KONTEKS SOP (hasil retrieval FAISS):
{context}

DATA:
{json.dumps(stats, indent=2)}
"""
        report_text = self.gemini.generate_content(prompt, timeout=60)
        return report_text, retrieved
