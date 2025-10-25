from flask import Flask, request, jsonify, send_file, render_template, redirect, url_for, session
from datetime import datetime
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

    # Seed Items
    if c.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
        c.executemany(
            "INSERT INTO items(name,price,active,sort) VALUES(?,?,?,?)",
            [
                ("Hot Dog", 6.00, 1, 0),
                ("Veggie Dog", 6.50, 1, 1),
                ("Getränk", 3.00, 1, 2),
                ("Kombi (Dog+Drink)", 8.50, 1, 3),
            ],
        )

    # Seed Payment Methods (Bar geschützt)
    if c.execute("SELECT COUNT(*) FROM payment_methods").fetchone()[0] == 0:
        c.executemany(
            "INSERT INTO payment_methods(name,active,sort,protected) VALUES(?,?,?,?)",
            [("Bar", 1, 0, 1), ("Twint", 1, 1, 0), ("Karte", 1, 2, 0)],
        )

    # Seed Users
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
    c = conn(); rows = c.execute(
        "SELECT ts,item_name,qty,price,total FROM sales ORDER BY id"
    ).fetchall(); c.close()
    buf = io.StringIO(); w = csv.writer(buf, delimiter=';')
    w.writerow(["Zeit","Artikel","Menge","Preis","Gesamt"])
    for r in rows:
        w.writerow([r["ts"], r["item_name"], r["qty"], f"{r['price']:.2f}", f"{r['total']:.2f}"])
    mem = io.BytesIO(buf.getvalue().encode("utf-8")); mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="verkauf.csv")


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
    for u in users:
        _id = u.get("id"); _delete = bool(u.get("delete"))
        username = (u.get("username") or "").strip()
        is_admin = 1 if u.get("is_admin") else 0
        active = 1 if u.get("active") else 0
        new_pin = (u.get("pin") or "").strip()
        if _id and _delete:
            cur.execute("DELETE FROM users WHERE id=?", (_id,)); continue
        if _id:
            if new_pin:
                cur.execute(
                    "UPDATE users SET username=?, is_admin=?, active=?, pin_hash=? WHERE id=?",
                    (username, is_admin, active, generate_password_hash(new_pin), _id),
                )
            else:
                cur.execute(
                    "UPDATE users SET username=?, is_admin=?, active=? WHERE id=?",
                    (username, is_admin, active, _id),
                )
        else:
            if not new_pin:
                new_pin = "0000"
            cur.execute(
                "INSERT INTO users(username,pin_hash,is_admin,active) VALUES(?,?,?,?)",
                (username, generate_password_hash(new_pin), is_admin, active),
            )
    c.commit(); c.close(); return jsonify(ok=True)


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
    total = sum(r["total"] for r in rows) if rows else 0
    distinct_ts = {r["ts"] for r in rows}
    count = len(distinct_ts)
    c.close()
    return jsonify(
        ok=True,
        date_label=datetime.now().strftime("%Y-%m-%d"),
        rows=rows,
        per_item=per_item,
        total=round(total, 2),
        count=count,
        currency=CURRENCY,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
  # Debug AUS, kein Auto-Reload, Port 8000
    app.run(host='0.0.0.0', port=8000, debug=False, use_reloader=False)
