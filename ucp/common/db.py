from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from ucp.common.config import get_settings


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    # shortcut: one connection per request, no pool; add psycopg_pool when load needs it
    with psycopg.connect(get_settings().database_url, row_factory=dict_row) as conn:
        yield conn


def apply_schemas() -> None:
    # shortcut: plain idempotent SQL at startup instead of Alembic; switch once a schema needs a real migration
    with connect() as conn:
        for f in sorted(Path(__file__).parents[1].glob("modules/*/schema.sql")):
            conn.execute(f.read_text())
