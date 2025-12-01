"""
Utility helpers for executing SQL against PostgreSQL over JDBC.

Connection details come from config.py. The helpers accept a raw SQL string
and database credentials that should be supplied at runtime (e.g., from
Streamlit session state).
"""

from __future__ import annotations

import os
from typing import Any, Iterable

import jaydebeapi

import config


def _shorten_for_log(query: str, limit: int = 150) -> str:
    """Return a truncated version of the query for logging."""
    if len(query) <= limit:
        return query
    return query[:limit] + "... (truncated)"


def _connect_postgres(user: str, password: str) -> jaydebeapi.Connection | None:
    """
    Open a JDBC connection to PostgreSQL using the provided parameters.

    jdbc_url and driver_jar are taken from config.py.
    """
    jdbc_url = getattr(config, "jdbc_url", None)
    driver_jar = getattr(config, "driver_jar", None)
    if not jdbc_url or not driver_jar:
        print("Ошибка: jdbc_url или driver_jar не заданы в config.py")
        return None
    driver_class = "org.postgresql.Driver"
    if not os.path.isfile(driver_jar):
        print(f"Driver JAR not found: {driver_jar}")
        return None
    try:
        return jaydebeapi.connect(
            jclassname=driver_class,
            url=jdbc_url,
            driver_args={"user": user, "password": password},
            jars=[driver_jar],
        )
    except Exception as exc:
        print("Ошибка: не удалось установить соединение с базой данных.", exc)
        return None


def get_fetch(query: str, user_name: str, user_passw: str) -> tuple[list[tuple[Any, ...]], list[str]] | None:
    """
    Execute a SELECT (or any read) query and return fetched rows with column names.

    Parameters mirror the legacy helper but use PostgreSQL JDBC connection details from config.py.
    """
    conn = _connect_postgres(user_name, user_passw)
    if conn is None:
        return None

    try:
        with conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                result = cursor.fetchall()
                columns = [col[0] for col in (cursor.description or [])]
                print("Успешно выполнено:", _shorten_for_log(query))
                return list(result), columns
    except Exception as exc:
        print("Ошибка при выполнении запроса:", exc)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_execute(query: str, user_name: str, user_passw: str) -> int | None:
    """
    Execute a DDL/DML query (INSERT/UPDATE/DELETE/DDL).

    Returns the number of affected rows when available, otherwise None.
    """
    conn = _connect_postgres(user_name, user_passw)
    if conn is None:
        return None

    try:
        with conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                affected = cursor.rowcount
                print("Успешно выполнено:", _shorten_for_log(query))
                return affected
    except Exception as exc:
        print("Ошибка при выполнении запроса:", exc)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def test_connection(user_name: str, user_passw: str) -> bool:
    """
    Проверяет успешность подключения.

    Возвращает True, если удалось подключиться и выполнить простой запрос,
    иначе False. Сообщает версию PostgreSQL при успехе.
    """
    conn = _connect_postgres(user_name, user_passw)
    if conn is None:
        return False

    try:
        with conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version()")
                row = cursor.fetchone()
                version = row[0] if row else "unknown"
                print("Подключение установлено. PostgreSQL version:", version)
                return True
    except Exception as exc:
        print("Ошибка при проверке соединения:", exc)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
