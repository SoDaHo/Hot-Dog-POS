from __future__ import annotations
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from datetime import datetime, timedelta
import sqlite3
from pathlib import Path

# Verzeichnisse/DB
APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / 'sales.db'

# Flask-App (Templates im selben Ordner)
app = Flask(__name__, template_folder=str(APP_DIR), static_folder=str(APP_DIR))
app.secret_key = 'change-me'
CURRENCY = 'CHF'

# ----------------------------
# DB helpers
# ----------------------------
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_methods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  protected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  pin TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  payment_method_id INTEGER NOT NULL,
  total REAL NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id),
  FOREIGN KEY(payment_method_id) REFERENCES payment_methods(id)
);
CREATE TABLE IF NOT EXISTS sale_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sale_id INTEGER NOT NULL,
  item_id INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  price REAL NOT NULL,
  FOREIGN KEY(sale_id) REFERENCES items(id),
  FOREIGN KEY(item_id) REFERENCES items(id)
);
"""

SEED_SQL = [
    ("INSERT OR IGNORE INTO items (id,name,price) VALUES (1,'Hot Dog',9.0)",),
    ("INSERT OR IGNORE INTO items (id,name,price) VALUES (2,'Hot Dog Kids',6.0)",),
    ("INSERT OR IGNORE INTO items (id,name,price) VALUES (3,'Hot Dog Veggie',7.0)",),
    ("INSERT OR IGNORE INTO items (id,name,price) VALUES (4,'Getränk',8.0)",),
    ("INSERT OR IGNORE INTO payment_methods (id,name,protected) VALUES (1,'Bar',1)",),
    ("INSERT OR IGNORE INTO users (id,username,pin,is_admin) VALUES (1,'Admin','0000',1)",),
]

def init_db():
    con = get_db()
    with con:
        con.executescript(SCHEMA_SQL)
        for (sql,) in SEED_SQL:
            con.execute(sql)
    con.close()

# Beim Start initialisieren
with app.app_context():
    init_db()

# ----------------------------
# Auth – API Login für login.tpl
# ----------------------------
@app.get('/api/users')
def api_users():
    con = get_db()
    rows = con.execute('SELECT id, username, is_admin FROM users ORDER BY id').fetchall()
    users = [{'id': r['id'], 'username': r['username'], 'is_admin': bool(r['is_admin']), 'active': True} for r in rows]
    return jsonify({'users': users})

@app.post('/api/login')
def api_login():
    data = request.get_json(force=True) or {}
    username = (data.get('username') or '').strip()
    pin = (data.get('pin') or '').strip()
    if not username or not pin:
        return jsonify({'ok': False, 'msg': 'Benutzer/PIN fehlt'}), 400
    con = get_db()
    row = con.execute('SELECT id, username, is_admin, pin FROM users WHERE username=?', (username,)).fetchone()
    if not row or row['pin'] != pin:
        return jsonify({'ok': False, 'msg': 'PIN falsch'}), 401
    session['user'] = {'id': row['id'], 'username': row['username'], 'is_admin': bool(row['is_admin'])}
    return jsonify({'ok': True})

@app.get('/favicon.ico')
def favicon():
    return ('', 204)

# ----------------------------
# Seiten
# ----------------------------
@app.get('/login')
def login_page():
    return render_template('login.html')

@app.get('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

def require_login():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    return None

@app.route('/')
def index():
    guard = require_login()
    if guard: return guard
    con = get_db()
    items = [dict(r) for r in con.execute('SELECT id,name,price FROM items ORDER BY id')]
    pay_methods = [dict(r) for r in con.execute('SELECT id,name,protected FROM payment_methods ORDER BY id')]
    return render_template('pos.html', items=items, pay_methods=pay_methods, currency=CURRENCY, user=session['user'])

@app.route('/admin')
def admin_page():
    guard = require_login()
    if guard: return guard
    if not session['user'].get('is_admin'):
        return redirect(url_for('index'))
    return render_template('admin.html', currency=CURRENCY)

# ----------------------------
# Verkauf speichern (POS)
# ----------------------------
@app.post('/sale')
def create_sale():
    guard = require_login()
    if guard: return guard
    data = request.get_json(force=True)
    pm_id = int(data['payment_method_id'])
    lines = data.get('lines', [])
    con = get_db()
    items_map = { r['id']: r for r in con.execute('SELECT id, price FROM items WHERE id IN (%s)' % ','.join(['?']*len(lines)), [int(x['item_id']) for x in lines]) } if lines else {}
    total = 0.0
    norm = []
    for ln in lines:
        iid = int(ln['item_id']); qty = int(ln['qty'])
        if qty <= 0 or iid not in items_map: continue
        price = float(items_map[iid]['price'])
        total += price * qty
        norm.append((iid, qty, price))
    if total <= 0:
        return jsonify(ok=False, msg='Leerer Verkauf'), 400
    now = datetime.utcnow().isoformat()
    with con:
        cur = con.execute('INSERT INTO sales (user_id,payment_method_id,total,created_at) VALUES (?,?,?,?)', (session['user']['id'], pm_id, total, now))
        sale_id = cur.lastrowid
        con.executemany('INSERT INTO sale_lines (sale_id,item_id,qty,price) VALUES (?,?,?,?)', [(sale_id, iid, qty, price) for (iid, qty, price) in norm])
    return jsonify(ok=True, sale_id=sale_id)

# ----------------------------
# Admin APIs (heute) & CRUD
# ----------------------------
def _day_bounds_local(dt: datetime) -> tuple[str, str]:
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()

@app.get('/api/admin/today')
def api_today():
    guard = require_login()
    if guard: return guard
    if not session['user'].get('is_admin'):
        return jsonify({'error': 'forbidden'}), 403

    con = get_db()
    start, end = _day_bounds_local(datetime.now())
    kpi = con.execute('SELECT COUNT(*) AS orders, IFNULL(SUM(total),0) AS amount FROM sales WHERE created_at>=? AND created_at<?', (start, end)).fetchone()
    lines_count = con.execute('SELECT IFNULL(SUM(qty),0) AS lines FROM sale_lines sl JOIN sales s ON s.id=sl.sale_id WHERE s.created_at>=? AND s.created_at<?', (start, end)).fetchone()
    items = con.execute('''
        SELECT i.id, i.name, IFNULL(SUM(sl.qty),0) AS qty, IFNULL(SUM(sl.qty*sl.price),0) AS amount
        FROM items i
        LEFT JOIN sale_lines sl ON sl.item_id=i.id
        LEFT JOIN sales s ON s.id=sl.sale_id AND s.created_at>=? AND s.created_at<?
        GROUP BY i.id, i.name
        ORDER BY i.id
    ''', (start, end)).fetchall()
    orders = con.execute('''
        SELECT s.id, s.created_at, s.total, pm.name AS payment_name
        FROM sales s JOIN payment_methods pm ON pm.id=s.payment_method_id
        WHERE s.created_at>=? AND s.created_at<?
        ORDER BY s.id DESC LIMIT 20
    ''', (start, end)).fetchall()

    recent = []
    for o in orders:
        lines = con.execute('SELECT i.name, SUM(sl.qty) AS qty FROM sale_lines sl JOIN items i ON i.id=sl.item_id WHERE sl.sale_id=? GROUP BY i.name', (o['id'],)).fetchall()
        recent.append({'id': o['id'], 'created_at': o['created_at'], 'total': o['total'], 'payment_name': o['payment_name'], 'lines': [{'name': r['name'], 'qty': r['qty']} for r in lines]})

    return jsonify({
        'amount': float(kpi['amount'] or 0),
        'orders': int(kpi['orders'] or 0),
        'lines': int((lines_count['lines'] or 0)),
        'items': [{'id': r['id'], 'name': r['name'], 'qty': int(r['qty'] or 0), 'amount': float(r['amount'] or 0)} for r in items],
        'recent': recent,
    })

# Items CRUD
@app.get('/api/items')
def items_list():
    con = get_db()
    rows = con.execute('SELECT id, name, price FROM items ORDER BY id').fetchall()
    return jsonify([{'id': r['id'], 'name': r['name'], 'price': float(r['price'])} for r in rows])

@app.post('/api/items')
def items_create():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    price = float(data.get('price') or 0)
    if not name or price <= 0:
        return jsonify({'ok': False, 'msg': 'Ungültig'}), 400
    con = get_db()
    with con:
        con.execute('INSERT INTO items (name, price) VALUES (?,?)', (name, price))
    return jsonify({'ok': True})

@app.put('/api/items/<int:item_id>')
def items_update(item_id: int):
    data = request.get_json(force=True)
    name = data.get('name')
    price = data.get('price')
    con = get_db()
    with con:
        if name is not None:
            con.execute('UPDATE items SET name=? WHERE id=?', (name.strip(), item_id))
        if price is not None:
            con.execute('UPDATE items SET price=? WHERE id=?', (float(price), item_id))
    return jsonify({'ok': True})

@app.delete('/api/items/<int:item_id>')
def items_delete(item_id: int):
    con = get_db()
    with con:
        con.execute('DELETE FROM items WHERE id=?', (item_id,))
    return jsonify({'ok': True})

# Users CRUD (nur Admin UI)
@app.post('/api/users')
def users_create():
    data = request.get_json(force=True)
    username = (data.get('username') or '').strip()
    pin = (data.get('pin') or '').strip()
    is_admin = 1 if data.get('is_admin') else 0
    if not username or not pin:
        return jsonify({'ok': False, 'msg': 'Ungültig'}), 400
    con = get_db()
    try:
        with con:
            con.execute('INSERT INTO users (username, pin, is_admin) VALUES (?,?,?)', (username, pin, is_admin))
    except sqlite3.IntegrityError:
        return jsonify({'ok': False, 'msg': 'Benutzer existiert bereits'}), 400
    return jsonify({'ok': True})

@app.put('/api/users/<int:user_id>')
def users_update(user_id: int):
    data = request.get_json(force=True)
    username = data.get('username')
    pin = data.get('pin')
    is_admin = 1 if data.get('is_admin') else 0
    con = get_db()
    with con:
        if username is not None:
            con.execute('UPDATE users SET username=? WHERE id=?', (username.strip(), user_id))
        con.execute('UPDATE users SET is_admin=? WHERE id=?', (is_admin, user_id))
        if pin:
            con.execute('UPDATE users SET pin=? WHERE id=?', (pin.strip(), user_id))
    return jsonify({'ok': True})

@app.delete('/api/users/<int:user_id>')
def users_delete(user_id: int):
    if 'user' in session and session['user']['id'] == user_id:
        return jsonify({'ok': False, 'msg': 'Eigener Benutzer kann nicht gelöscht werden'}), 400
    con = get_db()
    with con:
        con.execute('DELETE FROM users WHERE id=?', (user_id,))
    return jsonify({'ok': True})

# Payment Methods CRUD
@app.get('/api/payments')
def payments_list():
    con = get_db()
    rows = con.execute('SELECT id, name, protected FROM payment_methods ORDER BY id').fetchall()
    return jsonify([{'id': r['id'], 'name': r['name'], 'protected': bool(r['protected'])} for r in rows])

@app.post('/api/payments')
def payments_create():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    prot = 1 if data.get('protected') else 0
    if not name:
        return jsonify({'ok': False, 'msg': 'Ungültig'}), 400
    con = get_db()
    try:
        with con:
            con.execute('INSERT INTO payment_methods (name, protected) VALUES (?,?)', (name, prot))
    except sqlite3.IntegrityError:
        return jsonify({'ok': False, 'msg': 'Zahlart existiert bereits'}), 400
    return jsonify({'ok': True})

@app.put('/api/payments/<int:pm_id>')
def payments_update(pm_id: int):
    data = request.get_json(force=True)
    name = data.get('name')
    prot = 1 if data.get('protected') else 0
    con = get_db()
    with con:
        if name is not None:
            con.execute('UPDATE payment_methods SET name=? WHERE id=?', (name.strip(), pm_id))
        con.execute('UPDATE payment_methods SET protected=? WHERE id=?', (prot, pm_id))
    return jsonify({'ok': True})

@app.delete('/api/payments/<int:pm_id>')
def payments_delete(pm_id: int):
    con = get_db()
    row = con.execute('SELECT protected FROM payment_methods WHERE id=?', (pm_id,)).fetchone()
    if row and row['protected']:
        return jsonify({'ok': False, 'msg': 'Geschützte Zahlart kann nicht gelöscht werden'}), 400
    with con:
        con.execute('DELETE FROM payment_methods WHERE id=?', (pm_id,))
    return jsonify({'ok': True})

# --- Start ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False, use_reloader=False)
