from flask import Flask, request, jsonify, send_file, render_template, redirect, url_for, session
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, csv, io, os

# Flask lädt Templates (index.html, admin.html, login.html) aus dem aktuellen Ordner
app = Flask(__name__, template_folder='.')
app.secret_key = os.environ.get("POS_SECRET", "please_change_me")

CURRENCY = "CHF"
DB_PATH = os.environ.get("POS_DB", "sales.db")

# ---------- DB ----------

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS sales(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price REAL NOT NULL,
            total REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            sort INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS payment_methods(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            sort INTEGER NOT NULL DEFAULT 0,
            protected INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            pin_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS sale_headers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            payment_method_id INTEGER NOT NULL,
            total REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(payment_method_id) REFERENCES payment_methods(id)
        );
        CREATE TABLE IF NOT EXISTS sale_lines(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price REAL NOT NULL,
            total REAL NOT NULL,
            FOREIGN KEY(sale_id) REFERENCES sale_headers(id)
        );
        """
    )

    # Neu: vorhandene Duplikate per Name aufräumen (behält jeweils die kleinste ID)
    c.execute("DELETE FROM items WHERE id NOT IN (SELECT MIN(id) FROM items GROUP BY name)")
    c.execute("DELETE FROM payment_methods WHERE id NOT IN (SELECT MIN(id) FROM payment_methods GROUP BY name)")

    # Neu: eindeutige Indizes gegen zukünftige Duplikate
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_items_name ON items(name)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_pm_name ON payment_methods(name)")

    # Seed Items (mit OR IGNORE abgesichert)
    if c.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
        c.executemany(
            "INSERT OR IGNORE INTO items(name,price,active,sort) VALUES(?,?,?,?)",
            [
                ("Hot Dog", 6.00, 1, 0),
                ("Veggie Dog", 6.50, 1, 1),
                ("Getränk", 3.00, 1, 2),
                ("Kombi (Dog+Drink)", 8.50, 1, 3),
            ],
        )

    # Seed Payment Methods (Bar geschützt) – mit OR IGNORE
    if c.execute("SELECT COUNT(*) FROM payment_methods").fetchone()[0] == 0:
        c.executemany(
            "INSERT OR IGNORE INTO payment_methods(name,active,sort,protected) VALUES(?,?,?,?)",
            [("Bar", 1, 0, 1), ("Twint", 1, 1, 0), ("Karte", 1, 2, 0)],
        )

    # Seed Users (username bereits UNIQUE)
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        c.execute(
            "INSERT INTO users(username,pin_hash,is_admin,active) VALUES(?,?,?,1)",
            ("Admin", generate_password_hash("1234"), 1),
        )
        c.execute(
            "INSERT INTO users(username,pin_hash,is_admin,active) VALUES(?,?,?,1)",
            ("Kasse", generate_password_hash("0000"), 0),
        )

    c.commit(); c.close()


init_db()

# ---------- Helpers ----------

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    c = conn()
    u = c.execute(
        "SELECT id, username, is_admin FROM users WHERE id=? AND active=1", (uid,)
    ).fetchone()
    c.close()
    return dict(u) if u else None


# ---------- Auth ----------
@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    pin = (data.get("pin") or "").strip()
    c = conn()
    u = c.execute(
        "SELECT id, username, pin_hash, is_admin FROM users WHERE username=? AND active=1",
        (username,),
    ).fetchone()
    c.close()
    if not u or not check_password_hash(u["pin_hash"], pin):
        return jsonify(ok=False, msg="Benutzer oder PIN falsch."), 401
    session["user_id"] = u["id"]
    return jsonify(ok=True, is_admin=bool(u["is_admin"]))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ---------- Views ----------
@app.route("/")
def index():
    user = current_user()
    if not user:
        return redirect(url_for("login_page"))
    c = conn()
    items = [
        dict(r)
        for r in c.execute(
            "SELECT id, name, price FROM items WHERE active=1 ORDER BY sort, id"
        ).fetchall()
    ]
    # >>> Wichtig: protected mitgeben! <<<
    pay_methods = [
        dict(r)
        for r in c.execute(
            "SELECT id, name, protected FROM payment_methods WHERE active=1 ORDER BY sort, id"
        ).fetchall()
    ]
    c.close()
    return render_template(
        "pos.html", items=items, currency=CURRENCY, user=user, pay_methods=pay_methods
    )


@app.route("/admin")
def admin():
    user = current_user()
    if not user or not user.get("is_admin"):
        return redirect(url_for("index"))
    return render_template("admin.html")


# ---------- POS Actions ----------
@app.route("/sale", methods=["POST"])
def sale():
    user = current_user()
    if not user:
        return jsonify(ok=False, msg="Nicht angemeldet"), 401

    data = request.get_json(silent=True) or {}
    lines = data.get("lines", [])
    payment_method_id = int(data.get("payment_method_id")) if data.get("payment_method_id") else None
    if not lines:
        return jsonify(ok=False, msg="Keine Artikel"), 400
    if not payment_method_id:
        return jsonify(ok=False, msg="Zahlart fehlt"), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c = conn(); cur = c.cursor()

    # Validate payment method
    if not cur.execute(
        "SELECT id FROM payment_methods WHERE id=? AND active=1", (payment_method_id,)
    ).fetchone():
        c.close(); return jsonify(ok=False, msg="Ungültige Zahlart"), 400

    cart_total = 0.0; norm_lines = []
    for line in lines:
        try:
            item_id = int(line.get("item_id"))
        except (TypeError, ValueError):
            continue
        qty = int(line.get("qty", 1))
        it = cur.execute(
            "SELECT name, price FROM items WHERE id=? AND active=1", (item_id,)
        ).fetchone()
        if not it:
            continue
        name, price = it["name"], float(it["price"])
        total = qty * price
        cart_total += total
        norm_lines.append(
            {"item_id": item_id, "item_name": name, "qty": qty, "price": price, "total": total}
        )

    if not norm_lines:
        c.close(); return jsonify(ok=False, msg="Keine gültigen Artikel"), 400

    # Header
    cur.execute(
        "INSERT INTO sale_headers(ts,user_id,payment_method_id,total) VALUES(?,?,?,?)",
        (now, user["id"], payment_method_id, cart_total),
    )
    sale_id = cur.lastrowid

    # Lines + Kompatibilitätstabelle
    for ln in norm_lines:
        cur.execute(
            "INSERT INTO sale_lines(sale_id,item_id,item_name,qty,price,total) VALUES(?,?,?,?,?,?)",
            (sale_id, ln["item_id"], ln["item_name"], ln["qty"], ln["price"], ln["total"]),
        )
        cur.execute(
            "INSERT INTO sales(ts,item_id,item_name,qty,price,total) VALUES(?,?,?,?,?,?)",
            (now, ln["item_id"], ln["item_name"], ln["qty"], ln["price"], ln["total"]),
        )

    c.commit(); c.close()
    return jsonify(ok=True, sale_id=sale_id, total=round(cart_total, 2))


@app.route("/undo", methods=["POST"])
def undo():
    c = conn()
    last = c.execute("SELECT id FROM sale_headers ORDER BY id DESC LIMIT 1").fetchone()
    if not last:
        c.close(); return jsonify(ok=False, msg="Nichts zu löschen.")
    sale_id = last[0]
    ts_row = c.execute("SELECT ts FROM sale_headers WHERE id=?", (sale_id,)).fetchone()
    ts = ts_row[0] if ts_row else None
    c.execute("DELETE FROM sale_lines WHERE sale_id=?", (sale_id,))
    c.execute("DELETE FROM sale_headers WHERE id=?", (sale_id,))
    if ts:
        c.execute("DELETE FROM sales WHERE ts=?", (ts,))
    c.commit(); c.close()
    return jsonify(ok=True)


@app.route("/export.csv")
def export_csv():
    c = conn()
    # Export jetzt auf Basis der normalisierten Tabellen inkl. sale_id
    rows = c.execute(
        """
        SELECT h.id AS sale_id, h.ts, l.item_name, l.qty, l.price, l.total
        FROM sale_lines l
        JOIN sale_headers h ON h.id = l.sale_id
        ORDER BY h.id ASC, l.id ASC
        """
    ).fetchall()
    c.close()

    buf = io.StringIO(); w = csv.writer(buf, delimiter=';')
    # Header inkl. SaleID
    w.writerow(["SaleID","Zeit","Artikel","Menge","Preis","Gesamt"])
    for r in rows:
        w.writerow([r["sale_id"], r["ts"], r["item_name"], r["qty"], f"{r['price']:.2f}", f"{r['total']:.2f}"])
    mem = io.BytesIO(buf.getvalue().encode("utf-8")); mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="verkauf.csv")


@app.route("/export_purchases.csv")
def export_purchases_csv():
    c = conn()
    
    # Query-Parameter (gleiche wie bei /api/admin/purchases)
    start = request.args.get('start')
    end = request.args.get('end')
    payment_method_id = request.args.get('payment_method_id')
    user_id = request.args.get('user_id')
    group = request.args.get('group', '1') in ('1', 'true', 'yes')
    
    # WHERE-Klausel bauen
    where_clauses = []
    params = []
    if start:
        where_clauses.append("date(h.ts) >= ?")
        params.append(start)
    if end:
        where_clauses.append("date(h.ts) <= ?")
        params.append(end)
    if payment_method_id:
        where_clauses.append("h.payment_method_id = ?")
        params.append(payment_method_id)
    if user_id:
        where_clauses.append("h.user_id = ?")
        params.append(user_id)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    
    if group:
        # Gruppierte Ansicht: eine Zeile pro Bestellung
        w.writerow(["Bestellung-ID", "Zeit", "Artikel", "Zahlart", "Benutzer", "Gesamt"])
        headers = c.execute(
            "SELECT h.id, h.ts, h.total, u.username as user, p.name as payment_method "
            "FROM sale_headers h "
            "LEFT JOIN users u ON u.id=h.user_id "
            "LEFT JOIN payment_methods p ON p.id=h.payment_method_id "
            f"{where_sql} "
            "ORDER BY h.id DESC", params
        ).fetchall()
        
        lines_rows = c.execute(
            "SELECT l.sale_id, l.item_name, l.qty "
            "FROM sale_lines l JOIN sale_headers h ON h.id=l.sale_id "
            f"{where_sql} "
            "ORDER BY l.id ASC", params
        ).fetchall()
        
        grouped = {}
        for l in lines_rows:
            sid = l['sale_id']
            grouped.setdefault(sid, []).append(l)
        
        for h in headers:
            items_list = grouped.get(h["id"], [])
            items_str = ", ".join([f"{l['qty']}x {l['item_name']}" for l in items_list])
            w.writerow([
                h["id"],
                h["ts"],
                items_str,
                h["payment_method"] or "",
                h["user"] or "",
                f"{h['total']:.2f}"
            ])
    else:
        # Einzelposten: jede Zeile ist ein Item
        w.writerow(["Bestellung-ID", "Zeit", "Artikel", "Menge", "Preis", "Zahlart", "Benutzer", "Gesamt"])
        rows = c.execute(
            "SELECT l.sale_id, h.ts, l.item_name, l.qty, l.price, l.total, "
            "u.username as user, p.name as payment_method "
            "FROM sale_lines l "
            "JOIN sale_headers h ON h.id=l.sale_id "
            "LEFT JOIN users u ON u.id=h.user_id "
            "LEFT JOIN payment_methods p ON p.id=h.payment_method_id "
            f"{where_sql} "
            "ORDER BY h.id DESC, l.id ASC", params
        ).fetchall()
        
        for r in rows:
            w.writerow([
                r["sale_id"],
                r["ts"],
                r["item_name"],
                r["qty"],
                f"{r['price']:.2f}",
                r["payment_method"] or "",
                r["user"] or "",
                f"{r['total']:.2f}"
            ])
    
    c.close()
    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="kaeufe.csv")


# ---------- Admin APIs ----------
@app.route("/api/items")
def api_items_list():
    c = conn(); items = [dict(r) for r in c.execute(
        "SELECT id,name,price,active,sort FROM items ORDER BY sort,id"
    ).fetchall()]; c.close()
    for it in items: it["active"] = bool(it["active"])  # 0/1 -> True/False
    return jsonify(items=items)


@app.route("/api/items/bulk", methods=["POST"])
def api_items_bulk():
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    if not isinstance(items, list):
        return jsonify(ok=False, msg="Invalid payload"), 400
    c = conn(); cur = c.cursor()
    for it in items:
        _id = it.get("id"); _delete = bool(it.get("delete"))
        name = (it.get("name") or "").strip()
        price = float(it.get("price") or 0)
        active = 1 if it.get("active") else 0
        sort = int(it.get("sort") or 0)
        if _id and _delete:
            cur.execute("DELETE FROM items WHERE id=?", (_id,))
        elif _id:
            cur.execute(
                "UPDATE items SET name=?, price=?, active=?, sort=? WHERE id=?",
                (name, price, active, sort, _id),
            )
        else:
            cur.execute(
                "INSERT INTO items(name, price, active, sort) VALUES(?,?,?,?)",
                (name, price, active, sort),
            )
    c.commit(); c.close(); return jsonify(ok=True)


@app.route("/api/payment_methods")
def api_pm_list():
    c = conn(); rows = [dict(r) for r in c.execute(
        "SELECT id,name,active,sort,protected FROM payment_methods ORDER BY sort,id"
    ).fetchall()]; c.close()
    for r in rows: r["active"] = bool(r["active"]); r["protected"] = bool(r["protected"])
    return jsonify(methods=rows)


@app.route("/api/payment_methods/bulk", methods=["POST"])
def api_pm_bulk():
    data = request.get_json(silent=True) or {}
    methods = data.get("methods", [])
    if not isinstance(methods, list):
        return jsonify(ok=False, msg="Invalid payload"), 400
    c = conn(); cur = c.cursor()
    for m in methods:
        _id = m.get("id"); _delete = bool(m.get("delete"))
        name = (m.get("name") or "").strip()
        active = 1 if m.get("active") else 0
        sort = int(m.get("sort") or 0)
        protected = 1 if m.get("protected") else 0
        is_protected = False
        if _id:
            row = cur.execute("SELECT protected FROM payment_methods WHERE id=?", (_id,)).fetchone()
            is_protected = bool(row[0]) if row else False
        if _id and _delete:
            if is_protected:  # Bar nicht löschen
                continue
            cur.execute("DELETE FROM payment_methods WHERE id=?", (_id,))
        elif _id:
            if is_protected:
                cur.execute(
                    "UPDATE payment_methods SET name=?, active=?, sort=? WHERE id=?",
                    (name or "Bar", active, sort, _id),
                )
            else:
                cur.execute(
                    "UPDATE payment_methods SET name=?, active=?, sort=?, protected=? WHERE id=?",
                    (name, active, sort, protected, _id),
                )
        else:
            cur.execute(
                "INSERT INTO payment_methods(name,active,sort,protected) VALUES(?,?,?,?)",
                (name, active, sort, protected),
            )
    c.commit(); c.close(); return jsonify(ok=True)


@app.route("/api/users")
def api_users_list():
    c = conn(); rows = [dict(r) for r in c.execute(
        "SELECT id,username,is_admin,active FROM users ORDER BY username"
    ).fetchall()]; c.close()
    for r in rows: r["is_admin"] = bool(r["is_admin"]); r["active"] = bool(r["active"])
    return jsonify(users=rows)


@app.route("/api/users/bulk", methods=["POST"])
def api_users_bulk():
    data = request.get_json(silent=True) or {}
    users = data.get("users", [])
    if not isinstance(users, list):
        return jsonify(ok=False, msg="Invalid payload"), 400

    c = conn(); cur = c.cursor()

    # Anzahl aktiver Admins ermitteln
    admin_count = cur.execute(
        "SELECT COUNT(*) FROM users WHERE is_admin=1 AND active=1"
    ).fetchone()[0]

    skipped = []  # Nutzer, bei denen Admin-Schutz gegriffen hat

    for u in users:
        _id = u.get("id")
        _delete = bool(u.get("delete"))
        username = (u.get("username") or "").strip()
        is_admin_new = 1 if u.get("is_admin") else 0
        active_new = 1 if u.get("active") else 0
        new_pin = (u.get("pin") or "").strip()

        if _id and _delete:
            row = cur.execute("SELECT username,is_admin,active FROM users WHERE id=?", (_id,)).fetchone()
            if not row:
                continue
            was_admin_active = bool(row["is_admin"]) and bool(row["active"])
            if was_admin_active and admin_count <= 1:
                skipped.append(row["username"] or f"#{_id}")
                continue
            cur.execute("DELETE FROM users WHERE id=?", (_id,))
            if was_admin_active:
                admin_count -= 1
            continue

        if _id:
            row = cur.execute("SELECT username,is_admin,active FROM users WHERE id=?", (_id,)).fetchone()
            if not row:
                continue
            was_admin_active = bool(row["is_admin"]) and bool(row["active"])
            will_be_admin_active = bool(is_admin_new) and bool(active_new)

            # Würde diese Änderung den letzten aktiven Admin "verlieren"?
            if was_admin_active and not will_be_admin_active and admin_count <= 1:
                # Admin-/Aktiv-Flags erzwingen, damit mindestens 1 Admin bleibt
                skipped.append(row["username"] or f"#{_id}")
                if new_pin:
                    cur.execute(
                        "UPDATE users SET username=?, is_admin=1, active=1, pin_hash=? WHERE id=?",
                        (username, generate_password_hash(new_pin), _id),
                    )
                else:
                    cur.execute(
                        "UPDATE users SET username=?, is_admin=1, active=1 WHERE id=?",
                        (username, _id),
                    )
                # admin_count bleibt unverändert (weiterhin Admin aktiv)
                continue

            # Änderungen sind erlaubt -> ggf. Zähler anpassen
            became_admin_active = (not was_admin_active) and will_be_admin_active
            left_admin_active = was_admin_active and (not will_be_admin_active)
            if became_admin_active:
                admin_count += 1
            if left_admin_active:
                admin_count -= 1

            if new_pin:
                cur.execute(
                    "UPDATE users SET username=?, is_admin=?, active=?, pin_hash=? WHERE id=?",
                    (username, is_admin_new, active_new, generate_password_hash(new_pin), _id),
                )
            else:
                cur.execute(
                    "UPDATE users SET username=?, is_admin=?, active=? WHERE id=?",
                    (username, is_admin_new, active_new, _id),
                )
            continue

        # Neuer Benutzer
        if not new_pin:
            new_pin = "0000"
        cur.execute(
            "INSERT INTO users(username,pin_hash,is_admin,active) VALUES(?,?,?,?)",
            (username, generate_password_hash(new_pin), is_admin_new, active_new),
        )
        if is_admin_new and active_new:
            admin_count += 1

    c.commit(); c.close()
    # Optional: Warnungen zurückgeben (Frontend nutzt aktuell nur ok)
    return jsonify(ok=True, warn=(skipped if skipped else None))


@app.route("/api/admin/summary")
def api_admin_summary():
    c = conn()
    rows = [
        dict(r)
        for r in c.execute(
            "SELECT ts, item_name, qty, price, total FROM sales WHERE date(ts)=date('now','localtime') ORDER BY id DESC"
        ).fetchall()
    ]
    per_item = [
        dict(r)
        for r in c.execute(
            "SELECT item_name, SUM(qty) as qty, SUM(total) as total FROM sales WHERE date(ts)=date('now','localtime') GROUP BY item_name ORDER BY qty DESC"
        ).fetchall()
    ]
    # Neu: letzte 5 Bestellungen (Einzelposten, farblich gruppierbar via sale_id)
    last_ids = [r["id"] for r in c.execute(
        "SELECT id FROM sale_headers ORDER BY id DESC LIMIT 5"
    ).fetchall()]
    last5_rows = []
    if last_ids:
        q_marks = ",".join(["?"] * len(last_ids))
        last5_rows = [dict(r) for r in c.execute(
            f"SELECT l.sale_id as sale_id, h.ts, l.item_name, l.qty, l.price, l.total, "
            f"u.username as user, p.name as payment_method "
            f"FROM sale_lines l "
            f"JOIN sale_headers h ON h.id=l.sale_id "
            f"LEFT JOIN users u ON u.id=h.user_id "
            f"LEFT JOIN payment_methods p ON p.id=h.payment_method_id "
            f"WHERE h.id IN ({q_marks}) "
            f"ORDER BY h.id DESC, l.id ASC", last_ids
        ).fetchall()]
        for r in last5_rows:
            r["price"] = float(r["price"])
            r["total"] = float(r["total"])

    total = sum(r["total"] for r in rows) if rows else 0
    distinct_ts = {r["ts"] for r in rows}
    count = len(distinct_ts)
    c.close()
    return jsonify(
        ok=True,
        date_label=datetime.now().strftime("%Y-%m-%d"),
        rows=rows,
        per_item=per_item,
        last5_rows=last5_rows,  # neu
        total=round(total, 2),
        count=count,
        currency=CURRENCY,
    )


# Neuer Endpoint: verfügbare Tage (max. 5) mit vorhandenen Verkäufen
@app.route("/api/admin/available_days")
def api_admin_available_days():
    c = conn()
    days = [dict(date=r["d"], count=r["cnt"]) for r in c.execute(
        "SELECT date(ts) AS d, COUNT(*) AS cnt FROM sale_headers GROUP BY d ORDER BY d DESC LIMIT 5"
    ).fetchall()]
    c.close()
    return jsonify(ok=True, days=days)

# Neuer Endpoint: alle Käufe gruppiert (in Reihenfolge wie gespeichert) + Filter/Group-Option
@app.route("/api/admin/purchases")
def api_admin_purchases():
    c = conn()

    # Query-Parameter
    start = request.args.get('start')   # YYYY-MM-DD
    end = request.args.get('end')       # YYYY-MM-DD
    last = request.args.get('last')     # int Tage (optional)
    payment_method_id = request.args.get('payment_method_id')
    user_id = request.args.get('user_id')
    group = request.args.get('group', '1') in ('1', 'true', 'yes')

    # Wenn 'last' gesetzt: berechne start (inklusive) als heute - (last-1)
    if last and not (start or end):
        try:
            n = int(last)
            start_date = (datetime.now() - timedelta(days=n-1)).date().isoformat()
            start = start_date
            end = datetime.now().date().isoformat()
        except Exception:
            pass

    # Baue WHERE-Klausel für sale_headers
    where_clauses = []
    params = []
    if start:
        where_clauses.append("date(h.ts) >= ?")
        params.append(start)
    if end:
        where_clauses.append("date(h.ts) <= ?")
        params.append(end)
    if payment_method_id:
        where_clauses.append("h.payment_method_id = ?")
        params.append(payment_method_id)
    if user_id:
        where_clauses.append("h.user_id = ?")
        params.append(user_id)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    if group:
        # Lade Header (chronologisch neueste zuerst)
        headers = [dict(r) for r in c.execute(
            "SELECT h.id, h.ts, h.user_id, h.payment_method_id, h.total, u.username as user, p.name as payment_method "
            "FROM sale_headers h "
            "LEFT JOIN users u ON u.id=h.user_id "
            "LEFT JOIN payment_methods p ON p.id=h.payment_method_id "
            f"{where_sql} "
            "ORDER BY h.id DESC", params
        ).fetchall()]
        # Alle passenden lines in gespeicherter Reihenfolge (l.id) holen
        lines_rows = [dict(r) for r in c.execute(
            "SELECT l.sale_id, l.item_name, l.qty, l.price, l.total "
            "FROM sale_lines l JOIN sale_headers h ON h.id=l.sale_id "
            f"{where_sql.replace('h.','h.')} "  # reuse same filters (h.)
            "ORDER BY l.id ASC", params
        ).fetchall()]
        c.close()
        grouped = {}
        for l in lines_rows:
            sid = l['sale_id']
            grouped.setdefault(sid, []).append(l)
        purchases = []
        for h in headers:
            purchases.append({
                "id": h["id"],
                "ts": h["ts"],
                "user": h.get("user"),
                "payment_method": h.get("payment_method"),
                "total": float(h["total"]),
                "lines": grouped.get(h["id"], [])
            })
        return jsonify(ok=True, grouped=True, purchases=purchases, currency=CURRENCY)
    else:
        # Einzelne Posten: jede sale_lines Zeile mit Header-Infos
        rows = [dict(r) for r in c.execute(
            "SELECT l.sale_id as sale_id, h.ts, l.item_name, l.qty, l.price, l.total, "
            "u.username as user, p.name as payment_method "
            "FROM sale_lines l "
            "JOIN sale_headers h ON h.id=l.sale_id "
            "LEFT JOIN users u ON u.id=h.user_id "
            "LEFT JOIN payment_methods p ON p.id=h.payment_method_id "
            f"{where_sql} "
            "ORDER BY h.id DESC, l.id ASC", params
        ).fetchall()]
        c.close()
        for r in rows:
            r['total'] = float(r['total'])
        return jsonify(ok=True, grouped=False, rows=rows, currency=CURRENCY)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)