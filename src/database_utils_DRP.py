from __future__ import annotations
import os
from typing import Any, Iterable
import jaydebeapi
import config
import pandas as pd
from typing import Any

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



def get_fetch(
    query: str,
    user_name: str,
    user_passw: str
) -> pd.DataFrame | None:
    """
    Execute a SELECT (or any read) query and return result as a pandas DataFrame.

    Uses PostgreSQL JDBC connection details from config.py.
    """
    conn = _connect_postgres(user_name, user_passw)
    if conn is None:
        return None

    try:
        with conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
                columns = [col[0] for col in (cursor.description or [])]

                df = pd.DataFrame(rows, columns=columns)

                print("Успешно выполнено:", _shorten_for_log(query))
                return df

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
