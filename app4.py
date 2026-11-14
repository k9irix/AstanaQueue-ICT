from flask import Flask, request, jsonify, send_from_directory, Response, send_file
from flask_cors import CORS
from functools import wraps
from datetime import datetime
import sqlite3
import os
import io
import csv
import hmac
import hashlib
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
CORS(app)

# ---- Настройки безопасности (меняйте через переменные окружения) ----
OPERATOR_PIN = os.environ.get("OPERATOR_PIN", "1234")
ADMIN_PIN = os.environ.get("ADMIN_PIN", "admin123")
SECRET = os.environ.get("AQ_SECRET") or secrets.token_hex(16)   # для токенов отмены талона


# ------------------------------------------------------------------ DB
def get_db_path():
    candidates = [
        os.path.join(BASE_DIR, "astanaqueue.sqlite"),
        os.path.join(BASE_DIR, "astanaqueue-v2.sqlite"),
        "astanaqueue.sqlite",
        "astanaqueue-v2.sqlite",
        "/workspace/artifacts/astanaqueue.sqlite",
        "/workspace/artifacts/astanaqueue-v2.sqlite",
        "/workspace/scratch/astanaqueue.sqlite",
    ]
    for p in candidates:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            try:
                conn = sqlite3.connect(p)
                conn.execute("SELECT 1 FROM queues LIMIT 1")
                conn.close()
                return p
            except Exception:
                pass
    return os.path.join(BASE_DIR, "astanaqueue.sqlite")


def get_db():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------ helpers
def normalize_phone(raw_phone):
    p = str(raw_phone).strip()
    for ch in (" ", "-", "(", ")"):
        p = p.replace(ch, "")
    if p.startswith("87"):
        p = "+7" + p[1:]
    elif p.startswith("77"):
        p = "+" + p
    elif not p.startswith("+77"):
        p = "+7707" + p[-7:]
    return p


def ahead_of(cursor, ticket_id):
    cursor.execute(
        "SELECT COUNT(*) AS c FROM tickets WHERE status = 'waiting' AND ticket_id < ?",
        (ticket_id,))
    return cursor.fetchone()["c"]


def make_token(ticket_number):
    return hmac.new(SECRET.encode(), ticket_number.encode(), hashlib.sha256).hexdigest()[:12]


def pin_ok(pin, role):
    pin = pin or ""
    if role == "admin":
        return hmac.compare_digest(pin, ADMIN_PIN)
    return hmac.compare_digest(pin, OPERATOR_PIN) or hmac.compare_digest(pin, ADMIN_PIN)


def staff_required(role):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            if not pin_ok(request.headers.get("X-Staff-Pin", ""), role):
                return jsonify({"error": "Staff PIN required", "auth": False}), 401
            return fn(*a, **k)
        return wrapper
    return deco


