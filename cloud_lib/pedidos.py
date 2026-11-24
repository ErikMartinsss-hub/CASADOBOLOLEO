"""Persistência e regras dos pedidos na versão Vercel."""

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import catalog, remote_db

STATUSES = ("novo", "em_preparo", "pronto", "finalizado", "cancelado")
NEXT_STATUS = {
    "novo": "em_preparo",
    "em_preparo": "pronto",
    "pronto": "finalizado",
    "finalizado": None,
    "cancelado": None,
}
TYPES = ("balcao", "delivery")
SESSION_HOURS = 12
PBKDF2_ROUNDS = 210_000
DEFAULT_USERNAME = "admin"

_initialized = False


def ensure_db() -> None:
    global _initialized
    if _initialized:
        return
    init_db()
    _initialized = True


def init_db() -> None:
    with remote_db.db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS operators (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                salt BLOB NOT NULL,
                digest BLOB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'novo',
                total_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                product_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                qty INTEGER NOT NULL,
                unit_cents INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS orders_created ON orders(created_at DESC);
            CREATE INDEX IF NOT EXISTS order_items_order ON order_items(order_id);
            """
        )

    with remote_db.db() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(operators)")}
        if "username" not in columns:
            connection.execute(
                f"ALTER TABLE operators ADD COLUMN username TEXT NOT NULL DEFAULT '{DEFAULT_USERNAME}'"
            )
        exists = connection.execute("SELECT 1 FROM operators WHERE id = 1").fetchone()
    if exists:
        return

    username = os.environ.get("OPERADOR_USUARIO", "").strip() or DEFAULT_USERNAME
    password = os.environ.get("OPERADOR_SENHA", "")
    if not password:
        raise RuntimeError("OPERADOR_SENHA não configurada na Vercel.")
    set_password(password, username)


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)


def set_password(password: str, username: str | None = None) -> None:
    with remote_db.db() as connection:
        row = connection.execute("SELECT username FROM operators WHERE id = 1").fetchone()
    if username is None:
        username = (row["username"] if row else "") or DEFAULT_USERNAME
    salt = secrets.token_bytes(16)
    with remote_db.db() as connection:
        connection.execute(
            "INSERT INTO operators(id, username, salt, digest) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET username = excluded.username, "
            "salt = excluded.salt, digest = excluded.digest",
            (username, salt, hash_password(password, salt)),
        )
        connection.execute("DELETE FROM sessions")


def verify_password(username: str, password: str) -> bool:
    with remote_db.db() as connection:
        row = connection.execute(
            "SELECT username, salt, digest FROM operators WHERE id = 1"
        ).fetchone()
    if not row or not username.strip():
        return False
    expected = row["username"] or DEFAULT_USERNAME
    if not hmac.compare_digest(username.strip().casefold(), expected.strip().casefold()):
        return False
    return hmac.compare_digest(hash_password(password, row["salt"]), row["digest"])


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def create_session() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
    with remote_db.db() as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
        connection.execute(
            "INSERT INTO sessions(token, created_at, expires_at) VALUES (?, ?, ?)",
            (token, now_iso(), expires),
        )
    return token, expires


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    with remote_db.db() as connection:
        row = connection.execute(
            "SELECT expires_at FROM sessions WHERE token = ?", (token,)
        ).fetchone()
    if not row:
        return False
    return row["expires_at"] > now_iso()


def delete_session(token: str | None) -> None:
    if token:
        with remote_db.db() as connection:
            connection.execute("DELETE FROM sessions WHERE token = ?", (token,))


def clean_text(value: object, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def clean_phone(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("55"):
        digits = digits[2:]
    return digits


def valid_phone(phone: str) -> bool:
    if len(phone) not in (10, 11) or not phone.isdigit():
        return False
    if not 11 <= int(phone[:2]) <= 99:
        return False
    return len(phone) == 10 or phone[2] == "9"


def price_cents(value: object) -> int:
    match = re.search(r"(\d+)[.,](\d{2})", str(value or ""))
    return int(match[1]) * 100 + int(match[2]) if match else 0


def code_for(order_id: int) -> str:
    return f"#{order_id:04d}"


def order_rows(rows: list) -> list[dict]:
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    with remote_db.db() as connection:
        items = connection.execute(
            f"SELECT * FROM order_items WHERE order_id IN ({placeholders}) ORDER BY id",
            ids,
        ).fetchall()
    grouped: dict[int, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["order_id"], []).append(
            {
                "product_id": item["product_id"],
                "name": item["name"],
                "qty": item["qty"],
                "unit_cents": item["unit_cents"],
            }
        )
    result = []
    for row in rows:
        order = dict(row)
        order["code"] = code_for(row["id"])
        order["items"] = grouped.get(row["id"], [])
        result.append(order)
    return result


def list_orders() -> list[dict]:
    with remote_db.db() as connection:
        rows = connection.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    return order_rows(list(rows))


def get_order(code: str, phone: str = "") -> dict | None:
    match = re.fullmatch(r"#?(\d{1,8})", str(code or ""))
    if not match:
        return None
    order_id = int(match[1])
    with remote_db.db() as connection:
        row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        return None
    if phone and row["phone"] != phone:
        return None
    return order_rows([row])[0]


def create_order(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Pedido inválido.")

    order_type = payload.get("type")
    if order_type not in TYPES:
        raise ValueError("Escolha retirada no balcão ou delivery.")

    name = clean_text(payload.get("name"), 80)
    phone = clean_phone(payload.get("phone"))
    address = clean_text(payload.get("address"), 200) if order_type == "delivery" else ""
    notes = clean_text(payload.get("notes"), 300)

    if len(name) < 2:
        raise ValueError("Informe seu nome.")
    if not valid_phone(phone):
        raise ValueError("Informe um telefone válido.")
    if order_type == "delivery" and len(address) < 5:
        raise ValueError("Informe o endereço de entrega.")

    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 60:
        raise ValueError("Carrinho inválido.")

    products = {product["id"]: product for product in catalog.data()["products"]}
    selected: list[tuple[int, int, str, int]] = []
    seen: set[int] = set()
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("Item inválido.")
        product_id = entry.get("id")
        qty = entry.get("qty")
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise ValueError("Item inválido.")
        if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= 99:
            raise ValueError("Quantidade inválida.")
        if product_id in seen:
            raise ValueError("Item repetido.")
        seen.add(product_id)
        product = products.get(product_id)
        if not product:
            raise ValueError("Produto indisponível.")
        unit = price_cents(product["price"])
        if unit <= 0:
            raise ValueError("Preço indisponível.")
        selected.append((product_id, product["name"], qty, unit))

    total = sum(qty * unit for _, _, qty, unit in selected)
    with remote_db.db() as connection:
        cursor = connection.execute(
            """INSERT INTO orders(type, customer_name, phone, address, notes, total_cents, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (order_type, name, phone, address, notes, total, now_iso()),
        )
        order_id = cursor.lastrowid
        connection.executemany(
            "INSERT INTO order_items(order_id, product_id, name, qty, unit_cents) VALUES (?, ?, ?, ?, ?)",
            [(order_id, *item) for item in selected],
        )
        row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return order_rows([row])[0]


def update_status(order_id: int, status: str) -> dict | None:
    if status not in STATUSES:
        raise ValueError("Status inválido.")
    with remote_db.db() as connection:
        row = connection.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return None
        if status != "cancelado" and status != row["status"]:
            allowed = [value for value, next_value in NEXT_STATUS.items() if next_value == status]
            if row["status"] not in allowed:
                raise ValueError("Transição de status inválida.")
        if status == "cancelado" and row["status"] in ("finalizado", "cancelado"):
            raise ValueError("Pedido já encerrado.")
        connection.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        updated = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return order_rows([updated])[0]
