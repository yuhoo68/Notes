# vers: 1.04
import importlib
import logging
import os
import streamlit as st
import streamlit.components.v1 as components
import subprocess
import sys

# if "reqs_installed" not in st.session_state:
#     req_path = os.path.join(os.path.dirname(__file__), "requirements.txt")
#     if os.path.exists(req_path):
#         try:
#             subprocess.check_call([
#                 sys.executable, "-m", "pip", "install", "--user", "-r", req_path
#             ])
#             st.session_state["reqs_installed"] = True
#         except subprocess.CalledProcessError as e:
#             print("Ошибка при установке зависимостей:", e)
#             st.error("Не удалось установить зависимости. См. терминал.")

import base64
import html
import json
import re
import urllib.parse
import datetime
import time
from typing import Any
import pandas as pd
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode
from st_aggrid.shared import GridUpdateMode, DataReturnMode
from streamlit_quill import st_quill
import config
from src import export as export_mod
from src.database_utils_DRP import execute_scalar, get_execute, get_fetch, get_fetchone, make_engine, test_connection

_import_mod = importlib.import_module("src.import")

SCHEMA = "sbx_dfip_ocpp"
USERS_TABLE = f"{SCHEMA}.notes_users"
NOTEBOOKS_TABLE = f"{SCHEMA}.notes_notebooks"
SECTIONS_TABLE = f"{SCHEMA}.notes_sections"
PAGES_TABLE = f"{SCHEMA}.notes_pages"
OWNERS_TABLE = f"{SCHEMA}.notes_notebook_owners"
DEPARTMENTS_TABLE = f"{SCHEMA}.notes_departments"
ATTACHMENTS_TABLE = f"{SCHEMA}.notes_page_attachments"
EMAIL_RECIPIENTS_TABLE = f"{SCHEMA}.notes_email_recipients"
EVENT_LOGS_TABLE = f"{SCHEMA}.notes_event_logs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("notes_app")

DEFAULT_SIDEBAR_REFRESH_SECONDS = 60


def _escape(val: str) -> str:
    """Минимальное экранирование строк для SQL."""
    safe = (val or "").replace("\x00", "")
    return safe.replace("'", "''")



def _escape_like(val: str) -> str:
    """
    Экранируем спецсимволы для ILIKE-шаблонов (% и _), чтобы пользователь
    не мог ломать шаблон. Используем ESCAPE '\\' в SQL.
    """
    s = (val or "")
    s = s.replace("\\", "\\\\")
    s = s.replace("%", "\\%")
    s = s.replace("_", "\\_")
    return _escape(s)


def _log_timing(label: str, start: float, min_seconds: float = 0.0) -> None:
    elapsed = time.perf_counter() - start
    if elapsed >= min_seconds:
        logger.info("%s took %.3f s", label, elapsed)


def _log_metric(label: str, value) -> None:
    logger.info("%s: %s", label, value)


# -------------------------
# Расширенный поиск (Google-like)
# -------------------------
class _Tok:
    def __init__(self, typ: str, val: str = ""):
        self.typ = typ  # WORD, PHRASE, LPAREN, RPAREN, OR, PLUS, MINUS
        self.val = val


def _lex_search(q: str) -> list[_Tok]:
    """
    Лексер:
      - "фраза" -> PHRASE(val)
      - ( )     -> LPAREN/RPAREN
      - |       -> OR
      - +       -> PLUS (унарный)
      - -       -> MINUS (унарный)
      - остальное -> WORD (может содержать '*')
    """
    s = (q or "").strip()
    out: list[_Tok] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]

        if ch.isspace():
            i += 1
            continue

        if ch == '"':
            i += 1
            buf = []
            while i < n and s[i] != '"':
                buf.append(s[i])
                i += 1
            if i < n and s[i] == '"':
                i += 1
            phrase = "".join(buf).strip()
            if phrase:
                out.append(_Tok("PHRASE", phrase))
            continue

        if ch == "(":
            out.append(_Tok("LPAREN", ch))
            i += 1
            continue
        if ch == ")":
            out.append(_Tok("RPAREN", ch))
            i += 1
            continue
        if ch == "|":
            out.append(_Tok("OR", ch))
            i += 1
            continue
        if ch == "+":
            out.append(_Tok("PLUS", ch))
            i += 1
            continue
        if ch == "-":
            out.append(_Tok("MINUS", ch))
            i += 1
            continue

        # WORD
        buf = []
        while i < n:
            ch2 = s[i]
            if ch2.isspace() or ch2 in ['"', "(", ")", "|"]:
                break
            # + и - считаем частью слова, если они НЕ в начале токена
            if ch2 in ["+", "-"] and not buf:
                break
            buf.append(ch2)
            i += 1
        word = "".join(buf).strip()
        if word:
            out.append(_Tok("WORD", word))
        else:
            # если упёрлись в + / - в начале — обработаем как оператор
            if i < n and s[i] == "+":
                out.append(_Tok("PLUS", "+"))
                i += 1
            elif i < n and s[i] == "-":
                out.append(_Tok("MINUS", "-"))
                i += 1
            else:
                i += 1

    return out




def _fields_like_expr_text(like_pattern_sql: str) -> str:
    # В Postgres ESCAPE должен быть пустым или состоять из ОДНОГО байта.
    # Надёжный вариант: ESCAPE E'\\' (это ровно один символ backslash).
    esc = " ESCAPE E'\\\\' "
    return (
        f"(COALESCE(p.title, '') ILIKE {like_pattern_sql}{esc}"
        f" OR COALESCE(p.body_html, '') ILIKE {like_pattern_sql}{esc}"
        f" OR COALESCE(p.tag, '') ILIKE {like_pattern_sql}{esc})"
    )


def _fields_like_expr_tags(like_pattern_sql: str) -> str:
    esc = " ESCAPE E'\\\\' "
    return f"(COALESCE(p.tag, '') ILIKE {like_pattern_sql}{esc})"



def _term_to_sql(tok: _Tok, mode: str = "text") -> str:
    """
    WORD / PHRASE -> SQL expr.
    WORD: поддержка wildcard '*' -> '%'

    mode:
      - "text": поиск по title/body_html
      - "tags": поиск по p.tag
    """
    def _fields_expr(pat_sql: str) -> str:
        return _fields_like_expr_tags(pat_sql) if mode == "tags" else _fields_like_expr_text(pat_sql)

    if tok.typ == "PHRASE":
        pat = f"'%{_escape_like(tok.val)}%'"
        return _fields_expr(pat)

    if tok.typ == "WORD":
        w = tok.val.strip()
        if not w:
            return "TRUE"

        if "*" in w:
            # '*' разрешаем как wildcard. В _escape_like '*' не экранируется, но на всякий случай:
            safe = _escape_like(w).replace("\\*", "*")
            safe = safe.replace("*", "%")
            if not safe.startswith("%"):
                safe = "%" + safe
            if not safe.endswith("%"):
                safe = safe + "%"
            return _fields_expr(f"'{safe}'")

        pat = f"'%{_escape_like(w)}%'"
        return _fields_expr(pat)

    return "TRUE"



class _Parser:
    """
    Грамматика (OR ниже AND, как Google):
      expr     := or_expr
      or_expr  := and_expr (OR and_expr)*
      and_expr := unary (unary)*            # неявный AND по соседству
      unary    := (PLUS|MINUS)* primary
      primary  := TERM | '(' expr ')'
      TERM     := WORD | PHRASE
    """
    def __init__(self, toks: list[_Tok], term_sql_func):
        self.toks = toks
        self.i = 0
        self.term_sql_func = term_sql_func  # TERM -> SQL

    def _peek(self) -> _Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _eat(self, typ: str) -> _Tok | None:
        t = self._peek()
        if t and t.typ == typ:
            self.i += 1
            return t
        return None

    def parse(self) -> str:
        if not self.toks:
            return ""
        return self._parse_or()

    def _parse_or(self) -> str:
        left = self._parse_and()
        while self._eat("OR"):
            right = self._parse_and()
            left = f"({left} OR {right})"
        return left

    def _starts_unary_or_primary(self, t: _Tok | None) -> bool:
        return bool(t and t.typ in ("PLUS", "MINUS", "WORD", "PHRASE", "LPAREN"))

    def _parse_and(self) -> str:
        left = self._parse_unary()
        while True:
            t = self._peek()
            if not self._starts_unary_or_primary(t):
                break
            if t and t.typ in ("RPAREN", "OR"):
                break
            right = self._parse_unary()
            left = f"({left} AND {right})"
        return left

    def _parse_unary(self) -> str:
        neg = False
        while True:
            if self._eat("PLUS"):
                continue
            if self._eat("MINUS"):
                neg = not neg
                continue
            break

        prim = self._parse_primary()
        return f"(NOT {prim})" if neg else prim

    def _parse_primary(self) -> str:
        if self._eat("LPAREN"):
            inside = self._parse_or()
            self._eat("RPAREN")
            return f"({inside})"

        t = self._peek()
        if t and t.typ in ("WORD", "PHRASE"):
            self.i += 1
            return self.term_sql_func(t)

        if t:
            self.i += 1
        return "TRUE"




def build_advanced_search_where(search_raw: str, mode: str = "text") -> str:
    toks = _lex_search(search_raw)
    if not toks:
        return ""

    def _term_func(t: _Tok) -> str:
        return _term_to_sql(t, mode=mode)

    parser = _Parser(toks, term_sql_func=_term_func)
    expr_sql = parser.parse().strip()
    if not expr_sql:
        return ""
    return " AND " + expr_sql



def ensure_db_credentials() -> dict[str, str]:
    """Запрос логина/пароля к БД один раз за сессию."""
    creds = st.session_state.get("db_credentials")
    if creds and creds.get("user") and creds.get("password"):
        st.session_state.setdefault("current_user_login", creds["user"])
        return creds

    # если уже открыт другой диалог, сначала закрываем его и запускаем rerun,
    # чтобы окно авторизации показалось отдельно (Streamlit запрещает вложенные диалоги)
    if st.session_state.get("dialog_open"):
        st.session_state["dialog_open"] = False
        st.rerun()
        st.stop()

    @st.dialog("Подключение к базе", width="small")
    def _ask_credentials():
        st.session_state["dialog_open"] = True
        st.write("Введите логин и пароль.")
        with st.form("db_login_form", clear_on_submit=False):
            user = st.text_input("Логин", key="db_login")
            pwd = st.text_input("Пароль", type="password", key="db_password")
            submitted = st.form_submit_button("Подключиться")
            if submitted:
                if not user or not pwd:
                    st.error("Укажите логин и пароль.")
                    return
                if not test_connection(user, pwd):
                    st.error("Не удалось подключиться. Проверьте данные.")
                    return
                st.session_state["db_credentials"] = {"user": user, "password": pwd}
                st.session_state["current_user_login"] = user
                add_event_log(topic="LOGON", subtopic="LOGON",notebook_id=0, section_id=0, page_id=0, event="LOGON user " + user, body_html="")
                st.success("Подключение установлено. Обновляем страницу...")
                st.session_state["dialog_open"] = False
                st.rerun()

    _ask_credentials()
    st.session_state["dialog_open"] = False
    st.stop()


