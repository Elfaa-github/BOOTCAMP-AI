import base64
import hashlib
import io
import json
import os

import requests
import streamlit as st
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

# WAJIB jadi command Streamlit pertama — bahkan akses st.secrets di bawah pun dihitung command.
st.set_page_config(page_title="Post-Disaster Damage Assessment", layout="wide")

# Styling ala shadcn/ui (palet zinc, kartu ber-border, radius 8px, font Inter) — murni CSS,
# tanpa dependensi tambahan. Warna dasar diatur di .streamlit/config.toml.
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, [class*="css"], .stMarkdown, button, input, textarea {
    font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif !important;
}

/* Judul & header lebih rapat, ala dashboard */
h1 { font-size: 1.6rem !important; font-weight: 700 !important; letter-spacing: -0.02em; }
h2 { font-size: 1.15rem !important; font-weight: 600 !important; letter-spacing: -0.01em;
     border-top: 1px solid #e4e4e7; padding-top: 1.2rem !important; margin-top: 0.6rem !important; }
h3 { font-size: 1rem !important; font-weight: 600 !important; }

/* Metric -> kartu shadcn */
[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid #e4e4e7;
    border-radius: 8px;
    padding: 14px 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
[data-testid="stMetricLabel"] { color: #71717a; font-size: 0.8rem !important; }
[data-testid="stMetricValue"] { font-weight: 700; letter-spacing: -0.02em; }

/* Tombol: radius-md, gaya solid/outline */
.stButton > button, .stDownloadButton > button {
    border-radius: 6px !important;
    font-weight: 500 !important;
    border: 1px solid #e4e4e7 !important;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    transition: background 0.15s ease;
}
.stButton > button[kind="primary"] {
    background: #18181b !important; color: #fafafa !important; border: none !important;
}
.stButton > button[kind="primary"]:hover { background: #3f3f46 !important; }

/* Uploader & input */
[data-testid="stFileUploader"] section {
    border: 1px dashed #d4d4d8 !important; border-radius: 8px !important; background: #fafafa;
}
.stTextInput input, .stSelectbox > div > div {
    border-radius: 6px !important;
}

/* Blok SITREP & expander */
.stCode, pre { border: 1px solid #e4e4e7 !important; border-radius: 8px !important; }
[data-testid="stExpander"] {
    border: 1px solid #e4e4e7 !important; border-radius: 8px !important; box-shadow: none !important;
}

/* Dataframe */
[data-testid="stDataFrame"] { border: 1px solid #e4e4e7; border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

# Konfigurasi API_URL — env var > st.secrets > default localhost
def get_api_url() -> str:
    env_url = os.environ.get("API_URL")
    if env_url:
        return env_url
    try:
        return st.secrets["API_URL"]
    except Exception:
        return "http://localhost:8000"  # fallback untuk pengembangan lokal


API_URL = get_api_url()
PRIORITY_COLOR = {"GREEN": "🟢", "YELLOW": "🟡", "ORANGE": "🟠", "RED": "🔴"}
SAMPLE_PRE, SAMPLE_POST = "pre_resized.png", "post_resized.png"

st.title("Post-Disaster Damage Assessment & Triage Dashboard")


def backend_status() -> str:
    try:
        r = requests.get(f"{API_URL}/health", timeout=2)
        if r.status_code == 200:
            rag_on = r.json().get("rag_enabled")
            return f"🟢 online — RAG {'aktif' if rag_on else 'nonaktif'}"
    except requests.exceptions.RequestException:
        pass
    return "🔴 offline — jalankan `uvicorn main:app --port 8000`"


st.caption(f"Backend: `{API_URL}` · {backend_status()}")

# Helper validasi gambar
def validate_image_pair(pre_bytes: bytes, post_bytes: bytes):
    """Return (is_valid, error_message, pre_size, post_size)."""
    try:
        pre_img = Image.open(io.BytesIO(pre_bytes))
        pre_img.verify()
        pre_img = Image.open(io.BytesIO(pre_bytes))  # re-open setelah verify()
    except Exception:
        return False, "Gambar pre-disaster tidak valid atau corrupt.", None, None

    try:
        post_img = Image.open(io.BytesIO(post_bytes))
        post_img.verify()
        post_img = Image.open(io.BytesIO(post_bytes))
    except Exception:
        return False, "Gambar post-disaster tidak valid atau corrupt.", None, None

    if pre_img.size[0] == 0 or pre_img.size[1] == 0 or post_img.size[0] == 0 or post_img.size[1] == 0:
        return False, "Salah satu gambar kosong (ukuran 0).", None, None

    if pre_img.size != post_img.size:
        return False, (f"Ukuran gambar pre {pre_img.size} dan post {post_img.size} berbeda. "
                       "Gunakan pasangan citra pre/post dengan dimensi yang sama."), None, None

    return True, None, pre_img.size, post_img.size


def b64_to_bytes(b64_str: str) -> bytes:
    return base64.b64decode(b64_str)


def input_digest(pre: bytes, post: bytes) -> str:
    return hashlib.md5(pre + post).hexdigest()


# 1) Upload
st.header("Upload Pre & Post Disaster Images")
col1, col2 = st.columns(2)
with col1:
    pre_file = st.file_uploader("Pre-disaster image", type=["png", "jpg", "jpeg"], key="pre")
with col2:
    post_file = st.file_uploader("Post-disaster image", type=["png", "jpg", "jpeg"], key="post")

# Tombol data contoh — pakai pasangan citra Palu yang sudah ada di repo,
# supaya orang bisa langsung mencoba tanpa harus mencari citra pre/post sendiri.
if os.path.exists(SAMPLE_PRE) and os.path.exists(SAMPLE_POST):
    if st.button("🧪 Coba dengan data contoh (Aceh 2004 — demo alur, di luar domain training)"):
        with open(SAMPLE_PRE, "rb") as f:
            st.session_state["pre_bytes"] = f.read()
        with open(SAMPLE_POST, "rb") as f:
            st.session_state["post_bytes"] = f.read()

# Upload user menimpa data contoh
if pre_file and post_file:
    st.session_state["pre_bytes"] = pre_file.getvalue()
    st.session_state["post_bytes"] = post_file.getvalue()

pre_bytes = st.session_state.get("pre_bytes")
post_bytes = st.session_state.get("post_bytes")

if pre_bytes and post_bytes:
    # Validasi input sebelum dikirim ke backend
    is_valid, error_msg, pre_size, post_size = validate_image_pair(pre_bytes, post_bytes)
    if not is_valid:
        st.error(f"Validasi gagal: {error_msg}")
        st.stop()

    # Hasil analisis lama tidak berlaku lagi kalau gambarnya sudah diganti
    digest = input_digest(pre_bytes, post_bytes)
    if st.session_state.get("result_digest") != digest:
        st.session_state.pop("result", None)

    # 2) Original Image
    st.header("Original Image")
    c1, c2 = st.columns(2)
    c1.image(pre_bytes, caption=f"Pre-disaster ({pre_size[0]}x{pre_size[1]})", use_container_width=True)
    c2.image(post_bytes, caption=f"Post-disaster ({post_size[0]}x{post_size[1]})", use_container_width=True)

    # Konteks kejadian — membuat laporan RAG spesifik & mengaktifkan koordinat untuk tim SAR
    st.subheader("Konteks kejadian (opsional, disarankan)")
    cc1, cc2, cc3, cc4 = st.columns(4)
    location = cc1.text_input("Lokasi", placeholder="Palu, Sulawesi Tengah")
    disaster_type = cc2.selectbox("Jenis bencana",
                                  ["", "Tsunami", "Gempa bumi", "Banjir", "Tanah longsor", "Erupsi gunung api", "Lainnya"])
    center_lat = cc3.text_input("Latitude pusat citra", placeholder="-0.8917")
    center_lon = cc4.text_input("Longitude pusat citra", placeholder="119.8707")
    st.caption("Isi lat/lon pusat citra untuk mendapatkan koordinat GPS tiap bangunan rusak + GeoJSON untuk tim lapangan.")

    if st.button("Run Full Analysis", type="primary", use_container_width=True):
        form_data = {}
        if location:
            form_data["location"] = location
        if disaster_type:
            form_data["disaster_type"] = disaster_type
        try:
            form_data["center_lat"] = float(center_lat)
            form_data["center_lon"] = float(center_lon)
        except ValueError:
            pass  # lat/lon kosong/tidak valid — analisis tetap jalan tanpa koordinat

        with st.status("Menjalankan analisis...", expanded=True) as status:
            status.update(label="Mengirim gambar ke backend (segmentasi + difference map + RAG + Gemini)...")
            try:
                resp = requests.post(
                    f"{API_URL}/analyze",
                    files={"pre_image": ("pre.png", pre_bytes, "image/png"),
                           "post_image": ("post.png", post_bytes, "image/png")},
                    data=form_data,
                    timeout=180,
                )
            except requests.exceptions.Timeout:
                status.update(label="Timeout", state="error")
                st.error("Permintaan timeout. Backend mungkin sedang memproses gambar besar atau kuota "
                          "Gemini API sedang lambat merespons. Coba lagi dalam beberapa saat.")
                st.stop()
            except requests.exceptions.ConnectionError:
                status.update(label="🔌 Tidak bisa terhubung", state="error")
                st.error(f"Tidak bisa terhubung ke backend di `{API_URL}`. Pastikan FastAPI (`main.py`) "
                          "sedang berjalan, atau periksa konfigurasi environment variable `API_URL`.")
                st.stop()
            except requests.exceptions.RequestException as e:
                status.update(label="Error", state="error")
                st.error(f"Terjadi kesalahan saat menghubungi backend: {e}")
                st.stop()

            if resp.status_code != 200:
                status.update(label="API error", state="error")
                try:
                    err_detail = resp.json().get("error", resp.text)
                except Exception:
                    err_detail = resp.text
                st.error(f"API error {resp.status_code}: {err_detail}")
                st.stop()

            status.update(label="Analisis selesai", state="complete", expanded=False)
            # Simpan di session_state supaya hasil tidak hilang saat Streamlit rerun
            # (mis. setelah klik tombol Download PDF).
            st.session_state["result"] = resp.json()
            st.session_state["result_digest"] = digest

    result = st.session_state.get("result")
    if result:
        stats = result["stats"]
        timing = stats.get("inference_time", {})
        if timing:
            st.caption(
                f"Waktu proses — model: {timing.get('model_seconds', '?')}s | "
                f"RAG+Gemini: {timing.get('rag_seconds', '?')}s | "
                f"total: {timing.get('total_seconds', '?')}s"
            )

        # 3) Segmentation Result
        st.header("Segmentation Result")
        s1, s2 = st.columns(2)
        if "mask_pre" in result:
            s1.image(b64_to_bytes(result["mask_pre"]), caption="Predicted Mask — Pre-disaster",
                      use_container_width=True)
        if "mask_post" in result:
            s2.image(b64_to_bytes(result["mask_post"]), caption="Predicted Mask — Post-disaster",
                      use_container_width=True)

        # 4) Difference Map
        st.header("Difference Map")
        if "difference_map" in result:
            st.image(b64_to_bytes(result["difference_map"]), caption="Difference Map", width=500)
        st.caption("🟩 Hijau = bangunan utuh · 🟥 Merah = bangunan rusak/hilang · Hitam = bukan bangunan")

        # 5) Statistics
        st.header("Statistics")
        colA, colB, colC, colD = st.columns(4)
        colA.metric("Damage %", f"{stats['damage_percentage']}%")
        colB.metric("Bangunan Total", f"{stats.get('buildings_total', 'N/A')}")
        colC.metric("Bangunan Rusak", f"{stats.get('buildings_damaged', 'N/A')}")
        colD.metric("Bangunan Aman", f"{stats.get('buildings_safe', 'N/A')}")

        colE, colF, colG = st.columns(3)
        colE.metric("Estimasi Luas Area", f"{stats.get('area_m2', 'N/A'):,} m²"
                    if isinstance(stats.get("area_m2"), (int, float)) else "N/A")
        colF.metric("Damaged Pixels", f"{stats['damaged_pixels']:,}")
        confidence_pct = f"{stats['confidence']*100:.1f}%" if stats.get("confidence") is not None else "N/A"
        colG.metric("Model Confidence", confidence_pct,
                    help=stats.get("confidence_note", "Rata-rata probabilitas softmax model."))

        # 6) Priority Score
        st.header("Priority Score")
        emoji = PRIORITY_COLOR.get(stats["priority"], "⚪")
        st.markdown(f"### {emoji} Priority: **{stats['priority']}**")

        # 7) Decision Support
        st.header("Decision Support")
        st.markdown(f"**Recommended Action:** {stats['recommended_action']}")
        st.markdown(f"**Evacuation Radius:** {stats['evacuation_radius_km']} km")
        st.markdown("**Required Logistics:**")
        for item in stats["required_logistics"]:
            st.markdown(f"- {item}")

        # 7b) Untuk tim lapangan: titik bangunan rusak + peta + GeoJSON
        locs = stats.get("damaged_building_locations", [])
        if locs:
            import pandas as pd
            st.header("Titik Bangunan Rusak (untuk tim lapangan)")
            df_loc = pd.DataFrame(locs)
            has_coords = "lat" in df_loc.columns
            if has_coords:
                st.map(df_loc[["lat", "lon"]])
            st.caption("Diurutkan dari kerusakan terluas — prioritas kunjungan tim lapangan.")
            st.dataframe(df_loc, use_container_width=True, height=240)
            if has_coords:
                geojson = {
                    "type": "FeatureCollection",
                    "features": [{
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                        "properties": {"area_m2": r["area_m2"], "rank": i + 1},
                    } for i, r in enumerate(locs)],
                }
                st.download_button("Download GeoJSON (buka di QGIS / Google Earth / geojson.io)",
                                   data=json.dumps(geojson, indent=2),
                                   file_name="bangunan_rusak.geojson", mime="application/geo+json")
            else:
                st.info("Isi latitude/longitude pusat citra lalu jalankan ulang analisis untuk "
                        "mendapatkan koordinat GPS tiap titik + file GeoJSON.")

        # 7c) SITREP siap salin (WhatsApp/radio ke posko)
        st.header("SITREP — siap salin ke posko")
        _loc_line = stats.get("location", "(lokasi belum diisi)")
        _dis_line = stats.get("disaster_type", "(jenis bencana belum diisi)")
        _top = [l for l in locs if "lat" in l][:3]
        _top_lines = "".join(f"\n  {i+1}. {l['lat']}, {l['lon']} (~{l['area_m2']} m2)"
                             for i, l in enumerate(_top))
        sitrep = (
            f"SITREP PENILAIAN KERUSAKAN (otomatis)\n"
            f"Lokasi     : {_loc_line}\n"
            f"Bencana    : {_dis_line}\n"
            f"Prioritas  : {stats['priority']} ({stats['damage_percentage']}% kerusakan)\n"
            f"Bangunan   : {stats.get('buildings_total', '?')} total | "
            f"{stats.get('buildings_damaged', '?')} rusak | {stats.get('buildings_safe', '?')} aman\n"
            f"Luas rusak : ~{stats.get('area_m2', '?')} m2\n"
            f"Aksi       : {stats['recommended_action']}\n"
            f"Evakuasi   : radius {stats['evacuation_radius_km']} km\n"
            f"Logistik   : {', '.join(stats['required_logistics'])}"
            + (f"\nTitik prioritas:{_top_lines}" if _top_lines else "")
            + f"\nCatatan    : estimasi otomatis dari citra satelit, wajib verifikasi lapangan."
        )
        st.code(sitrep, language=None)

        # 8) AI Report
        st.header("AI Report (RAG-Grounded)")
        st.markdown(stats.get("ai_report", "Report not available."))
        with st.expander("Lihat sumber SOP yang digunakan (RAG retrieval)"):
            for src in stats.get("rag_sources_used", []):
                st.markdown(f"- {src}...")

        # 9) Download PDF
        st.header("Download PDF")

        def build_pdf_with_images():
            buf = io.BytesIO()
            c = rl_canvas.Canvas(buf, pagesize=A4)
            width, height = A4
            y = height - 50

            c.setFont("Helvetica-Bold", 14)
            c.drawString(50, y, "Laporan Penilaian Kerusakan Pasca-Bencana")
            y -= 25
            c.setFont("Helvetica", 10)
            for line in [
                f"Priority: {stats['priority']} ({stats['damage_percentage']}%)",
                f"Bangunan: {stats.get('buildings_total', 'N/A')} total, "
                f"{stats.get('buildings_damaged', 'N/A')} rusak, {stats.get('buildings_safe', 'N/A')} aman",
                f"Estimasi luas area: {stats.get('area_m2', 'N/A')} m²",
                f"Recommended Action: {stats['recommended_action']}",
                f"Evacuation Radius: {stats['evacuation_radius_km']} km",
                f"Confidence: {confidence_pct}",
            ]:
                c.drawString(50, y, line)
                y -= 14
            y -= 10

            # Gambar: pre, post, mask_pre, mask_post, difference map
            images_to_embed = [
                ("Pre-disaster", pre_bytes),
                ("Post-disaster", post_bytes),
                ("Predicted Mask (Pre)", b64_to_bytes(result["mask_pre"])) if "mask_pre" in result else None,
                ("Predicted Mask (Post)", b64_to_bytes(result["mask_post"])) if "mask_post" in result else None,
                ("Difference Map", b64_to_bytes(result["difference_map"])) if "difference_map" in result else None,
            ]
            images_to_embed = [im for im in images_to_embed if im is not None]

            img_w, img_h = 240, 180
            x_positions = [50, 50 + img_w + 20]
            x_idx = 0
            for label, img_bytes in images_to_embed:
                if y - img_h < 60:
                    c.showPage()
                    y = height - 50
                    x_idx = 0
                x = x_positions[x_idx % 2]
                try:
                    c.drawImage(ImageReader(io.BytesIO(img_bytes)), x, y - img_h,
                                width=img_w, height=img_h, preserveAspectRatio=True, anchor='c')
                    c.setFont("Helvetica", 8)
                    c.drawString(x, y - img_h - 12, label)
                except Exception:
                    c.drawString(x, y - 12, f"[Gagal render gambar: {label}]")
                x_idx += 1
                if x_idx % 2 == 0:
                    y -= img_h + 30

            # Teks laporan AI
            c.showPage()
            y = height - 50
            c.setFont("Helvetica-Bold", 11)
            c.drawString(50, y, "AI Report (RAG-Grounded):")
            y -= 18
            c.setFont("Helvetica", 9)
            for raw_line in stats.get("ai_report", "").split("\n"):
                wrapped_lines = [raw_line[i:i + 100] for i in range(0, max(len(raw_line), 1), 100)] or [""]
                for wrapped in wrapped_lines:
                    c.drawString(50, y, wrapped)
                    y -= 12
                    if y < 60:
                        c.showPage()
                        y = height - 50

            c.save()
            buf.seek(0)
            return buf

        pdf_buf = build_pdf_with_images()
        d1, d2 = st.columns(2)
        d1.download_button("Download Report as PDF", data=pdf_buf,
                           file_name="damage_report.pdf", mime="application/pdf")
        d2.download_button("Download hasil lengkap (JSON)",
                           data=json.dumps(stats, indent=2, ensure_ascii=False),
                           file_name="analysis_result.json", mime="application/json")
else:
    st.info("Upload gambar pre-disaster dan post-disaster untuk memulai analisis, "
            "atau klik tombol data contoh di atas.")
