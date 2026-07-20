import base64
import hashlib
import io
import json
import os
import re
from datetime import datetime

import requests
import streamlit as st
from PIL import Image
from report_pdf import build_pdf

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
# Koordinat & GSD sample Palu diambil dari label xBD asli (polygon lng_lat) —
# bukan geocoding, jadi titik bangunan rusaknya akurat secara geografis.
SAMPLES = {
    "palu": {"pre": "sample_palu_pre.png", "post": "sample_palu_post.png",
             "coords": (-0.820928, 119.879618), "gsd": 0.495},
    "aceh": {"pre": "pre_resized.png", "post": "post_resized.png",
             "coords": None, "gsd": None},
}

@st.cache_resource(show_spinner=False)
def ensure_backend():
    """Jalankan backend FastAPI sebagai subprocess bila belum ada yang listen di port 8000.
    Dipakai di deployment satu-proses (Streamlit Community Cloud); secara lokal, kalau
    uvicorn sudah dijalankan manual, fungsi ini tidak melakukan apa-apa."""
    import socket
    import subprocess
    import sys
    try:
        with socket.create_connection(("localhost", 8000), timeout=0.4):
            return "sudah jalan"
    except OSError:
        pass
    env = dict(os.environ)
    try:
        # Streamlit Cloud menaruh secrets di st.secrets, bukan env var — teruskan ke subprocess.
        if "GOOGLE_API_KEY" in st.secrets:
            env["GOOGLE_API_KEY"] = st.secrets["GOOGLE_API_KEY"]
    except Exception:
        pass
    subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app",
                      "--host", "0.0.0.0", "--port", "8000"], env=env)
    return "dispawn"


if "localhost" in API_URL:
    ensure_backend()

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


@st.cache_data(ttl=3600, show_spinner=False)
def geocode_place(place: str):
    """Nama tempat -> (lat, lon) via Nominatim/OpenStreetMap — gratis, tanpa API key.
    Di-cache 1 jam supaya tidak menembak API di setiap rerun Streamlit."""
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": place, "format": "json", "limit": 1, "countrycodes": "id"},
                         headers={"User-Agent": "post-disaster-damage-assessment/1.0"}, timeout=6)
        data = r.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def parse_xbd_label(label_bytes: bytes, img_w: int, img_h: int):
    """Hitung (lat, lon, gsd) pusat citra dari file label xBD — label menyimpan polygon
    dalam ruang piksel (xy) dan geografis (lng_lat), jadi pemetaan liniernya bisa
    diturunkan untuk tile xBD MANA PUN, bukan cuma sample bawaan."""
    try:
        import math
        d = json.loads(label_bytes)
        fx = d.get("features", {}).get("xy", [])
        fl = d.get("features", {}).get("lng_lat", [])
        if not fx or not fl:
            return None
        xs, ys, lons, lats = [], [], [], []
        for f_xy, f_ll in zip(fx, fl):
            for a, b in re.findall(r"([\-0-9.]+) ([\-0-9.]+)", f_xy.get("wkt", "")):
                xs.append(float(a)); ys.append(float(b))
            for a, b in re.findall(r"([\-0-9.]+) ([\-0-9.]+)", f_ll.get("wkt", "")):
                lons.append(float(a)); lats.append(float(b))
        if len(set(xs)) < 2 or len(set(ys)) < 2:
            return None
        sx = (max(lons) - min(lons)) / (max(xs) - min(xs))
        sy = (max(lats) - min(lats)) / (max(ys) - min(ys))
        lon_c = min(lons) + (img_w / 2 - min(xs)) * sx
        lat_c = max(lats) - (img_h / 2 - min(ys)) * sy
        gsd = round((sx * 111320 * abs(math.cos(math.radians(lat_c))) + sy * 111320) / 2, 3)
        return lat_c, lon_c, gsd
    except Exception:
        return None