def cleanup_docs_dir() -> None:
    base_dir = os.path.dirname(__file__)
    docs_dir = os.path.join(base_dir, config.temp_attachments_dir)
    if not os.path.isdir(docs_dir):
        return

    today = datetime.date.today()
    for name in os.listdir(docs_dir):
        if name in ("__init__", "__init__.py"):
            continue
        path = os.path.join(docs_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            mtime = datetime.date.fromtimestamp(os.path.getmtime(path))
        except OSError:
            continue
        if mtime < today:
            try:
                os.remove(path)
                logger.info("Удален устаревший файл: %s", path)
            except OSError as exc:
                logger.warning("Не удалось удалить файл %s: %s", path, exc)


st.session_state.setdefault("edit_dialog_page_id", None)
st.session_state.setdefault("download_payload", None)  # tuple[bytes, str, str] | None
st.session_state.setdefault("download_att_id", None)  # int | None
st.session_state.setdefault("download_error", None)  # str | None
st.session_state.setdefault("open_editor_once_for_page", None)  # int|None
st.session_state.setdefault("open_editor_once_edit_mode", False)  # bool: открыть диалог сразу в режиме редактирования
st.session_state.setdefault("dialog_open", False)  # защита от вложенных диалогов
st.session_state.setdefault("email_dialog_page_id", None)  # int | None
st.session_state.setdefault("append_dialog_page_id", None)  # int | None
st.session_state.setdefault("_expanders_state_json", "")  # str (JSON from UI bridge)


def _creds() -> tuple[str, str]:
    creds = ensure_db_credentials()
    return creds["user"], creds["password"]


def run_fetch_df(query: str) -> pd.DataFrame:
    user, pwd = _creds()
    engine = None
    try:
        engine = make_engine(user, pwd)
        df = get_fetch(engine, query)
        if df is None:
            return pd.DataFrame()
        return df
    except Exception as e:
        logger.exception("DB fetch error: %s", e)
        st.error(f"Ошибка запроса к БД: {e}")
        return pd.DataFrame()
    finally:
        if engine is not None:
            engine.dispose()



def run_execute(query: str) -> int | None:
    user, pwd = _creds()
    engine = None
    try:
        engine = make_engine(user, pwd)
        return get_execute(engine, query)
    finally:
        if engine is not None:
            engine.dispose()


def run_scalar(query: str):
    user, pwd = _creds()
    engine = None
    try:
        engine = make_engine(user, pwd)
        return execute_scalar(engine, query)
    except Exception as e:
        logger.exception("DB scalar error: %s", e)
        st.error(f"Ошибка запроса к БД: {e}")
        return None
    finally:
        if engine is not None:
            engine.dispose()


def list_users() -> pd.DataFrame:
    return run_fetch_df(
        f"""
        SELECT login, full_name, department_id
        FROM {USERS_TABLE}
        ORDER BY COALESCE(full_name, login)
        """
    )


# По page_id возвращает notebook_id, section_id
def get_nb_section_id(page_id: int) -> tuple[int, int]:
    if page_id == 0 or page_id is None:
        return 0, 0

    user, pwd = _creds()
    engine = None
    try:
        engine = make_engine(user, pwd)
        row = get_fetchone(
            engine,
            f"""
            SELECT s.notebook_id, p.section_id
            FROM {PAGES_TABLE} p
                INNER JOIN {SECTIONS_TABLE} s ON p.section_id = s.id
            WHERE p.id = {int(page_id)}
            LIMIT 1
            """,
            as_dict=True,
        )
        if row is None:
            return 0, 0
        return int(row["notebook_id"]), int(row["section_id"])
    finally:
        if engine is not None:
            engine.dispose()


def get_user_signature(login: str) -> tuple[str, str]:
    user, pwd = _creds()
    engine = None
    try:
        engine = make_engine(user, pwd)
        row = get_fetchone(
            engine,
            f"""
            SELECT full_name, job_title
            FROM {USERS_TABLE}
            WHERE login = '{_escape(login)}'
            LIMIT 1
            """,
            as_dict=True,
        )
        if row is None:
            return login, ""

        full_name = str(row.get("full_name") or "").strip() or login
        job_title = str(row.get("job_title") or "").strip()
        return full_name, job_title
    finally:
        if engine is not None:
            engine.dispose()


def list_email_recipients() -> pd.DataFrame:
    return run_fetch_df(
        f"""
        SELECT email, fio, job_title, salutation
        FROM {EMAIL_RECIPIENTS_TABLE}
        ORDER BY COALESCE(fio, email)
        """
    )


def get_departments() -> pd.DataFrame:
    """Справочник подразделений (включая '99 Создатель книги')."""
    return run_fetch_df(
        f"""
        SELECT department_id, name_department
        FROM {DEPARTMENTS_TABLE}
        ORDER BY name_department
        """
    )



def add_notebook_owner(notebook_id: int, user_login: str) -> None:
    # logger.info("Добавление владельца: notebook_id=%s user=%s", notebook_id, user_login)
    run_execute(
        f"""
        INSERT INTO {OWNERS_TABLE} (notebook_id, user_login)
        VALUES ({int(notebook_id)}, '{_escape(user_login)}')
        """
    )


def add_event_log( topic: str, subtopic: str,notebook_id: int, section_id: int, page_id: int , event: str, body_html : str) -> None:
    run_execute(f"""
            INSERT INTO {EVENT_LOGS_TABLE} (user_name, topic, subtopic, notebook_id, section_id, page_id, "event", body_html)
            VALUES ('{st.session_state["current_user_login"]}','{_escape(topic)}','{_escape(subtopic)}',{int(notebook_id)},{int(section_id)},{int(page_id)},'{_escape(event)}','{_escape(body_html)}' )
            """
    )        


def remove_notebook_owner(notebook_id: int, user_login: str) -> None:
    """Удалить пользователя из владельцев книги."""
    run_execute(
        f"""
        DELETE FROM {OWNERS_TABLE}
        WHERE notebook_id = {int(notebook_id)}
          AND user_login = '{_escape(user_login)}'
        """
    )


def set_notebook_department(notebook_id: int, department_id: str | None) -> None:
    """
    Область видимости книги определяется department_id книги.

    - '00' или NULL -> видна всем
    - '01.01'       -> видна пользователям из '01.01' и ниже ('01.01.*')
    - '99'          -> видна только владельцам (owners)
    """
    dep_value = "NULL" if not department_id else f"'{_escape(department_id)}'"
    run_execute(
        f"""
        UPDATE {NOTEBOOKS_TABLE}
        SET department_id = {dep_value}, updated_at = NOW()
        WHERE id = {int(notebook_id)}
        """
    )


def is_notebook_owner(notebook_id: int, user_login: str) -> bool:
    result = run_scalar(
        f"""
        SELECT 1
        FROM {OWNERS_TABLE}
        WHERE notebook_id = {int(notebook_id)} AND user_login = '{_escape(user_login)}'
        LIMIT 1
        """
    )
    return bool(result)


def get_notebook_owners(notebook_id: int) -> pd.DataFrame:
    return run_fetch_df(
        f"""
        SELECT o.user_login AS login, u.full_name
        FROM {OWNERS_TABLE} o
        LEFT JOIN {USERS_TABLE} u ON u.login = o.user_login
        WHERE o.notebook_id = {int(notebook_id)}
        ORDER BY COALESCE(u.full_name, o.user_login)
        """
    )


def get_notebooks(user_login: str, user_department_id: str | None) -> pd.DataFrame:
    """
    Возвращает книги, которые пользователь может хотя бы читать.

    Правила видимости:
      - department_id IS NULL или '00' -> видна всем
      - department_id = '99'           -> видна только владельцам книги (notes_notebook_owners)
      - department_id = 'X'            -> видна пользователям из 'X' и всем подчинённым (X.*)
      - владелец книги видит её всегда
    """
    ulogin = _escape(user_login)
    user_dep = _escape(user_department_id or "")

    owner_condition = f"""
        n.id IN (
            SELECT notebook_id
            FROM {OWNERS_TABLE}
            WHERE user_login = '{ulogin}'
        )
    """

    if user_dep:
        dept_visibility_non99 = f"""
            (
                n.department_id IS NULL OR n.department_id = '00'
                OR '{user_dep}' = n.department_id
                OR '{user_dep}' LIKE (n.department_id || '.%')
            )
        """
    else:
        dept_visibility_non99 = " (n.department_id IS NULL OR n.department_id = '00') "

    return run_fetch_df(
        f"""
        SELECT n.id,
               n.name,
               n.department_id,
               n.created_at,
               n.updated_at,
               n.created_by
        FROM {NOTEBOOKS_TABLE} n
        WHERE
            (n.department_id = '99' AND ({owner_condition}))
            OR
            ((n.department_id IS NULL OR n.department_id <> '99')
                AND ( {owner_condition} OR {dept_visibility_non99} )
            )
        ORDER BY n.name
        """
    )


def get_owned_notebooks(user_login: str) -> pd.DataFrame:
    return run_fetch_df(
        f"""
        SELECT n.id,
               n.name,
               n.department_id,
               n.created_at,
               n.updated_at,
               n.created_by
        FROM {NOTEBOOKS_TABLE} n
        JOIN {OWNERS_TABLE} o
          ON o.notebook_id = n.id
        WHERE o.user_login = '{_escape(user_login)}'
        ORDER BY n.name
        """
    )


def get_sections(notebook_id: int | None) -> pd.DataFrame:
    params = {
        "notebook_id": int(notebook_id or 0),
        "sidebar_sources_version": int(st.session_state.get("sidebar_sources_version", 0)),
    }

    def _fetch() -> pd.DataFrame:
        query = f"""
            SELECT id, notebook_id, name, created_at, updated_at, created_by
            FROM {SECTIONS_TABLE}
        """
        if notebook_id:
            query += f" WHERE notebook_id = {int(notebook_id)}"
        query += " ORDER BY name DESC"
        return run_fetch_df(query)

    return _cached_df("cache_sections", params, _fetch)


def load_pages_df(
    notebook_id: int | None,
    section_id: int | None,
    allowed_notebook_ids: list[int],
    search_text: str | None,
    search_tags_only: bool,
) -> pd.DataFrame:
    if not allowed_notebook_ids:
        return pd.DataFrame()

    params = (
        int(notebook_id or 0),
        int(section_id or 0),
        tuple(sorted(int(x) for x in allowed_notebook_ids)),
        (search_text or "").strip(),
        bool(search_tags_only),
        int(st.session_state.get("sidebar_sources_version", 0)),
    )

    def _fetch() -> pd.DataFrame:
        query = f"""
        SELECT
            p.id,
            p.title,
            p.status
        FROM {PAGES_TABLE} p
            JOIN {SECTIONS_TABLE} s ON p.section_id = s.id
        WHERE 1=1
        """

        allowed_csv = ", ".join(str(int(x)) for x in allowed_notebook_ids)
        query += f" AND s.notebook_id IN ({allowed_csv})"

        if section_id:
            query += f" AND s.id = {int(section_id)}"

        if search_text:
            if search_tags_only:
                query += build_advanced_search_where(search_text, mode="tags")
            else:
                query += build_advanced_search_where(search_text, mode="text")

        query += " ORDER BY p.id DESC"

        df = run_fetch_df(query)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    return _cached_df("cache_pages_list", params, _fetch)


def load_page_detail_df(page_id: int) -> dict[str, Any] | None:
    params = {
        "page_id": int(page_id),
        "sidebar_sources_version": int(st.session_state.get("sidebar_sources_version", 0)),
    }

    def _fetch() -> dict[str, Any] | None:
        query = f"""
        SELECT
            p.id,
            p.title,
            p.status,
            p.tag,
            p.body_html,
            s.id AS section_id,
            s.name AS section_name,
            n.id AS notebook_id,
            n.name AS notebook_name,
            n.department_id AS notebook_department_id
        FROM {PAGES_TABLE} p
            JOIN {SECTIONS_TABLE} s ON p.section_id = s.id
            JOIN {NOTEBOOKS_TABLE} n ON s.notebook_id = n.id
        WHERE p.id = {int(page_id)}
        LIMIT 1
        """
        user, pwd = _creds()
        engine = None
        try:
            engine = make_engine(user, pwd)
            row = get_fetchone(engine, query, as_dict=True)
            return row if isinstance(row, dict) else None
        finally:
            if engine is not None:
                engine.dispose()

    return _cached_value("cache_page_detail", params, _fetch, default=None)
def create_notebook(name: str, user_login: str, department_id: str | None) -> int:
    cleaned = name.strip() or "Новая книга"
    dept_value = f"'{_escape(department_id)}'" if department_id else "NULL"
    new_id = run_scalar(
        f"""
        INSERT INTO {NOTEBOOKS_TABLE} (name, created_by, department_id)
        VALUES ('{_escape(cleaned)}', '{_escape(user_login)}', {dept_value})
        RETURNING id
        """
    )
    if new_id is None:
        raise RuntimeError("Не удалось создать книгу")
    add_notebook_owner(int(new_id), user_login)
    return int(new_id)


def create_section(notebook_id: int, name: str, user_login: str) -> int:
    cleaned = name.strip() or "Новый раздел"
    new_id = run_scalar(
        f"""
        INSERT INTO {SECTIONS_TABLE} (notebook_id, name, created_by)
        VALUES ({int(notebook_id)}, '{_escape(cleaned)}', '{_escape(user_login)}')
        RETURNING id
        """
    )
    if new_id is None:
        raise RuntimeError("Не удалось создать раздел")
    return int(new_id)


def rename_section(section_id: int, new_name: str) -> None:
    cleaned = (new_name or "").strip() or "Новый раздел"
    run_execute(
        f"""
        UPDATE {SECTIONS_TABLE}
        SET name = '{_escape(cleaned)}', updated_at = NOW()
        WHERE id = {int(section_id)}
        """
    )


def get_section_pages_count(section_id: int) -> int:
    cnt = run_scalar(
        f"""
        SELECT COUNT(*)
        FROM {PAGES_TABLE}
        WHERE section_id = {int(section_id)}
        """
    )
    return int(cnt or 0)


def delete_section(section_id: int) -> None:
    run_execute(f"DELETE FROM {SECTIONS_TABLE} WHERE id = {int(section_id)}")


def create_page(section_id: int, user_login: str, title: str | None = None) -> int:
    page_title = (title or "").strip() or "Новая страница"
    new_id = run_scalar(
        f"""
        INSERT INTO {PAGES_TABLE} (section_id, title, tag, body_html, created_by)
        VALUES ({int(section_id)}, '{_escape(page_title)}', '', '', '{_escape(user_login)}')
        RETURNING id
        """
    )
    if new_id is None:
        raise RuntimeError("Не удалось создать страницу")
    return int(new_id)


def insert_page_with_content(section_id: int, title: str, body_html: str, user_login: str) -> int:
    raw = body_html or ""

    if _import_mod._looks_like_imported_html(raw) or "mso-" in raw.lower():
        raw = _import_mod.strip_onenote_datetime_block(raw)
        raw = _import_mod.normalize_onenote_rich_html(raw)

    safe_html = _import_mod.sanitize_html_safe(raw)

    new_id = run_scalar(
        f"""
        INSERT INTO {PAGES_TABLE} (section_id, title, tag, body_html, created_by)
        VALUES ({int(section_id)},
                '{_escape(title.strip() or 'Untitled')}',
                '',
                '{_escape(safe_html)}',
                '{_escape(user_login)}')
        RETURNING id
        """
    )
    if new_id is None:
        raise RuntimeError("Не удалось импортировать страницу")
    return int(new_id)
STATUS_COLOR_OPTIONS = [
    ("#FFFFFF", "Без цвета"),
    ("#FFF59D", "Жёлтый"),
    ("#90CAF9", "Голубой"),
    ("#A5D6A7", "Зелёный"),
    ("#EF9A9A", "Красный"),
    ("#CE93D8", "Фиолетовый"),
]


def _normalize_status(val: str | None) -> str:
    v = (val or "").strip().upper()
    allowed = {c for c, _ in STATUS_COLOR_OPTIONS}
    if v not in allowed:
        return "#FFFFFF"
    return v


def update_page(page_id: int, title: str, body_html: str, tag: str, status: str) -> None:
    raw = body_html or ""
    status_norm = _normalize_status(status)

    if _import_mod._looks_like_imported_html(raw) or "mso-" in raw.lower():
        raw = _import_mod.strip_onenote_datetime_block(raw)
        raw = _import_mod.normalize_onenote_rich_html(raw)

    safe_html = _import_mod.sanitize_html_safe(raw)

    run_execute(
        f"""
        UPDATE {PAGES_TABLE}
        SET title = '{_escape(title.strip() or 'Без названия')}',
            tag = '{_escape(tag)}',
            status = '{_escape(status_norm)}',
            body_html = '{_escape(safe_html)}',
            updated_at = NOW()
        WHERE id = {int(page_id)}
        """
    )
    _bump_data_version()

    _notebook_id,_section_id = get_nb_section_id(int(page_id))
    add_event_log(
        topic="PAGE",
        subtopic="UPGRADE",
        notebook_id=_notebook_id,
        section_id=_section_id,
        page_id=int(page_id),
        event="new_title = " + _escape(title.strip()) + "; new_tag = " + _escape(tag) + "; new_status = " + _escape(status),
        body_html=_escape(safe_html),
    )

def delete_page(page_id: int) -> None:
    run_execute(f"DELETE FROM {PAGES_TABLE} WHERE id = {int(page_id)}")
    _bump_data_version()


def get_page_attachments(page_id: int) -> pd.DataFrame:
    params = {
        "page_id": int(page_id),
        "sidebar_sources_version": int(st.session_state.get("sidebar_sources_version", 0)),
    }

    def _fetch() -> pd.DataFrame:
        return run_fetch_df(
            f"""
            SELECT id,
                   attachment_type,
                   file_name,
                   mime_type,
                   file_size,
                   url,
                   created_at,
                   created_by,
                   question_number
            FROM {ATTACHMENTS_TABLE}
            WHERE page_id = {int(page_id)}
            ORDER BY
                CASE
                WHEN COALESCE(question_number,'') ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN COALESCE(question_number,'')::numeric
                ELSE NULL END
                , created_at DESC, id DESC
            """
        )

    return _cached_df("cache_page_attachments", params, _fetch)


def delete_attachment(attachment_id: int) -> None:
    run_execute(f"DELETE FROM {ATTACHMENTS_TABLE} WHERE id = {int(attachment_id)}")
    _bump_data_version()


def get_attachment_file(attachment_id: int) -> tuple[bytes, str, str] | None:
    df = run_fetch_df(
        f"""
        SELECT
            encode(file_data, 'base64') AS file_b64,
            file_name,
            COALESCE(mime_type, 'application/octet-stream') AS mime_type,
            file_size
        FROM {ATTACHMENTS_TABLE}
        WHERE id = {int(attachment_id)} AND attachment_type = 'file'
        LIMIT 1
        """
    )
    if df.empty:
        return None

    file_b64 = df.at[0, "file_b64"]
    file_name = str(df.at[0, "file_name"])
    mime_type = str(df.at[0, "mime_type"])
    file_size = df.at[0, "file_size"]

    if file_b64 is None:
        return None

    if isinstance(file_b64, (bytes, bytearray, memoryview)):
        file_b64 = file_b64.decode("ascii", errors="ignore")
    else:
        file_b64 = str(file_b64)

    file_b64 = file_b64.replace("\n", "").replace("\r", "").strip()

    try:
        data = base64.b64decode(file_b64, validate=False)
    except Exception as e:
        logger.error("Не удалось раскодировать base64 для вложения %s: %s", attachment_id, e)
        return None

    try:
        if file_size and int(file_size) != len(data):
            logger.warning(
                "Размер файла не совпадает: file_size=%s, len(data)=%s, attachment_id=%s",
                file_size,
                len(data),
                attachment_id,
            )
    except Exception:
        pass

    return data, file_name, mime_type



def save_file_attachment(page_id: int, uploaded_file, user_login: str, question_number: str | None = None) -> None:
    if uploaded_file is None:
        return
    content = uploaded_file.getvalue()
    if not content:
        raise ValueError("Файл пустой или не удалось прочитать.")
    mime_type = uploaded_file.type or "application/octet-stream"
    encoded = base64.b64encode(content).decode("ascii")
    question_number_clean = (question_number or "").strip()
    question_number_sql = f"'{_escape(question_number_clean)}'" if question_number_clean else "NULL"
    run_execute(
        f"""
        INSERT INTO {ATTACHMENTS_TABLE}
            (page_id, attachment_type, file_name, mime_type, file_size, file_data, created_by, question_number)
        VALUES
            ({int(page_id)},
             'file',
             '{_escape(uploaded_file.name)}',
             '{_escape(mime_type)}',
             {len(content)},
             decode('{encoded}', 'base64'),
             '{_escape(user_login)}',
              replace({question_number_sql}, ',', '.'))
        """
    )
    _notebook_id,_section_id = get_nb_section_id(int(page_id))
    add_event_log(topic="ATTACHMENT", subtopic="UPLOAD",notebook_id=_notebook_id, section_id=_section_id, page_id=int(page_id), event="load_attachment: attachment_type = 'file'; file_name = " + _escape(uploaded_file.name) + ": file_size = " + str(len(content)) , body_html="")
    _bump_data_version()

def save_link_attachment(page_id: int, url: str, title: str, user_login: str, question_number: str | None = None) -> None:
    cleaned_url = (url or "").strip()
    if not cleaned_url:
        raise ValueError("URL не указан.")
    low = cleaned_url.lower()
    if low.startswith(("javascript:", "vbscript:", "data:text/html", "data:application/xhtml+xml")):
        raise ValueError("Запрещённый URL (возможная XSS-атака).")

    name = (title or "").strip() or cleaned_url
    question_number_clean = (question_number or "").strip()
    question_number_sql = f"'{_escape(question_number_clean)}'" if question_number_clean else "NULL"
    run_execute(
        f"""
        INSERT INTO {ATTACHMENTS_TABLE}
            (page_id, attachment_type, file_name, url, created_by, question_number)
        VALUES
            ({int(page_id)},
             'link',
             '{_escape(name)}',
             '{_escape(cleaned_url)}',
             '{_escape(user_login)}',
             {question_number_sql})
        """
    )
    _notebook_id,_section_id = get_nb_section_id(int(page_id))
    add_event_log(topic="ATTACHMENT", subtopic="UPLOAD",notebook_id=_notebook_id, section_id=_section_id, page_id=int(page_id), event="load_attachment: attachment_type = 'link'; name = " + _escape(name), body_html=_escape(cleaned_url))
    _bump_data_version()




def _bump_data_version() -> None:
    st.session_state["data_version"] = int(st.session_state.get("data_version", 0)) + 1


def _bump_sidebar_sources_version() -> None:
    st.session_state["sidebar_sources_version"] = int(st.session_state.get("sidebar_sources_version", 0)) + 1


def _touch_sidebar_nav_state() -> None:
    st.session_state["_sidebar_nav_last_ts"] = time.time()


def _sidebar_sources_refresh_seconds() -> int:
    raw = getattr(config, "sidebar_sources_refresh_seconds", DEFAULT_SIDEBAR_REFRESH_SECONDS)
    try:
        value = int(raw)
    except Exception:
        value = DEFAULT_SIDEBAR_REFRESH_SECONDS
    return max(0, value)


def _to_int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _current_refresh_context_ids() -> tuple[int, int, int]:
    """
    Текущий UI-контекст для автообновления:
    (книга, раздел, текущая страница).
    """
    notebook_id = _to_int_or_zero(st.session_state.get("selected_notebook_id"))
    section_id = _to_int_or_zero(st.session_state.get("selected_section_id"))
    page_id = _to_int_or_zero(st.session_state.get("current_page_id"))
    return notebook_id, section_id, page_id


def _build_refresh_context_signature(notebook_id: int, section_id: int, page_id: int) -> str | None:
    """
    Сигнатура данных только по текущему контексту:
    - текущая книга
    - текущий раздел
    - список страниц текущего раздела
    - текущая страница
    - список вложений текущей страницы
    """
    query = f"""
    SELECT
        md5(
            concat_ws(
                '|',
                CASE
                    WHEN :notebook_id > 0 THEN COALESCE(
                        (
                            SELECT md5(
                                COALESCE(n.id::text, '') || ':' ||
                                COALESCE(n.name, '') || ':' ||
                                COALESCE(n.department_id, '') || ':' ||
                                COALESCE(to_char(n.updated_at, 'YYYY-MM-DD HH24:MI:SS.US'), '')
                            )
                            FROM {NOTEBOOKS_TABLE} n
                            WHERE n.id = :notebook_id
                            LIMIT 1
                        ),
                        'nb:missing'
                    )
                    ELSE 'nb:none'
                END,
                CASE
                    WHEN :section_id > 0 THEN COALESCE(
                        (
                            SELECT md5(
                                COALESCE(s.id::text, '') || ':' ||
                                COALESCE(s.notebook_id::text, '') || ':' ||
                                COALESCE(s.name, '') || ':' ||
                                COALESCE(to_char(s.updated_at, 'YYYY-MM-DD HH24:MI:SS.US'), '')
                            )
                            FROM {SECTIONS_TABLE} s
                            WHERE s.id = :section_id
                            LIMIT 1
                        ),
                        'section:missing'
                    )
                    ELSE 'section:none'
                END,
                CASE
                    WHEN :page_id > 0 THEN COALESCE(
                        (
                            SELECT md5(
                                COALESCE(n.id::text, '') || ':' ||
                                COALESCE(n.name, '') || ':' ||
                                COALESCE(n.department_id, '') || ':' ||
                                COALESCE(to_char(n.updated_at, 'YYYY-MM-DD HH24:MI:SS.US'), '') || ':' ||
                                COALESCE(s.id::text, '') || ':' ||
                                COALESCE(s.name, '') || ':' ||
                                COALESCE(to_char(s.updated_at, 'YYYY-MM-DD HH24:MI:SS.US'), '')
                            )
                            FROM {PAGES_TABLE} p
                            JOIN {SECTIONS_TABLE} s ON s.id = p.section_id
                            JOIN {NOTEBOOKS_TABLE} n ON n.id = s.notebook_id
                            WHERE p.id = :page_id
                            LIMIT 1
                        ),
                        'page-path:missing'
                    )
                    ELSE 'page-path:none'
                END,
                CASE
                    WHEN :section_id > 0 THEN COALESCE(
                        (
                            SELECT md5(
                                COALESCE(
                                    string_agg(
                                        p.id::text || ':' ||
                                        COALESCE(p.title, '') || ':' ||
                                        COALESCE(p.status, '') || ':' ||
                                        COALESCE(to_char(p.updated_at, 'YYYY-MM-DD HH24:MI:SS.US'), ''),
                                        '||'
                                        ORDER BY p.id
                                    ),
                                    ''
                                )
                            )
                            FROM {PAGES_TABLE} p
                            WHERE p.section_id = :section_id
                        ),
                        'pages:missing'
                    )
                    ELSE 'pages:none'
                END,
                CASE
                    WHEN :page_id > 0 THEN COALESCE(
                        (
                            SELECT md5(
                                COALESCE(p.id::text, '') || ':' ||
                                COALESCE(p.section_id::text, '') || ':' ||
                                COALESCE(p.title, '') || ':' ||
                                COALESCE(p.tag, '') || ':' ||
                                COALESCE(p.status, '') || ':' ||
                                COALESCE(to_char(p.updated_at, 'YYYY-MM-DD HH24:MI:SS.US'), '') || ':' ||
                                COALESCE(md5(p.body_html), '')
                            )
                            FROM {PAGES_TABLE} p
                            WHERE p.id = :page_id
                            LIMIT 1
                        ),
                        'page:missing'
                    )
                    ELSE 'page:none'
                END,
                CASE
                    WHEN :page_id > 0 THEN COALESCE(
                        (
                            SELECT md5(
                                COALESCE(
                                    string_agg(
                                        a.id::text || ':' ||
                                        COALESCE(a.attachment_type, '') || ':' ||
                                        COALESCE(a.file_name, '') || ':' ||
                                        COALESCE(a.url, '') || ':' ||
                                        COALESCE(a.question_number, '') || ':' ||
                                        COALESCE(to_char(a.created_at, 'YYYY-MM-DD HH24:MI:SS.US'), ''),
                                        '||'
                                        ORDER BY a.id
                                    ),
                                    ''
                                )
                            )
                            FROM {ATTACHMENTS_TABLE} a
                            WHERE a.page_id = :page_id
                        ),
                        'attachments:missing'
                    )
                    ELSE 'attachments:none'
                END
            )
        ) AS ctx_sig
    """

    user, pwd = _creds()
    engine = None
    try:
        engine = make_engine(user, pwd)
        row = get_fetchone(
            engine,
            query,
            params={
                "notebook_id": int(notebook_id),
                "section_id": int(section_id),
                "page_id": int(page_id),
            },
            as_dict=True,
        )
        if isinstance(row, dict):
            sig = row.get("ctx_sig")
            if sig:
                return str(sig)
    except Exception as e:
        logger.warning("Auto-refresh context signature failed: %s", e)
    finally:
        if engine is not None:
            engine.dispose()
    return None


def _run_sidebar_sources_auto_refresh() -> None:
    interval_s = _sidebar_sources_refresh_seconds()
    if interval_s <= 0:
        return

    @st.fragment(run_every=interval_s)
    def _sidebar_sources_refresh_fragment() -> None:
        now = time.time()
        last_key = "_sidebar_sources_last_refresh_ts"
        ctx_ids_key = "_sidebar_sources_ctx_ids"
        sig_key = "_sidebar_sources_ctx_sig"
        expand_block_prev_key = "_sidebar_sources_expand_blocked_prev"
        edit_expand_prev_key = "_sidebar_sources_edit_expand_prev_open"

        blocked_by_expanders = _is_expanders_auto_refresh_blocked()
        prev_expand_blocked = bool(st.session_state.get(expand_block_prev_key, False))
        st.session_state[expand_block_prev_key] = blocked_by_expanders
        is_edit_expand_open = _is_expander_open("Редактировать страницу")
        was_edit_expand_open = bool(st.session_state.get(edit_expand_prev_key, False))
        st.session_state[edit_expand_prev_key] = is_edit_expand_open
        edit_expand_just_closed = was_edit_expand_open and not is_edit_expand_open

        # После сворачивания expand-блоков запускаем проверку без ожидания полного интервала.
        if (prev_expand_blocked and not blocked_by_expanders) or edit_expand_just_closed:
            st.session_state[last_key] = 0.0

        if edit_expand_just_closed:
            _force_sidebar_sources_auto_refresh()
            st.rerun()
            return

        last = float(st.session_state.get(last_key, 0.0))
        ctx_ids = _current_refresh_context_ids()
        prev_ctx_ids = st.session_state.get(ctx_ids_key)

        # При смене контекста просто обновляем базовую сигнатуру без принудительного rerun.
        if prev_ctx_ids != ctx_ids:
            st.session_state[ctx_ids_key] = ctx_ids
            st.session_state[sig_key] = _build_refresh_context_signature(*ctx_ids)
            st.session_state[last_key] = now
            return

        if last <= 0:
            st.session_state[last_key] = now
            if st.session_state.get(sig_key) is None:
                st.session_state[sig_key] = _build_refresh_context_signature(*ctx_ids)
            return

        if now - last >= max(interval_s - 0.2, 0):
            recent_nav_ts = float(st.session_state.get("_sidebar_nav_last_ts", 0.0))
            cooldown_s = min(max(interval_s * 0.4, 1.0), 5.0)
            if recent_nav_ts > 0 and (now - recent_nav_ts) < cooldown_s:
                st.session_state[last_key] = now
                return

            st.session_state[last_key] = now
            current_sig = _build_refresh_context_signature(*ctx_ids)
            if current_sig is None:
                # fallback: если сигнатуру собрать не удалось, всё равно принудительно
                # обновим источники, чтобы подтянуть внешние изменения.
                _bump_sidebar_sources_version()
                st.rerun()
                return

            prev_sig = st.session_state.get(sig_key)
            if prev_sig is None:
                st.session_state[sig_key] = current_sig
                return

            if str(current_sig) != str(prev_sig):
                st.session_state[sig_key] = current_sig
                _bump_sidebar_sources_version()
                st.rerun()

    _sidebar_sources_refresh_fragment()


def _force_sidebar_sources_auto_refresh() -> None:
    _bump_sidebar_sources_version()
    now = time.time()
    st.session_state["_sidebar_sources_last_refresh_ts"] = now
    ctx_ids = _current_refresh_context_ids()
    st.session_state["_sidebar_sources_ctx_ids"] = ctx_ids
    st.session_state["_sidebar_sources_ctx_sig"] = _build_refresh_context_signature(*ctx_ids)


AUTO_REFRESH_TRACK_EXPANDER_LABELS = (
    "Экспорт",
    "Редактировать страницу",
    "Прикрепить файлы",
    "Переместить или скопировать",
)

AUTO_REFRESH_BLOCK_EXPANDER_LABELS = (
    "Экспорт",
    "Прикрепить файлы",
    "Переместить или скопировать",
)


def _read_expanders_state() -> dict[str, Any] | None:
    raw = st.session_state.get("_expanders_state_json", "")
    if not raw:
        return None
    try:
        data = json.loads(str(raw))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # Если мост перестал обновлять состояние, не держим автообновление заблокированным бесконечно.
    ts_ms = float(data.get("__ts", 0) or 0)
    now_ms = time.time() * 1000.0
    if ts_ms <= 0 or (now_ms - ts_ms) > 2500.0:
        return None
    return data


def _is_expanders_auto_refresh_blocked() -> bool:
    data = _read_expanders_state()
    if not data:
        return False
    return any(bool(data.get(label, False)) for label in AUTO_REFRESH_BLOCK_EXPANDER_LABELS)


def _is_expander_open(label: str) -> bool:
    data = _read_expanders_state()
    if not data:
        return False
    return bool(data.get(label, False))


def _render_expanders_state_bridge() -> None:
    st.markdown(
        """
        <style>
        .st-key-_expanders_state_json { display: none !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.text_input("_expanders_state_json", key="_expanders_state_json", label_visibility="collapsed")

    labels_json = json.dumps(list(AUTO_REFRESH_TRACK_EXPANDER_LABELS), ensure_ascii=False)
    bridge_html = f"""
    <script>
    (function () {{
      const TARGETS = {labels_json};
      const doc = (window.parent && window.parent.document) ? window.parent.document : document;

      function normalizeText(s) {{
        return String(s || "").replace(/\\u200b/g, "").trim();
      }}

      function collectStatesJson() {{
        const state = {{}};
        TARGETS.forEach((label) => {{ state[label] = false; }});
        const expanders = doc.querySelectorAll(
          'details[data-testid="stExpander"], [data-testid="stExpander"] details'
        );
        expanders.forEach((exp) => {{
          const summary = exp.querySelector("summary");
          const title = normalizeText(summary ? summary.innerText : "");
          TARGETS.forEach((label) => {{
            if (title.startsWith(label)) {{
              state[label] = !!exp.open;
            }}
          }});
        }});
        state.__ts = Date.now();
        return JSON.stringify(state);
      }}

      function findStateInput() {{
        const byClass = doc.querySelector(".st-key-_expanders_state_json input");
        if (byClass) return byClass;
        const byAria = doc.querySelector('input[aria-label="_expanders_state_json"]');
        if (byAria) return byAria;
        return null;
      }}

      function pushState() {{
        const input = findStateInput();
        if (!input) return;
        const next = collectStatesJson();
        if ((input.value || "") === next) return;
        input.value = next;
        input.dispatchEvent(new Event("input", {{ bubbles: true }}));
        input.dispatchEvent(new Event("change", {{ bubbles: true }}));
      }}

      const timerKey = "__notes_expanders_state_timer";
      if (window[timerKey]) {{
        clearInterval(window[timerKey]);
      }}
      pushState();
      window[timerKey] = setInterval(pushState, 400);
    }})();
    </script>
    """
    components.html(bridge_html, height=0)


def _cached_df(cache_key: str, params, fetcher):
    """
    Простое кэширование DataFrame в session_state с учетом data_version.
    """
    version = int(st.session_state.get("data_version", 0))
    cache = st.session_state.get(cache_key) or {}
    if cache.get("params") == params and cache.get("version") == version and cache.get("data") is not None:
        try:
            return cache["data"].copy()
        except Exception:
            pass

    df = fetcher()
    if df is None:
        df = pd.DataFrame()
    st.session_state[cache_key] = {"params": params, "version": version, "data": df}
    return df


def _cached_value(cache_key: str, params, fetcher, default=None):
    """
    Кэширование произвольного значения в session_state с учетом data_version.
    """
    version = int(st.session_state.get("data_version", 0))
    cache = st.session_state.get(cache_key) or {}
    if cache.get("params") == params and cache.get("version") == version:
        data = cache.get("data", default)
        try:
            return data.copy()
        except Exception:
            return data

    data = fetcher()
    if data is None:
        data = default

    st.session_state[cache_key] = {"params": params, "version": version, "data": data}
    try:
        return data.copy()
    except Exception:
        return data




def render_copy_sql_button(sql_text: str, btn_key: str) -> None:
    render_copy_text_button(
        text=sql_text or "",
        btn_key=btn_key,
        button_label="📋 Копировать",
        ok_message="SQL скопирован в буфер обмена.",
    )


def render_copy_rich_button(
    html: str,
    plain: str,
    btn_key: str,
    button_label: str = "📋 Копировать",
    ok_message: str = "Скопировано",
    button_style: str | None = None,
    align: str = "flex-end",
    height: int = 54,
) -> None:
    """
    Копирует в буфер обмена как при выделении содержимого preview:
    - text/html (для Word) + text/plain (fallback)
    """
    # ВАЖНО: html должен быть уже sanitized
    html_payload = json.dumps(html or "", ensure_ascii=False)
    plain_payload = json.dumps(plain or "", ensure_ascii=False)
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", btn_key)

    style = (
        button_style
        or """
            cursor:pointer;
            width:100%;
            padding:0.60rem 0.90rem;      # как у st.button
            min-height: 2.5rem;
            display:inline-flex;
            align-items:center;
            gap:0.35rem;
            border:1px solid rgba(49,51,63,.2);
            border-radius:0.5rem;
            background: white;
            font-size:14px;
            line-height:1.3;
        """
    )

    components.html(
        f"""
        <style>
        html, body {{ margin: 0; padding: 0; }}
        </style>
        <div style="display:flex; justify-content:{align}; align-items:center; width:100%; margin:0; padding:0;">
          <button id="copy_rich_btn_{safe_id}" style="{style}">{button_label}</button>
          <span id="copy_rich_msg_{safe_id}" style="margin-left:10px; font-size:13px;"></span>
        </div>

        <script>
        (function() {{
          const html = {html_payload};
          const plain = {plain_payload};

          const btn = document.getElementById("copy_rich_btn_{safe_id}");
          const msg = document.getElementById("copy_rich_msg_{safe_id}");

          function ok(message) {{
            if (!msg) return;
            msg.textContent = message || "Скопировано";
            msg.style.color = "green";
            setTimeout(() => {{ msg.textContent = ""; }}, 2500);
          }}

          function fail(message) {{
            if (!msg) return;
            msg.textContent = message || "Не удалось скопировать";
            msg.style.color = "crimson";
            setTimeout(() => {{ msg.textContent = ""; }}, 4000);
          }}

          async function copyRich() {{
            // 1) Современный путь: ClipboardItem (HTML + Text)
            try {{
              if (navigator.clipboard && window.ClipboardItem) {{
                const htmlBlob = new Blob([html], {{ type: "text/html" }});
                const textBlob = new Blob([plain], {{ type: "text/plain" }});
                const item = new ClipboardItem({{
                  "text/html": htmlBlob,
                  "text/plain": textBlob
                }});
                await navigator.clipboard.write([item]);
                ok({json.dumps(ok_message, ensure_ascii=False)});
                return;
              }}
            }} catch(e) {{
              // идём в fallback
            }}

            // 2) Fallback: "как выделили в preview" -> contentEditable + execCommand('copy')
            // Word обычно отлично подхватывает HTML из такого копирования
            try {{
              const host = document.createElement("div");
              host.setAttribute("contenteditable", "true");
              host.style.position = "fixed";
              host.style.left = "-9999px";
              host.style.top = "0";
              host.style.opacity = "0";
              host.style.pointerEvents = "none";

              // Добавим минимальную HTML-обёртку (Word иногда лучше распознаёт)
              host.innerHTML = "<div>" + html + "</div>";

              document.body.appendChild(host);

              const sel = window.getSelection();
              const range = document.createRange();
              range.selectNodeContents(host);
              sel.removeAllRanges();
              sel.addRange(range);

              const res = document.execCommand("copy");
              sel.removeAllRanges();
              document.body.removeChild(host);

              if (res) {{
                ok({json.dumps(ok_message, ensure_ascii=False)});
                return;
              }}
              fail("Браузер запретил копирование.");
            }} catch(e2) {{
              fail("Браузер запретил копирование.");
            }}
          }}

          if (btn) {{
            btn.addEventListener("click", function(ev) {{
              ev.preventDefault();
              ev.stopPropagation();
              copyRich();
            }});
          }}
        }})();
        </script>
        """,
        height=height,
        scrolling=False,
    )



def render_copy_text_button(
    text: str,
    btn_key: str,
    button_label: str = "📋 Копировать",
    ok_message: str = "Скопировано",
    button_style: str | None = None,
    align: str = "flex-end",
    height: int = 54,
) -> None:
    payload = json.dumps(text or "", ensure_ascii=False)
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", btn_key)
    style = (
        button_style
        or """
            cursor:pointer;
            width:100%;
            padding:0.60rem 0.90rem;
            min-height: 2.5rem;
            display:inline-flex;
            align-items:center;
            gap:0.35rem;
            border:1px solid rgba(49,51,63,.2);
            border-radius:0.5rem;
            background: white;
            font-size:14px;
            line-height:1.3;
        """
    )

    components.html(
        f"""
        <style>
        html, body {{ margin: 0; padding: 0; }}
        </style>
        <div style="display:flex; justify-content:{align}; align-items:center; width:100%; margin:0; padding:0;">
          <button id="copy_btn_{safe_id}" style="{style}">
            {button_label}
          </button>
          <span id="copy_msg_{safe_id}" style="margin-left:10px; font-size:13px;"></span>
        </div>

        <script>
        (function() {{
          const text = {payload};
          const btn = document.getElementById("copy_btn_{safe_id}");
          const msg = document.getElementById("copy_msg_{safe_id}");

          function ok(message) {{
            if (!msg) return;
            msg.textContent = message || "Скопировано";
            msg.style.color = "green";
            setTimeout(() => {{ msg.textContent = ""; }}, 2500);
          }}

          function fail(message) {{
            if (!msg) return;
            msg.textContent = message || "Не удалось скопировать";
            msg.style.color = "crimson";
            setTimeout(() => {{ msg.textContent = ""; }}, 4000);
          }}

          async function copy() {{
            try {{
              if (navigator.clipboard && navigator.clipboard.writeText) {{
                await navigator.clipboard.writeText(text);
                ok({json.dumps(ok_message, ensure_ascii=False)});
                return;
              }}
            }} catch (e) {{}}

            try {{
              const ta = document.createElement("textarea");
              ta.value = text;
              ta.setAttribute("readonly", "");
              ta.style.position = "fixed";
              ta.style.left = "-9999px";
              ta.style.top = "0";
              document.body.appendChild(ta);
              ta.select();
              const res = document.execCommand("copy");
              document.body.removeChild(ta);
              if (res) {{
                ok({json.dumps(ok_message, ensure_ascii=False)});
              }} else {{
                fail("Браузер запретил копирование.");
              }}
            }} catch (e2) {{
              fail("Браузер запретил копирование.");
            }}
          }}

          if (btn) {{
            btn.addEventListener("click", function(ev) {{
              ev.preventDefault();
              ev.stopPropagation();
              copy();
            }});
          }}
        }})();
        </script>
        """,
        height=height,
        scrolling=False,
    )


def build_page_deeplink(page_id: int) -> str:
    base = (getattr(config, "app_base_url", "") or "").strip()
    if not base:
        return ""

    parsed = urllib.parse.urlparse(base)
    existing = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    existing["page_id"] = str(int(page_id))
    new_query = urllib.parse.urlencode(existing)
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


def get_page_location(page_id: int) -> dict[str, int | str | None] | None:
    df = run_fetch_df(
        f"""
        SELECT
            p.id AS page_id,
            p.section_id AS section_id,
            s.notebook_id AS notebook_id,
            n.department_id AS department_id
        FROM {PAGES_TABLE} p
        JOIN {SECTIONS_TABLE} s ON p.section_id = s.id
        JOIN {NOTEBOOKS_TABLE} n ON s.notebook_id = n.id
        WHERE p.id = {int(page_id)}
        """
    )
    if df is None or df.empty:
        return None
    row = df.iloc[0]
    return {
        "page_id": int(row.get("page_id")),
        "section_id": int(row.get("section_id")),
        "notebook_id": int(row.get("notebook_id")),
        "department_id": None if row.get("department_id") is None else str(row.get("department_id")),
    }


def _close_edit_dialog_state(page_id_local: int | None = None):
    # закрыть редактор и убрать любые "авто-открывалки"
    st.session_state["edit_dialog_page_id"] = None
    st.session_state.pop("force_edit_page_id", None)
    st.session_state.pop("force_edit_page_id_once", None)
    st.session_state["dialog_open"] = False
    if page_id_local is not None:
        for key in (
            f"dlg_title_{page_id_local}",
            f"dlg_tag_{page_id_local}",
            f"dlg_status_{page_id_local}",
            f"dlg_edit_mode_{page_id_local}",
            f"dlg_quill_{page_id_local}",
        ):
            st.session_state.pop(key, None)


@st.dialog("Редактирование страницы", width="large")
def edit_page_dialog(page_id_local: int, title: str, html_body: str, tag: str, status: str, default_edit_mode: bool = False):
    # помечаем, что активен диалог (чтобы не допустить вложенные modals)
    st.session_state["dialog_open"] = True
    st.session_state["edit_dialog_page_id"] = int(page_id_local)
    st.markdown(
        """
        <style>
        div[data-testid="stDialog"] div[role="dialog"]{
            width: 96vw !important;
            max-width: 1400px !important;
        }
        div[data-testid="stDialog"] div[role="dialog"] > div{
            max-height: 90vh !important;
            overflow: auto !important;
        }
        div[data-testid="stDialog"] .ql-container{
            height: 60vh !important;
        }
        div[data-testid="stDialog"] .ql-editor{
            min-height: 60vh !important;
        }

        /* (опционально) скрыть крестик закрытия, чтобы не оставлять "висячие" состояния */
        div[data-testid="stDialog"] button[aria-label="Close"]{
            display: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    col_l, col_tag, col_status = st.columns([11, 8, 3])
    title_key = f"dlg_title_{page_id_local}"
    if title_key not in st.session_state:
        st.session_state[title_key] = title or ""
    with col_l:
        new_title = st.text_input("Название страницы", key=title_key)

    status_options = [c for c, _ in STATUS_COLOR_OPTIONS]
    status_labels = {c: label for c, label in STATUS_COLOR_OPTIONS}
    current_status = _normalize_status(status)

    tag_key = f"dlg_tag_{page_id_local}"
    if tag_key not in st.session_state:
        st.session_state[tag_key] = tag or ""
    with col_tag:
        new_tag = st.text_input(
            "Теги",
            key=tag_key,
            placeholder="Введите тег(и) через запятую без символа #",
        )

    status_key = f"dlg_status_{page_id_local}"
    if st.session_state.get(status_key) not in status_options:
        st.session_state[status_key] = current_status if current_status in status_options else status_options[0]
    with col_status:
        new_status = st.selectbox(
            "Статус",
            options=status_options,
            format_func=lambda v: status_labels.get(v, v),
            key=status_key,
        )



    edit_col, col_2 = st.columns([1, 2])

    edit_mode_key = f"dlg_edit_mode_{page_id_local}"
    if edit_mode_key not in st.session_state:
        st.session_state[edit_mode_key] = bool(default_edit_mode)
    with edit_col:
        edit_mode = st.checkbox(
            "Редактировать содержимое",
            key=edit_mode_key,
        )

    editable_html = html_body or ""

    if edit_mode:
        quill_value = st_quill(
            value=editable_html,
            html=True,
            placeholder="Начните ввод текста...",
            key=f"dlg_quill_{page_id_local}",
        )
        quill_html = editable_html if quill_value is None else (quill_value or "")
    else:
        safe_view_html = _import_mod.sanitize_html_safe(editable_html)
        components.html(safe_view_html, height=420, scrolling=True)
        quill_html = editable_html

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("Сохранить", key=f"dlg_save_{page_id_local}", use_container_width=True):
            update_page(page_id_local, new_title, quill_html, new_tag, new_status)

            _notebook_id,_section_id = get_nb_section_id(page_id_local)
            add_event_log(
                topic="PAGE",
                subtopic="UPGRADE",
                notebook_id=_notebook_id,
                section_id=_section_id,
                page_id=page_id_local,
                event="new_title = " + _escape(new_title) + "; new_tag = " + new_tag + "; new_status = " + new_status,
                body_html=quill_html,
            )
            st.success("Страница обновлена")

            # ✅ закрыть редактор и убрать любые авто-открывалки
            _close_edit_dialog_state(page_id_local)

            st.session_state["current_page_id"] = page_id_local
            st.session_state["force_page_id"] = page_id_local
            st.rerun()

    with c2:
        if st.button("Отмена", key=f"dlg_cancel_{page_id_local}", use_container_width=True):
            _close_edit_dialog_state(page_id_local)
            st.rerun()


# =========================
# ✅ STATUS column: renderer + dblclick cycle
# =========================
STATUS_VALUE_FORMATTER = JsCode(
    """
function(params) {
  return "●"; // рисуем кружок символом, без HTML/DOM
}
"""
)

STATUS_CELL_STYLE = JsCode(
    """
function(params) {
  const c = (params.value || "#FFFFFF").toString().trim();
  return {
    display: "flex",
    justifyContent: "center",
    alignItems: "center",
    // размер кружка = размер шрифта символа "●"
    fontSize: "27px",
    lineHeight: "27px",
    padding: "0",
    color: c,
    // лёгкая обводка/контраст на белом можно имитировать тенью:
    textShadow: "0 0 1px rgba(0,0,0,1)"
  };
}
"""
)



STATUS_DBLCLICK_HANDLER = JsCode(
    """
function(e) {
  if (!e || !e.colDef || e.colDef.field !== 'status') return;

const colors = ['#FFFFFF', '#FFF59D', '#90CAF9', '#A5D6A7', '#EF9A9A', '#CE93D8']; // белый, желтый, зеленый, красный, синий
  let cur = (e.data.status || '#FFFFFF').toString().trim().toUpperCase();

  const idx = colors.indexOf(cur);
  const next = colors[(idx >= 0 ? idx + 1 : 0) % colors.length];

  e.node.setDataValue('status', next);
  e.api.refreshCells({ rowNodes: [e.node], columns: ['status'], force: true });
}
"""
)




def main():
    st.set_page_config(
        layout="wide",
        page_title="ДФИП_Notes",
        page_icon="📒",
        initial_sidebar_state="expanded",
    )

    cleanup_docs_dir()

    st.markdown(
        """
        <style>
        .block-container { padding-top: 0.4rem !important; }
        header[data-testid="stHeader"] { padding-top: 0; padding-bottom: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] { min-width: 350px; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <style>
        div[data-testid="stDialog"] button[aria-label="Close"]{
            display: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    ensure_db_credentials()
    _run_sidebar_sources_auto_refresh()
    owned_notebooks_df = pd.DataFrame()

    # --- deep-link: открыть страницу по `?page_id=<id>` ---
    try:
        raw_page_id = st.query_params.get("page_id")
        if isinstance(raw_page_id, list):
            raw_page_id = raw_page_id[0] if raw_page_id else None
        qp_page_id = int(str(raw_page_id)) if raw_page_id not in (None, "", []) else None
    except Exception:
        qp_page_id = None

    if qp_page_id is not None:
        last_handled = st.session_state.get("deeplink_handled_page_id")
        if last_handled != qp_page_id:
            loc = get_page_location(qp_page_id)
            if loc:
                dept_id = loc.get("department_id")
                if dept_id:
                    st.session_state["force_department_id"] = dept_id
                st.session_state["force_notebook_id"] = int(loc["notebook_id"])
                st.session_state["force_section_id"] = int(loc["section_id"])
                st.session_state["current_page_id"] = int(loc["page_id"])
                st.session_state["force_page_id"] = int(loc["page_id"])
                st.session_state["page_search"] = ""
                st.session_state["deeplink_handled_page_id"] = int(loc["page_id"])
            else:
                st.warning(f"Страница с ID={qp_page_id} не найдена или недоступна.")
                st.session_state["deeplink_handled_page_id"] = qp_page_id

        # чтобы параметр не "фиксировал" навигацию при дальнейшем использовании приложения
        try:
            st.query_params.pop("page_id", None)
        except Exception:
            pass

    users_df = list_users()
    user_records = list(users_df.itertuples(index=False))
    user_map = {row.login: row.full_name for row in user_records}
    user_dept_map = {row.login: row.department_id for row in user_records}
    registered_users = {row.login for row in user_records}
    login_options = [row.login for row in user_records]

    stored_login = st.session_state.get("current_user_login")
    db_user = st.session_state.get("db_credentials", {}).get("user")
    preferred_login: str | None = stored_login or db_user or (login_options[0] if login_options else None)

    selected_login: str | None = preferred_login
    if selected_login:
        st.session_state["current_user_login"] = selected_login
    else:
        st.sidebar.info("Нет доступных пользователей.")
        return

    owned_notebooks_df = get_owned_notebooks(selected_login)

    departments_df = get_departments()
    department_map = {row.department_id: row.name_department for row in departments_df.itertuples(index=False)}

    user_dep_id = user_dept_map.get(selected_login)

    # --- Левый сайдбар: подразделение ---
    selected_department_id = str(st.session_state.get("selected_department_id", "00"))
    prev_department_id = selected_department_id
    dept_records = list(departments_df.itertuples(index=False)) if not departments_df.empty else []
    is_user_department_selected = False
    forced_department_id = st.session_state.pop("force_department_id", None)
    dep_default_login_key = "_department_default_for_login"
    need_apply_department_default = st.session_state.get(dep_default_login_key) != selected_login

    if dept_records:
        dept_rows_by_id = {str(r.department_id): r for r in dept_records}
        dept_id_options = list(dept_rows_by_id.keys())

        if need_apply_department_default:
            st.session_state[dep_default_login_key] = selected_login
            if forced_department_id is None:
                default_dep_id = "99" if not owned_notebooks_df.empty else "00"
                if default_dep_id not in dept_rows_by_id:
                    default_dep_id = dept_id_options[0]
                selected_department_id = default_dep_id
                st.session_state["selected_department_id"] = default_dep_id
                st.session_state["current_department_id"] = default_dep_id
                st.session_state["department_selector_widget"] = default_dep_id

        if selected_department_id not in dept_rows_by_id:
            has_owned_books = not owned_notebooks_df.empty
            default_dep_id = "99" if has_owned_books else "00"
            if default_dep_id not in dept_rows_by_id:
                default_dep_id = dept_id_options[0]
            selected_department_id = default_dep_id

        dep_widget_key = "department_selector_widget"
        if forced_department_id is not None:
            forced_dep_id = str(forced_department_id)
            if forced_dep_id in dept_rows_by_id:
                selected_department_id = forced_dep_id
                st.session_state[dep_widget_key] = forced_dep_id
        if str(st.session_state.get(dep_widget_key, "")) not in dept_rows_by_id:
            st.session_state[dep_widget_key] = selected_department_id

        selected_department_id = st.sidebar.selectbox(
            "Поиск по подразделению",
            dept_id_options,
            format_func=lambda dep_id: dept_rows_by_id[str(dep_id)].name_department,
            key=dep_widget_key,
        )
        selected_department_id = str(selected_department_id)
        user_dep_id = user_dept_map.get(selected_login)
        is_user_department_selected = bool(user_dep_id) and (str(selected_department_id) == str(user_dep_id))
    else:
        selected_department_id = "00"

    if str(prev_department_id) != str(selected_department_id):
        _touch_sidebar_nav_state()
    st.session_state["selected_department_id"] = str(selected_department_id)
    st.session_state["current_department_id"] = selected_department_id

    # --- поиск страниц ---
    def _clear_page_search():
        st.session_state["page_search"] = ""
        _force_sidebar_sources_auto_refresh()

    # сбрасываем поиск до отрисовки инпута (если была операция создания/импорта)
    if st.session_state.pop("reset_page_search_once", False):
        st.session_state["page_search"] = ""

    # уникальный префикс (можно оставить константой)
    SEARCH_UI_PREFIX = "pages_search_ui"

    search_col, clear_col = st.sidebar.columns([13, 3])

    with search_col:
        search_raw = st.text_input( 
            label="",
            key="page_search",
                    placeholder="Введите текст для поиска...",
            label_visibility="collapsed",
        )

    with clear_col:
        st.button(
            "🔄",
            key=f"{SEARCH_UI_PREFIX}_clear_btn",
            help="Очистить фильтр/Обновить",
            on_click=_clear_page_search,
            use_container_width=True,
        )


    search_raw = (search_raw or "").strip()
    search_tags_only = search_raw.startswith("#")
    search_text = search_raw[1:].strip() if search_tags_only else search_raw

    current_user_can_create_notebook = selected_login in registered_users

    # --- список книг ---
    notebooks_df = _cached_df(
        "cache_notebooks",
        params=(
            selected_login,
            user_dep_id,
            int(st.session_state.get("sidebar_sources_version", 0)),
        ),
        fetcher=lambda: get_notebooks(selected_login, user_dep_id),
    )
    filtered_notebooks_df = notebooks_df.copy()
    # ✅ Все книги, доступные пользователю по правилам get_notebooks (owners/00/99/подразделения)
    visible_notebook_ids = notebooks_df["id"].astype(int).tolist() if not notebooks_df.empty else []
    owned_ids = set(owned_notebooks_df["id"].astype(int).tolist()) if not owned_notebooks_df.empty else set()
    current_department_id: str = st.session_state.get("current_department_id", "00")



    if current_department_id != "00" and not filtered_notebooks_df.empty:
        dep_col = filtered_notebooks_df["department_id"].fillna("00").astype(str)
        owned_mask = filtered_notebooks_df["id"].astype(int).isin(owned_ids)

        if str(current_department_id) == "99":
            mask = owned_mask
        else:
            prefix = str(current_department_id).strip()
            prefix_like = prefix + "."
            dept_mask = (dep_col == "00") | (dep_col == prefix) | dep_col.str.startswith(prefix_like)
            mask = dept_mask | owned_mask

        filtered_notebooks_df = filtered_notebooks_df[mask]

    selected_notebook_id: int | None = None
    selected_section_id: int | None = None
    selected_notebook_row: pd.Series | None = None
    can_edit_notebook: bool = False

    notebook_records = list(filtered_notebooks_df.itertuples(index=False))
    top_col1, top_col2, top_col3 = st.columns([7, 1, 7])

    # --- диалог "Новая книга" ---
    if current_user_can_create_notebook:

        @st.dialog("Новая книга", width="small")
        def new_notebook_dialog():
            new_nb_name = st.text_input("Название новой книги", key="new_notebook_name_modal")
            create_clicked = st.button("Создать книгу", key="create_notebook_btn_modal")
            if create_clicked:
                user_department_id = user_dept_map.get(selected_login)
                new_nb_id = create_notebook(new_nb_name, selected_login, user_department_id)
                if user_department_id:
                    st.session_state["force_department_id"] = user_department_id
                st.session_state["force_notebook_id"] = new_nb_id
                add_event_log(topic="NOTEBOOK", subtopic="CREATE",notebook_id=new_nb_id, section_id=0, page_id=0, event="notebook_name = " + new_nb_name + "; user_department_id = " + user_department_id, body_html="")
                st.success("Книга создана")
                _bump_data_version()
                st.rerun()

    # --- верхняя панель: книга + кнопки (ВЕРНУЛИ 🔐) ---
    with top_col1:
        select_col, plus_col, info_col = st.columns([16, 2, 2])

        with select_col:
            if notebook_records:
                notebook_rows_by_id = {int(r.id): r for r in notebook_records}
                notebook_id_options = list(notebook_rows_by_id.keys())

                selected_notebook_id_state = st.session_state.get("selected_notebook_id")
                try:
                    selected_notebook_id_state = int(selected_notebook_id_state)
                except Exception:
                    selected_notebook_id_state = None
                prev_notebook_id = selected_notebook_id_state

                forced_nb_id = st.session_state.pop("force_notebook_id", None)
                if forced_nb_id is not None and int(forced_nb_id) in notebook_rows_by_id:
                    selected_notebook_id_state = int(forced_nb_id)

                if selected_notebook_id_state not in notebook_rows_by_id:
                    selected_notebook_id_state = notebook_id_options[0]

                nb_widget_key = "notebook_selector_widget"
                if forced_nb_id is not None and int(forced_nb_id) in notebook_rows_by_id:
                    st.session_state[nb_widget_key] = int(forced_nb_id)
                if st.session_state.get(nb_widget_key) not in notebook_rows_by_id:
                    st.session_state[nb_widget_key] = int(selected_notebook_id_state)

                def _nb_label(notebook_id: int) -> str:
                    row = notebook_rows_by_id.get(int(notebook_id))
                    if row is None:
                        return str(notebook_id)
                    if is_user_department_selected:
                        return f"{row.name}"
                    dept_id = getattr(row, "department_id", None)
                    dept_name = department_map.get(dept_id, "") if dept_id else ""
                    dept_txt = f" [{dept_name}]" if dept_name else ""
                    return f"{row.name}{dept_txt}"

                st.markdown("###### ")
                st.markdown("###### Записная книга")

                selected_notebook = st.selectbox(
                    label="",
                    options=notebook_id_options,
                    format_func=_nb_label,
                    key=nb_widget_key,
                    label_visibility="collapsed",
                )

                selected_notebook_id = int(selected_notebook)
                if prev_notebook_id != selected_notebook_id:
                    _touch_sidebar_nav_state()
                st.session_state["selected_notebook_id"] = selected_notebook_id

                selected_notebook_rows = filtered_notebooks_df[filtered_notebooks_df["id"] == selected_notebook_id]
                if not selected_notebook_rows.empty:
                    selected_notebook_row = selected_notebook_rows.iloc[0]
                    can_edit_notebook = bool(is_notebook_owner(selected_notebook_id, selected_login))
            else:
                st.info("Нет доступных книг")
    # ✅ диалог "Права доступа на книгу" (вернули)
    if selected_notebook_id is not None and can_edit_notebook:

        @st.dialog("Права доступа на книгу/Переименование", width="small")
        def notebook_access_dialog():
            owners_df = get_notebook_owners(selected_notebook_id)
            owners_text = (
                ", ".join(f"{row.full_name or row.login} ({row.login})" for row in owners_df.itertuples(index=False))
                or "Нет владельцев"
            )

            dept_id = selected_notebook_row.get("department_id", None) if selected_notebook_row is not None else None
            dept_id = "00" if dept_id is None or str(dept_id).strip() == "" else str(dept_id)
            dept_value = department_map.get(dept_id, "не задано")

            st.caption(f"Подразделение (видимость): {dept_value}")
            st.caption(f"Владельцы: {owners_text}")

            owner_logins = set(owners_df["login"].tolist()) if not owners_df.empty else set()

            # ⬇️ текущее имя книги (для подсказки/плейсхолдера)
            current_book_name = ""
            try:
                current_book_name = str(selected_notebook_row.get("name", "") or "").strip()
            except Exception:
                current_book_name = ""
            rename_key = f"rename_notebook_{selected_notebook_id}"
            if rename_key not in st.session_state:
                st.session_state[rename_key] = current_book_name

            with st.form(f"access_form_{selected_notebook_id}"):
                dept_records_all = list(departments_df.itertuples(index=False)) if not departments_df.empty else []
                current_nb_dept = str(dept_id or "00")

                current_dept_row = next(
                    (r for r in dept_records_all if str(r.department_id) == current_nb_dept),
                    None,
                )
                if current_dept_row is None and dept_records_all:
                    current_dept_row = dept_records_all[0]

                dept_widget_key = f"nb_department_{selected_notebook_id}"
                if dept_records_all and st.session_state.get(dept_widget_key) not in dept_records_all:
                    st.session_state[dept_widget_key] = current_dept_row if current_dept_row in dept_records_all else dept_records_all[0]
                selected_dept_row = st.selectbox(
                    "Область видимости книги (подразделение)",
                    options=dept_records_all,
                    format_func=lambda r: f"{r.name_department}",
                    key=dept_widget_key,
                )
                new_department_id = str(selected_dept_row.department_id) if selected_dept_row else "00"

                selectable_users = [login for login in login_options if login not in owner_logins]
                new_owner_login = st.selectbox(
                    "Добавить владельца книги",
                    options=[""] + selectable_users,
                    format_func=lambda login: "—" if login == "" else f"{user_map.get(login, login)} ({login})",
                    key=f"add_owner_{selected_notebook_id}",
                )

                removable_owners = [login for login in owner_logins if login != selected_login]
                remove_owner_login = st.selectbox(
                    "Удалить владельца книги",
                    options=[""] + removable_owners,
                    format_func=lambda login: "—" if login == "" else f"{user_map.get(login, login)} ({login})",
                    key=f"remove_owner_{selected_notebook_id}",
                )

                # ✅ НОВОЕ ПОЛЕ: переименование книги (внизу над кнопкой)
                # st.markdown("---")
                new_book_name = st.text_input(
                    "Переименовать книгу",
                    placeholder="Введите новое название книги",
                    key=rename_key,
                )

                # ✅ кнопки действий формы
                save_col, cancel_col = st.columns(2)
                with save_col:
                    submitted = st.form_submit_button("Сохранить", use_container_width=True)
                with cancel_col:
                    canceled = st.form_submit_button("Отмена", use_container_width=True)

                if canceled:
                    st.rerun()

                if submitted:
                    # 1) видимость
                    set_notebook_department(selected_notebook_id, new_department_id)

                    # 2) владельцы
                    if new_owner_login:
                        add_notebook_owner(selected_notebook_id, new_owner_login)
                        add_event_log(topic="NOTEBOOK", subtopic="ADD_OWNER",notebook_id=selected_notebook_id, section_id=0, page_id=0, event="new_owner = " + new_owner_login, body_html="")
                    if remove_owner_login and remove_owner_login != selected_login:
                        remove_notebook_owner(selected_notebook_id, remove_owner_login)
                        add_event_log(topic="NOTEBOOK", subtopic="REMOVE_OWNER",notebook_id=selected_notebook_id, section_id=0, page_id=0, event="remove_owner = " + remove_owner_login, body_html="")
                    # 3) переименование (только если введено)
                    nb_new = (new_book_name or "").strip()
                    if nb_new and nb_new != current_book_name:
                        run_execute(
                            f"""
                            UPDATE {NOTEBOOKS_TABLE}
                            SET name = '{_escape(nb_new)}', updated_at = NOW()
                            WHERE id = {int(selected_notebook_id)}
                            """
                        )
                        add_event_log(topic="NOTEBOOK", subtopic="RENAME",notebook_id=selected_notebook_id, section_id=0, page_id=0, event="new_notebook_name = " + _escape(nb_new) + "; old_notebook_name = " + _escape(current_book_name), body_html="")
                        st.session_state["force_notebook_id"] = selected_notebook_id
                        _bump_data_version()
                    st.success("Сохранено")
                    st.rerun()

    if current_user_can_create_notebook:
        with plus_col:
            st.markdown("###### ")
            st.markdown("###### ")
            if st.button("➕", key="open_new_notebook_dialog", help="Создать новую книгу", use_container_width=True):
                new_notebook_dialog()

    # ✅ КНОПКА 🔐 
    if selected_notebook_id is not None and can_edit_notebook:
        with info_col:
            st.markdown("###### ")
            st.markdown("###### ")
            if st.button("🔐", key="open_notebook_access_dialog", help="Права доступа на книгу/Переименование", use_container_width=True):
                rename_key = f"rename_notebook_{selected_notebook_id}"
                current_name = ""
                if selected_notebook_row is not None:
                    current_name = str(selected_notebook_row.get("name", "") or "").strip()
                st.session_state[rename_key] = current_name
                notebook_access_dialog()






    # --- диалог "Новый раздел" ---
    if selected_notebook_id is not None and can_edit_notebook:

        @st.dialog("Новый раздел", width="small")
        def new_section_dialog():
            new_section_name = st.text_input("Название раздела", key="new_section_name_modal")
            create_clicked = st.button("Создать раздел", key="create_section_btn_modal")
            if create_clicked:
                section_new_id=create_section(selected_notebook_id, new_section_name, selected_login)
                st.success("Раздел создан")
                _bump_data_version()
                st.session_state["force_section_id"] = int(section_new_id)
                add_event_log(topic="SECTION", subtopic="CREATE",notebook_id=selected_notebook_id, section_id=section_new_id, page_id=0, event="section_name = " + _escape(new_section_name), body_html="")
                st.rerun()

        @st.dialog("Переименовать или удалить раздел", width="small")
        def section_manage_dialog(section_row):
            section_id_local = int(section_row.id)
            st.caption(f"Текущий раздел: **{section_row.name}**")
            old_section_name = section_row.name
            section_rename_key = f"rename_section_name_{section_id_local}"
            section_rename_seed_key = f"{section_rename_key}_seed"
            section_name_default = str(section_row.name or "")
            if st.session_state.get(section_rename_seed_key) != section_name_default:
                st.session_state[section_rename_key] = section_name_default
                st.session_state[section_rename_seed_key] = section_name_default
            new_name = st.text_input(
                "Новое название раздела",
                key=section_rename_key,
            )
            pages_cnt = get_section_pages_count(section_id_local)

            col_rename, col_delete = st.columns(2)
            with col_rename:
                if st.button("Сохранить название", key=f"btn_rename_section_{section_id_local}", use_container_width=True):
                    rename_section(section_id_local, new_name)
                    _bump_data_version()
                    st.session_state["force_section_id"] = int(section_id_local)
                    add_event_log(topic="SECTION", subtopic="RENAME",notebook_id=selected_notebook_id, section_id=section_id_local, page_id=0, event="new_section_name = " + _escape(new_name) + "; old_saction_name = " + old_section_name, body_html="")
                    st.success("Название раздела обновлено")
                    st.rerun()

            with col_delete:
                if pages_cnt > 0:
                    st.caption(f"В разделе есть страницы ({pages_cnt}). Удаление недоступно.")
                else:
                    if st.button(
                        "Удалить раздел",
                        key=f"btn_delete_section_{section_id_local}",
                        use_container_width=True,
                        type="secondary",
                    ):
                        delete_section(section_id_local)
                        _bump_data_version()
                        add_event_log(topic="SECTION", subtopic="DELETE",notebook_id=selected_notebook_id, section_id=section_id_local, page_id=0, event="saction_name = " + old_section_name, body_html="")
                        st.success("Раздел удалён")
                        st.rerun()

            if st.button("Отмена", key=f"btn_cancel_section_manage_{section_id_local}", use_container_width=True):
                st.rerun()

    # --- список разделов + кнопки ➕/✎ (вернули) ---
    sections_df = pd.DataFrame()
    section_records: list = []
    if selected_notebook_id is not None:
        sections_df = get_sections(selected_notebook_id)
        section_records = list(sections_df.itertuples(index=False))

    with top_col3:
        select_col2, plus_col2, manage_col2 = st.columns([15.3, 2, 2])

        selected_section = None
        with select_col2:
            st.markdown("###### ")
            st.markdown("###### Раздел")
            if section_records:
                section_rows_by_id = {int(r.id): r for r in section_records}
                section_id_options = list(section_rows_by_id.keys())

                selected_section_id_state = st.session_state.get("selected_section_id")
                try:
                    selected_section_id_state = int(selected_section_id_state)
                except Exception:
                    selected_section_id_state = None
                prev_section_id = selected_section_id_state

                forced_section_id = st.session_state.pop("force_section_id", None)
                if forced_section_id is not None and int(forced_section_id) in section_rows_by_id:
                    selected_section_id_state = int(forced_section_id)

                if selected_section_id_state not in section_rows_by_id:
                    selected_section_id_state = section_id_options[0]

                sec_widget_key = "section_selector_widget"
                if forced_section_id is not None and int(forced_section_id) in section_rows_by_id:
                    st.session_state[sec_widget_key] = int(forced_section_id)
                if st.session_state.get(sec_widget_key) not in section_rows_by_id:
                    st.session_state[sec_widget_key] = int(selected_section_id_state)

                selected_section = st.selectbox(
                    label="",
                    options=section_id_options,
                    format_func=lambda section_id: section_rows_by_id[int(section_id)].name,
                    key=sec_widget_key,
                    label_visibility="collapsed",
                )
                selected_section_id = int(selected_section)
                if prev_section_id != selected_section_id:
                    _touch_sidebar_nav_state()
                st.session_state["selected_section_id"] = selected_section_id

                selected_section = section_rows_by_id.get(selected_section_id)
            else:
                st.session_state.pop("section_selector_widget", None)
                st.warning("В книге нет разделов.")
        if can_edit_notebook:
            with plus_col2:
                st.markdown("###### ")
                st.markdown("###### ")
                if st.button("➕", key="open_new_section_dialog", help="Создать новый раздел", use_container_width=True):
                    new_section_dialog()

            with manage_col2:
                st.markdown("###### ")
                st.markdown("###### ")
                if selected_section is not None:
                    if st.button("✎", key="open_section_manage_dialog", help="Переименовать или удалить раздел", use_container_width=True):
                        section_manage_dialog(selected_section)

    # ---------- Загрузка страниц ----------
    dept_notebook_ids = filtered_notebooks_df["id"].astype(int).tolist() if not filtered_notebooks_df.empty else []

    if search_text:
        # ✅ при поиске ищем по ВСЕМ доступным книгам пользователя
        search_notebook_id = None
        search_section_id = None
        search_allowed_ids = visible_notebook_ids
    else:
        # ✅ без поиска показываем только страницы выбранного раздела выбранной книги
        if not selected_section_id:
            pages_df = pd.DataFrame()
        search_notebook_id = selected_notebook_id
        search_section_id = selected_section_id
        search_allowed_ids = [selected_notebook_id] if selected_notebook_id else []

    if not search_text and not selected_section_id:
        pages_df = pd.DataFrame()
    else:
        t0 = time.perf_counter()
        pages_df = load_pages_df(
            search_notebook_id,
            search_section_id,
            search_allowed_ids,
            search_text or None,
            search_tags_only,
        )
        _log_timing("load_pages_df", t0)


    # ✅ защита: load_pages_df / run_fetch_df должны возвращать DataFrame
    if pages_df is None:
        pages_df = pd.DataFrame()


    if pages_df.empty:
        pages_df = pd.DataFrame(
            columns=[
                "id",
                "title",
                "status",
            ]
        )

    # ---------- Кнопки "Новая страница" и "Импорт страниц" ----------
    new_page_clicked = False

    if can_edit_notebook:

        @st.dialog("Импорт страниц", width="large")
        def import_pages_dialog():
            if not selected_section_id:
                st.info("Выберите раздел для импорта.")
                return

            uploaded = st.file_uploader(
                "Загрузите файлы для импорта",
                type=["mht", "docx", "xlsx", "xlsm", "csv", "png", "txt", "sql"],
                accept_multiple_files=True,
                key="import_files_modal",
            )

            col_import, col_cancel = st.columns(2)
            with col_import:
                import_clicked = st.button(
                    "Импортировать",
                    key="import_files_btn_modal",
                    use_container_width=True,
                    disabled=not bool(uploaded),
                )
            with col_cancel:
                cancel_clicked = st.button(
                    "Отмена",
                    key="cancel_import_files_btn_modal",
                    use_container_width=True,
                )

            if cancel_clicked:
                st.session_state.pop("import_files_modal", None)
                st.rerun()

            if uploaded and import_clicked:
                imported = 0
                last_imported_id: int | None = None
                errors: list[str] = []
                for file in uploaded:
                    try:
                        file_name = file.name or "file"
                        _, ext = os.path.splitext(file_name)
                        ext = ext.lower()
                        data = file.getvalue()

                        if ext == ".mht":
                            pages = _import_mod.parse_mht_to_pages(data, file_name)  # ✅ sanitized per page
                            for title, body_html in pages:
                                last_imported_id = insert_page_with_content(
                                    selected_section_id, title, body_html, selected_login
                                )
                                add_event_log(topic="PAGE", subtopic="CREATE_IMPORT_.MHT",notebook_id=selected_notebook_id, section_id=selected_section_id, page_id=last_imported_id, event="page_title = " + title, body_html=body_html)
                                imported += 1
                            continue

                        page_title = file_name
                        body_html = ""

                        if ext == ".docx":
                            body_html = _import_mod._docx_bytes_to_html(data)
                        elif ext in (".xlsx", ".xlsm"):
                            body_html = _import_mod._excel_bytes_to_html(data)
                        elif ext == ".csv":
                            body_html = _import_mod._csv_bytes_to_html(data)
                        elif ext in (".txt", ".sql"):
                            body_html = _import_mod._text_bytes_to_html(data)
                        elif ext == ".png":
                            body_html = _import_mod._png_bytes_to_html(data)
                        else:
                            raise ValueError(f"Неподдерживаемый тип файла: {ext}")

                        last_imported_id = insert_page_with_content(
                            selected_section_id, page_title, body_html, selected_login
                        )
                        add_event_log(topic="PAGE", subtopic="CREATE_IMPORT_" + str(ext.upper()),notebook_id=selected_notebook_id, section_id=selected_section_id, page_id=last_imported_id, event="page_title = " + page_title, body_html=body_html)
                        imported += 1
                    except Exception as exc:
                        errors.append(f"{file.name}: {exc}")

                if imported:
                    st.success(f"Импортировано {imported} страниц")
                    if last_imported_id is not None:
                        st.session_state["current_page_id"] = last_imported_id
                        st.session_state["force_page_id"] = last_imported_id
                        st.session_state["reset_page_search_once"] = True
                    _bump_data_version()
                    st.rerun()
                if errors:
                    st.warning(";\n".join(errors))

        btn_col_new, btn_col_import = st.sidebar.columns([1, 1])
        with btn_col_new:
            new_page_clicked = st.button("Новая страница", use_container_width=True)
        with btn_col_import:
            import_clicked = st.button("Импорт страниц", use_container_width=True)

        if import_clicked:
            import_pages_dialog()

    if new_page_clicked:
        if not selected_section_id:
            st.sidebar.warning("Сначала создайте и выберите раздел.")
        else:
            new_page_id = create_page(section_id=selected_section_id, user_login=selected_login, title=None)
            st.session_state["current_page_id"] = new_page_id
            st.session_state["force_page_id"] = new_page_id
            st.session_state["reset_page_search_once"] = True
            # ✅ открыть редактор ровно один раз на следующем ререндере
            st.session_state["open_editor_once_for_page"] = new_page_id
            st.session_state["open_editor_once_edit_mode"] = True
            _bump_data_version()

            add_event_log(topic="PAGE", subtopic="CREATE_HAND",notebook_id=selected_notebook_id, section_id=selected_section_id, page_id=new_page_id, event="create: page_id = " + str(new_page_id), body_html="")

            st.rerun()

    # ---------- Список страниц ----------
    # ✅ добавили статус в отображаемый df
    df_display = pages_df[["id", "title", "status"]].copy().reset_index(drop=True)
    _log_metric("pages_count", len(df_display))

    if "id" in df_display.columns:
        # гарантируем числовые id, чтобы корректно проставлять выбранные строки
        df_display["id"] = pd.to_numeric(df_display["id"], errors="coerce")

    # нормализация на всякий случай (если NULL)
    if "status" in df_display.columns:
        df_display["status"] = df_display["status"].fillna("#FFFFFF").astype(str)

    # снимок для сравнения изменений (чтобы поймать, какие status реально поменялись)
    if "pages_grid_prev_df" not in st.session_state:
        st.session_state["pages_grid_prev_df"] = df_display.copy()

    # если сменился контекст (книга/раздел/поиск), обновляем "предыдущее" состояние,
    # чтобы не ловить ложные изменения статусов при навигации
    current_pages_signature = (selected_notebook_id, selected_section_id, search_text, search_tags_only)
    prev_signature = st.session_state.get("pages_grid_signature")
    if prev_signature != current_pages_signature:
        st.session_state["pages_grid_signature"] = current_pages_signature
        st.session_state["pages_grid_prev_df"] = df_display.copy()

    gb = GridOptionsBuilder.from_dataframe(df_display)

    # selection
    gb.configure_selection("single", use_checkbox=False)

    # ВАЖНО: id скрыт и не участвует в подгонке ширины
    gb.configure_column("id", header_name="ID", hide=True, suppressSizeToFit=True)

    # ✅ колонка кружка status (СПРАВА от "Страница")
    gb.configure_column(
        "title",
        header_name="Страница",
        flex=1,
        minWidth=140,
        resizable=True,
    )
    gb.configure_column(
        "status",
        header_name="",
        width=52,
        minWidth=40,
        maxWidth=60,
        flex=0,
        sortable=False,
        filter=False,
        editable=False,
        resizable=False,
        valueFormatter=STATUS_VALUE_FORMATTER,
        cellStyle=STATUS_CELL_STYLE,
        suppressSizeToFit=False,
    )

    # (опционально) defaultColDef — на всякий случай
    gb.configure_default_column(resizable=True)

    force_page_id = st.session_state.pop("force_page_id", None)
    if force_page_id is not None and not df_display.empty:
        try:
            row_index = int(df_display.index[df_display["id"] == force_page_id][0])
            gb.configure_selection("single", pre_selected_rows=[row_index])
        except Exception:
            pass

    # JS: растянуть колонку на ширину грида (и при первом рендере, и при resize окна)
    on_grid_ready = JsCode(
        """
        function(params) {
            try { params.api.sizeColumnsToFit(); } catch(e) {}

            setTimeout(function(){ try { params.api.sizeColumnsToFit(); } catch(e) {} }, 50);
            setTimeout(function(){ try { params.api.sizeColumnsToFit(); } catch(e) {} }, 200);

            if (!window.__notes_pages_resize_bound) {
                window.__notes_pages_resize_bound = true;
                window.addEventListener('resize', function() {
                    try { params.api.sizeColumnsToFit(); } catch(e) {}
                });
            }
        }
        """
    )

    on_first_data_rendered = JsCode(
        """
        function(params) {
            try { params.api.sizeColumnsToFit(); } catch(e) {}
        }
        """
    )

    gb.configure_grid_options(
        onGridReady=on_grid_ready,
        onFirstDataRendered=on_first_data_rendered,
        onGridSizeChanged=JsCode("function(params){ try { params.api.sizeColumnsToFit(); } catch(e) {} }"),
        # ✅ dblclick по кружку только владельцам
        onCellDoubleClicked=(STATUS_DBLCLICK_HANDLER if can_edit_notebook else None),
    )

    list_container = st.sidebar.container()
    with list_container:
        t0 = time.perf_counter()
        grid_response_pages = AgGrid(
            df_display,
            gridOptions=gb.build(),
            enable_enterprise_modules=False,
            # ✅ важно: чтобы изменения из JS (setDataValue) возвращались в python
            # update_mode=GridUpdateMode.MODEL_CHANGED | GridUpdateMode.SELECTION_CHANGED,
            update_mode=GridUpdateMode.MODEL_CHANGED,
            data_return_mode=DataReturnMode.AS_INPUT,
            height=650,
            fit_columns_on_grid_load=True,
            allow_unsafe_jscode=True,
        )
        _log_timing("AgGrid pages list", t0)


    # --- безопасно достаём data из grid_response_pages (может быть list или DataFrame) ---
    raw_data = grid_response_pages.get("data", [])
    if isinstance(raw_data, pd.DataFrame):
        df_after = raw_data.copy()
    elif isinstance(raw_data, list):
        df_after = pd.DataFrame(raw_data)
    else:
        df_after = pd.DataFrame()

    df_before = st.session_state.get("pages_grid_prev_df", df_display.copy())


    if not df_after.empty and "id" in df_after.columns and "status" in df_after.columns:
        df_after["status"] = df_after["status"].fillna("#FFFFFF").astype(str)
        df_before_local = df_before.copy()

        if not isinstance(df_before, pd.DataFrame):
            df_before = df_display.copy()


        merged = df_after[["id", "status"]].merge(
            df_before_local[["id", "status"]],
            on="id",
            how="left",
            suffixes=("_new", "_old"),
        )

        changed = merged[merged["status_new"] != merged["status_old"]]
        if not changed.empty:
            if can_edit_notebook:
                for row in changed.itertuples(index=False):
                    page_id_upd = int(row.id)
                    status_old = str(row.status_old or "#FFFFFF").strip()
                    new_status = str(row.status_new or "#FFFFFF").strip()

                    # защита: разрешаем только заданные цвета, иначе ставим белый
                    new_status = new_status.upper()
                    allowed = {"#FFFFFF", "#FFF59D", "#90CAF9", "#A5D6A7", "#EF9A9A", "#CE93D8"}
                    if new_status not in allowed:
                        new_status = "#FFFFFF"

                    # Уберем изменение статуса при создании страницы, когда предыдущего статуса нет Nane  and status_old is not None
                    if status_old != "nan":
                        run_execute(
                            f"""
                            UPDATE {PAGES_TABLE}
                            SET status = '{_escape(new_status)}'
                            WHERE id = {int(page_id_upd)}
                                AND COALESCE(status,'') <> '{_escape(new_status)}'
                            """
                        )

                        add_event_log(topic="PAGE", subtopic="CHANGE_STATUS",notebook_id=selected_notebook_id, section_id=selected_section_id, page_id=page_id_upd, event="new_status = " + _escape(new_status) + "; old_status = " + status_old, body_html="")
                # обновляем "предыдущее" состояние, чтобы не обновлять повторно
                st.session_state["pages_grid_prev_df"] = df_after.copy()

                # перечитать/перерисовать, чтобы pages_df внизу был синхронен
                _bump_data_version()
                st.rerun()
            else:
                # не владелец — откатываем снимок и предупреждаем
                st.session_state["pages_grid_prev_df"] = df_display.copy()
                st.warning("Менять статус страниц может только владелец книги.")
                st.rerun()
    else:
        st.session_state["pages_grid_prev_df"] = df_after.copy() if not df_after.empty else df_display.copy()


    # --- выбор страницы из грида (ОБЯЗАТЕЛЬНО объявляем page_id заранее) ---
    page_id: int | None = None

    selected_rows = grid_response_pages.get("selected_rows", [])
    if isinstance(selected_rows, pd.DataFrame):
        selected_rows = selected_rows.to_dict("records")

    if selected_rows:
        row = selected_rows[0]
        try:
            page_id = int(row["id"])
            st.session_state["current_page_id"] = page_id
        except Exception:
            page_id = None
    else:
        stored_page_id = st.session_state.get("current_page_id")
        if stored_page_id is not None and not pages_df.empty:
            if (pages_df["id"].astype(int) == int(stored_page_id)).any():
                page_id = int(stored_page_id)

    # st.sidebar.caption(f"selected_rows: {len(grid_response_pages.get('selected_rows') or [])}")
    st.sidebar.caption(f"selected_rows:{page_id=}")
                       

    # ---------- Просмотр / редактирование выбранной страницы ----------
    if page_id is not None:
        t0 = time.perf_counter()
        current_page = load_page_detail_df(page_id)
        _log_timing("load_page_detail_df", t0)
        if current_page is None:
            st.warning("Не удалось загрузить данные страницы.")
            st.stop()

        current_title = current_page.get("title", "")
        current_html = current_page.get("body_html") or ""
        current_tag = current_page.get("tag") or ""

        dept_id_for_page = current_page.get("notebook_department_id")
        dept_name_for_page = department_map.get(dept_id_for_page, "") if dept_id_for_page else ""
        dept_prefix = f"[{dept_name_for_page}] " if dept_name_for_page else ""

        safe_title = current_title or f"Страница_{page_id}"
        page_path = f"{current_page['notebook_name']} > {current_page['section_name']} > {current_page['title']}"


        # sql_text = export_mod._sql_text_from_html(current_html or "", safe_title)
        page_link = build_page_deeplink(page_id)

        col_export, col_edit, col_attach, col_copy, col_link, col_help  = st.columns([10,25,25,16,15,4.5])
        
        # ---------------- Экспорт ----------------
        export_deps = export_mod.ExportDeps(
            list_email_recipients=list_email_recipients,
            get_user_signature=get_user_signature,
            get_page_attachments=get_page_attachments,
            get_attachment_file=get_attachment_file,
            run_execute=run_execute,
            run_scalar=run_scalar,
            add_event_log=add_event_log,
            get_nb_section_id=get_nb_section_id,
            build_page_deeplink=build_page_deeplink,
            escape=_escape,
            email_recipients_table=EMAIL_RECIPIENTS_TABLE,
        )
        export_mod.render_export_section(
            container=col_export,
            page_id=page_id,
            current_title=current_title,
            current_html=current_html or "",
            selected_login=selected_login,
            page_path=page_path,
            deps=export_deps,
        )




        # ---------------- Редактировать (expander) ----------------
        with col_edit:
            if can_edit_notebook:
                edit_nonce_key = f"edit_nonce_{page_id}"
                st.session_state.setdefault(edit_nonce_key, 0)

                @st.dialog("Загрузка из файла", width="large")
                def replace_from_files_dialog(page_id_local: int, title_local: str, tag_local: str, status_local: str, current_html_local: str):
                    sep_between_parts = "<p><br/></p><p><br/></p>"

                    def _parse_uploaded_files(files) -> tuple[str, list[str]]:
                        parts: list[str] = []
                        errors: list[str] = []

                        for file in files:
                            try:
                                file_name = file.name or "file"
                                _, ext = os.path.splitext(file_name)
                                ext = ext.lower()
                                data = file.getvalue()

                                if ext == ".mht":
                                    pages = _import_mod.parse_mht_to_pages(data, file_name)
                                    for _, body_html in pages:
                                        if body_html:
                                            parts.append(body_html)
                                    continue

                                body_html = ""
                                if ext == ".docx":
                                    body_html = _import_mod._docx_bytes_to_html(data)
                                elif ext in (".xlsx", ".xlsm"):
                                    body_html = _import_mod._excel_bytes_to_html(data)
                                elif ext == ".csv":
                                    body_html = _import_mod._csv_bytes_to_html(data)
                                elif ext in (".txt", ".sql"):
                                    body_html = _import_mod._text_bytes_to_html(data)
                                elif ext == ".png":
                                    body_html = _import_mod._png_bytes_to_html(data)
                                else:
                                    raise ValueError(f"Неподдерживаемый тип файла: {ext}")

                                if body_html:
                                    parts.append(body_html)
                            except Exception as exc:
                                errors.append(f"{file.name}: {exc}")

                        return sep_between_parts.join(parts), errors

                    def _merge_with_current(uploaded_html: str, action: str) -> str:
                        page_html = (current_html_local or "").strip()
                        uploaded_html = (uploaded_html or "").strip()

                        if action == "prepend":
                            if uploaded_html and page_html:
                                return uploaded_html + sep_between_parts + page_html
                            return uploaded_html or page_html

                        if action == "append":
                            if uploaded_html and page_html:
                                return page_html + sep_between_parts + uploaded_html
                            return uploaded_html or page_html

                        return uploaded_html

                    def _handle_upload(action: str, success_text: str):
                        combined_html, errors = _parse_uploaded_files(uploaded)

                        if combined_html:
                            new_body = _merge_with_current(combined_html, action)
                            update_page(page_id_local, title_local, new_body, tag_local, status_local)
                            _bump_data_version()
                            st.success(success_text)
                            st.session_state["current_page_id"] = page_id_local
                            st.rerun()

                        if errors:
                            st.warning(";\n".join(errors))
                        elif not combined_html:
                            st.warning("Нет данных для вставки из загруженных файлов.")

                    uploaded = st.file_uploader(
                        "Загрузите файлы для импорта",
                        type=["mht", "docx", "xlsx", "xlsm", "csv", "png", "txt", "sql"],
                        accept_multiple_files=True,
                        key=f"replace_files_{page_id_local}",
                    )

                    if uploaded:
                        col_prepend, col_replace_btn, col_append = st.columns(3)
                        with col_prepend:
                            if st.button("Вставить в начале", key=f"replace_prepend_{page_id_local}", use_container_width=True):
                                _handle_upload("prepend", "Содержимое файлов добавлено в начало страницы.")
                        with col_replace_btn:
                            if st.button("Заместить текст страницы", key=f"replace_full_{page_id_local}", use_container_width=True):
                                _handle_upload("replace", "Содержимое страницы обновлено из файлов.")
                        with col_append:
                            if st.button("Вставить в конец", key=f"replace_append_{page_id_local}", use_container_width=True):
                                _handle_upload("append", "Содержимое файлов добавлено в конец страницы.")

                    if st.button("Отмена", key=f"replace_cancel_{page_id_local}", use_container_width=True):
                        st.rerun()

                def _collapse_edit():
                    st.session_state[edit_nonce_key] += 1

                edit_label = "Редактировать страницу" + ("\u200b" * int(st.session_state[edit_nonce_key]))
                with st.expander(edit_label, expanded=False):
                    if st.button(
                        "Редактировать в Quill",
                        key=f"open_edit_dialog_{page_id}",
                        use_container_width=True,
                        on_click=_collapse_edit,
                    ):
                        st.session_state["edit_dialog_page_id"] = int(page_id)
                        edit_page_dialog(page_id, current_title, current_html, current_tag, current_page.get("status", ""), default_edit_mode=False)

                    if st.button(
                        "Вставить из файла",
                        key=f"replace_from_file_{page_id}",
                        use_container_width=True,
                        on_click=_collapse_edit,
                    ):
                        replace_from_files_dialog(page_id, current_title, current_tag, current_page.get("status", ""), current_html or "")

                    @st.dialog("Добавить текст в страницу", width="large")
                    def append_text_dialog(page_id_local: int, title_local: str, tag_local: str, status_local: str):
                        st.session_state["append_dialog_page_id"] = int(page_id_local)
                        new_html = st_quill(value="", html=True, placeholder="Введите или вставьте текст", key=f"append_quill_{page_id_local}")
                        new_html = new_html or ""

                        c_left, c_right, c_cancel = st.columns(3)
                        with c_left:
                            if st.button("Добавить в начало страницы", key=f"append_begin_{page_id_local}", use_container_width=True):
                                combined = (new_html + "<p><br/></p>" + (current_html or "")).strip()
                                update_page(page_id_local, title_local, combined, tag_local, status_local)
                                _bump_data_version()
                                st.success("Текст добавлен в начало страницы")
                                st.session_state["append_dialog_page_id"] = None
                                st.rerun()
                        with c_right:
                            if st.button("Добавить в конец страницы", key=f"append_end_{page_id_local}", use_container_width=True):
                                combined = ((current_html or "") + "<p><br/></p>" + new_html).strip()
                                update_page(page_id_local, title_local, combined, tag_local, status_local)
                                _bump_data_version()
                                st.success("Текст добавлен в конец страницы")
                                st.session_state["append_dialog_page_id"] = None
                                st.rerun()
                        with c_cancel:
                            if st.button("Отмена", key=f"append_cancel_{page_id_local}", use_container_width=True):
                                st.session_state["append_dialog_page_id"] = None
                                st.session_state.pop(f"append_quill_{page_id_local}", None)
                                st.rerun()

                    if st.button(
                        "Добавить текст",
                        key=f"append_text_{page_id}",
                        use_container_width=True,
                        on_click=_collapse_edit,
                    ):
                        st.session_state["append_dialog_page_id"] = int(page_id)
                        append_text_dialog(page_id, current_title, current_tag, current_page.get("status", ""))

                    title_key = f"page_title_input_{page_id}"
                    title_seed_key = f"{title_key}_seed"
                    tags_key = f"page_tags_input_{page_id}"
                    tags_seed_key = f"{tags_key}_seed"
                    edit_fields_context_key = "_edit_page_title_tags_context"
                    cancel_reset_key = f"_cancel_title_tags_reset_{page_id}"
                    current_edit_fields_context = int(page_id)

                    if st.session_state.pop(cancel_reset_key, False):
                        st.session_state[title_key] = current_title or ""
                        st.session_state[title_seed_key] = current_title or ""
                        st.session_state[tags_key] = current_tag or ""
                        st.session_state[tags_seed_key] = current_tag or ""
                        st.session_state[edit_fields_context_key] = current_edit_fields_context

                    title_seed_val = st.session_state.get(title_seed_key)
                    tags_seed_val = st.session_state.get(tags_seed_key)
                    title_input_val = st.session_state.get(title_key)
                    tags_input_val = st.session_state.get(tags_key)
                    has_local_title_changes = title_input_val is not None and title_seed_val is not None and str(title_input_val) != str(title_seed_val)
                    has_local_tags_changes = tags_input_val is not None and tags_seed_val is not None and str(tags_input_val) != str(tags_seed_val)
                    has_local_unsaved_changes = has_local_title_changes or has_local_tags_changes

                    should_seed_edit_fields = (
                        st.session_state.get(edit_fields_context_key) != current_edit_fields_context
                        or (
                            not has_local_unsaved_changes
                            and (
                                title_seed_val != (current_title or "")
                                or tags_seed_val != (current_tag or "")
                            )
                        )
                    )
                    if should_seed_edit_fields:
                        st.session_state[title_key] = current_title or ""
                        st.session_state[title_seed_key] = current_title or ""
                        st.session_state[tags_key] = current_tag or ""
                        st.session_state[tags_seed_key] = current_tag or ""
                        st.session_state[edit_fields_context_key] = current_edit_fields_context

                    new_title_val = st.text_input(
                        "Наименование страницы",
                        key=title_key,
                    )
                    new_tags_val = st.text_input(
                        "Теги",
                        key=tags_key,
                    )
                    save_col, cancel_col = st.columns(2)
                    with save_col:
                        if st.button(
                            "Сохранить",
                            key=f"save_title_tags_{page_id}",
                            use_container_width=True,
                        ):
                            update_page(
                                page_id,
                                new_title_val.strip(),
                                current_html or "",
                                new_tags_val.strip(),
                                current_page.get("status", ""),
                            )
                            _bump_data_version()
                            st.session_state.pop(title_key, None)
                            st.session_state.pop(title_seed_key, None)
                            st.session_state.pop(tags_key, None)
                            st.session_state.pop(tags_seed_key, None)
                            st.session_state["current_page_id"] = page_id
                            _collapse_edit()
                            st.success("Наименование и теги обновлены")
                            st.rerun()
                    with cancel_col:
                        if st.button(
                            "Отмена",
                            key=f"cancel_title_tags_{page_id}",
                            use_container_width=True,
                        ):
                            st.session_state[cancel_reset_key] = True
                            _collapse_edit()
                            st.rerun()
            else:
                edit_nonce_key = f"edit_nonce_{page_id}"
                st.session_state.setdefault(edit_nonce_key, 0)
                edit_label = "Редактировать страницу" + ("\u200b" * int(st.session_state[edit_nonce_key]))
                with st.expander(edit_label, expanded=False):
                    st.caption("У вас нет прав на редактирование книги.")
        

        
        # ---------------- Вложения ----------------
        with col_attach:
            exp_nonce_key = f"exp_files_nonce_{page_id}"
            up_nonce_files_key = f"uploader_nonce_files_{page_id}"
            up_nonce_links_key = f"uploader_nonce_links_{page_id}"
            question_number_key = f"attachment_question_number_{page_id}"

            st.session_state.setdefault(exp_nonce_key, 0)
            st.session_state.setdefault(up_nonce_files_key, 0)
            st.session_state.setdefault(up_nonce_links_key, 0)
            st.session_state.setdefault(question_number_key, "")

            exp_label = "Прикрепить файлы" + ("\u200b" * int(st.session_state[exp_nonce_key]))

            with st.expander(exp_label, expanded=False):
                if can_edit_notebook:
                    st.text_input("Номер вопроса", key=question_number_key)

                    uploader_key = f"files_uploader_{page_id}_{st.session_state[up_nonce_files_key]}"
                    uploaded_files = st.file_uploader(
                        "Загрузите файлы",
                        type=None,
                        accept_multiple_files=True,
                        key=uploader_key,
                    )

                    save_files_clicked = st.button(
                        "Прикрепить файлы",
                        key=f"save_files_btn_{page_id}",
                        use_container_width=True,
                    )

                    if save_files_clicked:
                        if not uploaded_files:
                            st.warning("Сначала выберите файлы для загрузки.")
                        else:
                            question_number_val = st.session_state.get(question_number_key, "")
                            try:
                                for uf in uploaded_files:
                                    save_file_attachment(page_id, uf, selected_login, question_number_val)

                                st.success("Файлы прикреплены")
                                st.session_state[up_nonce_files_key] += 1
                                st.session_state[exp_nonce_key] += 1
                                st.rerun()
                            except Exception as e:
                                st.error(f"Ошибка при сохранении файлов: {e}")

                    st.markdown("---")

                    link_title_key = f"page_link_title_{page_id}"
                    link_url_key = f"page_link_url_{page_id}"

                    link_title = st.text_input("Название для ссылки", key=link_title_key)
                    link_url = st.text_input("URL", key=link_url_key)

                    if st.button("Сохранить ссылку", key=f"btn_save_link_{page_id}", use_container_width=True):
                        question_number_val = st.session_state.get(question_number_key, "")
                        try:
                            save_link_attachment(page_id, link_url, link_title, selected_login, question_number_val)
                            st.success("Ссылка сохранена")
                            st.session_state[exp_nonce_key] += 1
                            st.rerun()
                        except ValueError as exc:
                            st.warning(str(exc))
                        except Exception as exc:
                            st.error(f"Ошибка при сохранении ссылки: {exc}")

                    if st.button("Отмена", key=f"btn_cancel_attach_{page_id}", use_container_width=True):
                        st.session_state[up_nonce_files_key] += 1
                        st.session_state.pop(link_title_key, None)
                        st.session_state.pop(link_url_key, None)
                        st.session_state[exp_nonce_key] += 1
                        st.rerun()
                else:
                    st.caption("У вас нет прав на добавление файлов и ссылок.")


        with col_copy:
            # копируем именно то, что видите в preview (HTML для Word + plain fallback)
            safe_preview_html_for_copy = _import_mod.sanitize_html_safe(current_html or "")
            plain_for_copy = export_mod._html_to_plain_preserving_layout(current_html or "")

            render_copy_rich_button(
                html=safe_preview_html_for_copy,
                plain=plain_for_copy,
                btn_key=f"copy_preview_rich_{page_id}",
                button_label="🗗 Копировать страницу",
                ok_message="Скопировано.",
                button_style="""
                    cursor:pointer;
                    width:100%;
                    padding:0.60rem 0.90rem;      # как у st.button
                    min-height: 2.5rem;
                    display:inline-flex;
                    align-items:center;
                    gap:0.35rem;
                    border:1px solid rgba(49,51,63,.2);
                    border-radius:0.5rem;
                    background: white;
                    font-size:14px;
                    line-height:1.3;
                """,
                align="flex-end",
                height=54,
            )


        with col_link:
            if page_link:
                render_copy_text_button(
                    text=page_link,
                    btn_key=f"copy_page_link_{page_id}",
                    button_label="🔗 Копировать ссылку",
                    ok_message="Скопирована",
                    button_style="""
                        cursor:pointer;
                        width:100%;
                        padding:0.60rem 0.90rem;
                        min-height: 2.5rem;
                        display:inline-flex;
                        align-items:center;
                        gap:0.35rem;
                        border:1px solid rgba(49,51,63,.2);
                        border-radius:0.5rem;
                        background: white;
                        font-size:14px;
                        line-height:1.3;
                    """,
                    align="flex-end",
                    height=54,
                )
            else:
                st.caption("Заполните `config.app_base_url`.")

        # --------------- Справка------------------------------------------
        with col_help:
            help_path = os.path.join(os.path.dirname(__file__), "assets", "Справка_ДФИП_Notes.pdf")
            if os.path.exists(help_path):
                try:
                    with open(help_path, "rb") as f:
                        help_bytes = f.read()
                    st.download_button(
                        label="❓",
                        data=help_bytes,
                        file_name="Справка_ДФИП_Notes.pdf",
                        mime="application/pdf",
                        key=f"btn_help_pdf_{page_id}",
                        use_container_width=True,
                    )
                except Exception as exc:
                    st.caption(f"Ошибка чтения справки: {exc}")
            else:
                st.caption("Файл справки не найден (.assets/Справка_ДФИП_Notes.pdf)")


        t0 = time.perf_counter()
        _log_metric("current_html_chars", len(current_html or ""))
        safe_preview_html = _import_mod.sanitize_html_safe(current_html or "")
        _log_timing("sanitize_preview_html", t0)
        _log_metric("safe_preview_html_chars", len(safe_preview_html or ""))
        # Info line only for preview (not persisted or shown in editor)
        path_info = f"{dept_prefix}{current_page['notebook_name']} > {current_page['section_name']} > {current_page['title']}"
        path_info = html.escape(path_info)
        tag_info = html.escape(current_tag) if current_tag else ""
        preview_meta = f"<div class='preview-meta'>{path_info}{(';       Tag: ' + tag_info) if tag_info else ''}</div>"
        highlight_query = (search_text or "").strip()
        highlight_block = ""
        def _extract_highlight_terms(raw: str) -> list[str]:
            toks = _lex_search(raw or "")
            seen: set[str] = set()
            terms: list[str] = []
            for t in toks:
                if t.typ not in ("WORD", "PHRASE"):
                    continue
                cleaned = (t.val or "")
                cleaned = re.sub(r"[|()]+", " ", cleaned)
                cleaned = cleaned.replace("*", " ")
                for part in cleaned.split():
                    part_norm = part.strip()
                    if part_norm and part_norm not in seen:
                        seen.add(part_norm)
                        terms.append(part_norm)
            return terms

        highlight_terms = _extract_highlight_terms(highlight_query)
        if highlight_terms:
            _terms_json = json.dumps(highlight_terms, ensure_ascii=False)
            highlight_block = """
            <style>
            mark.search-hit {
                background-color: #fff59d;
                padding: 0 2px;
            }
            </style>
            <script>
            (() => {
                const terms = TERMS_JSON;
                const keywords = Array.from(
                    new Set((terms || []).map((t) => (t || "").trim()).filter(Boolean))
                );
                if (!keywords.length) return;

                const escapeRegex = (s) =>
                    s.replace(/[-\\/\\^$*+?.()|[\\]{}]/g, "\\$&");
                const escapedParts = keywords.map(escapeRegex).filter(Boolean);
                if (!escapedParts.length) return;
                const regex = new RegExp(escapedParts.join("|"), "gi");

                const runHighlight = () => {
                    const root = document.querySelector(".preview-body");
                    if (!root) return;

                    // Убираем предыдущие подсветки перед новым поиском
                    root.querySelectorAll("mark.search-hit").forEach((el) => {
                        const textNode = document.createTextNode(el.textContent);
                        el.replaceWith(textNode);
                    });

                    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
                        acceptNode: (node) => {
                            const parent = node?.parentNode;
                            if (!parent) return NodeFilter.FILTER_REJECT;
                            const tag = parent.nodeName;
                            if (tag === "SCRIPT" || tag === "STYLE") return NodeFilter.FILTER_REJECT;
                            if (!node.textContent || !node.textContent.trim()) return NodeFilter.FILTER_SKIP;
                            return NodeFilter.FILTER_ACCEPT;
                        },
                    });

                    let firstMark = null;
                    const nodes = [];
                    while (true) {
                        const n = walker.nextNode();
                        if (!n) break;
                        nodes.push(n);
                    }

                    nodes.forEach((textNode) => {

                        const original = textNode.textContent || "";
                        if (!original) return;

                        regex.lastIndex = 0;
                        let match;
                        let lastIndex = 0;
                        const frag = document.createDocumentFragment();

                        while ((match = regex.exec(original)) !== null) {
                            const start = match.index;
                            const end = regex.lastIndex;
                            if (start > lastIndex) {
                                frag.appendChild(document.createTextNode(original.slice(lastIndex, start)));
                            }
                            const mark = document.createElement("mark");
                            mark.className = "search-hit";
                            mark.textContent = original.slice(start, end);
                            frag.appendChild(mark);
                            if (!firstMark) firstMark = mark;
                            lastIndex = end;
                            if (regex.lastIndex === match.index) {
                                regex.lastIndex++;
                            }
                        }
                        if (lastIndex < original.length) {
                            frag.appendChild(document.createTextNode(original.slice(lastIndex)));
                        }
                        if (frag.childNodes.length) {
                            textNode.replaceWith(frag);
                        }
                    });

                    if (firstMark && typeof firstMark.scrollIntoView === "function") {
                        firstMark.scrollIntoView({ behavior: "smooth", block: "center" });
                    }
                };

                if (document.readyState === "loading") {
                    document.addEventListener("DOMContentLoaded", runHighlight, { once: true });
                }
                runHighlight();
                setTimeout(runHighlight, 50);
            })();
            </script>
            """
            highlight_block = highlight_block.replace("TERMS_JSON", _terms_json)



        
        preview_html = f"""
        <style>
        .page-preview-wrapper {{
            border: 1px solid #d0d4da;
            border-radius: 6px;
            padding: 16px 18px;
            margin-top: 6px;  /* show top border while keeping spacing compact */
            background-color: #ffffff;
            box-shadow: 0 1px 2px rgba(0,0,0,0.04);
            min-height: 560px;
            box-sizing: border-box;
        }}
        .preview-body *,
        .preview-body p,
        .preview-body li {{
            line-height: 1.15 !important;
        }}
        .preview-body p {{
            margin: 0.2em 0 !important;
        }}
        .preview-meta {{
            font-size: 15px;  /* +2px for emphasis */
            color: #2563eb;   /* blue tone */
            margin: 0 0 8px 0;
            padding: 0;
            line-height: 1.2;
        }}
        </style>

        <div class="page-preview-wrapper">
            {preview_meta}
            <div class="preview-body">
                {safe_preview_html or "<p><em>Нет содержимого</em></p>"}
            </div>
        </div>
        {highlight_block}
        """
        _log_metric("preview_html_chars", len(preview_html))
        t0 = time.perf_counter()
        components.html(preview_html, height=580, scrolling=True)
        _log_timing("components_html_preview", t0)


        # ✅ one-shot авто-открытие редактора (после создания/копирования), НЕ будет открываться после открытия вложений
        st.session_state.setdefault("open_editor_once_for_page", None)
        open_once = st.session_state.pop("open_editor_once_for_page", None)
        default_edit_mode = bool(st.session_state.pop("open_editor_once_edit_mode", False))
        if can_edit_notebook and open_once == page_id:
            st.session_state["edit_dialog_page_id"] = int(page_id)
            edit_page_dialog(page_id, current_title, current_html, current_tag, current_page.get("status", ""), default_edit_mode=default_edit_mode)


        # --- таблица вложений ---
        t0 = time.perf_counter()
        attachments_df = get_page_attachments(page_id)
        _log_timing("get_page_attachments", t0)
        if not attachments_df.empty:
            _log_metric("attachments_count", len(attachments_df))
            att_display = attachments_df.copy()
            att_display["Размер"] = att_display["file_size"].apply(export_mod._format_file_size)
            att_display["Создано"] = pd.to_datetime(att_display["created_at"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
            att_display["Тип"] = att_display["attachment_type"].map({"file": "Файл", "link": "Ссылка"}).fillna(att_display["attachment_type"])
            att_display["Название"] = att_display["file_name"]
            att_display["Автор"] = att_display["created_by"]
            att_display["URL"] = att_display["url"].fillna("")
            att_display["Номер вопроса"] = att_display["question_number"].apply(lambda v: str(v).strip() if pd.notna(v) else "")
            grid_df = att_display[["id", "Номер вопроса", "Название", "Тип", "Размер", "Создано", "Автор", "URL"]]

            links_data = []
            t0 = time.perf_counter()
            files_processed = 0
            total_bytes = 0
            for row in attachments_df.itertuples(index=False):
                if row.attachment_type == "file":
                    payload = get_attachment_file(row.id)
                    if payload:
                        file_bytes, file_name, mime_type = payload
                        files_processed += 1
                        total_bytes += len(file_bytes)
                        b64 = base64.b64encode(file_bytes).decode("ascii")
                        links_data.append({"id": int(row.id), "name": str(file_name), "href": f"data:{mime_type};base64,{b64}"})
            _log_timing(f"attachments_build_links files={files_processed} bytes={total_bytes}", t0)
            _log_metric("attachments_links_count", len(links_data))

            if links_data:
                js_payload = json.dumps(links_data, ensure_ascii=False)
                _log_metric("attachments_links_payload_chars", len(js_payload))
                t0 = time.perf_counter()
                components.html(
                    f"""
                    <style>
                    html, body {{
                        margin: 0 !important;
                        padding: 0 !important;
                        height: 0 !important;
                        overflow: hidden !important;
                    }}
                    </style>
                    <script>
                    (function() {{
                        var data = {js_payload};
                        var doc = window.parent.document;
                        var container = doc.getElementById('attachments_hidden_links');
                        if (!container) {{
                            container = doc.createElement('div');
                            container.id = 'attachments_hidden_links';
                            container.style.display = 'none';
                            doc.body.appendChild(container);
                        }}
                        data.forEach(function(item) {{
                            var id = item.id;
                            var a = doc.getElementById('att_dl_' + id);
                            if (!a) {{
                                a = doc.createElement('a');
                                a.id = 'att_dl_' + id;
                                a.download = item.name;
                                a.href = item.href;
                                container.appendChild(a);
                            }} else {{
                                a.download = item.name;
                                a.href = item.href;
                            }}
                        }});
                    }})();
                    </script>
                    """,
                    height=1,
                    scrolling=False,
                )
                _log_timing("components_html_attachments_links", t0)

            gb_att = GridOptionsBuilder.from_dataframe(grid_df)
            gb_att.configure_selection("single", use_checkbox=False)
            gb_att.configure_column("id", hide=True)
            gb_att.configure_column("URL", hide=True)
            gb_att.configure_column("Номер вопроса", width=100)
            gb_att.configure_column("Название", width=500)
            gb_att.configure_column("Размер", width=90)
            gb_att.configure_column("Тип", width=80)

            row_doubleclick_js = JsCode(
                """
                function (e) {
                    if (e && e.event) {
                        e.event.preventDefault();
                        e.event.stopPropagation();
                        if (typeof e.event.stopImmediatePropagation === "function") {
                            e.event.stopImmediatePropagation();
                        }
                    }
                    var d = e.data || {};
                    var id = d.id;
                    var type = d["Тип"];
                    var url = d["URL"] || "";

                    if (!id) { return; }

                    if (type === "Файл") {
                        var a = window.parent.document.getElementById("att_dl_" + id);
                        if (!a) { return; }
                        var href = a.href || "";
                        var filename = a.download || "attachment";
                        if (href.indexOf("data:") === 0) {
                            try {
                                var comma = href.indexOf(",");
                                if (comma > 0) {
                                    var b64 = href.substring(comma + 1);
                                    // Насильно используем octet-stream, чтобы браузер не пытался открыть файл.
                                    var mime = "application/octet-stream";
                                    var byteChars = atob(b64);
                                    var byteNumbers = new Array(byteChars.length);
                                    for (var i = 0; i < byteChars.length; i++) {
                                        byteNumbers[i] = byteChars.charCodeAt(i);
                                    }
                                    var blob = new Blob([new Uint8Array(byteNumbers)], {type: mime});
                                    var blobUrl = window.parent.URL.createObjectURL(blob);
                                    var dl = window.parent.document.createElement("a");
                                    dl.style.display = "none";
                                    dl.href = blobUrl;
                                    dl.download = filename;
                                    dl.target = "_self";  // избегаем открытия новой вкладки
                                    window.parent.document.body.appendChild(dl);
                                    dl.click();
                                    dl.remove();
                                    window.parent.setTimeout(function() {
                                        window.parent.URL.revokeObjectURL(blobUrl);
                                    }, 1000);
                                    return;
                                }
                            } catch (err) {
                                // fallback to direct click
                            }
                        }
                        a.setAttribute("download", filename);
                        a.setAttribute("target", "_self");  // скачиваем в той же вкладке без перехода
                        a.click();
                    } else if (type === "Ссылка" && url) {
                        window.parent.open(url, "_blank");
                    }
                }
                """
            )
            gb_att.configure_grid_options(onRowDoubleClicked=row_doubleclick_js)

            t0 = time.perf_counter()
            grid_response = AgGrid(
                grid_df,
                gridOptions=gb_att.build(),
                enable_enterprise_modules=False,
                update_on=["selectionChanged"],
                height=220,
                fit_columns_on_grid_load=True,
                allow_unsafe_jscode=True,
            )
            _log_timing("AgGrid attachments list", t0)

            selected_rows = grid_response.get("selected_rows", [])
            if isinstance(selected_rows, pd.DataFrame):
                selected_rows = selected_rows.to_dict("records")
            selected_att = selected_rows[0] if selected_rows else None

            if selected_att:
                att_id = int(selected_att["id"])
                att_type = selected_att.get("Тип")
                url_val = selected_att.get("URL") or ""

                if att_type == "Файл":
                    if can_edit_notebook:
                        del_col, _ = st.columns([1,  4])
                        with del_col:
                            if st.button("Удалить вложение", key=f"delete_attachment_{att_id}", use_container_width=True):
                                delete_attachment(att_id)
                                if st.session_state.get("download_att_id") == att_id:
                                    st.session_state["download_att_id"] = None
                                    st.session_state["download_payload"] = None
                                    st.session_state["download_error"] = None

                                add_event_log(topic="ATTACHMENT", subtopic="DELETE",notebook_id=selected_notebook_id, section_id=selected_section_id, page_id=page_id, event="delete: attachment_id = " + str(att_id) + ": attachment_type = " + selected_att.get("Тип") + ": name = " + selected_att.get("Название"), body_html="")
                                st.success("Вложение удалено")
                                st.rerun()


                elif att_type == "Ссылка":
                    if not url_val:
                        st.warning("Ссылка не указана.")
                    else:
                        del_col, _ = st.columns([1, 4])
                        if can_edit_notebook:
                            with del_col:
                                if st.button("Удалить ссылку", key=f"delete_link_{att_id}", use_container_width=True):
                                    delete_attachment(att_id)
                                    st.success("Ссылка удалена")
                                    st.rerun()


        # --- Удаление страницы (с проверкой вложений) + справка (в одну строку) ---
        col_a, col_b, col_c, col_d = st.columns([1.8, 1.2, 2, 4])

        if can_edit_notebook:
            attachments_df_for_delete = attachments_df
            has_attachments = not attachments_df_for_delete.empty

            if has_attachments:
                st.warning("Удаление страницы запрещено: сначала удалите все прикреплённые файлы и ссылки.")

            with col_a:
                confirm_delete = st.checkbox(
                    "Подтвердить удаление",
                    key=f"confirm_delete_{page_id}",
                    disabled=has_attachments,
                )

            with col_b:
                delete_clicked = st.button(
                    "Удалить страницу",
                    key=f"delete_{page_id}",
                    type="secondary",
                    use_container_width=True,
                    disabled=has_attachments,
                )

            if delete_clicked:
                # если вложения есть — кнопка должна быть disabled,
                # но оставим защиту на всякий случай
                if has_attachments:
                    st.error("Нельзя удалить страницу: сначала удалите все вложения.")
                    st.stop()

                if not confirm_delete:
                    st.warning("Поставьте галочку для подтверждения.")
                    st.stop()

                _body_html = run_scalar(
                                            f"""
                                            SELECT body_html
                                            FROM {PAGES_TABLE}
                                            WHERE id = {int(page_id)}
                                            LIMIT 1
                                            """
                                        )
                delete_page(page_id)
                add_event_log(topic="PAGE", subtopic="DELETE",notebook_id=selected_notebook_id, section_id=selected_section_id, page_id=page_id, event="delete: page_id = " + str(page_id), body_html=_body_html)
                st.success("Страница удалена")

                if st.session_state.get("edit_dialog_page_id") == page_id:
                    st.session_state["edit_dialog_page_id"] = None
                if st.session_state.get("append_dialog_page_id") == page_id:
                    st.session_state["append_dialog_page_id"] = None
                if st.session_state.get("email_dialog_page_id") == page_id:
                    st.session_state["email_dialog_page_id"] = None

                st.session_state["current_page_id"] = None
                st.rerun()
                
        else:
            st.info("У вас права только на просмотр этой записной книжки.")

        with col_c:
            pass

        # ---------------- Переместить/скопировать ----------------
        with col_d:
            exp_move_nonce_key = f"exp_move_nonce_{page_id}"
            st.session_state.setdefault(exp_move_nonce_key, 0)

            def _collapse_move():
                st.session_state[exp_move_nonce_key] += 1

            exp_move_label = "Переместить или скопировать" + ("\u200b" * int(st.session_state[exp_move_nonce_key]))

            with st.expander(exp_move_label, expanded=False):
                st.write(f"Текущая страница: **{current_title or f'ID {page_id}'}**")

                if owned_notebooks_df.empty:
                    st.info(
                        "У вас нет записных книг, в которых вы являетесь владельцем. "
                        "Копирование и перемещение недоступны."
                    )
                else:
                    dest_nb_records = list(owned_notebooks_df.itertuples(index=False))

                    cur_nb_id = int(current_page["notebook_id"])
                    default_nb = next((r for r in dest_nb_records if int(r.id) == cur_nb_id), dest_nb_records[0])
                    move_nb_key = f"move_nb_{page_id}"
                    if st.session_state.get(move_nb_key) not in dest_nb_records:
                        st.session_state[move_nb_key] = default_nb

                    dest_notebook = st.selectbox(
                        "Записная книга (для копирования/перемещения)",
                        dest_nb_records,
                        format_func=lambda r: r.name,
                        key=move_nb_key,
                    )

                    dest_sections_df = get_sections(int(dest_notebook.id))
                    dest_sec_records = list(dest_sections_df.itertuples(index=False))

                    if not dest_sec_records:
                        st.warning("В выбранной книге нет разделов. Сначала создайте раздел.")
                    else:
                        cur_sec_id = int(current_page["section_id"])
                        default_sec = next((r for r in dest_sec_records if int(r.id) == cur_sec_id), dest_sec_records[0])
                        move_sec_key = f"move_sec_{page_id}"
                        if st.session_state.get(move_sec_key) not in dest_sec_records:
                            st.session_state[move_sec_key] = default_sec

                        dest_section = st.selectbox(
                            "Раздел",
                            dest_sec_records,
                            format_func=lambda r: r.name,
                            key=move_sec_key,
                        )

                        col_move, col_copy, col_cancel = st.columns(3)

                        with col_move:
                            if can_edit_notebook:
                                move_clicked = st.button(
                                    "Переместить",
                                    type="primary",
                                    key=f"btn_move_{page_id}",
                                    use_container_width=True,
                                )
                            else:
                                move_clicked = False
                                st.caption("Перемещение доступно только владельцу книги.")

                        with col_copy:
                            copy_clicked = st.button(
                                "Копировать",
                                key=f"btn_copy_{page_id}",
                                use_container_width=True,
                            )

                        with col_cancel:
                            cancel_clicked = st.button(
                                "Отмена",
                                key=f"btn_cancel_{page_id}",
                                use_container_width=True,
                            )

                        if move_clicked and can_edit_notebook:
                            if int(dest_section.id) == int(current_page["section_id"]):
                                st.info("Страница уже находится в выбранном разделе.")
                            else:
                                moved_page_id = run_scalar(
                                    f"""
                                    WITH moved_page AS (
                                        INSERT INTO {PAGES_TABLE}
                                            (section_id, title, tag, body_html, created_by)
                                        VALUES
                                            ({int(dest_section.id)},
                                             '{_escape(current_title)}',
                                             '{_escape(current_tag)}',
                                             '{_escape(current_html)}',
                                             '{_escape(selected_login)}')
                                        RETURNING id
                                    ),
                                    copied_attachments AS (
                                        INSERT INTO {ATTACHMENTS_TABLE}
                                            (page_id, attachment_type, file_name, mime_type, file_size, file_data, url, created_by, question_number)
                                        SELECT
                                            mp.id,
                                            a.attachment_type,
                                            a.file_name,
                                            a.mime_type,
                                            a.file_size,
                                            a.file_data,
                                            a.url,
                                            a.created_by,
                                            a.question_number
                                        FROM {ATTACHMENTS_TABLE} a
                                        CROSS JOIN moved_page mp
                                        WHERE a.page_id = {int(page_id)}
                                    ),
                                    deleted_attachments AS (
                                        DELETE FROM {ATTACHMENTS_TABLE}
                                        WHERE page_id = {int(page_id)}
                                    ),
                                    deleted_page AS (
                                        DELETE FROM {PAGES_TABLE}
                                        WHERE id = {int(page_id)}
                                    )
                                    SELECT id FROM moved_page
                                    """
                                )
                        
                                if moved_page_id is None:
                                    st.error("Ошибка при переносе страницы.")
                                else:
                                    moved_page_id = int(moved_page_id)
                                    st.success("Страница перемещена")
                                    st.session_state["current_page_id"] = moved_page_id
                                    st.session_state["force_page_id"] = moved_page_id
                                    _collapse_move()
                                    _bump_data_version()
                        
                                    add_event_log(topic="PAGE", subtopic="MOVE",notebook_id=int(dest_notebook.id), section_id=int(dest_section.id), page_id=moved_page_id, event="move: new_page_id = " + str(moved_page_id) + "; old_page_id = " + str(page_id) + "; new_section_id = " + str(dest_section.id) + "; old_section_id = " + str(selected_section_id) + "; old_notebook_id = " + str(selected_notebook_id), body_html="")
                                    st.rerun()
                        
                        if copy_clicked:
                            new_page_id = run_scalar(
                                f"""
                                INSERT INTO {PAGES_TABLE}
                                    (section_id, title, tag, body_html, created_by)
                                VALUES
                                    ({int(dest_section.id)},
                                     '{_escape(current_title)}',
                                     '{_escape(current_tag)}',
                                     '{_escape(current_html)}',
                                     '{_escape(selected_login)}')
                                RETURNING id
                                """
                            )
                            add_event_log(topic="PAGE", subtopic="COPY",notebook_id=int(dest_notebook.id), section_id=int(dest_section.id), page_id=new_page_id, event="copy page: new_page_id = " + str(new_page_id) + "; from_page_id = " + str(current_page["id"]), body_html=current_page["body_html"]) 

                            if new_page_id is None:
                                st.error("Ошибка при копировании страницы (не получен новый id).")
                            else:
                                new_page_id = int(new_page_id)
                                st.success("Страница скопирована")
                                st.session_state["current_page_id"] = new_page_id
                                st.session_state["force_page_id"] = new_page_id
                                st.session_state["open_editor_once_for_page"] = new_page_id
                                st.session_state["open_editor_once_edit_mode"] = False
                                _collapse_move()
                                _bump_data_version()
                                st.rerun()

                        if cancel_clicked:
                            _collapse_move()
                            st.rerun()


    _render_expanders_state_bridge()
    st.logo("assets/logo.png", size="medium")


if __name__ == "__main__":
    main()