# ---------------------------------------------------------------- pages
@app.route("/")
def index():
    """Сайт отдаётся самим Flask — поэтому ngrok на порт 5000 открывает страницу."""
    return send_from_directory(BASE_DIR, "astanaqueue4.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True})


@app.route("/api/auth", methods=["POST"])
def auth():
    data = request.json or {}
    role = "admin" if data.get("role") == "admin" else "operator"
    if pin_ok(str(data.get("pin", "")), role):
        return jsonify({"success": True, "role": role})
    return jsonify({"success": False, "error": "Wrong PIN"}), 401


# -------------------------------------------------------------- citizen
@app.route("/api/tickets", methods=["POST"])
def create_ticket():
    try:
        data = request.json or {}
        raw_phone = str(data.get("phone", "")).strip()
        raw_category = str(data.get("category", "ST")).strip().upper()

        if not raw_phone:
            return jsonify({"error": "Phone number is required!"}), 400
        clean_phone = normalize_phone(raw_phone)
        if len(clean_phone) != 12 or not clean_phone.startswith("+77"):
            return jsonify({"error": "Invalid phone format. Use +7 (7XX) XXX-XX-XX"}), 400

        conn = get_db()
        cursor = conn.cursor()

        cat_mapping = {
            "C": ["C", "ST"], "ST": ["ST", "C"],
            "Ш": ["Ш", "SC"], "SC": ["SC", "Ш"],
            "П": ["П", "SR"], "SR": ["SR", "П"],
        }
        search_codes = cat_mapping.get(raw_category, [raw_category, "C", "ST"])
        placeholders = ",".join("?" * len(search_codes))
        cursor.execute(
            f"SELECT queue_id, code, name, daily_limit, avg_service_time_min "
            f"FROM queues WHERE code IN ({placeholders})", search_codes)
        queue = cursor.fetchone()
        if not queue:
            cursor.execute(
                "SELECT queue_id, code, name, daily_limit, avg_service_time_min FROM queues LIMIT 1")
            queue = cursor.fetchone()
        if not queue:
            conn.close()
            return jsonify({"error": "No queues available in database."}), 400

        queue_id, actual_code = queue["queue_id"], queue["code"]
        queue_name, daily_limit = queue["name"], queue["daily_limit"]
        avg_time = queue["avg_service_time_min"]

        cursor.execute(
            "SELECT ticket_number, status FROM tickets "
            "WHERE phone_number = ? AND status IN ('waiting', 'called')", (clean_phone,))
        existing = cursor.fetchone()
        if existing:
            conn.close()
            return jsonify({
                "error": f"Active Ticket Exists! You already have ticket "
                         f"[{existing['ticket_number']}] in status '{existing['status']}'.",
                "ticket_number": existing["ticket_number"],
            }), 400

        cursor.execute("SELECT COUNT(*) AS cnt FROM tickets WHERE queue_id = ?", (queue_id,))
        count = cursor.fetchone()["cnt"] + 1
        if count > daily_limit:
            conn.close()
            return jsonify({
                "error": f"Daily Limit Reached! Category '{queue_name}' max limit is {daily_limit} tickets per day."
            }), 400

        ticket_number = f"{actual_code}-{count:02d}"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT INTO tickets (ticket_number, queue_id, phone_number, status, created_at) "
            "VALUES (?, ?, ?, 'waiting', ?)",
            (ticket_number, queue_id, clean_phone, now_str))
        new_id = cursor.lastrowid
        conn.commit()
        people_ahead = ahead_of(cursor, new_id)
        conn.close()

        print(f"✅ Ticket issued: {ticket_number} | ahead: {people_ahead}")
        return jsonify({
            "success": True,
            "ticket_number": ticket_number,
            "token": make_token(ticket_number),      # нужен для отмены талона
            "queue_name": queue_name,
            "people_ahead": people_ahead,
            "est_wait_minutes": people_ahead * avg_time,
        })
    except Exception as e:
        print(f"❌ [DATABASE ERROR]: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/tickets/<ticket_number>", methods=["GET"])
