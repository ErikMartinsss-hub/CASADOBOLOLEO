"""Catálogo estático incluído no pacote da função."""

import json
from functools import lru_cache
from pathlib import Path


def _catalog_path() -> Path:
    here = Path(__file__).resolve().parent
    candidates = (
        here / "catalog.json",
        here.parent / "cloud_lib" / "catalog.json",
        here.parent / "catalog.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("catalog.json não encontrado no pacote de deploy.")


@lru_cache(maxsize=1)
def data() -> dict:
    return json.loads(_catalog_path().read_text(encoding="utf-8"))