def exif_gps(img_bytes: bytes):
    """Baca koordinat GPS dari metadata EXIF (umumnya ada di foto kamera/drone;
    citra satelit PNG seperti xBD tidak punya — fallback ke input manual)."""
    try:
        exif = Image.open(io.BytesIO(img_bytes))._getexif()
        gps = exif.get(34853) if exif else None  # 34853 = tag GPSInfo
        if not gps or 2 not in gps or 4 not in gps:
            return None

        def to_deg(vals, ref):
            deg = float(vals[0]) + float(vals[1]) / 60 + float(vals[2]) / 3600
            return -deg if ref in ("S", "W") else deg

        return to_deg(gps[2], gps.get(1, "N")), to_deg(gps[4], gps.get(3, "E"))
    except Exception:
        return None


# 1) Upload
st.header("Upload Pre & Post Disaster Images")
col1, col2 = st.columns(2)
with col1:
    pre_file = st.file_uploader("Pre-disaster image", type=["png", "jpg", "jpeg"], key="pre")
with col2:
    post_file = st.file_uploader("Post-disaster image", type=["png", "jpg", "jpeg"], key="post")

# Tombol data contoh — supaya orang bisa langsung mencoba tanpa mencari citra sendiri.
def load_sample(key: str):
    meta = SAMPLES[key]
    with open(meta["pre"], "rb") as f:
        st.session_state["pre_bytes"] = f.read()
    with open(meta["post"], "rb") as f:
        st.session_state["post_bytes"] = f.read()
    st.session_state["sample_meta"] = meta


sb1, sb2 = st.columns(2)
if os.path.exists(SAMPLES["palu"]["pre"]) and os.path.exists(SAMPLES["palu"]["post"]):
    if sb1.button("🛰️ Data contoh: Palu Tsunami 2018 (xBD asli — sesuai domain training)",
                  use_container_width=True):
        load_sample("palu")
if os.path.exists(SAMPLES["aceh"]["pre"]) and os.path.exists(SAMPLES["aceh"]["post"]):
    if sb2.button("🧪 Data contoh: Aceh 2004 (foto internet — hanya demo alur)",
                  use_container_width=True):
        load_sample("aceh")

