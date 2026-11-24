"""API da Casa do Bolo do Léo Doce e Mel na Vercel.

Rotas: /api/catalog, /api/health, /api/orders, /api/orders/{code},
/api/client/session, /api/client/login, /api/client/logout, /api/client/orders,
/api/operator/session, /api/operator/login, /api/operator/logout,
/api/operator/orders, /api/operator/orders/{id}, /api/operator/users,
/api/operator/users/{id}, /api/operator/register, /api/operator/tables e
/api/operator/tables/{id}.
"""

import json
import re
import sys
import time
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud_lib import catalog, pedidos, remote_db  # noqa: E402

COOKIE_NAME = "cdm_session"
CLIENT_COOKIE = "cdm_table"
WRITE_HEADER = "X-Requested-With"
WRITE_VALUE = "CasaDoBolo"
RATE_LIMITS: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def log_error(message: str) -> None:
    sys.stderr.write(f"{message}\n")


class handler(BaseHTTPRequestHandler):
    server_version = "CasaDoBolo/1.0"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def route_parts(self) -> tuple[str, dict[str, list[str]]]:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        override = query.pop("__path", [None])[0]

        header_path = None
        for name in ("X-Vercel-Forwarded-Path", "X-Forwarded-Path", "X-Original-URL"):
            value = self.headers.get(name)
            if value:
                header_path = value if value.startswith("/") else urlsplit(value).path
                break

        target = header_path or override or parts.path or self.path
        target_parts = urlsplit(target)
        if target_parts.query:
            merged = parse_qs(target_parts.query)
            merged.update(query)
            query = merged
        path = target_parts.path
        if target_parts.fragment:
            path = f"{path}#{target_parts.fragment}"
        return unquote(path), query

    def write_headers(
        self,
        content_type: str,
        length: int,
        status: int = 200,
        cache: str = "no-store",
        extra: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()

    def send_json(
        self,
        payload: dict,
        status: int = 200,
        extra: tuple[tuple[str, str], ...] = (),
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.write_headers("application/json; charset=utf-8", len(body), status, extra=extra)
        self.wfile.write(body)

    def deny(self, message: str, status: int = 401) -> None:
        self.send_json({"error": message}, status)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 65_536:
            raise ValueError("Requisição inválida.")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Requisição inválida.")
        return data

    def client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.headers.get("X-Real-IP", "") or "unknown"

    def allow(self, bucket: str, limit: int, window: int) -> bool:
        key = (self.client_ip(), bucket)
        entries = RATE_LIMITS[key]
        now = time.monotonic()
        while entries and entries[0] < now - window:
            entries.popleft()
        if len(entries) >= limit:
            return False
        entries.append(now)
        return True

    def cookie_value(self, name: str) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        morsel = cookie.get(name)
        return morsel.value if morsel else None

    def token(self) -> str | None:
        return self.cookie_value(COOKIE_NAME)

    def client_token(self) -> str | None:
        return self.cookie_value(CLIENT_COOKIE)

    def client_session(self) -> dict | None:
        try:
            pedidos.ensure_db()
            return pedidos.table_for_session(self.client_token())
        except Exception:
            return None

    def is_operator(self) -> bool:
        try:
            pedidos.ensure_db()
            return pedidos.session_valid(self.token())
        except Exception:
            return False

    def require_write(self) -> bool:
        if self.headers.get(WRITE_HEADER) != WRITE_VALUE:
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(":")[0]
        return parsed.scheme in ("http", "https") and parsed.hostname == host

    def cookie(self, name: str, value: str, max_age: int) -> str:
        proto = self.headers.get("X-Forwarded-Proto", "")
        secure = "; Secure" if proto == "https" else ""
        return (
            f"{name}={value}; HttpOnly; SameSite=Strict; Path=/; "
            f"Max-Age={max_age}{secure}"
        )

    def public_order(self, order: dict) -> dict:
        return {
            "code": order["code"],
            "type": order["type"],
            "mesa": order.get("table_number", ""),
            "status": order["status"],
            "total_cents": order["total_cents"],
            "created_at": order["created_at"],
            "items": order["items"],
        }

    def do_GET(self) -> None:
        route, query = self.route_parts()

        if route == "/api/catalog":
            data = catalog.data()
            featured = query.get("featured", ["0"])[0] == "1"
            products = [p for p in data["products"] if not featured or p["featured"]]
            self.send_json({"categories": data["categories"], "products": products})
            return

        if route == "/api/health":
            orders_database = False
            try:
                pedidos.ensure_db()
                orders_database = True
            except Exception as error:
                log_error(f"Falha no banco de pedidos: {error}")
            self.send_json(
                {
                    "ok": True,
                    "database": remote_db.configured(),
                    "orders_database": orders_database,
                    "catalog_products": len(catalog.data()["products"]),
                }
            )
            return

        if route == "/api/operator/session":
            self.send_json({"ok": self.is_operator(), "cadastro": False})
            return

        mesa = self.client_session()
        if route == "/api/client/session":
            self.send_json({"ok": bool(mesa), "mesa": mesa["number"] if mesa else ""})
            return
        if route == "/api/client/orders":
            if not mesa:
                self.deny("Entre com a mesa e o código.", 401)
                return
            try:
                pedidos.ensure_db()
                orders = pedidos.list_table_orders(mesa["number"])
                self.send_json({"orders": [self.public_order(order) for order in orders]})
            except Exception as error:
                log_error(f"Falha ao listar pedidos da mesa: {error}")
                self.deny("Erro interno. Tente novamente.", 500)
            return
        if route == "/api/operator/tables":
            if not self.is_operator():
                self.deny("Não autorizado.")
                return
            try:
                self.send_json({"tables": pedidos.list_tables()})
            except Exception as error:
                log_error(f"Falha ao listar mesas: {error}")
                self.deny("Erro interno. Tente novamente.", 500)
            return

        if route == "/api/operator/users":
            if not self.is_operator():
                self.deny("Não autorizado.")
                return
            try:
                pedidos.ensure_db()
                self.send_json(
                    {
                        "users": pedidos.list_operators(),
                        "current_id": pedidos.operator_id_for_session(self.token()),
                    }
                )
            except Exception as error:
                log_error(f"Falha ao listar usuarios: {error}")
                self.deny("Erro interno. Tente novamente.", 500)
            return

        if route == "/api/operator/orders":
            if not self.is_operator():
                self.deny("Não autorizado.")
                return
            try:
                self.send_json({"orders": pedidos.list_orders()})
            except Exception as error:
                log_error(f"Falha ao listar pedidos: {error}")
                self.deny("Erro interno. Tente novamente.", 500)
            return

        tracked = re.fullmatch(r"/api/orders/(.+)", route)
        if tracked:
            if not self.allow("track", 60, 600):
                self.deny("Muitas consultas. Tente depois.", 429)
                return
            store = query.get("loja", ["0"])[0] == "1"
            operator = self.is_operator()
            if store and not operator:
                self.deny("Acesso restrito à loja.", 403)
                return
            phone = "" if store else pedidos.clean_phone(query.get("phone", [""])[0])
            try:
                pedidos.ensure_db()
                order = pedidos.get_order(tracked.group(1), phone)
            except Exception as error:
                log_error(f"Falha ao consultar pedido: {error}")
                self.deny("Erro interno. Tente novamente.", 500)
                return
            if not order:
                self.deny("Pedido não encontrado.", 404)
                return
            self.send_json({"order": order if store else self.public_order(order)})
            return

        self.deny("Não encontrado.", 404)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        route, _ = self.route_parts()
        code_route = re.fullmatch(r"/api/operator/tables/(\d+)/code", route)
        if route not in (
            "/api/orders",
            "/api/client/login",
            "/api/client/logout",
            "/api/operator/login",
            "/api/operator/logout",
            "/api/operator/users",
            "/api/operator/register",
            "/api/operator/tables",
        ) and not code_route:
            self.deny("Não encontrado.", 404)
            return
        if not self.require_write():
            self.deny("Requisição não permitida.", 403)
            return

        try:
            if route == "/api/orders":
                if not self.allow("order", 20, 3600):
                    self.deny("Muitos pedidos. Aguarde um momento.", 429)
                    return
                pedidos.ensure_db()
                mesa = self.client_session()
                order = pedidos.create_order(
                    self.read_json(), mesa["number"] if mesa else ""
                )
                self.send_json(
                    {
                        "ok": True,
                        "code": order["code"],
                        "total_cents": order["total_cents"],
                        "order": self.public_order(order),
                    },
                    201,
                )
                return

            if route == "/api/client/login":
                if not self.allow("client_login", 30, 900):
                    self.deny("Muitas tentativas. Tente novamente em 15 minutos.", 429)
                    return
                data = self.read_json()
                pedidos.ensure_db()
                mesaid = pedidos.verify_table(data.get("mesa"), data.get("code"))
                if not mesaid:
                    self.deny("Mesa ou código incorretos.")
                    return
                token, expires = pedidos.create_table_session(mesaid)
                cookie = self.cookie(
                    CLIENT_COOKIE, token, pedidos.CLIENT_SESSION_HOURS * 3600
                )
                self.send_json({"ok": True, "expires": expires}, extra=(("Set-Cookie", cookie),))
                return

            if route == "/api/client/logout":
                pedidos.delete_table_session(self.client_token())
                cookie = self.cookie(CLIENT_COOKIE, "", 0)
                self.send_json({"ok": True}, extra=(("Set-Cookie", cookie),))
                return

            if route == "/api/operator/tables":
                if not self.is_operator() or not self.require_write():
                    self.deny("Não autorizado.", 403)
                    return
                pedidos.ensure_db()
                data = self.read_json()
                table = pedidos.add_table(data.get("mesa"))
                self.send_json({"ok": True, **table}, 201)
                return

            if code_route:
                if not self.is_operator() or not self.require_write():
                    self.deny("Não autorizado.", 403)
                    return
                pedidos.ensure_db()
                code = pedidos.regenerate_table_code(int(code_route.group(1)))
                self.send_json({"ok": True, "code": code})
                return

            if route == "/api/operator/register":
                self.deny("Cadastro disponível apenas no servidor local.", 403)
                return

            if route == "/api/operator/login":
                if not self.allow("login", 20, 900):
                    self.deny("Muitas tentativas. Tente novamente em 15 minutos.", 429)
                    return
                data = self.read_json()
                username = str(data.get("username") or "")
                password = str(data.get("password") or "")
                pedidos.ensure_db()
                operator_id = pedidos.verify_password(username, password)
                if not operator_id:
                    self.deny("Usuário ou senha incorretos.")
                    return
                token, expires = pedidos.create_session(operator_id)
                cookie = self.cookie(COOKIE_NAME, token, pedidos.SESSION_HOURS * 3600)
                self.send_json({"ok": True, "expires": expires}, extra=(("Set-Cookie", cookie),))
                return

            if route == "/api/operator/users":
                if not self.is_operator() or not self.require_write():
                    self.deny("Não autorizado.", 403)
                    return
                if not self.allow("users", 20, 3600):
                    self.deny("Muitos cadastros. Aguarde um momento.", 429)
                    return
                pedidos.ensure_db()
                data = self.read_json()
                operator_id = pedidos.add_operator(data.get("username"), data.get("password"))
                self.send_json({"ok": True, "id": operator_id}, 201)
                return

            pedidos.delete_session(self.token())
            cookie = self.cookie(COOKIE_NAME, "", 0)
            self.send_json({"ok": True}, extra=(("Set-Cookie", cookie),))
        except ValueError as error:
            self.deny(str(error), 400)
        except json.JSONDecodeError:
            self.deny("JSON inválido.", 400)
        except Exception as error:
            log_error(f"Erro ao processar {route}: {error}")
            self.deny("Erro interno. Tente novamente.", 500)

    def do_DELETE(self) -> None:
        route, _ = self.route_parts()
        match = re.fullmatch(r"/api/operator/users/(\d+)", route)
        table_match = re.fullmatch(r"/api/operator/tables/(\d+)", route)
        if not match and not table_match:
            self.deny("Não encontrado.", 404)
            return
        if not self.is_operator() or not self.require_write():
            self.deny("Não autorizado.", 403)
            return
        try:
            pedidos.ensure_db()
            if match:
                pedidos.remove_operator(
                    int(match.group(1)), pedidos.operator_id_for_session(self.token())
                )
            else:
                pedidos.remove_table(int(table_match.group(1)))
            self.send_json({"ok": True})
        except ValueError as error:
            self.deny(str(error), 400)
        except Exception as error:
            log_error(f"Falha ao remover usuario: {error}")
            self.deny("Erro interno. Tente novamente.", 500)

    def do_PATCH(self) -> None:
        route, _ = self.route_parts()
        match = re.fullmatch(r"/api/operator/orders/(\d+)", route)
        if not match:
            self.deny("Não encontrado.", 404)
            return
        if not self.is_operator() or not self.require_write():
            self.deny("Não autorizado.", 403)
            return
        try:
            pedidos.ensure_db()
            status = str(self.read_json().get("status") or "")
            order = pedidos.update_status(int(match.group(1)), status)
            if not order:
                self.deny("Pedido não encontrado.", 404)
                return
            self.send_json({"ok": True, "order": order})
        except ValueError as error:
            self.deny(str(error), 400)
        except json.JSONDecodeError:
            self.deny("JSON inválido.", 400)
        except Exception as error:
            log_error(f"Erro ao atualizar pedido: {error}")
            self.deny("Erro interno. Tente novamente.", 500)
