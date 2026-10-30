"""
MediCore Clinic – Medical Practice Management System
Streamlit + SQLite + Gemini AI (report reading with a "doctor voice")

Roles: admin, doctor, nurse, receptionist, lab, pharmacist, billing, patient
Run:   streamlit run app.py
"""
import datetime as dt
import hashlib
import hmac
import io
import os
import secrets
import sqlite3
import wave

import pandas as pd
import streamlit as st

st.set_page_config(page_title="MediCore Clinic", page_icon="🩺", layout="wide")

DB_PATH = os.environ.get("DB_PATH", "clinic.db")
ROLES = ["admin", "doctor", "nurse", "receptionist", "lab", "pharmacist", "billing", "patient"]
APPT_STATUS = ["Requested", "Scheduled", "Checked-in", "In consultation", "Completed", "Cancelled", "No-show"]
SLOTS = [f"{h:02d}:{m:02d}" for h in range(9, 18) for m in (0, 15, 30, 45)]
LANGS = {"English": "en", "Hindi": "hi", "Kannada": "kn", "Tamil": "ta", "Telugu": "te",
         "Malayalam": "ml", "Marathi": "mr", "Bengali": "bn", "Spanish": "es", "French": "fr", "Arabic": "ar"}
GEMINI_VOICES = {"Charon (male, informative)": "Charon", "Kore (female, firm)": "Kore",
                 "Aoede (female, breezy)": "Aoede", "Fenrir (male, excitable)": "Fenrir",
                 "Sulafat (female, warm)": "Sulafat", "Puck (male, upbeat)": "Puck"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, full_name TEXT, role TEXT,
  password_hash TEXT, salt TEXT, specialization TEXT, email TEXT, phone TEXT, active INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS patients(id INTEGER PRIMARY KEY, code TEXT, user_id INTEGER, full_name TEXT, dob TEXT,
  gender TEXT, phone TEXT, email TEXT, blood_group TEXT, address TEXT, allergies TEXT, history TEXT,
  emergency_contact TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY, patient_id INTEGER, doctor_id INTEGER, appt_date TEXT,
  appt_time TEXT, reason TEXT, status TEXT, created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS vitals(id INTEGER PRIMARY KEY, patient_id INTEGER, appointment_id INTEGER, bp_sys INTEGER,
  bp_dia INTEGER, pulse INTEGER, temp_c REAL, spo2 INTEGER, weight_kg REAL, height_cm REAL, bmi REAL, notes TEXT,
  recorded_by TEXT, recorded_at TEXT);
CREATE TABLE IF NOT EXISTS visits(id INTEGER PRIMARY KEY, patient_id INTEGER, doctor_id INTEGER, appointment_id INTEGER,
  complaint TEXT, subjective TEXT, objective TEXT, assessment TEXT, plan TEXT, diagnosis TEXT, follow_up TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS prescriptions(id INTEGER PRIMARY KEY, visit_id INTEGER, patient_id INTEGER, doctor_id INTEGER,
  status TEXT DEFAULT 'Pending', created_at TEXT);
CREATE TABLE IF NOT EXISTS prescription_items(id INTEGER PRIMARY KEY, prescription_id INTEGER, medicine TEXT, dosage TEXT,
  frequency TEXT, duration TEXT, instructions TEXT, qty INTEGER);
CREATE TABLE IF NOT EXISTS lab_tests(id INTEGER PRIMARY KEY, name TEXT UNIQUE, price REAL, ref_range TEXT);
CREATE TABLE IF NOT EXISTS lab_orders(id INTEGER PRIMARY KEY, patient_id INTEGER, visit_id INTEGER, doctor_id INTEGER,
  test_id INTEGER, status TEXT DEFAULT 'Ordered', result TEXT, flag TEXT, result_file BLOB, file_name TEXT, file_mime TEXT,
  ordered_at TEXT, resulted_at TEXT, resulted_by TEXT);
CREATE TABLE IF NOT EXISTS inventory(id INTEGER PRIMARY KEY, medicine TEXT UNIQUE, category TEXT, stock INTEGER,
  reorder_level INTEGER, unit_price REAL, expiry TEXT);
CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY, number TEXT, patient_id INTEGER, visit_id INTEGER,
  created_at TEXT, discount REAL DEFAULT 0, tax_pct REAL DEFAULT 0, status TEXT DEFAULT 'Unpaid', notes TEXT);
CREATE TABLE IF NOT EXISTS invoice_items(id INTEGER PRIMARY KEY, invoice_id INTEGER, description TEXT, qty REAL, unit_price REAL);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, invoice_id INTEGER, amount REAL, method TEXT, paid_at TEXT, received_by TEXT);
CREATE TABLE IF NOT EXISTS ai_reports(id INTEGER PRIMARY KEY, patient_id INTEGER, requested_by TEXT, requester_role TEXT,
  source_name TEXT, language TEXT, doctor_name TEXT, explanation TEXT, spoken_script TEXT, audio BLOB, audio_mime TEXT,
  approved INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, user TEXT, action TEXT, detail TEXT, ts TEXT);
"""

DEFAULT_SETTINGS = {
    "clinic_name": "MediCore Clinic", "clinic_address": "12 Health Street, Bengaluru", "clinic_phone": "+91 80 0000 0000",
    "consult_fee": "500", "tax_pct": "0", "currency": "₹",
    "gemini_model": "gemini-2.5-flash", "gemini_tts_model": "gemini-2.5-flash-preview-tts",
}
DEFAULT_TESTS = [("Complete Blood Count (CBC)", 350, "Hb 12-16 g/dL; WBC 4-11 x10^3/uL"),
                 ("Fasting Blood Sugar", 100, "70-100 mg/dL"), ("HbA1c", 450, "< 5.7 %"),
                 ("Lipid Profile", 600, "LDL < 100 mg/dL"), ("Thyroid Profile (TSH)", 400, "0.4-4.0 mIU/L"),
                 ("Liver Function Test", 700, "ALT 7-56 U/L"), ("Kidney Function Test", 650, "Creatinine 0.6-1.3 mg/dL"),
                 ("Urine Routine", 150, "Normal"), ("Chest X-Ray", 500, "-"), ("ECG", 300, "-")]


# ───────────────────────────── database helpers ─────────────────────────────
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def run(sql, p=()):
    c = _conn()
    try:
        cur = c.execute(sql, p)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def rows(sql, p=()):
    c = _conn()
    try:
        return [dict(r) for r in c.execute(sql, p).fetchall()]
    finally:
        c.close()


def one(sql, p=()):
    r = rows(sql, p)
    return r[0] if r else None


def frame(sql, p=()):
    c = _conn()
    try:
        return pd.read_sql_query(sql, c, params=p)
    finally:
        c.close()


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def S(key, default=""):
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else DEFAULT_SETTINGS.get(key, default)


def set_setting(key, value):
    run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def audit(action, detail=""):
    u = st.session_state.get("user")
    run("INSERT INTO audit_log(user,action,detail,ts) VALUES(?,?,?,?)", (u["username"] if u else "-", action, detail, now()))


# ───────────────────────────── security ─────────────────────────────
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return h, salt


def verify_pw(pw, h, salt):
    return hmac.compare_digest(hash_pw(pw, salt)[0], h)


def create_user(username, full_name, role, pw, spec="", email="", phone=""):
    h, s = hash_pw(pw)
    return run("INSERT INTO users(username,full_name,role,password_hash,salt,specialization,email,phone,active,created_at)"
               " VALUES(?,?,?,?,?,?,?,?,1,?)", (username.strip().lower(), full_name, role, h, s, spec, email, phone, now()))


@st.cache_resource
def init_db():
    c = _conn()
    c.executescript(SCHEMA)
    c.commit()
    c.close()
    for k, v in DEFAULT_SETTINGS.items():
        run("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    for n, p, r in DEFAULT_TESTS:
        run("INSERT OR IGNORE INTO lab_tests(name,price,ref_range) VALUES(?,?,?)", (n, p, r))
    if not one("SELECT id FROM users LIMIT 1"):
        try:
            pw = st.secrets["ADMIN_PASSWORD"]
        except Exception:
            pw = os.environ.get("ADMIN_PASSWORD", "admin123")
        create_user("admin", "System Administrator", "admin", pw)
    return True


def get_gemini_key():
    k = st.session_state.get("gemini_key") or os.environ.get("GEMINI_API_KEY")
    if not k:
        try:
            k = st.secrets["GEMINI_API_KEY"]
        except Exception:
            k = None
    return k


# ───────────────────────────── shared helpers ─────────────────────────────
def me():
    return st.session_state["user"]


def age(dob):
    try:
        d = dt.date.fromisoformat(dob)
        t = dt.date.today()
        return t.year - d.year - ((t.month, t.day) < (d.month, d.day))
    except Exception:
        return None


def money(x):
    return f"{S('currency', '₹')}{float(x or 0):,.2f}"


def doctors():
    return rows("SELECT id, full_name, specialization FROM users WHERE role='doctor' AND active=1 ORDER BY full_name")


def my_patient_id():
    r = one("SELECT id FROM patients WHERE user_id=?", (me()["id"],))
    return r["id"] if r else None


def patient_picker(label="Patient", key="pp"):
    ps = rows("SELECT id, code, full_name, phone FROM patients ORDER BY full_name")
    if not ps:
        st.info("No patients registered yet.")
        return None
    opts = {p["id"]: f'{p["code"]} · {p["full_name"]} · {p["phone"] or ""}' for p in ps}
    return st.selectbox(label, list(opts), format_func=opts.get, key=key)


def get_invoice(patient_id, visit_id=None):
    r = one("SELECT id FROM invoices WHERE patient_id=? AND visit_id IS ?", (patient_id, visit_id))
    if r:
        return r["id"]
    i = run("INSERT INTO invoices(patient_id,visit_id,created_at,tax_pct,status) VALUES(?,?,?,?, 'Unpaid')",
            (patient_id, visit_id, now(), float(S("tax_pct", "0"))))
    run("UPDATE invoices SET number=? WHERE id=?", (f"INV-{i:06d}", i))
    return i


def add_item(inv, desc, qty, price):
    run("INSERT INTO invoice_items(invoice_id,description,qty,unit_price) VALUES(?,?,?,?)", (inv, desc, qty, price))


def inv_totals(inv_id):
    inv = one("SELECT * FROM invoices WHERE id=?", (inv_id,))
    sub = one("SELECT COALESCE(SUM(qty*unit_price),0) s FROM invoice_items WHERE invoice_id=?", (inv_id,))["s"]
    disc = inv["discount"] or 0
    taxable = max(sub - disc, 0)
    tax = taxable * (inv["tax_pct"] or 0) / 100
    total = taxable + tax
    paid = one("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE invoice_id=?", (inv_id,))["s"]
    return dict(sub=sub, disc=disc, tax=tax, total=total, paid=paid, bal=total - paid)


def refresh_invoice_status(inv_id):
    t = inv_totals(inv_id)
    status = "Paid" if t["total"] > 0 and t["bal"] <= 0.005 else ("Partial" if t["paid"] > 0 else "Unpaid")
    run("UPDATE invoices SET status=? WHERE id=?", (status, inv_id))


def invoice_html(inv_id):
    inv = one("SELECT i.*, p.full_name, p.code, p.phone FROM invoices i JOIN patients p ON p.id=i.patient_id WHERE i.id=?", (inv_id,))
    items = rows("SELECT * FROM invoice_items WHERE invoice_id=?", (inv_id,))
    t = inv_totals(inv_id)
    body = "".join(f"<tr><td>{i['description']}</td><td align=right>{i['qty']:g}</td><td align=right>{i['unit_price']:.2f}</td>"
                   f"<td align=right>{i['qty'] * i['unit_price']:.2f}</td></tr>" for i in items)
    return f"""<html><body style="font-family:Arial;max-width:720px;margin:auto">
<h2>{S('clinic_name')}</h2><p>{S('clinic_address')} · {S('clinic_phone')}</p><hr>
<h3>Invoice {inv['number']}</h3><p>Date: {inv['created_at']}<br>Patient: {inv['full_name']} ({inv['code']})</p>
<table width=100% border=1 cellspacing=0 cellpadding=6><tr><th align=left>Description</th><th>Qty</th><th>Rate</th><th>Amount</th></tr>{body}</table>
<p align=right>Subtotal: {t['sub']:.2f}<br>Discount: {t['disc']:.2f}<br>Tax: {t['tax']:.2f}<br><b>Total: {t['total']:.2f}</b><br>
Paid: {t['paid']:.2f}<br><b>Balance: {t['bal']:.2f}</b></p><p>Status: {inv['status']}</p></body></html>"""


# ───────────────────────────── Gemini: report reading + doctor voice ─────────────────────────────
def explain_report(file_bytes, mime, text, lang, doctor_name, level, ctx):
    from google import genai
    from google.genai import types
    key = get_gemini_key()
    if not key:
        raise RuntimeError("Gemini API key missing. Add GEMINI_API_KEY in Streamlit secrets or Admin → AI settings.")
    system = f"""You are {doctor_name}, a caring and experienced physician explaining a medical report to your patient in {lang}.
Reading level: {level}. Patient context (no name shared): {ctx or 'not provided'}.
Rules: use plain language and define medical terms; compare each value against the reference range printed in the report;
clearly separate normal findings from those needing attention; never state a definitive diagnosis or change medicines/doses;
list red-flag symptoms that need urgent care; suggest 3-5 questions to ask the doctor; end by saying this explanation was
AI-assisted and must be confirmed by the treating clinician. If the document is unreadable or not a medical report, say so.
OUTPUT FORMAT: first a Markdown WRITTEN SUMMARY with headings (Overview, Key findings (table), Normal results, Needs attention,
Next steps, Questions to ask). Then a line containing exactly =====SPOKEN===== followed by a SPOKEN SCRIPT of 150-300 words:
first person as the doctor, warm and reassuring, short sentences, no markdown, no tables, no symbols, written entirely in {lang}."""
    parts = []
    if file_bytes:
        parts.append(types.Part.from_bytes(data=file_bytes, mime_type=mime))
    if text:
        parts.append(f"Report text:\n{text}")
    parts.append("Please explain this report to the patient.")
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(model=S("gemini_model"), contents=parts,
                                          config=types.GenerateContentConfig(system_instruction=system, temperature=0.3))
    out = resp.text or ""
    if "=====SPOKEN=====" in out:
        written, spoken = out.split("=====SPOKEN=====", 1)
    else:
        written, spoken = out, out
    return written.strip(), spoken.strip()


def tts_gtts(text, lang_code):
    from gtts import gTTS
    buf = io.BytesIO()
    gTTS(text=text, lang=lang_code, tld="co.in" if lang_code == "en" else "com").write_to_fp(buf)
    return buf.getvalue(), "audio/mp3"


def tts_gemini(text, voice):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=get_gemini_key())
    r = client.models.generate_content(
        model=S("gemini_tts_model"),
        contents="Say in a warm, calm and reassuring tone, like a caring doctor talking to a patient: " + text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))))
    pcm = r.candidates[0].content.parts[0].inline_data.data
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return buf.getvalue(), "audio/wav"


def make_voice(script, engine, lang_name, voice):
    if engine.startswith("Gemini"):
        try:
            return tts_gemini(script, voice)
        except Exception as e:
            st.warning(f"Gemini voice unavailable ({e}). Falling back to Google TTS.")
    return tts_gtts(script, LANGS[lang_name])


# ───────────────────────────── pages ─────────────────────────────
def page_dashboard():
    u = me()
    st.header(f"📊 Dashboard · {S('clinic_name')}")
    today = dt.date.today().isoformat()
    month = dt.date.today().replace(day=1).isoformat()
    c = st.columns(5)
    c[0].metric("Patients", one("SELECT COUNT(*) n FROM patients")["n"])
    q = "SELECT COUNT(*) n FROM appointments WHERE appt_date=? AND status NOT IN ('Cancelled')"
    p = [today]
    if u["role"] == "doctor":
        q += " AND doctor_id=?"
        p.append(u["id"])
    c[1].metric("Today's appointments", one(q, tuple(p))["n"])
    c[2].metric("Pending lab orders", one("SELECT COUNT(*) n FROM lab_orders WHERE status!='Completed'")["n"])
    c[3].metric("Low-stock items", one("SELECT COUNT(*) n FROM inventory WHERE stock<=reorder_level")["n"])
    if u["role"] in ("admin", "billing"):
        c[4].metric("Collected this month", money(one("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE paid_at>=?", (month,))["s"]))
    else:
        c[4].metric("Pending prescriptions", one("SELECT COUNT(*) n FROM prescriptions WHERE status='Pending'")["n"])
    a, b = st.columns(2)
    with a:
        st.subheader("Appointments (last 14 days → next 7)")
        d = frame("SELECT appt_date, COUNT(*) AS appointments FROM appointments WHERE appt_date BETWEEN date('now','-14 day') "
                  "AND date('now','+7 day') GROUP BY appt_date ORDER BY appt_date")
        if len(d):
            st.bar_chart(d.set_index("appt_date"))
        else:
            st.caption("No data yet.")
    with b:
        if u["role"] in ("admin", "billing"):
            st.subheader("Collections (last 30 days)")
            d = frame("SELECT substr(paid_at,1,10) AS day, SUM(amount) AS collected FROM payments WHERE paid_at>=date('now','-30 day') "
                      "GROUP BY day ORDER BY day")
            if len(d):
                st.line_chart(d.set_index("day"))
            else:
                st.caption("No payments yet.")
        else:
            st.subheader("Upcoming appointments")
            st.dataframe(frame("SELECT a.appt_date, a.appt_time, p.full_name AS patient, a.status FROM appointments a JOIN patients p "
                               "ON p.id=a.patient_id WHERE a.appt_date>=? AND a.status IN ('Scheduled','Checked-in') "
                               "ORDER BY a.appt_date, a.appt_time LIMIT 10", (today,)), hide_index=True)
    low = frame("SELECT medicine, stock, reorder_level, expiry FROM inventory WHERE stock<=reorder_level OR expiry<=date('now','+60 day')")
    if len(low):
        st.warning("Inventory alerts: low stock or expiring within 60 days")
        st.dataframe(low, hide_index=True)


def patient_profile(pid):
    p = one("SELECT * FROM patients WHERE id=?", (pid,))
    st.subheader(f"{p['full_name']} · {p['code']}")
    st.caption(f"{age(p['dob'])} yrs · {p['gender']} · {p['blood_group']} · {p['phone']}")
    if p["allergies"]:
        st.error(f"⚠️ Allergies: {p['allergies']}")
    tabs = st.tabs(["Details", "Visits", "Vitals", "Prescriptions", "Labs", "Billing", "AI explanations"])
    with tabs[0]:
        with st.form(f"edit{pid}"):
            c1, c2 = st.columns(2)
            name = c1.text_input("Full name", p["full_name"])
            phone = c2.text_input("Phone", p["phone"] or "")
            email = c1.text_input("Email", p["email"] or "")
            emer = c2.text_input("Emergency contact", p["emergency_contact"] or "")
            addr = st.text_area("Address", p["address"] or "")
            allg = st.text_area("Allergies", p["allergies"] or "")
            hist = st.text_area("Medical history", p["history"] or "")
            if st.form_submit_button("Save changes"):
                run("UPDATE patients SET full_name=?,phone=?,email=?,emergency_contact=?,address=?,allergies=?,history=? WHERE id=?",
                    (name, phone, email, emer, addr, allg, hist, pid))
                audit("patient_update", p["code"])
                st.success("Saved")
                st.rerun()
        if not p["user_id"] and me()["role"] in ("admin", "receptionist"):
            with st.expander("Create patient-portal login"):
                un = st.text_input("Username", key=f"pu{pid}")
                pw = st.text_input("Temporary password (min 8)", type="password", key=f"pp{pid}")
                if st.button("Create login", key=f"pb{pid}") and un and len(pw) >= 8:
                    try:
                        uid = create_user(un, p["full_name"], "patient", pw, email=p["email"] or "", phone=p["phone"] or "")
                        run("UPDATE patients SET user_id=? WHERE id=?", (uid, pid))
                        audit("portal_login_created", p["code"])
                        st.success("Portal login created")
                    except sqlite3.IntegrityError:
                        st.error("Username already exists")
    with tabs[1]:
        st.dataframe(frame("SELECT v.created_at, u.full_name AS doctor, v.complaint, v.diagnosis, v.assessment, v.plan, v.follow_up "
                           "FROM visits v LEFT JOIN users u ON u.id=v.doctor_id WHERE v.patient_id=? ORDER BY v.id DESC", (pid,)),
                     hide_index=True)
    with tabs[2]:
        v = frame("SELECT recorded_at, bp_sys, bp_dia, pulse, temp_c, spo2, weight_kg, bmi FROM vitals WHERE patient_id=? ORDER BY id", (pid,))
        st.dataframe(v, hide_index=True)
        if len(v) > 1:
            st.line_chart(v.set_index("recorded_at")[["pulse", "bp_sys", "bp_dia"]])
    with tabs[3]:
        for rx in rows("SELECT r.id, r.created_at, r.status, u.full_name doc FROM prescriptions r LEFT JOIN users u ON u.id=r.doctor_id "
                       "WHERE r.patient_id=? ORDER BY r.id DESC", (pid,)):
            with st.expander(f"Rx #{rx['id']} · {rx['created_at']} · {rx['doc']} · {rx['status']}"):
                st.dataframe(frame("SELECT medicine,dosage,frequency,duration,instructions,qty FROM prescription_items "
                                   "WHERE prescription_id=?", (rx["id"],)), hide_index=True)
    with tabs[4]:
        st.dataframe(frame("SELECT o.ordered_at, t.name test, o.status, o.result, o.flag FROM lab_orders o JOIN lab_tests t ON t.id=o.test_id "
                           "WHERE o.patient_id=? ORDER BY o.id DESC", (pid,)), hide_index=True)
    with tabs[5]:
        for i in rows("SELECT id, number, created_at, status FROM invoices WHERE patient_id=? ORDER BY id DESC", (pid,)):
            t = inv_totals(i["id"])
            st.write(f"**{i['number']}** · {i['created_at']} · {i['status']} · Total {money(t['total'])} · Balance {money(t['bal'])}")
    with tabs[6]:
        for r in rows("SELECT id, created_at, source_name, approved FROM ai_reports WHERE patient_id=? ORDER BY id DESC", (pid,)):
            st.write(f"#{r['id']} · {r['created_at']} · {r['source_name']} · {'✅ reviewed' if r['approved'] else '⏳ not reviewed'}")


def page_patients():
    st.header("👥 Patients")
    t1, t2 = st.tabs(["Search & profile", "Register new patient"])
    with t2:
        with st.form("newpt", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            name = c1.text_input("Full name*")
            dob = c2.date_input("Date of birth", dt.date(1990, 1, 1), min_value=dt.date(1900, 1, 1), max_value=dt.date.today())
            gender = c3.selectbox("Gender", ["Female", "Male", "Other"])
            phone = c1.text_input("Phone")
            email = c2.text_input("Email")
            bg = c3.selectbox("Blood group", ["Unknown", "A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"])
            addr = st.text_area("Address")
            allg = st.text_input("Known allergies")
            hist = st.text_area("Medical history")
            emer = st.text_input("Emergency contact")
            if st.form_submit_button("Register patient"):
                if not name.strip():
                    st.error("Name is required")
                else:
                    i = run("INSERT INTO patients(full_name,dob,gender,phone,email,blood_group,address,allergies,history,"
                            "emergency_contact,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (name.strip(), dob.isoformat(), gender, phone, email, bg, addr, allg, hist, emer, now()))
                    run("UPDATE patients SET code=? WHERE id=?", (f"PT-{i:06d}", i))
                    audit("patient_register", f"PT-{i:06d}")
                    st.success(f"Registered PT-{i:06d}")
    with t1:
        term = st.text_input("Search by name, phone or code")
        like = f"%{term}%"
        res = rows("SELECT id, code, full_name, phone FROM patients WHERE full_name LIKE ? OR phone LIKE ? OR code LIKE ? "
                   "ORDER BY full_name LIMIT 50", (like, like, like))
        if res:
            opts = {r["id"]: f"{r['code']} · {r['full_name']} · {r['phone'] or ''}" for r in res}
            pid = st.selectbox("Select patient", list(opts), format_func=opts.get)
            patient_profile(pid)
        else:
            st.info("No matching patients.")


def page_appointments():
    u = me()
    st.header("📅 Appointments")
    t1, t2 = st.tabs(["Schedule", "Book appointment"])
    with t2:
        pid = patient_picker("Patient", "ap_pt")
        docs = doctors()
        if pid and docs:
            dmap = {d["id"]: f"{d['full_name']} ({d['specialization'] or 'General'})" for d in docs}
            with st.form("book"):
                c1, c2, c3 = st.columns(3)
                did = c1.selectbox("Doctor", list(dmap), format_func=dmap.get)
                d = c2.date_input("Date", dt.date.today(), min_value=dt.date.today())
                tm = c3.selectbox("Time", SLOTS)
                reason = st.text_input("Reason for visit")
                if st.form_submit_button("Book"):
                    clash = one("SELECT id FROM appointments WHERE doctor_id=? AND appt_date=? AND appt_time=? "
                                "AND status NOT IN ('Cancelled','No-show')", (did, d.isoformat(), tm))
                    if clash:
                        st.error("That slot is already booked for this doctor.")
                    else:
                        run("INSERT INTO appointments(patient_id,doctor_id,appt_date,appt_time,reason,status,created_by,created_at) "
                            "VALUES(?,?,?,?,?,'Scheduled',?,?)", (pid, did, d.isoformat(), tm, reason, u["username"], now()))
                        audit("appointment_book", f"patient {pid}")
                        st.success("Appointment booked")
        elif not docs:
            st.warning("Add at least one doctor in Admin → Users.")
    with t1:
        c1, c2, c3 = st.columns(3)
        start = c1.date_input("From", dt.date.today())
        end = c2.date_input("To", dt.date.today() + dt.timedelta(days=7))
        status = c3.multiselect("Status", APPT_STATUS, default=[s for s in APPT_STATUS if s not in ("Cancelled", "No-show")])
        q = ("SELECT a.id, a.appt_date, a.appt_time, p.code, p.full_name patient, d.full_name doctor, a.reason, a.status FROM appointments a "
             "JOIN patients p ON p.id=a.patient_id LEFT JOIN users d ON d.id=a.doctor_id WHERE a.appt_date BETWEEN ? AND ?")
        prm = [start.isoformat(), end.isoformat()]
        if u["role"] == "doctor":
            q += " AND a.doctor_id=?"
            prm.append(u["id"])
        df = frame(q + " ORDER BY a.appt_date, a.appt_time", tuple(prm))
        df = df[df["status"].isin(status)] if status else df
        st.dataframe(df, hide_index=True)
        if len(df):
            c1, c2, c3 = st.columns([1, 2, 1])
            aid = c1.selectbox("Appointment ID", df["id"].tolist())
            ns = c2.selectbox("New status", APPT_STATUS)
            if c3.button("Update status"):
                run("UPDATE appointments SET status=? WHERE id=?", (ns, aid))
                audit("appointment_status", f"{aid}->{ns}")
                st.rerun()


def page_vitals():
    st.header("🩺 Vitals & Triage")
    pid = patient_picker("Patient", "vt_pt")
    if not pid:
        return
    with st.form("vitals"):
        c = st.columns(4)
        sys_ = c[0].number_input("BP systolic", 0, 300, 120)
        dia = c[1].number_input("BP diastolic", 0, 200, 80)
        pulse = c[2].number_input("Pulse (bpm)", 0, 250, 76)
        spo2 = c[3].number_input("SpO₂ %", 0, 100, 98)
        c = st.columns(3)
        temp = c[0].number_input("Temp (°C)", 30.0, 45.0, 36.8, 0.1)
        wt = c[1].number_input("Weight (kg)", 0.0, 400.0, 70.0, 0.1)
        ht = c[2].number_input("Height (cm)", 0.0, 250.0, 170.0, 0.5)
        notes = st.text_input("Notes")
        if st.form_submit_button("Record vitals"):
            bmi = round(wt / ((ht / 100) ** 2), 1) if ht else None
            run("INSERT INTO vitals(patient_id,bp_sys,bp_dia,pulse,temp_c,spo2,weight_kg,height_cm,bmi,notes,recorded_by,recorded_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (pid, sys_, dia, pulse, temp, spo2, wt, ht, bmi, notes, me()["username"], now()))
            if sys_ >= 180 or dia >= 120 or spo2 < 90:
                st.error("🚨 Critical value – alert the doctor immediately.")
            st.success(f"Recorded. BMI {bmi}")
    st.dataframe(frame("SELECT recorded_at, bp_sys, bp_dia, pulse, temp_c, spo2, weight_kg, bmi, recorded_by FROM vitals "
                       "WHERE patient_id=? ORDER BY id DESC", (pid,)), hide_index=True)


def page_consultation():
    u = me()
    st.header("👨‍⚕️ Consultation")
    pid = patient_picker("Patient", "cs_pt")
    if not pid:
        return
    p = one("SELECT * FROM patients WHERE id=?", (pid,))
    if p["allergies"]:
        st.error(f"⚠️ Allergies: {p['allergies']}")
    with st.expander("Patient summary, latest vitals and history", expanded=False):
        st.write(f"{p['full_name']} · {age(p['dob'])} yrs · {p['gender']} · History: {p['history'] or '-'}")
        st.dataframe(frame("SELECT recorded_at,bp_sys,bp_dia,pulse,temp_c,spo2,weight_kg,bmi FROM vitals WHERE patient_id=? "
                           "ORDER BY id DESC LIMIT 3", (pid,)), hide_index=True)
        st.dataframe(frame("SELECT created_at,complaint,diagnosis,plan FROM visits WHERE patient_id=? ORDER BY id DESC LIMIT 5", (pid,)),
                     hide_index=True)
    appts = rows("SELECT id, appt_date, appt_time, reason FROM appointments WHERE patient_id=? AND status IN "
                 "('Scheduled','Checked-in','In consultation') ORDER BY appt_date", (pid,))
    amap = {0: "— walk-in / no appointment —"}
    amap.update({a["id"]: f"{a['appt_date']} {a['appt_time']} · {a['reason'] or ''}" for a in appts})
    aid = st.selectbox("Linked appointment", list(amap), format_func=amap.get)
    n = st.session_state.setdefault("rx_n", 0)
    with st.form(f"consult{n}"):
        complaint = st.text_input("Chief complaint")
        c1, c2 = st.columns(2)
        subj = c1.text_area("Subjective (history of illness)")
        obj = c2.text_area("Objective (examination findings)")
        assess = c1.text_area("Assessment")
        plan = c2.text_area("Plan / advice")
        diag = st.text_input("Diagnosis")
        st.markdown("**Prescription**")
        base = pd.DataFrame([{"medicine": "", "dosage": "", "frequency": "", "duration": "", "instructions": "", "qty": 1}])
        rx = st.data_editor(base, num_rows="dynamic", key=f"rx_{n}")
        tests = rows("SELECT id, name, price FROM lab_tests ORDER BY name")
        tmap = {t["id"]: t["name"] for t in tests}
        ordered = st.multiselect("Order lab tests", list(tmap), format_func=tmap.get)
        fu = st.date_input("Follow-up date (optional)", value=None)
        if st.form_submit_button("Save consultation"):
            vid = run("INSERT INTO visits(patient_id,doctor_id,appointment_id,complaint,subjective,objective,assessment,plan,diagnosis,"
                      "follow_up,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (pid, u["id"], aid or None, complaint, subj, obj, assess, plan, diag, fu.isoformat() if fu else None, now()))
            inv = get_invoice(pid, vid)
            add_item(inv, f"Consultation – {u['full_name']}", 1, float(S("consult_fee", "0")))
            items = rx[rx["medicine"].astype(str).str.strip() != ""]
            if len(items):
                rid = run("INSERT INTO prescriptions(visit_id,patient_id,doctor_id,status,created_at) VALUES(?,?,?, 'Pending',?)",
                          (vid, pid, u["id"], now()))
                for _, r in items.iterrows():
                    run("INSERT INTO prescription_items(prescription_id,medicine,dosage,frequency,duration,instructions,qty) "
                        "VALUES(?,?,?,?,?,?,?)", (rid, r["medicine"], r["dosage"], r["frequency"], r["duration"], r["instructions"],
                                                   int(r["qty"] or 1)))
            for tid in ordered:
                run("INSERT INTO lab_orders(patient_id,visit_id,doctor_id,test_id,status,ordered_at) VALUES(?,?,?,?, 'Ordered',?)",
                    (pid, vid, u["id"], tid, now()))
                price = next(t["price"] for t in tests if t["id"] == tid)
                add_item(inv, f"Lab: {tmap[tid]}", 1, price)
            if aid:
                run("UPDATE appointments SET status='Completed' WHERE id=?", (aid,))
            refresh_invoice_status(inv)
            audit("consultation_saved", f"visit {vid}")
            st.session_state["rx_n"] = n + 1
            st.toast("Consultation saved")
            st.rerun()


def page_lab():
    u = me()
    st.header("🧪 Laboratory")
    t1, t2 = st.tabs(["Orders & results", "Test catalogue"])
    with t1:
        flt = st.multiselect("Status", ["Ordered", "In progress", "Completed"], default=["Ordered", "In progress"])
        df = frame("SELECT o.id, o.ordered_at, p.code, p.full_name patient, t.name test, t.ref_range, o.status, o.flag, d.full_name doctor "
                   "FROM lab_orders o JOIN patients p ON p.id=o.patient_id JOIN lab_tests t ON t.id=o.test_id "
                   "LEFT JOIN users d ON d.id=o.doctor_id ORDER BY o.id DESC")
        df = df[df["status"].isin(flt)] if flt else df
        st.dataframe(df, hide_index=True)
        if len(df):
            oid = st.selectbox("Order ID", df["id"].tolist())
            o = one("SELECT o.*, t.name test, t.ref_range FROM lab_orders o JOIN lab_tests t ON t.id=o.test_id WHERE o.id=?", (oid,))
            st.write(f"**{o['test']}** · reference: {o['ref_range']}")
            if o["result"]:
                st.info(f"Result: {o['result']} ({o['flag'] or 'n/a'}) – {o['resulted_at']}")
            if o["result_file"]:
                st.download_button("Download result file", o["result_file"], o["file_name"], o["file_mime"])
            if u["role"] in ("lab", "admin"):
                with st.form(f"res{oid}"):
                    res = st.text_area("Result", o["result"] or "")
                    flag = st.selectbox("Flag", ["Normal", "Low", "High", "Critical"])
                    up = st.file_uploader("Attach report (PDF/image)", type=["pdf", "png", "jpg", "jpeg"])
                    done = st.checkbox("Mark as completed", True)
                    if st.form_submit_button("Save result"):
                        fb = up.getvalue() if up else o["result_file"]
                        run("UPDATE lab_orders SET result=?, flag=?, result_file=?, file_name=?, file_mime=?, status=?, resulted_at=?, "
                            "resulted_by=? WHERE id=?", (res, flag, fb, up.name if up else o["file_name"], up.type if up else o["file_mime"],
                                                         "Completed" if done else "In progress", now(), u["username"], oid))
                        audit("lab_result", f"order {oid}")
                        st.rerun()
    with t2:
        cat = frame("SELECT id, name, price, ref_range FROM lab_tests ORDER BY name")
        if u["role"] in ("admin", "lab"):
            ed = st.data_editor(cat, num_rows="dynamic", disabled=["id"], key="labcat")
            if st.button("Save catalogue"):
                keep = set(ed["id"].dropna().astype(int))
                for i in set(cat["id"]) - keep:
                    run("DELETE FROM lab_tests WHERE id=?", (int(i),))
                for _, r in ed.iterrows():
                    if not str(r["name"]).strip() or str(r["name"]) == "None":
                        continue
                    if pd.isna(r["id"]):
                        run("INSERT OR IGNORE INTO lab_tests(name,price,ref_range) VALUES(?,?,?)", (r["name"], r["price"] or 0, r["ref_range"]))
                    else:
                        run("UPDATE lab_tests SET name=?,price=?,ref_range=? WHERE id=?", (r["name"], r["price"], r["ref_range"], int(r["id"])))
                st.success("Saved")
                st.rerun()
        else:
            st.dataframe(cat, hide_index=True)


def page_pharmacy():
    st.header("💊 Pharmacy")
    t1, t2 = st.tabs(["Dispense prescriptions", "Inventory"])
    with t1:
        pend = rows("SELECT r.id, r.created_at, p.full_name, p.code, r.patient_id, r.visit_id FROM prescriptions r "
                    "JOIN patients p ON p.id=r.patient_id WHERE r.status='Pending' ORDER BY r.id")
        if not pend:
            st.info("No pending prescriptions.")
        else:
            m = {r["id"]: f"Rx #{r['id']} · {r['full_name']} ({r['code']}) · {r['created_at']}" for r in pend}
            rid = st.selectbox("Prescription", list(m), format_func=m.get)
            rx = next(r for r in pend if r["id"] == rid)
            its = frame("SELECT medicine,dosage,frequency,duration,instructions,qty FROM prescription_items WHERE prescription_id=?", (rid,))
            st.dataframe(its, hide_index=True)
            if st.button("Dispense (deduct stock & add to invoice)"):
                inv = get_invoice(rx["patient_id"], rx["visit_id"])
                missing = []
                for _, it in its.iterrows():
                    s = one("SELECT * FROM inventory WHERE lower(medicine)=lower(?)", (it["medicine"].strip(),))
                    q = int(it["qty"] or 1)
                    if s and s["stock"] >= q:
                        run("UPDATE inventory SET stock=stock-? WHERE id=?", (q, s["id"]))
                        add_item(inv, f"Medicine: {s['medicine']}", q, s["unit_price"])
                    else:
                        missing.append(it["medicine"])
                run("UPDATE prescriptions SET status=? WHERE id=?", ("Partial" if missing else "Dispensed", rid))
                refresh_invoice_status(inv)
                audit("dispense", f"rx {rid}")
                if missing:
                    st.warning("Not in stock / not found: " + ", ".join(missing))
                else:
                    st.success("Dispensed")
                st.rerun()
    with t2:
        inv = frame("SELECT id, medicine, category, stock, reorder_level, unit_price, expiry FROM inventory ORDER BY medicine")
        editable = me()["role"] in ("pharmacist", "admin")
        ed = st.data_editor(inv, num_rows="dynamic" if editable else "fixed", disabled=True if not editable else ["id"], key="inv_ed")
        if editable and st.button("Save inventory"):
            for i in set(inv["id"]) - set(ed["id"].dropna().astype(int)):
                run("DELETE FROM inventory WHERE id=?", (int(i),))
            for _, r in ed.iterrows():
                if not str(r["medicine"]).strip() or str(r["medicine"]) == "None":
                    continue
                vals = (r["medicine"], r["category"], int(r["stock"] or 0), int(r["reorder_level"] or 10), float(r["unit_price"] or 0),
                        None if pd.isna(r["expiry"]) else str(r["expiry"]))
                if pd.isna(r["id"]):
                    run("INSERT OR IGNORE INTO inventory(medicine,category,stock,reorder_level,unit_price,expiry) VALUES(?,?,?,?,?,?)", vals)
                else:
                    run("UPDATE inventory SET medicine=?,category=?,stock=?,reorder_level=?,unit_price=?,expiry=? WHERE id=?",
                        vals + (int(r["id"]),))
            audit("inventory_update")
            st.success("Saved")
            st.rerun()


def page_billing():
    u = me()
    st.header("💳 Billing")
    t1, t2, t3 = st.tabs(["Invoices", "New invoice", "Revenue report"])
    with t2:
        pid = patient_picker("Patient", "bl_pt")
        if pid and st.button("Create blank invoice"):
            i = run("INSERT INTO invoices(patient_id,visit_id,created_at,tax_pct,status) VALUES(?,NULL,?,?, 'Unpaid')",
                    (pid, now(), float(S("tax_pct", "0"))))
            run("UPDATE invoices SET number=? WHERE id=?", (f"INV-{i:06d}", i))
            st.success(f"Created INV-{i:06d}")
    with t1:
        df = frame("SELECT i.id, i.number, i.created_at, p.full_name patient, i.status FROM invoices i JOIN patients p ON p.id=i.patient_id "
                   "ORDER BY i.id DESC LIMIT 200")
        st.dataframe(df, hide_index=True)
        if len(df):
            iid = st.selectbox("Open invoice", df["id"].tolist(), format_func=lambda x: df[df["id"] == x]["number"].iloc[0])
            st.dataframe(frame("SELECT description, qty, unit_price, qty*unit_price AS amount FROM invoice_items WHERE invoice_id=?", (iid,)),
                         hide_index=True)
            t = inv_totals(iid)
            c = st.columns(5)
            for col, (lbl, k) in zip(c, [("Subtotal", "sub"), ("Discount", "disc"), ("Tax", "tax"), ("Total", "total"), ("Balance", "bal")]):
                col.metric(lbl, money(t[k]))
            a, b = st.columns(2)
            with a.form(f"add{iid}"):
                st.markdown("**Add line item / adjust**")
                d = st.text_input("Description")
                q = st.number_input("Qty", 1.0, 1000.0, 1.0)
                pr = st.number_input("Unit price", 0.0, 1_000_000.0, 0.0)
                disc = st.number_input("Invoice discount (amount)", 0.0, 1_000_000.0, float(t["disc"]))
                tax = st.number_input("Tax %", 0.0, 100.0, float(one("SELECT tax_pct FROM invoices WHERE id=?", (iid,))["tax_pct"] or 0))
                if st.form_submit_button("Apply"):
                    if d:
                        add_item(iid, d, q, pr)
                    run("UPDATE invoices SET discount=?, tax_pct=? WHERE id=?", (disc, tax, iid))
                    refresh_invoice_status(iid)
                    st.rerun()
            with b.form(f"pay{iid}"):
                st.markdown("**Record payment**")
                amt = st.number_input("Amount", 0.0, 10_000_000.0, float(max(t["bal"], 0)))
                meth = st.selectbox("Method", ["Cash", "Card", "UPI", "Insurance", "Bank transfer"])
                if st.form_submit_button("Receive payment") and amt > 0:
                    run("INSERT INTO payments(invoice_id,amount,method,paid_at,received_by) VALUES(?,?,?,?,?)",
                        (iid, amt, meth, now(), u["username"]))
                    refresh_invoice_status(iid)
                    audit("payment", f"invoice {iid} {amt}")
                    st.rerun()
            st.download_button("⬇️ Download invoice (HTML – print to PDF)", invoice_html(iid), f"invoice_{iid}.html", "text/html")
    with t3:
        c1, c2 = st.columns(2)
        s = c1.date_input("From", dt.date.today().replace(day=1), key="rv1")
        e = c2.date_input("To", dt.date.today(), key="rv2")
        pay = frame("SELECT method, COUNT(*) payments, SUM(amount) total FROM payments WHERE substr(paid_at,1,10) BETWEEN ? AND ? "
                    "GROUP BY method", (s.isoformat(), e.isoformat()))
        st.dataframe(pay, hide_index=True)
        st.metric("Total collected", money(pay["total"].sum() if len(pay) else 0))
        out = sum(inv_totals(r["id"])["bal"] for r in rows("SELECT id FROM invoices WHERE status!='Paid'"))
        st.metric("Outstanding receivables", money(out))


def page_ai_reports():
    u = me()
    st.header("🤖 AI Report Reader – Doctor Voice")
    st.caption("Upload a lab/radiology report; Gemini explains it in simple language and speaks it in a doctor's voice. "
               "AI output is informational and must be confirmed by a clinician.")
    if u["role"] == "patient":
        pid = my_patient_id()
        if not pid:
            st.warning("Your account is not linked to a patient record. Please contact reception.")
            return
    else:
        pid = patient_picker("Patient", "ai_pt")
        if not pid:
            return
    p = one("SELECT * FROM patients WHERE id=?", (pid,))
    t1, t2 = st.tabs(["New explanation", "Saved explanations"])
    with t1:
        c1, c2, c3 = st.columns(3)
        lang = c1.selectbox("Language", list(LANGS))
        level = c2.selectbox("Explanation level", ["Simple (layperson)", "Detailed", "Clinical summary"])
        dname = c3.text_input("Doctor name for voice", u["full_name"] if u["role"] == "doctor" else "Dr. Care")
        c1, c2 = st.columns(2)
        engine = c1.selectbox("Voice engine", ["Gemini TTS (natural voices)", "Google TTS (free, fast)"])
        voice_label = c2.selectbox("Gemini voice", list(GEMINI_VOICES), disabled=not engine.startswith("Gemini"))
        use_ctx = st.checkbox("Include age, gender, allergies and history as context (name is never sent)", True)
        src = st.radio("Report source", ["Upload file", "Existing lab result", "Paste text"], horizontal=True)
        fb, mime, txt, sname = None, None, None, ""
        if src == "Upload file":
            f = st.file_uploader("Report (PDF / image)", type=["pdf", "png", "jpg", "jpeg", "webp"])
            if f:
                fb, mime, sname = f.getvalue(), f.type, f.name
        elif src == "Existing lab result":
            labs = rows("SELECT o.id, t.name, o.result, o.flag, o.result_file, o.file_mime, o.file_name FROM lab_orders o JOIN lab_tests t "
                        "ON t.id=o.test_id WHERE o.patient_id=? AND o.status='Completed'", (pid,))
            if labs:
                lm = {x["id"]: x["name"] for x in labs}
                sel = st.selectbox("Completed lab order", list(lm), format_func=lm.get)
                lab = next(x for x in labs if x["id"] == sel)
                sname = lab["name"]
                txt = f"Test: {lab['name']}\nResult: {lab['result']}\nFlag: {lab['flag']}"
                if lab["result_file"]:
                    fb, mime = lab["result_file"], lab["file_mime"]
            else:
                st.info("No completed lab results for this patient.")
        else:
            txt = st.text_area("Paste report text", height=180)
            sname = "Pasted text"
        if st.button("Explain report & generate voice", type="primary", disabled=not (fb or txt)):
            ctx = (f"{age(p['dob'])}-year-old {p['gender']}; allergies: {p['allergies'] or 'none'}; history: {p['history'] or 'none'}"
                   if use_ctx else "")
            try:
                with st.spinner("Gemini is reading the report…"):
                    written, spoken = explain_report(fb, mime, txt, lang, dname, level, ctx)
                with st.spinner("Generating doctor voice…"):
                    audio, amime = make_voice(spoken, engine, lang, GEMINI_VOICES[voice_label])
                st.session_state["ai_draft"] = dict(pid=pid, sname=sname, lang=lang, dname=dname, written=written,
                                                    spoken=spoken, audio=audio, amime=amime)
            except Exception as e:
                st.error(f"AI request failed: {e}")
        d = st.session_state.get("ai_draft")
        if d and d["pid"] == pid:
            st.divider()
            st.markdown(d["written"])
            st.audio(d["audio"], format=d["amime"])
            with st.expander("Spoken script"):
                st.write(d["spoken"])
            appr = False
            if u["role"] in ("doctor", "admin"):
                appr = st.checkbox("Approve & share with patient (clinician reviewed)", True)
            if st.button("💾 Save explanation"):
                run("INSERT INTO ai_reports(patient_id,requested_by,requester_role,source_name,language,doctor_name,explanation,"
                    "spoken_script,audio,audio_mime,approved,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, u["username"], u["role"], d["sname"], d["lang"], d["dname"], d["written"], d["spoken"], d["audio"],
                     d["amime"], 1 if appr else 0, now()))
                audit("ai_report_saved", d["sname"])
                st.session_state.pop("ai_draft", None)
                st.success("Saved")
    with t2:
        q = "SELECT * FROM ai_reports WHERE patient_id=?"
        prm = [pid]
        if u["role"] == "patient":
            q += " AND (approved=1 OR requested_by=?)"
            prm.append(u["username"])
        for r in rows(q + " ORDER BY id DESC", tuple(prm)):
            badge = "✅ Reviewed by clinician" if r["approved"] else "⏳ AI-generated, not yet clinician-reviewed"
            with st.expander(f"#{r['id']} · {r['created_at']} · {r['source_name']} · {r['language']} · {badge}"):
                st.markdown(r["explanation"])
                if r["audio"]:
                    st.audio(r["audio"], format=r["audio_mime"])
                if u["role"] in ("doctor", "admin") and not r["approved"]:
                    if st.button("Approve for patient", key=f"ap{r['id']}"):
                        run("UPDATE ai_reports SET approved=1 WHERE id=?", (r["id"],))
                        audit("ai_report_approved", str(r["id"]))
                        st.rerun()


def page_portal():
    pid = my_patient_id()
    st.header("🏠 My Health Portal")
    if not pid:
        st.warning("Your account is not linked to a patient record. Please contact reception.")
        return
    p = one("SELECT * FROM patients WHERE id=?", (pid,))
    st.subheader(f"Welcome, {p['full_name']}")
    t1, t2, t3, t4, t5 = st.tabs(["Appointments", "Prescriptions", "Lab results", "Bills", "Request appointment"])
    with t1:
        st.dataframe(frame("SELECT a.appt_date, a.appt_time, d.full_name doctor, a.reason, a.status FROM appointments a "
                           "LEFT JOIN users d ON d.id=a.doctor_id WHERE a.patient_id=? ORDER BY a.appt_date DESC", (pid,)), hide_index=True)
    with t2:
        for rx in rows("SELECT id, created_at, status FROM prescriptions WHERE patient_id=? ORDER BY id DESC", (pid,)):
            with st.expander(f"Prescription #{rx['id']} · {rx['created_at']} · {rx['status']}"):
                st.dataframe(frame("SELECT medicine,dosage,frequency,duration,instructions FROM prescription_items WHERE prescription_id=?",
                                   (rx["id"],)), hide_index=True)
    with t3:
        st.dataframe(frame("SELECT o.resulted_at, t.name test, o.result, o.flag FROM lab_orders o JOIN lab_tests t ON t.id=o.test_id "
                           "WHERE o.patient_id=? AND o.status='Completed' ORDER BY o.id DESC", (pid,)), hide_index=True)
        st.info("Use **AI Report Reader** in the menu to hear an explanation of your report.")
    with t4:
        for i in rows("SELECT id, number, created_at, status FROM invoices WHERE patient_id=? ORDER BY id DESC", (pid,)):
            t = inv_totals(i["id"])
            st.write(f"**{i['number']}** · {i['created_at']} · {i['status']} · Total {money(t['total'])} · Due {money(t['bal'])}")
            st.download_button("Download", invoice_html(i["id"]), f"{i['number']}.html", "text/html", key=f"dl{i['id']}")
    with t5:
        docs = doctors()
        if docs:
            dmap = {d["id"]: d["full_name"] for d in docs}
            with st.form("req"):
                did = st.selectbox("Doctor", list(dmap), format_func=dmap.get)
                d = st.date_input("Preferred date", dt.date.today() + dt.timedelta(days=1), min_value=dt.date.today())
                tm = st.selectbox("Preferred time", SLOTS)
                reason = st.text_input("Reason")
                if st.form_submit_button("Request"):
                    run("INSERT INTO appointments(patient_id,doctor_id,appt_date,appt_time,reason,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,'Requested',?,?)", (pid, did, d.isoformat(), tm, reason, me()["username"], now()))
                    st.success("Request sent. Reception will confirm.")


def page_admin():
    st.header("⚙️ Administration")
    t1, t2, t3, t4, t5 = st.tabs(["Users", "Clinic settings", "AI settings", "Audit log", "Data & backup"])
    with t1:
        st.dataframe(frame("SELECT id, username, full_name, role, specialization, active, created_at FROM users ORDER BY id"), hide_index=True)
        with st.form("nu", clear_on_submit=True):
            st.markdown("**Create user**")
            c = st.columns(3)
            un = c[0].text_input("Username")
            fn = c[1].text_input("Full name")
            rl = c[2].selectbox("Role", [r for r in ROLES if r != "patient"])
            sp = c[0].text_input("Specialization (doctors)")
            pw = c[1].text_input("Temporary password (min 8)", type="password")
            if st.form_submit_button("Create"):
                if len(pw) < 8 or not un or not fn:
                    st.error("Fill all fields; password ≥ 8 characters.")
                else:
                    try:
                        create_user(un, fn, rl, pw, sp)
                        audit("user_created", un)
                        st.success("User created")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Username exists")
        us = rows("SELECT id, username, active FROM users")
        m = {x["id"]: x["username"] for x in us}
        c = st.columns(3)
        uid = c[0].selectbox("Manage user", list(m), format_func=m.get)
        if c[1].button("Toggle active"):
            run("UPDATE users SET active = 1-active WHERE id=? AND username!='admin'", (uid,))
            audit("user_toggle", m[uid])
            st.rerun()
        npw = c[2].text_input("New password", type="password", key="adm_np")
        if c[2].button("Reset password") and len(npw) >= 8:
            h, s = hash_pw(npw)
            run("UPDATE users SET password_hash=?, salt=? WHERE id=?", (h, s, uid))
            audit("password_reset", m[uid])
            st.success("Password reset")
    with t2:
        with st.form("cs"):
            vals = {k: st.text_input(k.replace("_", " ").title(), S(k)) for k in
                    ["clinic_name", "clinic_address", "clinic_phone", "consult_fee", "tax_pct", "currency"]}
            if st.form_submit_button("Save settings"):
                for k, v in vals.items():
                    set_setting(k, v)
                audit("settings_updated")
                st.success("Saved")
    with t3:
        st.write("Gemini key status:", "✅ configured" if get_gemini_key() else "❌ missing")
        k = st.text_input("Session-only API key (not stored)", type="password")
        if k:
            st.session_state["gemini_key"] = k
        set_setting("gemini_model", st.text_input("Gemini model", S("gemini_model")))
        set_setting("gemini_tts_model", st.text_input("Gemini TTS model", S("gemini_tts_model")))
        st.caption("Prefer storing GEMINI_API_KEY in Streamlit secrets.")
    with t4:
        st.dataframe(frame("SELECT ts, user, action, detail FROM audit_log ORDER BY id DESC LIMIT 500"), hide_index=True)
    with t5:
        exports = {
            "patients": "SELECT * FROM patients", "appointments": "SELECT * FROM appointments", "visits": "SELECT * FROM visits",
            "invoices": "SELECT * FROM invoices", "payments": "SELECT * FROM payments", "inventory": "SELECT * FROM inventory",
            "audit_log": "SELECT * FROM audit_log"}
        tb = st.selectbox("Export table to CSV", list(exports))
        st.download_button("Download CSV", frame(exports[tb]).to_csv(index=False), f"{tb}.csv", "text/csv")
        st.divider()
        if os.path.exists(DB_PATH):
            with open(DB_PATH, "rb") as fh:
                st.download_button("⬇️ Download full database backup", fh.read(), "clinic_backup.db")
        st.warning("Streamlit Cloud storage is ephemeral – download backups regularly or use an external database.")
        up = st.file_uploader("Restore from backup (.db)", type=["db"])
        if up and st.checkbox("I understand this overwrites ALL current data") and st.button("Restore"):
            data = up.getvalue()
            if data[:15] == b"SQLite format 3":
                with open(DB_PATH, "wb") as fh:
                    fh.write(data)
                st.cache_resource.clear()
                st.session_state.clear()
                st.rerun()
            else:
                st.error("Not a valid SQLite file.")


# ───────────────────────────── routing & auth ─────────────────────────────
ALL_STAFF = ["admin", "doctor", "nurse", "receptionist", "lab", "pharmacist", "billing"]
PAGES = {
    "📊 Dashboard": (page_dashboard, ALL_STAFF),
    "👥 Patients": (page_patients, ["admin", "doctor", "nurse", "receptionist"]),
    "📅 Appointments": (page_appointments, ["admin", "doctor", "nurse", "receptionist"]),
    "🩺 Vitals & Triage": (page_vitals, ["admin", "nurse", "doctor"]),
    "👨‍⚕️ Consultation": (page_consultation, ["admin", "doctor"]),
    "🧪 Laboratory": (page_lab, ["admin", "doctor", "nurse", "lab"]),
    "💊 Pharmacy": (page_pharmacy, ["admin", "pharmacist", "doctor"]),
    "💳 Billing": (page_billing, ["admin", "billing", "receptionist"]),
    "🤖 AI Report Reader": (page_ai_reports, ["admin", "doctor", "nurse", "lab", "patient"]),
    "🏠 My Portal": (page_portal, ["patient"]),
    "⚙️ Admin": (page_admin, ["admin"]),
}


def login_view():
    st.title("🩺 MediCore Clinic")
    st.caption("Medical practice management")
    if st.session_state.get("fails", 0) >= 5:
        st.error("Too many failed attempts. Reload the page to try again.")
        return
    with st.form("login"):
        un = st.text_input("Username")
        pw = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            r = one("SELECT * FROM users WHERE username=? AND active=1", (un.strip().lower(),))
            if r and verify_pw(pw, r["password_hash"], r["salt"]):
                st.session_state["user"] = r
                st.session_state["fails"] = 0
                audit("login")
                st.rerun()
            else:
                st.session_state["fails"] = st.session_state.get("fails", 0) + 1
                st.error("Invalid credentials")


def main():
    init_db()
    if "user" not in st.session_state:
        login_view()
        return
    u = me()
    allowed = {k: v[0] for k, v in PAGES.items() if u["role"] in v[1]}
    with st.sidebar:
        st.markdown(f"### {S('clinic_name')}")
        st.write(f"**{u['full_name']}**  \n`{u['role']}`")
        choice = st.radio("Navigation", list(allowed), label_visibility="collapsed")
        with st.expander("Change my password"):
            old = st.text_input("Current", type="password", key="op")
            new = st.text_input("New (min 8)", type="password", key="np")
            if st.button("Update password") and len(new) >= 8:
                cur = one("SELECT * FROM users WHERE id=?", (u["id"],))
                if verify_pw(old, cur["password_hash"], cur["salt"]):
                    h, s = hash_pw(new)
                    run("UPDATE users SET password_hash=?, salt=? WHERE id=?", (h, s, u["id"]))
                    audit("password_changed")
                    st.success("Updated")
                else:
                    st.error("Wrong current password")
        if st.button("Sign out"):
            audit("logout")
            st.session_state.clear()
            st.rerun()
    if u["role"] == "admin" and verify_pw("admin123", u["password_hash"], u["salt"]):
        st.warning("⚠️ Default admin password in use. Change it from the sidebar immediately.")
    allowed[choice]()
    st.sidebar.caption("For clinical decision support only. Not a substitute for professional judgement.")


main()
