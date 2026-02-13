# vers: 1.05
from __future__ import annotations

from typing import Any, Mapping, Optional
import logging
import sys
import time

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Engine, URL


logger = logging.getLogger("notes_app.db")

Params = Optional[Mapping[str, Any]]


def _safe_text(value: Any) -> str:
    raw = str(value)
    encoding = getattr(getattr(sys, "stdout", None), "encoding", None) or "utf-8"
    return raw.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _friendly_error_text(exc: Exception) -> str:
    # На некоторых Windows+PostgreSQL локалях psycopg2 может кидать UnicodeDecodeError
    # вместо штатного сообщения об отказе в подключении.
    if isinstance(exc, UnicodeDecodeError):
        return "Не удалось декодировать ответ PostgreSQL. Проверьте логин/пароль и локаль сервера."
    return _safe_text(exc)


def _shorten_query(query: str, limit: int = 1050) -> str:
    q = (query or "").strip()
    if len(q) <= limit:
        return q
    return q[:limit] + "... (обрезано)"


def _log_ok(fn_name: str, query: str, elapsed_s: float) -> None:
    logger.info("%s took %.3f s", fn_name, elapsed_s)
    # print(f"+++++++++++++++++++++++ Запрос выполнен: {_safe_text(_shorten_query(query))}")


def _log_err(fn_name: str, query: str, elapsed_s: float, exc: Exception) -> None:
    logger.info("%s failed after %.3f s", fn_name, elapsed_s)
    print(f"Ошибка при выполнении запроса: {_friendly_error_text(exc)}")
    # print(f"+++++++++++++++++++++++ Запрос (ошибка): {_safe_text(_shorten_query(query))}")


def _defaults() -> tuple[str, int, str]:
    return "localhost", 5432, "postgres"




def make_engine(
    user_name: str,
    user_passw: str,
    host: str | None = None,
    port: int | None = None,
    database: str | None = None,
) -> Engine:
    default_host, default_port, default_db = _defaults()

    url = URL.create(
        "postgresql+psycopg2",
        username=user_name,
        password=user_passw,
        host=host or default_host,
        port=port if port is not None else default_port,
        database=database or default_db,
    )

    # Не задаем server options (например lc_messages), потому что у части ролей
    # нет прав на изменение параметров сеанса.
    connect_args = {"connect_timeout": 5}
    return sa.create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)


def execute_scalar(engine: Engine, query: str, params: Params = None) -> Any | None:
    """
    Возвращает первый столбец первой строки, либо None.
    SELECT count(*), INSERT/UPDATE/DELETE ... RETURNING id, и т.п.
    """
    t0 = time.perf_counter()
    try:
        stmt = text(query)
        with engine.begin() as conn:
            result = conn.execute(stmt, params or {})
            value = result.scalar_one_or_none() if result.returns_rows else None

        _log_ok("execute_scalar", query, time.perf_counter() - t0)
        return value
    except Exception as exc:
        _log_err("execute_scalar", query, time.perf_counter() - t0, exc)
        logger.warning("execute_scalar SQLAlchemy failed: %s", _friendly_error_text(exc))
        return None


def get_fetchone(engine: Engine, query: str, params: Params = None, *, as_dict: bool = False) -> Any | None:
    """
    Возвращает одну строку, либо None.
    - as_dict=False -> Row
    - as_dict=True  -> dict
    """
    t0 = time.perf_counter()
    try:
        stmt = text(query)
        with engine.begin() as conn:
            result = conn.execute(stmt, params or {})
            row = result.fetchone() if result.returns_rows else None

        _log_ok("get_fetchone", query, time.perf_counter() - t0)
        if row is None:
            return None
        return dict(row._mapping) if as_dict else row
    except Exception as exc:
        _log_err("get_fetchone", query, time.perf_counter() - t0, exc)
        logger.warning("get_fetchone SQLAlchemy failed: %s", _friendly_error_text(exc))
        return None


def get_fetch(engine: Engine, query: str) -> pd.DataFrame | None:
    t0 = time.perf_counter()

    try:
        stmt = text(query)
        with engine.connect() as conn:
            result = conn.execute(stmt)
            rows = result.fetchall() if result.returns_rows else []
            columns = list(result.keys()) if result.returns_rows else []

        _log_ok("get_fetch", query, time.perf_counter() - t0)
        return pd.DataFrame(rows, columns=columns)
    except Exception as exc:
        _log_err("get_fetch", query, time.perf_counter() - t0, exc)
        logger.warning("get_fetch SQLAlchemy failed: %s", _friendly_error_text(exc))
        return None


def get_execute(engine: Engine, query: str) -> int | None:
    t0 = time.perf_counter()

    try:
        stmt = text(query)

        with engine.begin() as conn:
            result = conn.execute(stmt)
            affected = result.rowcount

        _log_ok("get_execute", query, time.perf_counter() - t0)
        if affected is None or affected < 0:
            return None
        return int(affected)
    except Exception as exc:
        _log_err("get_execute", query, time.perf_counter() - t0, exc)
        logger.warning("get_execute SQLAlchemy failed: %s", _friendly_error_text(exc))
        return None


def test_connection(user_name: str, user_passw: str) -> bool:
    t0 = time.perf_counter()
    query = "SELECT version()"
    engine: Engine | None = None

    try:
        engine = make_engine(user_name, user_passw)
        stmt = text(query)
        with engine.connect() as conn:
            version = conn.execute(stmt).scalar_one_or_none() or "unknown"

        _log_ok("test_connection", query, time.perf_counter() - t0)
        print(f"Подключение установлено. Версия PostgreSQL: {_safe_text(version)}")
        return True
    except Exception as exc:
        _log_err("test_connection", query, time.perf_counter() - t0, exc)
        logger.warning("test_connection SQLAlchemy failed: %s", _friendly_error_text(exc))
        return False
    finally:
        if engine is not None:
            engine.dispose()