# Upload user menimpa data contoh
if pre_file and post_file:
    st.session_state["pre_bytes"] = pre_file.getvalue()
    st.session_state["post_bytes"] = post_file.getvalue()
    st.session_state.pop("sample_meta", None)  # metadata sample tidak berlaku untuk upload sendiri

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
    # Hanya Tsunami yang didukung model saat ini (xBD Palu) — aktif tanpa perlu dipilih.
    # Jenis lain ditampilkan sebagai tombol NON-AKTIF (bukan opsi yang bisa dipilih lalu diblokir)
    # supaya jelas sejak awal bahwa opsi itu belum bisa diklik, bukan baru ketahuan setelah dipilih.
    disaster_type = "Tsunami"
    coming_soon = False
    cc2.markdown(
        '<div style="padding-top:0.4rem;">'
        '<span style="background:#f0fdf4;color:#16a34a;border:1px solid #16a34a40;'
        'padding:6px 12px;border-radius:9999px;font-size:0.85rem;font-weight:600;">'
        '&#9679; Tsunami</span></div>', unsafe_allow_html=True)
    cc2.caption("Jenis bencana lain:")
    soon_cols = cc2.columns(4)
    for col, label in zip(soon_cols, ["Gempa bumi", "Banjir", "Longsor", "Erupsi"]):
        col.button(label, disabled=True, key=f"soon_{label}", use_container_width=True,
                  help="Coming soon — model belum dilatih untuk jenis bencana ini")
    # Koordinat diisi otomatis, urut akurasi: metadata sample xBD > EXIF gambar > geocode nama lokasi.
    # Field manual tetap ada sebagai override.
    label_file = st.file_uploader("Opsional: file label xBD (.json) — georeference otomatis untuk tile xBD mana pun",
                                  type=["json"], key="xbdlabel")
    sample_meta = st.session_state.get("sample_meta") or {}
    gps, gps_src, label_gsd = None, None, None
    if label_file is not None:
        parsed = parse_xbd_label(label_file.getvalue(), pre_size[0], pre_size[1])
        if parsed:
            gps, gps_src = (parsed[0], parsed[1]), "label xBD yang di-upload (akurat)"
            label_gsd = parsed[2]
    if not gps and sample_meta.get("coords"):
        gps, gps_src = sample_meta["coords"], "georeference label xBD (akurat)"
    if not gps:
        gps, gps_src = exif_gps(post_bytes) or exif_gps(pre_bytes), "metadata EXIF gambar"
    if not gps and location:
        gps, gps_src = geocode_place(location), (f'nama lokasi "{location}" via OpenStreetMap — '
                                                 "perkiraan pusat wilayah, titik bangunan akurat "
                                                 "secara RELATIF saja")
    center_lat = cc3.text_input("Latitude pusat citra",
                                value=(f"{gps[0]:.6f}" if gps else ""), placeholder="-0.8917")
    center_lon = cc4.text_input("Longitude pusat citra",
                                value=(f"{gps[1]:.6f}" if gps else ""), placeholder="119.8707")
    if gps:
        st.caption(f"📍 Koordinat terisi otomatis dari {gps_src} — silakan koreksi bila perlu.")
    else:
        st.caption("Ketik nama lokasi di kolom pertama — koordinat terisi otomatis. "
                   "(Atau isi lat/lon manual.)")

    if st.button("Run Full Analysis", type="primary", use_container_width=True, disabled=coming_soon):
        form_data = {}
        if location:
            form_data["location"] = location
        if disaster_type and not coming_soon:
            form_data["disaster_type"] = disaster_type
        try:
            form_data["center_lat"] = float(center_lat)
            form_data["center_lon"] = float(center_lon)
        except ValueError:
            pass  # lat/lon kosong/tidak valid — analisis tetap jalan tanpa koordinat
        if label_gsd:
            form_data["gsd"] = label_gsd
        elif sample_meta.get("gsd"):
            form_data["gsd"] = sample_meta["gsd"]

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
            s1.image(b64_to_bytes(result["mask_pre"]), caption="Segmentasi Bangunan — Pre-disaster (kuning = bangunan terdeteksi)",
                      use_container_width=True)
        if "mask_post" in result:
            s2.image(b64_to_bytes(result["mask_post"]), caption="Segmentasi Bangunan — Post-disaster (kuning = bangunan terdeteksi)",
                      use_container_width=True)

        # 4) Difference Map
        st.header("Difference Map")
        if "difference_map" in result:
            st.image(b64_to_bytes(result["difference_map"]), caption="Difference Map", width=500)
        st.caption("🟩 Hijau = bangunan utuh · 🟥 Merah = bangunan rusak/hilang — di-overlay langsung di atas citra post-disaster")

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
        conf_main = (f"{stats['confidence_pre']*100:.1f}%"
                     if stats.get("confidence_pre") is not None else confidence_pct)
        conf_delta = None
        if stats.get("confidence") is not None and stats.get("confidence_pre") is not None:
            conf_delta = f"{(stats['confidence'] - stats['confidence_pre']) * 100:.1f}% pada citra post"
        colG.metric("Keyakinan Segmentasi", conf_main, delta=conf_delta,
                    help="Rata-rata probabilitas softmax (TTA 3-arah, piksel interior bangunan) pada citra "
                         "PRE — mengukur kemampuan model mengenali bangunan di wilayah ini. Penurunan pada "
                         "citra post BUKAN penurunan kualitas model, melainkan indikasi bangunan hancur "
                         "(puing tidak lagi dikenali sebagai bangunan).")
        st.caption("Akurasi model pada set validasi (xBD Palu): **mIoU 79.9% · Dice 82.9% · Building IoU 70.7%**")

        # 6) Priority Score — badge ala shadcn
        st.header("Priority Score")
        PRIORITY_STYLE = {"GREEN": ("#16a34a", "#f0fdf4"), "YELLOW": ("#ca8a04", "#fefce8"),
                          "ORANGE": ("#ea580c", "#fff7ed"), "RED": ("#dc2626", "#fef2f2")}
        pc, pbg = PRIORITY_STYLE.get(stats["priority"], ("#71717a", "#fafafa"))
        st.markdown(f'<span style="background:{pbg};color:{pc};border:1px solid {pc}40;'
                    f'padding:8px 18px;border-radius:9999px;font-weight:600;font-size:1.1rem;">'
                    f'&#9679;&nbsp; {stats["priority"]} &nbsp;&middot;&nbsp; '
                    f'{stats["damage_percentage"]}% kerusakan</span>', unsafe_allow_html=True)

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
            import pydeck as pdk
            st.header("Titik Bangunan Rusak (untuk tim lapangan)")
            df_loc = pd.DataFrame(locs)
            has_coords = "lat" in df_loc.columns
            if has_coords:
                df_map = df_loc.copy()
                df_map["rank"] = range(1, len(df_map) + 1)
                # radius titik proporsional akar luas kerusakan (meter), minimal 6 m biar terlihat
                df_map["radius"] = (df_map["area_m2"] ** 0.5).clip(lower=6)
                c_lat, c_lon = float(df_map["lat"].mean()), float(df_map["lon"].mean())
                layers = []
                evac_km = stats.get("evacuation_radius_km") or 0
                if evac_km:
                    layers.append(pdk.Layer(
                        "ScatterplotLayer",
                        data=[{"lat": c_lat, "lon": c_lon}],
                        get_position="[lon, lat]", get_radius=evac_km * 1000,
                        get_fill_color=[249, 115, 22, 22], get_line_color=[234, 88, 12, 170],
                        stroked=True, line_width_min_pixels=2,
                    ))
                layers.append(pdk.Layer(
                    "ScatterplotLayer",
                    data=df_map,
                    get_position="[lon, lat]", get_radius="radius",
                    get_fill_color=[220, 38, 38, 185], get_line_color=[127, 29, 29],
                    stroked=True, line_width_min_pixels=1, pickable=True,
                ))
                st.pydeck_chart(pdk.Deck(
                    map_style=None,
                    initial_view_state=pdk.ViewState(latitude=c_lat, longitude=c_lon, zoom=15),
                    layers=layers,
                    tooltip={"html": "<b>Bangunan rusak #{rank}</b><br/>Luas ~{area_m2} m2<br/>{lat}, {lon}",
                             "style": {"backgroundColor": "#18181b", "color": "#fafafa",
                                       "fontSize": "12px", "borderRadius": "6px"}},
                ))
                st.caption("🔴 Titik = bangunan rusak (besar titik ∝ luas kerusakan, hover untuk detail) · "
                           "🟠 Lingkaran = radius evakuasi dari pusat area terdampak")
            st.caption("Tabel diurutkan dari kerusakan terluas — prioritas kunjungan tim lapangan.")
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
        with st.container(border=True):
            st.markdown(stats.get("ai_report", "Report not available."))
        with st.expander("Lihat sumber SOP yang digunakan (RAG retrieval)"):
            st.caption("Potongan dokumen SOP BNPB/BPBD yang paling relevan hasil pencarian FAISS — "
                       "dasar (grounding) laporan AI di atas.")
            for i, src in enumerate(stats.get("rag_sources_used", []), 1):
                if isinstance(src, dict):
                    st.markdown(f"**Sumber {i}** · similarity `{src.get('score', '?')}`")
                    st.markdown(f"> {src.get('text', '')}")
                else:
                    st.markdown(f"- {src}...")

        # 9) Download PDF
        st.header("Download PDF")

        pdf_images = [("Pre-disaster", pre_bytes), ("Post-disaster", post_bytes)]
        for key, lab in [("mask_pre", "Segmentasi (Pre)"), ("mask_post", "Segmentasi (Post)"),
                         ("difference_map", "Difference Map")]:
            if key in result:
                pdf_images.append((lab, b64_to_bytes(result[key])))
        pdf_buf = build_pdf(stats, pdf_images, confidence_pct)
        d1, d2 = st.columns(2)
        d1.download_button("Download Report as PDF", data=pdf_buf,
                           file_name="damage_report.pdf", mime="application/pdf")
        d2.download_button("Download hasil lengkap (JSON)",
                           data=json.dumps(stats, indent=2, ensure_ascii=False),
                           file_name="analysis_result.json", mime="application/json")
else:
    st.info("Upload gambar pre-disaster dan post-disaster untuk memulai analisis, "
            "atau klik tombol data contoh di atas.")