def ticket_status(ticket_number):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.ticket_id, t.ticket_number, t.status, t.window_id,
                   q.name AS queue_name, q.avg_service_time_min AS avg_time
            FROM tickets t JOIN queues q ON q.queue_id = t.queue_id
            WHERE t.ticket_number = ?
        """, (ticket_number.upper(),))
        t = cursor.fetchone()
        if not t:
            conn.close()
            return jsonify({"error": "Ticket not found"}), 404
        ahead = ahead_of(cursor, t["ticket_id"]) if t["status"] == "waiting" else 0
        cursor.execute("SELECT ticket_number FROM tickets WHERE status = 'called' "
                       "ORDER BY called_at DESC LIMIT 1")
        ns = cursor.fetchone()
        conn.close()
        return jsonify({
            "ticket_number": t["ticket_number"],
            "status": t["status"],
            "window_id": t["window_id"],
            "queue_name": t["queue_name"],
            "people_ahead": ahead,
            "est_wait_minutes": ahead * t["avg_time"],
            "now_serving": ns["ticket_number"] if ns else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tickets/cancel", methods=["POST"])
def cancel_ticket():
    """Отмена только по токену, который получил владелец талона."""
    try:
        data = request.json or {}
        number = str(data.get("ticket_number", "")).strip().upper()
        token = str(data.get("token", ""))
        if not number or not hmac.compare_digest(token, make_token(number)):
            return jsonify({"success": False, "error": "Not allowed to cancel this ticket"}), 403
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT ticket_id, status FROM tickets WHERE ticket_number = ?", (number,))
        t = cursor.fetchone()
        if not t:
            conn.close()
            return jsonify({"success": False, "error": "Ticket not found"}), 404
        if t["status"] != "waiting":
            conn.close()
            return jsonify({"success": False,
                            "error": f"Cannot cancel ticket in status '{t['status']}'"}), 400
        cursor.execute("UPDATE tickets SET status = 'cancelled' WHERE ticket_id = ?", (t["ticket_id"],))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "ticket_number": number})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -------------------------------------------------------------- public live
@app.route("/api/status", methods=["GET"])
def get_live_status():
    """Публичные данные для табло: без телефонов и личных данных."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT q.code, q.name, COUNT(t.ticket_id) AS waiting_count
            FROM queues q
            LEFT JOIN tickets t ON q.queue_id = t.queue_id AND t.status = 'waiting'
            GROUP BY q.queue_id
        """)
        queues = [dict(r) for r in cursor.fetchall()]
        cursor.execute("SELECT ticket_number, window_id, called_at FROM tickets "
                       "WHERE status = 'called' ORDER BY called_at DESC")
        serving = [dict(r) for r in cursor.fetchall()]
        cursor.execute("SELECT COUNT(*) AS c FROM tickets WHERE status = 'waiting'")
        total_waiting = cursor.fetchone()["c"]
        conn.close()
        return jsonify({"queues": queues, "serving": serving, "total_waiting": total_waiting})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------- operator
@app.route("/api/operator/call", methods=["POST"])
@staff_required("operator")
def operator_call_ticket():
    try:
        window_id = int((request.json or {}).get("window_id", 1))
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT ticket_number FROM tickets WHERE window_id = ? AND status = 'called'",
                       (window_id,))
        busy = cursor.fetchone()
        if busy:
            conn.close()
            return jsonify({"success": False, "code": "busy", "ticket_number": busy["ticket_number"]}), 200
        cursor.execute("SELECT ticket_id, ticket_number FROM tickets "
                       "WHERE status = 'waiting' ORDER BY ticket_id ASC LIMIT 1")
        ticket = cursor.fetchone()
        if not ticket:
            conn.close()
            return jsonify({"success": False, "code": "empty"}), 200
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("UPDATE tickets SET status = 'called', window_id = ?, called_at = ? "
                       "WHERE ticket_id = ?", (window_id, now_str, ticket["ticket_id"]))
        conn.commit()
        conn.close()
        print(f"📢 Called {ticket['ticket_number']} -> window {window_id}")
        return jsonify({"success": True, "ticket_number": ticket["ticket_number"], "window_id": window_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _close_called(new_status):
    window_id = int((request.json or {}).get("window_id", 1))
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT ticket_id, ticket_number FROM tickets "
                   "WHERE window_id = ? AND status = 'called' ORDER BY called_at DESC LIMIT 1",
                   (window_id,))
    ticket = cursor.fetchone()
    if not ticket:
        conn.close()
        return jsonify({"success": False, "code": "none"}), 200
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if new_status == "served":
        cursor.execute("UPDATE tickets SET status = 'served', served_at = ? WHERE ticket_id = ?",
                       (now_str, ticket["ticket_id"]))
    else:  # no-show
        cursor.execute("UPDATE tickets SET status = 'cancelled' WHERE ticket_id = ?", (ticket["ticket_id"],))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "ticket_number": ticket["ticket_number"]})


@app.route("/api/operator/serve", methods=["POST"])
@staff_required("operator")
def operator_serve_ticket():
    try:
        return _close_called("served")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/operator/noshow", methods=["POST"])
@staff_required("operator")
def operator_noshow_ticket():
    try:
        return _close_called("noshow")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------- admin
@app.route("/api/stats", methods=["GET"])
@staff_required("admin")
def get_stats():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) AS c FROM tickets GROUP BY status")
        by_status = {r["status"]: r["c"] for r in cursor.fetchall()}
        cursor.execute("SELECT strftime('%H', created_at) AS hour, COUNT(*) AS c "
                       "FROM tickets GROUP BY hour ORDER BY hour")
        by_hour = [{"hour": r["hour"], "count": r["c"]} for r in cursor.fetchall()]
        cursor.execute("SELECT AVG((julianday(called_at) - julianday(created_at)) * 1440.0) AS w "
                       "FROM tickets WHERE called_at IS NOT NULL")
        w = cursor.fetchone()["w"]
        conn.close()
        return jsonify({"by_status": by_status, "by_hour": by_hour,
                        "avg_wait_min": round(w, 1) if w is not None else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export.csv", methods=["GET"])
@staff_required("admin")
def export_csv():
    """CSV без телефонов — для анализа данных (Stage 2, Part C)."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT t.ticket_id, t.ticket_number, q.code AS queue_code, t.status, t.window_id,
               t.created_at, t.called_at, t.served_at
        FROM tickets t JOIN queues q ON q.queue_id = t.queue_id ORDER BY t.ticket_id
    """)
    rows = cursor.fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ticket_id", "ticket_number", "queue_code", "status", "window_id",
                "created_at", "called_at", "served_at"])
    for r in rows:
        w.writerow(list(r))
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=astanaqueue_tickets.csv"})


@app.route("/api/admin/clear", methods=["POST"])
@staff_required("admin")
def clear_db():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tickets;")
        cursor.execute("DELETE FROM sqlite_sequence WHERE name='tickets';")
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("🚀 AstanaQueue running on http://127.0.0.1:5000")
    print(f"📁 Database: {os.path.abspath(get_db_path())}")
    print(f"🔑 Operator PIN: {OPERATOR_PIN} | Admin PIN: {ADMIN_PIN}  (change via env vars)")
    # debug=False — обязательно, пока сайт открыт через ngrok (иначе наружу торчит отладчик Werkzeug)
    app.run(host="127.0.0.1", port=5000, debug=False)
