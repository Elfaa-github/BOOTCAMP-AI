# Post-Disaster Damage Assessment & Triage System

Sistem end-to-end untuk menilai dan memprioritaskan kerusakan bangunan pasca-bencana dari
citra satelit pre/post-bencana, menggunakan **SegFormer** untuk segmentasi bangunan,
**bi-temporal difference map** untuk deteksi kerusakan, dan **RAG (Retrieval-Augmented
Generation) berbasis FAISS + Gemini** untuk menghasilkan laporan naratif yang berpijak pada
SOP resmi (BNPB).

Dataset: xBD subset (Palu Tsunami) · Model: SegFormer (mit-b0), dibandingkan dengan U-Net &
DeepLabV3+ sebagai baseline.

---

## Arsitektur

```
Frontend (Streamlit)  ──HTTP──►  Backend (FastAPI, main.py)
                                        │
                        ┌───────────────┴───────────────┐
                        ▼                                ▼
              Model Service                        RAG Service
        (services/model_service.py)          (services/rag_service.py)
        - Inferensi SegFormer                 - Retrieval FAISS
        - Difference map                      - Generasi laporan Gemini
        - Damage stats                        (lewat services/gemini_service.py)
```

`main.py` hanya berperan sebagai orchestrator — tidak berisi logika model atau RAG secara
langsung, supaya masing-masing bagian bisa dikembangkan/diuji secara independen.

---

## Struktur Folder

```
bootcamp/
│
├── main.py                  # Backend FastAPI (orchestrator)
├── streamlit_app.py          # Frontend dashboard
├── requirements.txt
├── .env.example               # Salin jadi .env, isi API key kamu
│
├── models/
│   ├── segformer_best.pt      # tidak ikut di-commit (lihat bagian "File Besar" di bawah)
│   └── config.json
│
├── rag/
│   ├── sop_faiss.index        # tidak ikut di-commit — generate ulang, lihat di bawah
│   ├── sop_chunks.json
│   └── bnpb_sop.pdf           # opsional, dokumen sumber SOP
│
├── services/
│   ├── model_service.py       # Inferensi SegFormer + difference map
│   ├── rag_service.py         # Retrieval FAISS + panggilan Gemini
│   ├── gemini_service.py      # Client REST terpusat ke Gemini API
│   └── pdf_service.py         # Ingestion PDF SOP + generate PDF laporan
│
├── uploads/                   # Tempat sementara file upload (runtime, di-ignore git)
├── outputs/                   # Hasil generate (runtime, di-ignore git)
├── reports/                   # Contoh hasil: triage_results.json, triage_report.md, dst.
└── assets/                    # Opsional (logo, gambar dokumentasi, dll.)
```

---

## Instalasi

1. Clone repo ini, lalu masuk ke foldernya:
   ```bash
   git clone https://github.com/USERNAME/NAMA-REPO.git
   cd NAMA-REPO
   ```

2. Buat virtual environment (opsional tapi disarankan):
   ```bash
   python -m venv venv
   venv\Scripts\activate        # Windows
   source venv/bin/activate     # Mac/Linux
   ```

3. Install dependency:
   ```bash
   pip install -r requirements.txt
   ```

4. Salin `.env.example` menjadi `.env`, lalu isi API key Gemini kamu:
   ```bash
   copy .env.example .env        # Windows
   cp .env.example .env          # Mac/Linux
   ```
   Buka `.env`, isi:
   ```
   GOOGLE_API_KEY=isi_api_key_kamu_di_sini
   ```
   Ambil API key gratis di https://aistudio.google.com/apikey

   > Catatan: `.env` **tidak otomatis dibaca oleh PowerShell**. Cara paling gampang di
   > Windows: set manual di terminal sebelum menjalankan backend —
   > ```powershell
   > $env:GOOGLE_API_KEY = "isi_api_key_kamu"
   > ```
   > (berlaku untuk sesi terminal itu saja). Untuk permanen, gunakan
   > `setx GOOGLE_API_KEY "isi_api_key_kamu"` lalu buka terminal baru.

