"""s3rds.psycopg2.extras — psycopg2.extras: dict cursors, Json, execute_values / execute_batch."""
import json

from ..sqlite import Cursor, DictCursor


class RealDictCursor(DictCursor):
    """Rows are dicts (psycopg2's RealDictRow is a dict too)."""


class DictRow(list):
    """A row read by index or by column name, as psycopg2's DictRow."""
    def __init__(self, columns, row):
        super().__init__(row)
        self._index = {c: i for i, c in enumerate(columns)}

    def __getitem__(self, key):
        return super().__getitem__(self._index[key] if isinstance(key, str) else key)

    def keys(self):
        return list(self._index)

    def items(self):
        return [(k, self[i]) for k, i in self._index.items()]

    def get(self, key, default=None):
        return self[key] if key in self._index else default


class DictCursor(Cursor):  # noqa: F811  (psycopg2's DictCursor: index and name access)
    def _row(self, columns, row):
        return DictRow(columns, row)


def Json(adapted, dumps=None):
    """psycopg2.extras.Json: a Python object sent as JSON text."""
    return (dumps or json.dumps)(adapted)


def execute_batch(cur, sql, argslist, page_size=100):
    cur.executemany(sql, list(argslist))


def execute_values(cur, sql, argslist, template=None, page_size=100, fetch=False):
    """INSERT ... VALUES %s with many rows per statement, as psycopg2.extras.execute_values."""
    rows, out = list(argslist), []
    for i in range(0, len(rows), page_size):
        page = rows[i:i + page_size]
        tpl = template or "(" + ", ".join(["%s"] * len(page[0])) + ")"
        cur.execute(sql.replace("%s", ", ".join([tpl] * len(page)), 1), [v for r in page for v in r])
        if fetch:
            out += cur.fetchall()
    return out if fetch else None
