import os, re, time
import pandas as pd
import streamlit as st
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sqlalchemy import create_engine, text
import google.generativeai as genai

# ---------- Halaman ----------
st.set_page_config(page_title="Chatbot Aset & Gangguan PLN", page_icon="⚡", layout="centered")
st.title("⚡ Chatbot Analitik — Aset & Gangguan")
st.caption("Tanya data gangguan dengan bahasa biasa · PLN x Hacktiv8")

# ---------- TODO 1: LLM (key dari Secrets) ----------
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
except Exception:
    GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
MODEL_NAME = "gemini-2.5-flash"
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel(MODEL_NAME)

# ---------- Database: Supabase (dari Secrets DB_URL) ----------
@st.cache_resource
def get_engine():
    return create_engine(st.secrets["DB_URL"], pool_pre_ping=True)
engine = get_engine()

# ---------- Skema ----------
SCHEMA_STR = """assets(asset_id, nama, jenis, lokasi)
outages(outage_id, asset_id, mulai, selesai, durasi_menit, penyebab)

Relasi: outages.asset_id -> assets.asset_id
Catatan: 'mulai' & 'selesai' bertipe timestamp. durasi_menit = lama gangguan (menit)."""

CONTOH = """
Contoh:
Q: Jumlah gangguan per aset
A: SELECT a.nama, COUNT(*) AS jumlah_gangguan
   FROM outages o JOIN assets a ON a.asset_id = o.asset_id
   GROUP BY a.nama ORDER BY jumlah_gangguan DESC
Q: Jumlah gangguan pada bulan Mei 2026
A: SELECT a.nama, COUNT(*) AS jumlah_gangguan
   FROM outages o JOIN assets a ON a.asset_id = o.asset_id
   WHERE o.mulai >= '2026-05-01' AND o.mulai < '2026-06-01'
   GROUP BY a.nama ORDER BY jumlah_gangguan DESC
"""

# ---------- TODO 2: build_prompt ----------
def build_prompt(question):
    aturan = ("Anda ahli SQL untuk PostgreSQL. Gunakan HANYA tabel/kolom pada skema. "
              "Buat SATU query SELECT (tanpa perintah lain). Gunakan JOIN bila perlu. "
              "Kolom 'mulai' & 'selesai' bertipe timestamp; untuk menyaring per bulan gunakan "
              "rentang tanggal, contoh Juni 2026: o.mulai >= '2026-06-01' AND o.mulai < '2026-07-01'.")
    keluaran = "Balas HANYA query SQL, tanpa penjelasan, tanpa pembungkus kode."
    return f"{aturan}\n\nSkema:\n{SCHEMA_STR}\n{CONTOH}\n{keluaran}\n\nPertanyaan: {question}"

# ---------- TODO 3: generate_sql (+ backoff 429/503) ----------
def _bersihkan_sql(teks):
    teks = teks.strip()
    m = re.search(r"```(?:sql)?\s*(.+?)```", teks, re.S)
    if m: teks = m.group(1).strip()
    m = re.search(r"(select\b.+)", teks, re.I | re.S)
    if m: teks = m.group(1)
    return teks.rstrip(";").strip()

def generate_sql(question, maks_coba=4):
    prompt = build_prompt(question)
    for i in range(maks_coba):
        try:
            resp = model.generate_content(
                prompt, generation_config={"temperature": 0, "max_output_tokens": 2048})
            return _bersihkan_sql((resp.text or "").strip())
        except Exception as e:
            if any(k in str(e).lower() for k in ["429", "503", "resource"]) and i < maks_coba - 1:
                time.sleep(5 * (i + 1)); continue
            raise

# ---------- TODO 4: validate_sql ----------
FORBIDDEN = ["drop", "delete", "update", "insert", "alter", "truncate", "create", "grant"]
def validate_sql(sql):
    teks = sql.strip().rstrip(";").strip(); low = teks.lower()
    if not low or not low.startswith("select"): return False
    for k in FORBIDDEN:
        if re.search(rf"\b{k}\b", low): return False
    if ";" in teks: return False
    return True

# ---------- run_sql ----------
def run_sql(sql):
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn)

# ---------- TODO 5: chart ----------
def buat_chart(df):
    if df is None or df.shape[1] != 2 or not pd.api.types.is_numeric_dtype(df[df.columns[-1]]):
        return None
    x, y = df.columns[0], df.columns[-1]
    fig, ax = plt.subplots(figsize=(7, 4))
    if any(k in str(x).lower() for k in ["waktu", "bulan", "periode", "tanggal", "mulai"]):
        ax.plot(df[x].astype(str), df[y], marker="o", color="#0E8388")
    else:
        ax.bar(df[x].astype(str), df[y], color="#0E8388")
    ax.set_xlabel(str(x)); ax.set_ylabel(str(y)); ax.set_title(f"{y} per {x}")
    plt.xticks(rotation=30, ha="right"); plt.tight_layout()
    return fig

# ---------- TODO 6: pipeline ----------
def jawab(question):
    sql = generate_sql(question)
    if not validate_sql(sql):
        sql = generate_sql(question)
        if not validate_sql(sql):
            return {"ok": False, "pesan": "Maaf, query aman tidak dapat disusun. Coba perjelas pertanyaan."}
    try:
        df = run_sql(sql)
    except Exception as e:
        return {"ok": False, "pesan": f"Gagal menjalankan query.\n\nSQL: {sql}\n\nError: {e}"}
    return {"ok": True, "sql": sql, "df": df}

# ---------- UI Chat ----------
if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.subheader("Contoh pertanyaan")
    for ex in ["Berapa jumlah gangguan per aset pada bulan Juni 2026?",
               "Berapa rata-rata durasi pemulihan per jenis aset?",
               "Apa penyebab gangguan yang paling sering terjadi?"]:
        st.caption("• " + ex)
    if st.button("🗑️ Bersihkan chat"):
        st.session_state.messages = []; st.rerun()

for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        if m["role"] == "user":
            st.markdown(m["content"])
        else:
            p = m["payload"]
            if not p["ok"]:
                st.error(p["pesan"])
            else:
                with st.expander("🔎 SQL"):
                    st.code(p["sql"], language="sql")
                st.dataframe(p["df"], use_container_width=True)
                fig = buat_chart(p["df"])
                if fig: st.pyplot(fig)

q = st.chat_input("Tanya tentang aset & gangguan…")
if q:
    st.session_state.messages.append({"role": "user", "content": q})
    with st.spinner("Memproses…"):
        payload = jawab(q)
    st.session_state.messages.append({"role": "assistant", "payload": payload})
    st.rerun()
