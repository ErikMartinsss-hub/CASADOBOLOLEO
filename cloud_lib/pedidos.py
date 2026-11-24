"""Persistência e regras dos pedidos na versão Vercel."""

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import catalog, remote_db

STATUSES = ("novo", "em_preparo", "pronto", "a_pagar", "pago", "cancelado")
NEXT_STATUS = {
    "novo": "em_preparo",
    "em_preparo": "pronto",
    "pronto": "a_pagar",
    "a_pagar": "pago",
    "pago": None,
    "cancelado": None,
}
CANCEL_BLOCKED = ("pago", "cancelado")
TYPES = ("balcao", "delivery")
SESSION_HOURS = 12
CLIENT_SESSION_HOURS = 12
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
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                salt BLOB NOT NULL,
                digest BLOB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                operator_id INTEGER,
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
                table_number TEXT NOT NULL DEFAULT '',
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

            CREATE TABLE IF NOT EXISTS tables (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL UNIQUE,
                salt BLOB NOT NULL,
                digest BLOB NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS table_sessions (
                token TEXT PRIMARY KEY,
                table_id INTEGER NOT NULL REFERENCES tables(id),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS orders_created ON orders(created_at DESC);
            CREATE INDEX IF NOT EXISTS order_items_order ON order_items(order_id);
            """
        )

    migrate_operators()
    migrate_orders()
    with remote_db.db() as connection:
        exists = connection.execute("SELECT 1 FROM operators LIMIT 1").fetchone()
    if exists:
        return

    username = os.environ.get("OPERADOR_USUARIO", "").strip() or DEFAULT_USERNAME
    password = os.environ.get("OPERADOR_SENHA", "")
    if not password:
        raise RuntimeError("OPERADOR_SENHA não configurada na Vercel.")
    add_operator(username, password)


def migrate_operators() -> None:
    with remote_db.db() as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'operators'"
        ).fetchone()
        schema = " ".join((row["sql"] if row else "").split())
        columns = {item["name"] for item in connection.execute("PRAGMA table_info(operators)")}
        if "username" not in columns:
            connection.execute(
                f"ALTER TABLE operators ADD COLUMN username TEXT NOT NULL DEFAULT '{DEFAULT_USERNAME}'"
            )
        if "CHECK (id = 1)" in schema:
            connection.executescript(
                """
                CREATE TABLE operators_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    salt BLOB NOT NULL,
                    digest BLOB NOT NULL
                );
                INSERT INTO operators_new(id, username, salt, digest)
                    SELECT id, username, salt, digest FROM operators;
                DROP TABLE operators;
                ALTER TABLE operators_new RENAME TO operators;
                """
            )
        columns = {item["name"] for item in connection.execute("PRAGMA table_info(sessions)")}
        if "operator_id" not in columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN operator_id INTEGER")
            connection.execute("UPDATE sessions SET operator_id = 1")


def migrate_orders() -> None:
    with remote_db.db() as connection:
        columns = {item["name"] for item in connection.execute("PRAGMA table_info(orders)")}
        if "table_number" not in columns:
            connection.execute("ALTER TABLE orders ADD COLUMN table_number TEXT NOT NULL DEFAULT ''")
        connection.execute("CREATE INDEX IF NOT EXISTS orders_table ON orders(table_number)")
        connection.execute("UPDATE orders SET status = 'pago' WHERE status = 'finalizado'")


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)


def valid_username(value: object) -> str:
    username = str(value or "").strip()
    if not 3 <= len(username) <= 30 or not username.isalnum():
        raise ValueError("Usuário inválido. Use de 3 a 30 letras e números.")
    return username


def valid_password(value: object) -> str:
    password = str(value or "")
    if not 6 <= len(password) <= 200:
        raise ValueError("Senha inválida. Use de 6 a 200 caracteres.")
    return password


def add_operator(username: object, password: object) -> int:
    name = valid_username(username)
    secret = valid_password(password)
    salt = secrets.token_bytes(16)
    with remote_db.db() as connection:
        try:
            cursor = connection.execute(
                "INSERT INTO operators(username, salt, digest) VALUES (?, ?, ?)",
                (name, salt, hash_password(secret, salt)),
            )
        except Exception as error:
            if "UNIQUE" not in str(error):
                raise
            raise ValueError("Esse usuário já existe.") from error
    return int(cursor.lastrowid)


def list_operators() -> list[dict]:
    with remote_db.db() as connection:
        rows = connection.execute("SELECT id, username FROM operators ORDER BY id").fetchall()
    return [{"id": row["id"], "username": row["username"]} for row in rows]


def remove_operator(operator_id: int, current_id: int | None) -> None:
    if len(list_operators()) <= 1:
        raise ValueError("Não é possível remover o único usuário.")
    if current_id is not None and int(operator_id) == int(current_id):
        raise ValueError("Você não pode remover seu próprio usuário.")
    with remote_db.db() as connection:
        cursor = connection.execute("DELETE FROM operators WHERE id = ?", (int(operator_id),))
        if not cursor.rowcount:
            raise ValueError("Usuário não encontrado.")
        connection.execute("DELETE FROM sessions WHERE operator_id = ?", (int(operator_id),))


def set_password(password: str, username: str | None = None) -> None:
    secret = valid_password(password)
    with remote_db.db() as connection:
        if username is None:
            row = connection.execute("SELECT id FROM operators ORDER BY id LIMIT 1").fetchone()
        else:
            row = connection.execute(
                "SELECT id FROM operators WHERE username = ? COLLATE NOCASE",
                (str(username).strip(),),
            ).fetchone()
        if not row:
            raise ValueError("Usuário não encontrado.")
        operator_id = row["id"]
        salt = secrets.token_bytes(16)
        connection.execute(
            "UPDATE operators SET salt = ?, digest = ? WHERE id = ?",
            (salt, hash_password(secret, salt), operator_id),
        )
        connection.execute("DELETE FROM sessions WHERE operator_id = ?", (operator_id,))


def verify_password(username: str, password: str) -> int | None:
    name = str(username or "").strip()
    if not name:
        return None
    with remote_db.db() as connection:
        row = connection.execute(
            "SELECT id, salt, digest FROM operators WHERE username = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
    if not row:
        return None
    if not hmac.compare_digest(hash_password(str(password or ""), row["salt"]), row["digest"]):
        return None
    return int(row["id"])


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def create_session(operator_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
    with remote_db.db() as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
        connection.execute(
            "INSERT INTO sessions(token, operator_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, int(operator_id), now_iso(), expires),
        )
    return token, expires


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    with remote_db.db() as connection:
        row = connection.execute(
            "SELECT s.expires_at FROM sessions s "
            "JOIN operators o ON o.id = s.operator_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    if not row:
        return False
    return row["expires_at"] > now_iso()


def operator_id_for_session(token: str | None) -> int | None:
    if not token:
        return None
    with remote_db.db() as connection:
        row = connection.execute(
            "SELECT operator_id FROM sessions WHERE token = ?", (token,)
        ).fetchone()
    return int(row["operator_id"]) if row and row["operator_id"] else None


def delete_session(token: str | None) -> None:
    if token:
        with remote_db.db() as connection:
            connection.execute("DELETE FROM sessions WHERE token = ?", (token,))


def valid_table_number(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits.isdigit() or not 1 <= int(digits) <= 9999:
        raise ValueError("Número da mesa inválido. Use de 1 a 9999.")
    return str(int(digits))


def new_table_code() -> str:
    return f"{secrets.randbelow(9000) + 1000:04d}"


def add_table(number: object) -> dict:
    table_number = valid_table_number(number)
    code = new_table_code()
    salt = secrets.token_bytes(16)
    with remote_db.db() as connection:
        try:
            cursor = connection.execute(
                "INSERT INTO tables(number, salt, digest, created_at) VALUES (?, ?, ?, ?)",
                (table_number, salt, hash_password(code, salt), now_iso()),
            )
        except Exception as error:
            if "UNIQUE" not in str(error):
                raise
            raise ValueError("Essa mesa já existe.") from error
    return {"id": int(cursor.lastrowid), "number": table_number, "code": code}


def list_tables() -> list[dict]:
    with remote_db.db() as connection:
        rows = connection.execute(
            "SELECT id, number FROM tables ORDER BY CAST(number AS INTEGER), id"
        ).fetchall()
    return [{"id": row["id"], "number": row["number"]} for row in rows]


def remove_table(table_id: int) -> None:
    with remote_db.db() as connection:
        row = connection.execute("SELECT number FROM tables WHERE id = ?", (int(table_id),)).fetchone()
        if not row:
            raise ValueError("Mesa não encontrada.")
        used = connection.execute(
            "SELECT 1 FROM orders WHERE table_number = ? LIMIT 1", (row["number"],)
        ).fetchone()
        if used:
            raise ValueError("Não é possível remover uma mesa com pedidos.")
        connection.execute("DELETE FROM table_sessions WHERE table_id = ?", (int(table_id),))
        connection.execute("DELETE FROM tables WHERE id = ?", (int(table_id),))


def regenerate_table_code(table_id: int) -> str:
    code = new_table_code()
    salt = secrets.token_bytes(16)
    with remote_db.db() as connection:
        cursor = connection.execute(
            "UPDATE tables SET salt = ?, digest = ? WHERE id = ?",
            (salt, hash_password(code, salt), int(table_id)),
        )
        if not cursor.rowcount:
            raise ValueError("Mesa não encontrada.")
        connection.execute("DELETE FROM table_sessions WHERE table_id = ?", (int(table_id),))
    return code


def verify_table(number: object, code: object) -> int | None:
    table_number = valid_table_number(number)
    with remote_db.db() as connection:
        row = connection.execute(
            "SELECT id, salt, digest FROM tables WHERE number = ?", (table_number,)
        ).fetchone()
    if not row:
        return None
    if not hmac.compare_digest(hash_password(str(code or "").strip(), row["salt"]), row["digest"]):
        return None
    return int(row["id"])


def create_table_session(table_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=CLIENT_SESSION_HOURS)).isoformat(
        timespec="seconds"
    )
    with remote_db.db() as connection:
        connection.execute("DELETE FROM table_sessions WHERE expires_at < ?", (now_iso(),))
        connection.execute(
            "INSERT INTO table_sessions(token, table_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, int(table_id), now_iso(), expires),
        )
    return token, expires


def table_for_session(token: str | None) -> dict | None:
    if not token:
        return None
    with remote_db.db() as connection:
        row = connection.execute(
            "SELECT t.id, t.number, s.expires_at FROM table_sessions s "
            "JOIN tables t ON t.id = s.table_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    if not row or row["expires_at"] <= now_iso():
        return None
    return {"id": row["id"], "number": row["number"]}


def delete_table_session(token: str | None) -> None:
    if token:
        with remote_db.db() as connection:
            connection.execute("DELETE FROM table_sessions WHERE token = ?", (token,))


def list_table_orders(number: str) -> list[dict]:
    with remote_db.db() as connection:
        rows = connection.execute(
            "SELECT * FROM orders WHERE table_number = ? ORDER BY id DESC", (number,)
        ).fetchall()
    return order_rows(list(rows))


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
        order["mesa"] = order.get("table_number", "")
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


def create_order(payload: dict, table_number: str = "") -> dict:
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
    mesa = str(table_number or "").strip()
    with remote_db.db() as connection:
        cursor = connection.execute(
            """INSERT INTO orders(type, customer_name, phone, address, notes, table_number,
                                  total_cents, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_type, name, phone, address, notes, mesa, total, now_iso()),
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
        if status == "cancelado" and row["status"] in CANCEL_BLOCKED:
            raise ValueError("Pedido já encerrado.")
        connection.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        updated = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return order_rows([updated])[0]