---

## ▶Menjalankan

Butuh **dua terminal berjalan bersamaan**:

**Terminal 1 — Backend (FastAPI):**
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
Tunggu sampai muncul `Uvicorn running on http://0.0.0.0:8000` dan `RagService siap.`
(kalau masih warning `GOOGLE_API_KEY tidak diset`, berarti env var belum kebaca di terminal ini).

**Terminal 2 — Frontend (Streamlit):**
```bash
streamlit run streamlit_app.py
```
Buka browser ke `http://localhost:8501`, upload gambar pre-disaster & post-disaster
(pastikan **dimensi keduanya sama persis**), lalu klik **Run Full Analysis**.

---

## Environment Variables

Lihat `.env.example` untuk daftar lengkap. Yang penting:

| Variabel | Wajib? | Default | Keterangan |
|---|---|---|---|
| `GOOGLE_API_KEY` | Ya | — | API key Gemini, untuk fitur RAG + laporan naratif |
| `MODEL_DIR` | Tidak | `models` | Folder checkpoint SegFormer |
| `CHECKPOINT_NAME` | Tidak | `segformer_best.pt` | Nama file checkpoint |
| `RAG_DIR` | Tidak | `rag` | Folder FAISS index & chunk SOP |
| `GSD_METERS_PER_PIXEL` | Tidak | `0.3` | Asumsi resolusi citra untuk estimasi luas area |
| `API_URL` | Tidak (frontend) | `http://localhost:8000` | Alamat backend, diisi ulang saat deployment |

Tanpa `GOOGLE_API_KEY`, aplikasi tetap bisa jalan (segmentasi, difference map, statistik,
decision support tetap berfungsi) — hanya bagian **AI Report (RAG-Grounded)** yang nonaktif.

---

## File Besar (Model & FAISS Index)

`models/*.pt` dan `rag/sop_faiss.index` **tidak ikut di-commit** ke repo ini (lihat
`.gitignore`) karena ukurannya besar dan melebihi batas wajar GitHub (limit keras 100MB/file).

Untuk mendapatkan file-file ini:
- **Model checkpoint** (`segformer_best.pt`): jalankan ulang notebook training
  (bagian "Model & Training Loop") — hasil checkpoint otomatis tersimpan.
- **FAISS index + chunk SOP** (`sop_faiss.index`, `sop_chunks.json`): jalankan
  `services/pdf_service.py` → fungsi `build_sop_index(pdf_path, output_dir, gemini_service)`
  dengan PDF SOP BNPB (atau dokumen SOP lain) sebagai input.

Kalau file-file ini memang perlu ikut di-commit (misal untuk kemudahan kolaborasi tim),
gunakan **Git LFS**:
```bash
git lfs install
git lfs track "*.pt"
git lfs track "rag/*.index"
git add .gitattributes
```

---

## Menguji Tanpa Data Asli

Kalau belum sempat mengunduh dataset xBD dari Kaggle, gunakan pasangan gambar sintetis
untuk uji alur teknis (upload → segmentasi → difference map → dashboard) — bukan untuk
menilai akurasi model. Pastikan ukuran pre & post sama persis sebelum upload.

---

## Ringkasan Fitur

- **EDA** — distribusi kelas kerusakan, imbalance pixel building vs background
- **Model comparison** — SegFormer vs U-Net vs DeepLabV3+
- **Evaluasi** — IoU, Dice, Precision, Recall, confusion matrix, qualitative grid
- **Explainable AI** — confidence/probability heatmap
- **Error analysis** — studi kasus kegagalan model
- **Bi-temporal difference map** — deteksi kerusakan antar waktu
- **Decision support** — priority bucket, recommended action, radius evakuasi, logistik
- **RAG asli** — PDF SOP → chunking → embedding → FAISS → retrieval → Gemini
- **Deployment modular** — Frontend (Streamlit) / Backend (FastAPI) / Model Service / RAG Service terpisah
