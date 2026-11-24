"""Conexão Turso/libSQL usada pelas funções da Vercel."""

import os
from contextlib import contextmanager

import turso_serverless


def config() -> tuple[str, str]:
    url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    return url, token


def configured() -> bool:
    url, token = config()
    return bool(url and token)


def connect() -> turso_serverless.Connection:
    url, token = config()
    if not url or not token:
        raise RuntimeError("Turso não configurado nas variáveis de ambiente.")
    connection = turso_serverless.connect(url, auth_token=token)
    connection.row_factory = turso_serverless.Row
    return connection


@contextmanager
def db():
    connection = connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()
