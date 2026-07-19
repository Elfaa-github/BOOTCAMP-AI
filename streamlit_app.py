import base64
import hashlib
import io
import os

import requests
import streamlit as st
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

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

st.set_page_config(page_title="Post-Disaster Damage Assessment", layout="wide")
st.title("Post-Disaster Damage Assessment & Triage Dashboard")
st.caption(f"Backend: `{API_URL}`")

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
st.header("1️⃣ Upload Pre & Post Disaster Images")
col1, col2 = st.columns(2)
with col1:
    pre_file = st.file_uploader("Pre-disaster image", type=["png", "jpg", "jpeg"], key="pre")
with col2:
    post_file = st.file_uploader("Post-disaster image", type=["png", "jpg", "jpeg"], key="post")

# Tombol data contoh — pakai pasangan citra Palu yang sudah ada di repo,
# supaya orang bisa langsung mencoba tanpa harus mencari citra pre/post sendiri.
if os.path.exists(SAMPLE_PRE) and os.path.exists(SAMPLE_POST):
    if st.button("🧪 Coba dengan data contoh (Palu Tsunami)"):
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
    st.header("2️⃣ Original Image")
    c1, c2 = st.columns(2)
    c1.image(pre_bytes, caption=f"Pre-disaster ({pre_size[0]}x{pre_size[1]})", use_container_width=True)
    c2.image(post_bytes, caption=f"Post-disaster ({post_size[0]}x{post_size[1]})", use_container_width=True)

    if st.button("Run Full Analysis"):
        with st.status("Menjalankan analisis...", expanded=True) as status:
            status.update(label="Mengirim gambar ke backend (segmentasi + difference map + RAG + Gemini)...")
            try:
                resp = requests.post(
                    f"{API_URL}/analyze",
                    files={"pre_image": ("pre.png", pre_bytes, "image/png"),
                           "post_image": ("post.png", post_bytes, "image/png")},
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
        st.header("3️⃣ Segmentation Result")
        s1, s2 = st.columns(2)
        if "mask_pre" in result:
            s1.image(b64_to_bytes(result["mask_pre"]), caption="Predicted Mask — Pre-disaster",
                      use_container_width=True)
        if "mask_post" in result:
            s2.image(b64_to_bytes(result["mask_post"]), caption="Predicted Mask — Post-disaster",
                      use_container_width=True)

        # 4) Difference Map
        st.header("4️⃣ Difference Map")
        if "difference_map" in result:
            st.image(b64_to_bytes(result["difference_map"]), caption="Difference Map", width=500)
        st.caption("🟩 Hijau = bangunan utuh · 🟥 Merah = bangunan rusak/hilang · Hitam = bukan bangunan")

        # 5) Statistics
        st.header("5️⃣ Statistics")
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
        st.header("6️⃣ Priority Score")
        emoji = PRIORITY_COLOR.get(stats["priority"], "⚪")
        st.markdown(f"### {emoji} Priority: **{stats['priority']}**")

        # 7) Decision Support
        st.header("7️⃣ Decision Support")
        st.markdown(f"**Recommended Action:** {stats['recommended_action']}")
        st.markdown(f"**Evacuation Radius:** {stats['evacuation_radius_km']} km")
        st.markdown("**Required Logistics:**")
        for item in stats["required_logistics"]:
            st.markdown(f"- {item}")

        # 8) AI Report
        st.header("8️⃣ AI Report (RAG-Grounded)")
        st.markdown(stats.get("ai_report", "Report not available."))
        with st.expander("Lihat sumber SOP yang digunakan (RAG retrieval)"):
            for src in stats.get("rag_sources_used", []):
                st.markdown(f"- {src}...")

        # 9) Download PDF
        st.header("9️⃣ Download PDF")

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
        st.download_button("Download Report as PDF", data=pdf_buf,
                            file_name="damage_report.pdf", mime="application/pdf")
else:
    st.info("Upload gambar pre-disaster dan post-disaster untuk memulai analisis, "
            "atau klik tombol data contoh di atas.")
