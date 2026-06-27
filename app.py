import os, json, calendar, hashlib, secrets
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from datetime import date
from functools import wraps
from sqlalchemy import create_engine, text
from scheduler import generate_schedule, date_range, is_sunday, SHIFT_HOURS, SHIFT_COLORS
from excel_import import import_excel

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "gestor-turnos-secret-2026")

# ── Database connection ───────────────────────────────────────────────────────
# Render provides DATABASE_URL as postgres://... but SQLAlchemy needs postgresql://
def get_db_url():
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if not url:
        # Fallback to SQLite for local development without DATABASE_URL set
        return "sqlite:///horarios.db"
    return url

engine = create_engine(get_db_url(), pool_pre_ping=True)

def get_db():
    return engine.connect()

def init_db():
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS employees (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                specialist_start TEXT DEFAULT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vacations (
                id SERIAL PRIMARY KEY,
                employee_id INTEGER,
                start_date TEXT,
                end_date TEXT,
                FOREIGN KEY(employee_id) REFERENCES employees(id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS holidays (
                id SERIAL PRIMARY KEY,
                date TEXT UNIQUE,
                name TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY,
                year INTEGER,
                month INTEGER,
                data TEXT,
                status TEXT DEFAULT 'draft',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS month_state (
                id SERIAL PRIMARY KEY,
                year INTEGER,
                month INTEGER,
                employee_id INTEGER,
                last_shift TEXT,
                last_work_date TEXT,
                rest_served INTEGER DEFAULT 0,
                block_remaining INTEGER DEFAULT 0,
                UNIQUE(year, month, employee_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.commit()

        # Seed employees if empty
        count = conn.execute(text("SELECT COUNT(*) FROM employees")).scalar()
        if count == 0:
            seed = [
                ("Ariel Painel",    "especial",    None),
                ("Ricardo Pino",    "rotativo",    None),
                ("Axel Lavin",      "rotativo",    None),
                ("Andres Cona",     "rotativo",    None),
                ("Kaiby Tapia",     "rotativo",    None),
                ("Cristobal Barrios","rotativo",   None),
                ("Cristian Millán", "rotativo",    None),
                ("Yercko Legue",    "rotativo",    None),
                ("Matias Toledo",   "rotativo",    None),
                ("Oscar Castro",    "rotativo",    None),
                ("Jose Villagran",  "rotativo",    None),
                ("Sebastian Palma", "especialista","AM"),
                ("Jose linares",    "especialista","PM"),
                ("Ricardo Moena",   "especialista","FIXED_PM"),
            ]
            conn.execute(text("""
                INSERT INTO employees (name, type, specialist_start) VALUES (:n, :t, :s)
            """), [{"n": n, "t": t, "s": s} for n, t, s in seed])
            conn.commit()

        # Tabla de usuarios del sistema
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_users (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'vista'
            )
        """))
        conn.commit()

        # Seed superuser y usuario vista
        def _hash(pw): return hashlib.sha256(pw.encode()).hexdigest()
        su = conn.execute(text("SELECT id FROM app_users WHERE username='admin'")).fetchone()
        if not su:
            conn.execute(text("INSERT INTO app_users (username,password_hash,role) VALUES (:u,:p,:r)"),
                         {"u":"admin","p":_hash("Anibal.,2026"),"r":"superuser"})
        vista = conn.execute(text("SELECT id FROM app_users WHERE username='solovista'")).fetchone()
        if not vista:
            conn.execute(text("INSERT INTO app_users (username,password_hash,role) VALUES (:u,:p,:r)"),
                         {"u":"solovista","p":_hash("solovista"),"r":"vista"})
        conn.commit()

        # Migration: fix Ricardo Moena if already exists as rotativo
        moena = conn.execute(text(
            "SELECT id, type, specialist_start FROM employees WHERE name='Ricardo Moena'"
        )).fetchone()
        if moena and (moena[1] != "especialista" or moena[2] != "FIXED_PM"):
            conn.execute(text(
                "UPDATE employees SET type='especialista', specialist_start='FIXED_PM' WHERE name='Ricardo Moena'"
            ))
            conn.commit()


def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def log_action(action, detail=""):
    """Registra una acción en el audit_log."""
    from datetime import datetime
    username = session.get("user", "sistema")
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            conn.execute(text(
                "INSERT INTO audit_log (username, action, detail, timestamp) VALUES (:u, :a, :d, :t)"
            ), {"u": username, "a": action, "d": detail, "t": timestamp})
            conn.commit()
    except Exception:
        pass  # No interrumpir el flujo si falla el log

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "No autenticado"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "No autenticado"}), 401
            return redirect(url_for("login_page"))
        if session.get("role") not in ("admin", "superuser"):
            return jsonify({"error": "Sin permisos"}), 403
        return f(*args, **kwargs)
    return decorated

def rows_as_dicts(result):
    """Convert SQLAlchemy result rows to list of dicts."""
    keys = result.keys()
    return [dict(zip(keys, row)) for row in result.fetchall()]

def row_as_dict(result):
    """Convert single SQLAlchemy row to dict, or None."""
    keys = result.keys()
    row = result.fetchone()
    return dict(zip(keys, row)) if row else None

# ── Ejecutar init_db al importar (necesario para gunicorn) ────────────────────
init_db()

# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET"])
def login_page():
    if "user" in session:
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def login_post():
    d = request.json or {}
    username = d.get("username","").strip()
    password = d.get("password","")
    with get_db() as conn:
        user = row_as_dict(conn.execute(text(
            "SELECT * FROM app_users WHERE username=:u AND password_hash=:p"
        ), {"u": username, "p": hash_pw(password)}))
    if not user:
        return jsonify({"error": "Usuario o contraseña incorrectos"}), 401
    session["user"]    = user["username"]
    session["role"]    = user["role"]
    session["user_id"] = user["id"]
    return jsonify({"ok": True, "role": user["role"]})

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

@app.route("/api/me")
@login_required
def get_me():
    return jsonify({"user": session["user"], "role": session["role"]})

@app.route("/api/users", methods=["GET"])
@login_required
def get_users():
    if session.get("role") not in ("admin","superuser"):
        return jsonify({"error":"Sin permisos"}), 403
    with get_db() as conn:
        rows = rows_as_dicts(conn.execute(text(
            "SELECT id, username, role FROM app_users ORDER BY role, username"
        )))
    return jsonify(rows)

@app.route("/api/users", methods=["POST"])
@login_required
def create_user():
    if session.get("role") not in ("admin","superuser"):
        return jsonify({"error":"Sin permisos"}), 403
    d    = request.json
    role = d.get("role","vista")
    if role in ("admin","superuser") and session.get("role") != "superuser":
        return jsonify({"error":"Solo el superusuario puede crear administradores"}), 403
    try:
        with get_db() as conn:
            conn.execute(text(
                "INSERT INTO app_users (username,password_hash,role) VALUES (:u,:p,:r)"
            ), {"u": d["username"], "p": hash_pw(d["password"]), "r": role})
            conn.commit()
    except Exception:
        return jsonify({"error":"El usuario ya existe"}), 400
    log_action("CREAR USUARIO", f"Usuario: {d['username']} | Rol: {role}")
    return jsonify({"ok": True})

@app.route("/api/users/<int:uid>", methods=["DELETE"])
@login_required
def delete_user(uid):
    if session.get("role") != "superuser":
        return jsonify({"error":"Solo el superusuario puede eliminar usuarios"}), 403
    with get_db() as conn:
        user = row_as_dict(conn.execute(text(
            "SELECT role FROM app_users WHERE id=:id"
        ), {"id": uid}))
        if user and user["role"] == "superuser":
            return jsonify({"error":"No se puede eliminar al superusuario"}), 403
        uname = row_as_dict(conn.execute(text("SELECT username FROM app_users WHERE id=:id"), {"id": uid}))
        conn.execute(text("DELETE FROM app_users WHERE id=:id"), {"id": uid})
        conn.commit()
    log_action("ELIMINAR USUARIO", f"Usuario eliminado: {uname['username'] if uname else uid}")
    return jsonify({"ok": True})

# ── Main routes ───────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/api/employees", methods=["GET"])
@login_required
def get_employees():
    with get_db() as conn:
        rows = rows_as_dicts(conn.execute(text(
            "SELECT * FROM employees ORDER BY type, name"
        )))
    return jsonify(rows)

@app.route("/api/employees", methods=["POST"])
@admin_required
def add_employee():
    d = request.json
    try:
        with get_db() as conn:
            conn.execute(text(
                "INSERT INTO employees (name, type, specialist_start) VALUES (:n, :t, :s)"
            ), {"n": d["name"], "t": d["type"], "s": d.get("specialist_start")})
            conn.commit()
    except Exception:
        return jsonify({"error": "Ya existe un empleado con ese nombre"}), 400
    log_action("CREAR EMPLEADO", f"{d['name']} | Tipo: {d['type']}")
    return jsonify({"ok": True})

@app.route("/api/employees/<int:eid>", methods=["DELETE"])
@admin_required
def delete_employee(eid):
    with get_db() as conn:
        emp = row_as_dict(conn.execute(text("SELECT name FROM employees WHERE id=:id"), {"id": eid}))
        conn.execute(text("DELETE FROM employees WHERE id=:id"), {"id": eid})
        conn.execute(text("DELETE FROM vacations WHERE employee_id=:id"), {"id": eid})
        conn.commit()
    log_action("ELIMINAR EMPLEADO", f"{emp['name'] if emp else eid}")
    return jsonify({"ok": True})

@app.route("/api/vacations", methods=["GET"])
@login_required
def get_vacations():
    with get_db() as conn:
        rows = rows_as_dicts(conn.execute(text(
            "SELECT v.*, e.name as employee_name FROM vacations v "
            "JOIN employees e ON v.employee_id = e.id ORDER BY v.start_date"
        )))
    return jsonify(rows)

@app.route("/api/vacations", methods=["POST"])
@admin_required
def add_vacation():
    d = request.json
    with get_db() as conn:
        conn.execute(text(
            "INSERT INTO vacations (employee_id, start_date, end_date) VALUES (:eid, :s, :e)"
        ), {"eid": d["employee_id"], "s": d["start_date"], "e": d["end_date"]})
        conn.commit()
        emp = row_as_dict(conn.execute(text("SELECT name FROM employees WHERE id=:id"), {"id": d["employee_id"]}))
    log_action("ASIGNAR VACACIONES", f"{emp['name'] if emp else d['employee_id']} | {d['start_date']} → {d['end_date']}")
    return jsonify({"ok": True})

@app.route("/api/vacations/<int:vid>", methods=["DELETE"])
@admin_required
def delete_vacation(vid):
    with get_db() as conn:
        vac = row_as_dict(conn.execute(text(
            "SELECT v.start_date, v.end_date, e.name FROM vacations v JOIN employees e ON v.employee_id=e.id WHERE v.id=:id"
        ), {"id": vid}))
        conn.execute(text("DELETE FROM vacations WHERE id=:id"), {"id": vid})
        conn.commit()
    if vac:
        log_action("ELIMINAR VACACIONES", f"{vac['name']} | {vac['start_date']} → {vac['end_date']}")
    return jsonify({"ok": True})

@app.route("/api/holidays", methods=["GET"])
@login_required
def get_holidays():
    with get_db() as conn:
        rows = rows_as_dicts(conn.execute(text(
            "SELECT * FROM holidays ORDER BY date"
        )))
    return jsonify(rows)

@app.route("/api/holidays", methods=["POST"])
@admin_required
def add_holiday():
    d = request.json
    try:
        with get_db() as conn:
            conn.execute(text(
                "INSERT INTO holidays (date, name) VALUES (:d, :n)"
            ), {"d": d["date"], "n": d["name"]})
            conn.commit()
    except Exception:
        pass
    return jsonify({"ok": True})

@app.route("/api/holidays/<int:hid>", methods=["DELETE"])
@admin_required
def delete_holiday(hid):
    with get_db() as conn:
        conn.execute(text("DELETE FROM holidays WHERE id=:id"), {"id": hid})
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/generate", methods=["POST"])
@admin_required
def generate():
    d = request.json
    year, month = int(d["year"]), int(d["month"])

    with get_db() as conn:
        employees = rows_as_dicts(conn.execute(text("SELECT * FROM employees")))
        holidays  = [r[0] for r in conn.execute(text("SELECT date FROM holidays")).fetchall()]
        vacations = rows_as_dicts(conn.execute(text("SELECT * FROM vacations")))
        prev_month = month - 1 if month > 1 else 12
        prev_year  = year if month > 1 else year - 1

        prev_states = {}
        for r in rows_as_dicts(conn.execute(text(
            "SELECT * FROM month_state WHERE year=:y AND month=:m"
        ), {"y": prev_year, "m": prev_month})):
            prev_states[r["employee_id"]] = r
            prev_states[r["employee_id"]].setdefault("rest_served", 0)
            prev_states[r["employee_id"]].setdefault("block_remaining", 0)

        if not prev_states:
            prev_row = row_as_dict(conn.execute(text(
                "SELECT data FROM schedules WHERE year=:y AND month=:m ORDER BY id DESC LIMIT 1"
            ), {"y": prev_year, "m": prev_month}))
            if prev_row:
                prev_sched = json.loads(prev_row["data"])
                prev_assignments = prev_sched.get("assignments", {})
                prev_emps = [e for e in prev_sched.get("employees", []) if e["type"] == "rotativo"]
                num_prev  = calendar.monthrange(prev_year, prev_month)[1]
                prev_days = list(range(1, num_prev + 1))
                bp = {"PM": 4, "AM": 3, "NIGHT": 3}

                for emp in prev_emps:
                    eid = str(emp["id"])
                    dm  = prev_assignments.get(eid, {})
                    last_shift = last_work = None
                    trailing = 0
                    for dn in reversed(prev_days):
                        v = dm.get(str(dn), "L")
                        if v != "L":
                            last_shift = v
                            last_work  = date(prev_year, prev_month, dn).isoformat()
                            break
                        trailing += 1
                    block_done = 0
                    if last_shift:
                        found = False
                        for dn in reversed(prev_days):
                            v = dm.get(str(dn), "L")
                            if v == last_shift: found = True; block_done += 1
                            elif found: break
                    br = max(0, bp.get(last_shift, 0) - block_done) if last_shift else 0
                    st = {"last_shift": last_shift, "last_work_date": last_work,
                          "rest_served": trailing, "block_remaining": br}
                    prev_states[int(eid)] = st
                    conn.execute(text("""
                        INSERT INTO month_state
                            (year, month, employee_id, last_shift, last_work_date, rest_served, block_remaining)
                        VALUES (:y, :m, :eid, :ls, :ld, :rs, :br)
                        ON CONFLICT (year, month, employee_id) DO UPDATE SET
                            last_shift=EXCLUDED.last_shift,
                            last_work_date=EXCLUDED.last_work_date,
                            rest_served=EXCLUDED.rest_served,
                            block_remaining=EXCLUDED.block_remaining
                    """), {"y": prev_year, "m": prev_month, "eid": int(eid),
                           "ls": st["last_shift"], "ld": st["last_work_date"],
                           "rs": st["rest_served"], "br": st["block_remaining"]})
                conn.commit()

    try:
        schedule, _ = generate_schedule(year, month, employees, holidays, vacations, prev_states)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 400

    with get_db() as conn:
        conn.execute(text(
            "DELETE FROM schedules WHERE year=:y AND month=:m AND status='draft'"
        ), {"y": year, "m": month})
        conn.execute(text(
            "INSERT INTO schedules (year, month, data, status) VALUES (:y, :m, :d, 'draft')"
        ), {"y": year, "m": month, "d": json.dumps(schedule)})
        conn.commit()

    log_action("GENERAR HORARIO", f"{year}-{month:02d}")
    return jsonify({"schedule": schedule, "warnings": []})


@app.route("/api/approve", methods=["POST"])
@admin_required
def approve():
    d = request.json
    year, month = int(d["year"]), int(d["month"])
    schedule    = d["schedule"]

    with get_db() as conn:
        conn.execute(text(
            "UPDATE schedules SET status='approved', data=:data "
            "WHERE year=:y AND month=:m AND status='draft'"
        ), {"data": json.dumps(schedule), "y": year, "m": month})

        assignments = schedule.get("assignments", {})
        num_days    = calendar.monthrange(year, month)[1]
        days_list   = list(range(1, num_days + 1))
        block_pref  = {"PM": 4, "AM": 3, "NIGHT": 3}

        for emp_id in schedule.get("states", {}):
            dm = assignments.get(str(emp_id), {})
            last_shift = last_work_date = None
            rest_served = 0
            for day in reversed(days_list):
                v = dm.get(str(day), "L")
                if v != "L":
                    last_shift = v
                    last_work_date = date(year, month, day).isoformat()
                    break
                rest_served += 1
            block_done = 0
            if last_shift:
                found = False
                for day in reversed(days_list):
                    v = dm.get(str(day), "L")
                    if v == last_shift: found = True; block_done += 1
                    elif found: break
            block_remaining = max(0, block_pref.get(last_shift, 0) - block_done) if last_shift else 0

            conn.execute(text("""
                INSERT INTO month_state
                    (year, month, employee_id, last_shift, last_work_date, rest_served, block_remaining)
                VALUES (:y, :m, :eid, :ls, :ld, :rs, :br)
                ON CONFLICT (year, month, employee_id) DO UPDATE SET
                    last_shift=EXCLUDED.last_shift,
                    last_work_date=EXCLUDED.last_work_date,
                    rest_served=EXCLUDED.rest_served,
                    block_remaining=EXCLUDED.block_remaining
            """), {"y": year, "m": month, "eid": int(emp_id),
                   "ls": last_shift, "ld": last_work_date,
                   "rs": rest_served, "br": block_remaining})
        conn.commit()

    log_action("APROBAR HORARIO", f"{year}-{month:02d}")
    return jsonify({"ok": True})
@login_required
def get_schedule(year, month):
    with get_db() as conn:
        row = row_as_dict(conn.execute(text(
            "SELECT * FROM schedules WHERE year=:y AND month=:m ORDER BY id DESC LIMIT 1"
        ), {"y": year, "m": month}))
    if row:
        return jsonify({"schedule": json.loads(row["data"]), "status": row["status"]})
    return jsonify({"schedule": None, "status": None})


@app.route("/api/schedule/<int:year>/<int:month>", methods=["DELETE"])
@admin_required
def delete_schedule(year, month):
    with get_db() as conn:
        conn.execute(text("DELETE FROM schedules WHERE year=:y AND month=:m"), {"y": year, "m": month})
        conn.execute(text("DELETE FROM month_state WHERE year=:y AND month=:m"), {"y": year, "m": month})
        conn.commit()
    log_action("ELIMINAR HORARIO", f"{year}-{month:02d}")
    return jsonify({"ok": True})


@app.route("/api/schedule/cell", methods=["POST"])
@admin_required
def update_cell():
    d = request.json
    year, month = int(d["year"]), int(d["month"])

    with get_db() as conn:
        row = row_as_dict(conn.execute(text(
            "SELECT data FROM schedules WHERE year=:y AND month=:m ORDER BY id DESC LIMIT 1"
        ), {"y": year, "m": month}))
        if not row:
            return jsonify({"error": "No schedule found"}), 404

        schedule = json.loads(row["data"])
        schedule["assignments"].setdefault(str(d["employee_id"]), {})[str(d["day"])] = d["value"]

        days_in_month = schedule["days"]
        weekdays      = schedule["weekdays"]
        assignments   = schedule["assignments"]
        employees     = schedule["employees"]
        rotativos     = [e for e in employees if e["type"] == "rotativo"]
        especiales    = [e for e in employees if e["type"] == "especial"]

        especial_night_days = set(
            dn for dn, wd in zip(days_in_month, weekdays)
            if wd in ("Mon", "Tue", "Wed", "Thu") and especiales
        )

        new_summary  = {}
        new_overwork = []
        for emp in employees:
            eid     = str(emp["id"])
            day_map = assignments.get(eid, {})
            worked       = sum(1 for v in day_map.values() if v != "L")
            free_sundays = sum(
                1 for d2, wd in zip(days_in_month, weekdays)
                if wd == "Sun" and day_map.get(str(d2), "L") == "L"
            )
            new_summary[eid] = {"worked": worked, "free_sundays": free_sundays}
            if emp["type"] == "rotativo" and worked >= 20:
                new_overwork.append(eid)

        new_warnings = []
        all_operators = rotativos + especiales
        for day_num, wd in zip(days_in_month, weekdays):
            for shift in ["AM", "PM", "NIGHT"]:
                cnt = sum(
                    1 for emp in all_operators
                    if assignments.get(str(emp["id"]), {}).get(str(day_num), "L") == shift
                )
                if cnt == 0:
                    new_warnings.append({"day": day_num, "shift": shift, "count": 0,
                        "level": 4, "msg": f"Día {day_num} — {shift}: SIN COBERTURA"})
                elif cnt == 1:
                    lv = {"AM": 1, "PM": 2, "NIGHT": 3}[shift]
                    lb = {"AM": "baja", "PM": "media", "NIGHT": "baja"}[shift]
                    new_warnings.append({"day": day_num, "shift": shift, "count": 1,
                        "level": lv, "msg": f"Día {day_num} — {shift}: 1 persona ({lb})"})

        # Recalculate states for continuity
        num_days   = calendar.monthrange(year, month)[1]
        days_list  = list(range(1, num_days + 1))
        block_pref = {"PM": 4, "AM": 3, "NIGHT": 3}
        new_states = {}
        for emp in rotativos:
            eid = str(emp["id"])
            dm  = assignments.get(eid, {})
            last_shift = last_work_date = None
            rest_served = 0
            for day in reversed(days_list):
                v = dm.get(str(day), "L")
                if v != "L":
                    last_shift = v
                    last_work_date = date(year, month, day).isoformat()
                    break
                rest_served += 1
            block_done = 0
            if last_shift:
                found = False
                for day in reversed(days_list):
                    v = dm.get(str(day), "L")
                    if v == last_shift: found = True; block_done += 1
                    elif found: break
            block_remaining = max(0, block_pref.get(last_shift, 0) - block_done) if last_shift else 0
            new_states[eid] = {
                "last_shift": last_shift, "last_work_date": last_work_date,
                "rest_served": rest_served, "block_remaining": block_remaining
            }
            conn.execute(text("""
                INSERT INTO month_state
                    (year, month, employee_id, last_shift, last_work_date, rest_served, block_remaining)
                VALUES (:y, :m, :eid, :ls, :ld, :rs, :br)
                ON CONFLICT (year, month, employee_id) DO UPDATE SET
                    last_shift=EXCLUDED.last_shift,
                    last_work_date=EXCLUDED.last_work_date,
                    rest_served=EXCLUDED.rest_served,
                    block_remaining=EXCLUDED.block_remaining
            """), {"y": year, "m": month, "eid": int(eid),
                   "ls": last_shift, "ld": last_work_date,
                   "rs": rest_served, "br": block_remaining})

        schedule["states"]  = new_states
        schedule["summary"] = new_summary
        conn.execute(text(
            "UPDATE schedules SET data=:data WHERE year=:y AND month=:m"
        ), {"data": json.dumps(schedule), "y": year, "m": month})
        conn.commit()

    log_action("EDITAR CELDA", f"{year}-{month:02d} | Empleado ID {d['employee_id']} | Día {d['day']} → {d['value']}")
    return jsonify({"ok": True, "summary": new_summary,
                    "warnings": new_warnings, "overwork": new_overwork})


# ── Excel import ──────────────────────────────────────────────────────────────

@app.route("/api/import-excel", methods=["POST"])
@admin_required
def import_excel_route():
    if "file" not in request.files:
        return jsonify({"error": "No se recibió archivo"}), 400
    f    = request.files["file"]
    path = f"/tmp/horario_import_{f.filename}"
    f.save(path)
    try:
        result = import_excel(path)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        try: os.remove(path)
        except: pass

    year  = result["year"]
    month = result["month"]

    import unicodedata
    def normalize_name(n):
        n = n.lower().strip()
        n = unicodedata.normalize("NFD", n)
        n = "".join(c for c in n if unicodedata.category(c) != "Mn")
        return " ".join(n.split())

    with get_db() as conn:
        existing_employees = rows_as_dicts(conn.execute(text("SELECT id, name, type FROM employees")))
        norm_map = {normalize_name(e["name"]): e for e in existing_employees}

        new_employees = []
        id_map = {}

        for emp in result["employees"]:
            norm  = normalize_name(emp["name"])
            match = norm_map.get(norm)
            if not match:
                excel_parts = set(norm.split())
                best_score, best_emp = 0, None
                for db_norm, db_emp in norm_map.items():
                    db_parts = set(db_norm.split())
                    common   = excel_parts & db_parts
                    score    = len(common)
                    if score >= 2 and score > best_score:
                        best_score = score; best_emp = db_emp
                    elif score == 1 and len(excel_parts) == 1 and score > best_score:
                        best_score = score; best_emp = db_emp
                match = best_emp

            if match:
                id_map[emp["name"]] = match["id"]
            else:
                emp_type = emp.get("type", "rotativo")
                result2 = conn.execute(text(
                    "INSERT INTO employees (name, type, specialist_start) VALUES (:n, :t, :s) RETURNING id"
                ), {"n": emp["name"], "t": emp_type, "s": emp.get("specialist_start")})
                new_id = result2.fetchone()[0]
                id_map[emp["name"]] = new_id
                new_employees.append(emp["name"])
                norm_map[norm] = {"id": new_id, "name": emp["name"], "type": emp_type}

        conn.commit()

        day_objs = date_range(year, month)
        for emp in result["employees"]:
            if emp["name"] not in id_map: continue
            eid = id_map[emp["name"]]

            vac_days = sorted(emp.get("vacation_days", []))
            if vac_days:
                ranges = []
                start = end = vac_days[0]
                for dv in vac_days[1:]:
                    if dv == end + 1: end = dv
                    else: ranges.append((start, end)); start = end = dv
                ranges.append((start, end))
                for (s, e) in ranges:
                    sd = date(year, month, s).isoformat()
                    ed = date(year, month, e).isoformat()
                    exists = conn.execute(text(
                        "SELECT id FROM vacations WHERE employee_id=:eid AND start_date=:s AND end_date=:e"
                    ), {"eid": eid, "s": sd, "e": ed}).fetchone()
                    if not exists:
                        conn.execute(text(
                            "INSERT INTO vacations (employee_id, start_date, end_date) VALUES (:eid, :s, :e)"
                        ), {"eid": eid, "s": sd, "e": ed})

            if emp["type"] != "rotativo": continue
            conn.execute(text("""
                INSERT INTO month_state
                    (year, month, employee_id, last_shift, last_work_date, rest_served, block_remaining)
                VALUES (:y, :m, :eid, :ls, :ld, :rs, :br)
                ON CONFLICT (year, month, employee_id) DO UPDATE SET
                    last_shift=EXCLUDED.last_shift,
                    last_work_date=EXCLUDED.last_work_date,
                    rest_served=EXCLUDED.rest_served,
                    block_remaining=EXCLUDED.block_remaining
            """), {"y": year, "m": month, "eid": eid,
                   "ls": emp["last_shift"], "ld": emp["last_work_date"],
                   "rs": emp.get("rest_served", 0), "br": emp.get("block_remaining", 0)})

        conn.commit()

        assignments = {str(id_map[e["name"]]): e["assignments"]
                       for e in result["employees"] if e["name"] in id_map}
        schedule_eids = set(assignments.keys())
        all_employees = [r for r in rows_as_dicts(conn.execute(text("SELECT * FROM employees")))
                         if str(r["id"]) in schedule_eids]

        num_days  = calendar.monthrange(year, month)[1]
        days_list = list(range(1, num_days + 1))
        weekdays  = [date(year, month, dv).strftime("%a") for dv in days_list]

        summary = {}
        for emp in result["employees"]:
            eid     = id_map[emp["name"]]
            day_map = emp["assignments"]
            worked  = sum(1 for v in day_map.values() if v != "L")
            free_su = sum(1 for dv in day_objs if is_sunday(dv) and day_map.get(str(dv.day),"L") == "L")
            summary[str(eid)] = {"worked": worked, "free_sundays": free_su}

        states = {}
        for emp in result["employees"]:
            if emp["type"] != "rotativo": continue
            eid = id_map[emp["name"]]
            states[str(eid)] = {
                "last_shift":      emp["last_shift"],
                "last_work_date":  emp["last_work_date"],
                "rest_served":     emp.get("rest_served", 0),
                "block_remaining": emp.get("block_remaining", 0),
            }

        schedule = {
            "year": year, "month": month,
            "days": days_list, "weekdays": weekdays,
            "assignments": assignments, "employees": all_employees,
            "summary": summary, "states": states,
            "shift_hours": SHIFT_HOURS, "shift_colors": SHIFT_COLORS,
        }

        conn.execute(text("DELETE FROM schedules WHERE year=:y AND month=:m"), {"y": year, "m": month})
        conn.execute(text(
            "INSERT INTO schedules (year, month, data, status) VALUES (:y, :m, :d, 'approved')"
        ), {"y": year, "m": month, "d": json.dumps(schedule)})
        conn.commit()

    log_action("IMPORTAR EXCEL", f"{year}-{month:02d} | {len(result['employees'])} empleados")
    return jsonify({
        "ok": True, "year": year, "month": month,
        "employees_found": len(result["employees"]),
        "new_employees": new_employees,
        "warnings": result["warnings"],
        "schedule": schedule,
    })


# ── Audit log ─────────────────────────────────────────────────────────────────

@app.route("/api/audit-log", methods=["GET"])
@login_required
def get_audit_log():
    if session.get("role") != "superuser":
        return jsonify({"error": "Sin permisos"}), 403
    with get_db() as conn:
        rows = rows_as_dicts(conn.execute(text(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 500"
        )))
    return jsonify(rows)


if __name__ == "__main__":
    init_db()
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
