import logging
import os
import streamlit as st
import streamlit.components.v1 as components
import subprocess
import sys
import tempfile

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
import email
import io
import json
import re
import urllib.parse
import datetime
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from docx import Document
from docx.enum.text import WD_LINE_SPACING
from docx.shared import Inches, Pt
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode
from st_aggrid.shared import GridUpdateMode, DataReturnMode
from streamlit_quill import st_quill

# ---- XSS sanitize (bleach optional) ----
try:
    import bleach  # type: ignore

    HAVE_BLEACH = True
except Exception:
    bleach = None
    HAVE_BLEACH = False

import config
from src.database_utils_DRP import get_execute, get_fetch, test_connection
from src.mail import send_mail

SCHEMA = "sbx_dfip_ocpp"
USERS_TABLE = f"{SCHEMA}.notes_users"
NOTEBOOKS_TABLE = f"{SCHEMA}.notes_notebooks"
SECTIONS_TABLE = f"{SCHEMA}.notes_sections"
PAGES_TABLE = f"{SCHEMA}.notes_pages"
OWNERS_TABLE = f"{SCHEMA}.notes_notebook_owners"
DEPARTMENTS_TABLE = f"{SCHEMA}.notes_departments"
ATTACHMENTS_TABLE = f"{SCHEMA}.notes_page_attachments"
EMAIL_RECIPIENTS_TABLE = f"{SCHEMA}.notes_email_recipients"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("notes_app")


def _escape(val: str) -> str:
    """Минимальное экранирование строк для SQL."""
    return (val or "").replace("'", "''")



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


# -------------------------
# Advanced search parser (Google-like)
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
        f"OR COALESCE(p.body_html, '') ILIKE {like_pattern_sql}{esc})"
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





# =========================
# XSS protection (sanitize HTML)
# =========================
_DANGEROUS_TAGS = {
    "script",
    "iframe",
    "object",
    "embed",
    "link",
    "meta",
    "base",
    "form",
    "input",
    "button",
    "textarea",
    "select",
    "option",
    "svg",
    "math",
}


def _is_bad_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return (
        u.startswith("javascript:")
        or u.startswith("vbscript:")
        or u.startswith("data:text/html")
        or u.startswith("data:application/xhtml+xml")
    )


def _clean_style_attr(style: str) -> str:
    """
    Минимальная чистка CSS.
    Убираем expression(), javascript: и url(javascript:...)
    """
    s = (style or "")
    s = re.sub(r"expression\s*\([^)]*\)", "", s, flags=re.I)
    s = re.sub(r"javascript\s*:", "", s, flags=re.I)
    s = re.sub(r"url\s*\(\s*['\"]?\s*javascript:[^)]*\)", "", s, flags=re.I)
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    return s.strip()


def sanitize_html_xss(html: str) -> str:
    """
    Санитизация HTML против XSS.
    1) Если установлен bleach — используем whitelist.
    2) Иначе — fallback через BeautifulSoup: удаляем опасные теги и атрибуты.
    """
    if not html:
        return ""

    if HAVE_BLEACH and bleach is not None:
        allowed_tags = [
            "p",
            "br",
            "div",
            "span",
            "b",
            "strong",
            "i",
            "em",
            "u",
            "s",
            "sub",
            "sup",
            "blockquote",
            "pre",
            "code",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "ul",
            "ol",
            "li",
            "table",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "th",
            "td",
            "colgroup",
            "col",
            "a",
            "img",
        ]
        allowed_attrs = {
            "*": ["style", "class"],
            "a": ["href", "title", "target", "rel", "name"],
            "img": ["src", "alt", "title", "width", "height"],
            "td": ["colspan", "rowspan"],
            "th": ["colspan", "rowspan"],
            "col": ["span", "width"],
        }

        cleaned = bleach.clean(
            html,
            tags=allowed_tags,
            attributes=allowed_attrs,
            protocols=["http", "https", "mailto", "data"],
            strip=True,
        )

        soup = BeautifulSoup(cleaned, "html.parser")
        for tag in soup.find_all(True):
            # remove event handlers
            for attr in list(tag.attrs.keys()):
                if str(attr).lower().startswith("on"):
                    del tag.attrs[attr]

            if tag.has_attr("href") and _is_bad_url(tag.get("href", "")):
                del tag.attrs["href"]
            if tag.has_attr("src") and _is_bad_url(tag.get("src", "")):
                del tag.attrs["src"]

            if tag.has_attr("style"):
                tag.attrs["style"] = _clean_style_attr(tag.attrs.get("style", ""))

            if tag.name and tag.name.lower() == "a":
                rel = (tag.get("rel") or [])
                if isinstance(rel, str):
                    rel = [rel]
                rel_set = set([r.lower() for r in rel])
                rel_set.update({"noopener", "noreferrer"})
                tag["rel"] = " ".join(sorted(rel_set))

        return str(soup)

    soup = BeautifulSoup(html, "html.parser")

    for bad in soup.find_all(list(_DANGEROUS_TAGS)):
        bad.decompose()

    for tag in soup.find_all(True):
        for attr in list(tag.attrs.keys()):
            if str(attr).lower().startswith("on"):
                del tag.attrs[attr]

        if tag.has_attr("href") and _is_bad_url(tag.get("href", "")):
            del tag.attrs["href"]
        if tag.has_attr("src") and _is_bad_url(tag.get("src", "")):
            del tag.attrs["src"]

        if tag.has_attr("style"):
            tag.attrs["style"] = _clean_style_attr(tag.attrs.get("style", ""))

        if tag.name and tag.name.lower() == "a":
            rel = (tag.get("rel") or [])
            if isinstance(rel, str):
                rel = [rel]
            rel_set = set([r.lower() for r in rel])
            rel_set.update({"noopener", "noreferrer"})
            tag["rel"] = " ".join(sorted(rel_set))

    return str(soup)


def _name_patronymic(full_name: str | None, fallback_login: str) -> str:
    """Возвращает Имя Отчество из full_name, иначе login."""
    if not full_name:
        return fallback_login
    parts = full_name.strip().split()
    if len(parts) >= 3:
        return " ".join(parts[1:3])
    if len(parts) >= 2:
        return " ".join(parts[1:])
    return full_name.strip() or fallback_login


def _format_file_size(size: int | float | None) -> str:
    if not size or size <= 0:
        return ""
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} B"


def _parse_email_list(raw: str) -> list[str]:
    parts = re.split(r"[,;\s]+", raw or "")
    out: list[str] = []
    for part in parts:
        item = part.strip()
        if item:
            out.append(item)
    return out


def _parse_path_list(raw: str) -> list[str]:
    parts = re.split(r"[;,\n]+", raw or "")
    out: list[str] = []
    for part in parts:
        item = part.strip()
        if item:
            out.append(item)
    return out


def _merge_email_lists(*lists: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for items in lists:
        for item in items:
            if not item or item in seen:
                continue
            seen.add(item)
            out.append(item)
    return out


def _is_valid_email(value: str) -> bool:
    val = (value or "").strip()
    if not val:
        return False
    return re.match(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", val) is not None


def _sanitize_attachment_filename(name: str) -> str:
    base = os.path.basename(name or "attachment")
    base = _transliterate_ru_filename(base)
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base)
    base = re.sub(r"_+", "_", base).strip("._- ")
    return base or "attachment"


def _transliterate_ru_filename(text: str) -> str:
    if not text:
        return ""

    m = {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "yo",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "kh",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
    out: list[str] = []
    for ch in text:
        low = ch.lower()
        if low in m:
            rep = m[low]
            if ch.isupper() and rep:
                rep = rep[0].upper() + rep[1:]
            out.append(rep)
        else:
            out.append(ch)
    return "".join(out)


def ensure_db_credentials() -> dict[str, str]:
    """Запрос логина/пароля к БД один раз за сессию."""
    creds = st.session_state.get("db_credentials")
    if creds and creds.get("user") and creds.get("password"):
        st.session_state.setdefault("current_user_login", creds["user"])
        return creds

    @st.dialog("Подключение к базе", width="small")
    def _ask_credentials():
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
                st.success("Подключение установлено. Обновляем страницу...")
                st.rerun()

    _ask_credentials()
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
st.session_state.setdefault("email_dialog_page_id", None)  # int | None


def _creds() -> tuple[str, str]:
    creds = ensure_db_credentials()
    return creds["user"], creds["password"]


def run_fetch_df(query: str) -> pd.DataFrame:
    user, pwd = _creds()
    try:
        df = get_fetch(query, user, pwd)
        if df is None:
            return pd.DataFrame()
        return df
    except Exception as e:
        logger.exception("DB fetch error: %s", e)
        st.error(f"Ошибка запроса к БД: {e}")
        return pd.DataFrame()



def run_execute(query: str) -> int | None:
    user, pwd = _creds()
    return get_execute(query, user, pwd)


def run_scalar(query: str):
    df = run_fetch_df(query)
    if df.empty:
        return None
    return df.iat[0, 0]


def list_users() -> pd.DataFrame:
    return run_fetch_df(
        f"""
        SELECT login, full_name, department_id
        FROM {USERS_TABLE}
        ORDER BY COALESCE(full_name, login)
        """
    )


def get_user_signature(login: str) -> tuple[str, str]:
    df = run_fetch_df(
        f"""
        SELECT full_name, job_title
        FROM {USERS_TABLE}
        WHERE login = '{_escape(login)}'
        LIMIT 1
        """
    )
    if df.empty:
        return login, ""
    full_name = str(df.at[0, "full_name"] or "").strip() or login
    job_title = str(df.at[0, "job_title"] or "").strip()
    return full_name, job_title


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


def create_user(login: str, full_name: str) -> str:
    normalized = login.strip().lower()
    if not normalized:
        raise ValueError("Логин не может быть пустым")
    name = full_name.strip() or normalized
    logger.info("Создание пользователя: login=%s, full_name=%s", normalized, name)
    run_execute(
        f"""
        INSERT INTO {USERS_TABLE} (login, full_name)
        VALUES ('{_escape(normalized)}', '{_escape(name)}')
        """
    )
    return normalized


def add_notebook_owner(notebook_id: int, user_login: str) -> None:
    logger.info("Добавление владельца: notebook_id=%s user=%s", notebook_id, user_login)
    run_execute(
        f"""
        INSERT INTO {OWNERS_TABLE} (notebook_id, user_login)
        VALUES ({int(notebook_id)}, '{_escape(user_login)}')
        """
    )


def remove_notebook_owner(notebook_id: int, user_login: str) -> None:
    """Удалить пользователя из владельцев книги."""
    logger.info("Удаление владельца: notebook_id=%s user=%s", notebook_id, user_login)
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
    logger.info(
        "Изменение department_id книги: id=%s department_id=%s",
        notebook_id,
        department_id,
    )
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
    query = f"""
        SELECT id, notebook_id, name, created_at, updated_at, created_by
        FROM {SECTIONS_TABLE}
    """
    if notebook_id:
        query += f" WHERE notebook_id = {int(notebook_id)}"
    query += " ORDER BY name"
    return run_fetch_df(query)


def load_pages_df(
    notebook_id: int | None,
    section_id: int | None,
    allowed_notebook_ids: list[int],
    search_text: str | None,
    search_tags_only: bool,
) -> pd.DataFrame:
    if not allowed_notebook_ids:
        return pd.DataFrame()

    # ✅ ДОБАВИЛИ p.status
    query = f"""
        SELECT
            p.id,
            p.title,
            p.status,
            p.tag,
            p.body_html,
            p.created_at,
            p.updated_at,
            p.created_by,
            s.id AS section_id,
            s.name AS section_name,
            n.id AS notebook_id,
            n.name AS notebook_name,
            n.department_id AS notebook_department_id
        FROM {PAGES_TABLE} p
        JOIN {SECTIONS_TABLE} s ON p.section_id = s.id
        JOIN {NOTEBOOKS_TABLE} n ON s.notebook_id = n.id
        WHERE 1=1
    """

    allowed_csv = ", ".join(str(int(x)) for x in allowed_notebook_ids)
    query += f" AND n.id IN ({allowed_csv})"


    if section_id:
        query += f" AND s.id = {int(section_id)}"


    if search_text:
        if search_tags_only:
            # ✅ расширенный поиск (Google-like) по тегам (p.tag)
            query += build_advanced_search_where(search_text, mode="tags")
        else:
            # ✅ расширенный поиск по title/body_html
            query += build_advanced_search_where(search_text, mode="text")





    query += " ORDER BY p.updated_at DESC, p.id DESC"

    logger.info("PAGES SEARCH SQL:\n%s", query)

    df = run_fetch_df(query)
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()



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
    safe_html = sanitize_html_xss(body_html or "")
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


def update_page(page_id: int, title: str, body_html: str, tag: str) -> None:
    safe_html = sanitize_html_xss(body_html or "")
    run_execute(
        f"""
        UPDATE {PAGES_TABLE}
        SET title = '{_escape(title.strip() or 'Без названия')}',
            tag = '{_escape(tag)}',
            body_html = '{_escape(safe_html)}',
            updated_at = NOW()
        WHERE id = {int(page_id)}
        """
    )


def delete_page(page_id: int) -> None:
    run_execute(f"DELETE FROM {PAGES_TABLE} WHERE id = {int(page_id)}")


def get_page_attachments(page_id: int) -> pd.DataFrame:
    return run_fetch_df(
        f"""
        SELECT id,
               attachment_type,
               file_name,
               mime_type,
               file_size,
               url,
               created_at,
               created_by
        FROM {ATTACHMENTS_TABLE}
        WHERE page_id = {int(page_id)}
        ORDER BY created_at DESC, id DESC
        """
    )


def delete_attachment(attachment_id: int) -> None:
    run_execute(f"DELETE FROM {ATTACHMENTS_TABLE} WHERE id = {int(attachment_id)}")


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


@st.dialog("Новое сообщение", width="large")
def email_message_dialog(page_id: int, page_title: str, page_html: str, sender_login: str, page_path: str):
    recipients_df = list_email_recipients()
    recipient_options = list(recipients_df.itertuples(index=False)) if not recipients_df.empty else []
    sender_full_name, sender_job_title = get_user_signature(sender_login)

    def _recipient_label(row) -> str:
        fio = getattr(row, "fio", "") or ""
        email_addr = getattr(row, "email", "") or ""
        job_title = getattr(row, "job_title", "") or ""
        if fio and email_addr:
            label = f"{fio} <{email_addr}>"
        else:
            label = fio or email_addr
        if job_title:
            label = f"{label} ({job_title})"
        return label

    def _attachment_label(row) -> str:
        size_txt = _format_file_size(getattr(row, "file_size", None))
        suffix = f" ({size_txt})" if size_txt else ""
        return f"{row.file_name}{suffix}"

    attachments_df = get_page_attachments(page_id)
    file_records = [
        row for row in attachments_df.itertuples(index=False) if getattr(row, "attachment_type", "") == "file"
    ]
    link_records = [
        row
        for row in attachments_df.itertuples(index=False)
        if getattr(row, "attachment_type", "") == "link" and getattr(row, "url", None)
    ]

    def _build_links_block() -> str:
        if not link_records:
            return ""
        lines = []
        for row in link_records:
            title = (getattr(row, "file_name", "") or "").strip()
            url = (getattr(row, "url", "") or "").strip()
            if not url:
                continue
            if title:
                lines.append(f"- {title}: {url}")
            else:
                lines.append(f"- {url}")
        if not lines:
            return ""
        return "Ссылки:\n" + "\n".join(lines)

    subject_default = page_title or f"Страница {page_id}"
    body_plain = _html_to_plain_for_email(page_html or "")
    greeting = "Добрый день!"
    links_block = _build_links_block()
    page_info = f"{page_path}".strip()
    signature = f"С уважением!\n{sender_full_name}\n{sender_job_title}".rstrip()

    body_parts = [greeting]
    if body_plain:
        body_parts.append(body_plain)
    if links_block:
        body_parts.append(links_block)
    if page_path:
        body_parts.append(page_info)
    body_parts.append(signature)
    body_default = "\n\n".join(part for part in body_parts if part)

    with st.form(f"email_form_{page_id}", clear_on_submit=False):
        subject = st.text_input("Тема", value=subject_default)

        if not recipient_options:
            st.caption("Справочник получателей пуст.")

        to_dir = st.multiselect(
            "Получатели (справочник)",
            options=recipient_options,
            format_func=_recipient_label,
            key=f"email_to_dir_{page_id}",
        )
        cc_dir = st.multiselect(
            "Копия (справочник)",
            options=recipient_options,
            format_func=_recipient_label,
            key=f"email_cc_dir_{page_id}",
        )

        bcc_dir = st.multiselect(
            "Скрытая копия (справочник)",
            options=recipient_options,
            format_func=_recipient_label,
            key=f"email_bcc_dir_{page_id}",
        )

        body = st.text_area("Тело письма", value=body_default, height=200)
        important = st.checkbox("Важное", value=False)

        if file_records:
            selected_files = st.multiselect(
                "Вложения страницы",
                options=file_records,
                format_func=_attachment_label,
                key=f"email_files_{page_id}",
            )
        else:
            st.caption("Вложения страницы отсутствуют.")
            selected_files = []

        send_col, cancel_col, add_col = st.columns([1, 1, 2])
        with send_col:
            submitted = st.form_submit_button("Отправить")
        with cancel_col:
            canceled = st.form_submit_button("Отмена")
        with add_col:
            with st.expander("Добавить получателя", expanded=False):
                new_email = st.text_input("eMail", key=f"email_new_email_{page_id}")
                new_fio = st.text_input("ФИО", key=f"email_new_fio_{page_id}")
                new_gender = st.selectbox(
                    "Пол",
                    options=["муж", "жен"],
                    key=f"email_new_gender_{page_id}",
                )
                new_job_title = st.text_input("Должность", key=f"email_new_job_title_{page_id}")
                new_salutation = st.text_input("Как обращаться", key=f"email_new_salutation_{page_id}")
                add_recipient = st.form_submit_button("Добавить")

    if canceled:
        st.session_state["email_dialog_page_id"] = None
        st.rerun()
    elif add_recipient:
        email_trim = new_email.strip().lower()
        fio_trim = new_fio.strip()

        if not email_trim or not fio_trim:
            st.error("Заполните eMail и ФИО.")
            return

        if not _is_valid_email(email_trim):
            st.error("Некорректный формат eMail.")
            return

        exists = run_scalar(
            f"""
            SELECT 1
            FROM {EMAIL_RECIPIENTS_TABLE}
            WHERE lower(email) = lower('{_escape(email_trim)}')
            LIMIT 1
            """
        )
        if exists:
            st.error("eMail уже существует.")
            return

        run_execute(
            f"""
            INSERT INTO {EMAIL_RECIPIENTS_TABLE}
                (email, fio, job_title, salutation, gender, created_by)
            VALUES
                ('{_escape(email_trim)}',
                 '{_escape(fio_trim)}',
                 '{_escape(new_job_title.strip())}',
                 '{_escape(new_salutation.strip())}',
                 '{_escape(new_gender)}',
                 '{_escape(sender_login)}')
            """
        )
        st.success("Получатель добавлен.")
        st.session_state["email_dialog_page_id"] = page_id
        st.rerun()
    elif submitted:
        to_list = [r.email for r in to_dir if getattr(r, "email", None)]
        cc_list = [r.email for r in cc_dir if getattr(r, "email", None)]
        bcc_list = [r.email for r in bcc_dir if getattr(r, "email", None)]

        recipients = _merge_email_lists(to_list)
        cc = _merge_email_lists(cc_list)
        bcc = _merge_email_lists(bcc_list)

        if not recipients:
            st.error("Укажите получателей.")
            return

        temp_paths: list[str] = []
        try:
            base_dir = os.path.dirname(__file__)
            temp_root = os.path.join(base_dir, config.temp_attachments_dir)
            os.makedirs(temp_root, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="notes_email_", dir=temp_root) as tmpdir:
                for row in selected_files:
                    payload = get_attachment_file(int(row.id))
                    if not payload:
                        st.warning(
                            f"Не удалось подгрузить вложение: {row.file_name}"
                        )
                        continue
                    data, file_name, _mime = payload
                    safe_name = _sanitize_attachment_filename(file_name)
                    file_path = os.path.join(tmpdir, safe_name)
                    with open(file_path, "wb") as handle:
                        handle.write(data)
                    temp_paths.append(file_path)

                body_text = body or ""
                if not body_text.strip():
                    body_text = greeting

                if not body_text.lstrip().startswith(greeting):
                    body_text = f"{greeting}\n\n{body_text.lstrip()}"

                links_block = _build_links_block()
                page_info = f"{page_path}".strip()
                signature = f"С уважением!\n{sender_full_name}\n{sender_job_title}".rstrip()

                if links_block and "Ссылки:" not in body_text:
                    if "С уважением!" in body_text:
                        body_text = body_text.replace("С уважением!", f"{links_block}\n\nС уважением!", 1)
                    else:
                        body_text = f"{body_text.rstrip()}\n\n{links_block}"

                if page_path and page_info not in body_text:
                    if "С уважением!" in body_text:
                        body_text = body_text.replace("С уважением!", f"{page_info}\n\nС уважением!", 1)
                    else:
                        body_text = f"{body_text.rstrip()}\n\n{page_info}"

                if "С уважением!" not in body_text:
                    body_text = f"{body_text.rstrip()}\n\n{signature}"

                send_mail(
                    subject=subject or "",
                    recipients=recipients,
                    cc=cc,
                    bcc=bcc,
                    body=body_text,
                    important=bool(important),
                    files=temp_paths,
                )

            st.success("Письмо отправлено.")
            st.session_state["email_dialog_page_id"] = None
            st.rerun()
        except Exception as exc:
            st.error(f"Ошибка отправки: {exc}")


def save_file_attachment(page_id: int, uploaded_file, user_login: str) -> None:
    if uploaded_file is None:
        return
    content = uploaded_file.getvalue()
    if not content:
        raise ValueError("Файл пустой или не удалось прочитать.")
    mime_type = uploaded_file.type or "application/octet-stream"
    encoded = base64.b64encode(content).decode("ascii")
    run_execute(
        f"""
        INSERT INTO {ATTACHMENTS_TABLE}
            (page_id, attachment_type, file_name, mime_type, file_size, file_data, created_by)
        VALUES
            ({int(page_id)},
             'file',
             '{_escape(uploaded_file.name)}',
             '{_escape(mime_type)}',
             {len(content)},
             decode('{encoded}', 'base64'),
             '{_escape(user_login)}')
        """
    )


def save_link_attachment(page_id: int, url: str, title: str, user_login: str) -> None:
    cleaned_url = (url or "").strip()
    if not cleaned_url:
        raise ValueError("URL не указан.")
    low = cleaned_url.lower()
    if low.startswith(("javascript:", "vbscript:", "data:text/html", "data:application/xhtml+xml")):
        raise ValueError("Запрещённый URL (возможная XSS-атака).")

    name = (title or "").strip() or cleaned_url
    run_execute(
        f"""
        INSERT INTO {ATTACHMENTS_TABLE}
            (page_id, attachment_type, file_name, url, created_by)
        VALUES
            ({int(page_id)},
             'link',
             '{_escape(name)}',
             '{_escape(cleaned_url)}',
             '{_escape(user_login)}')
        """
    )


def html_to_body(text: str, fallback_title: str):
    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else fallback_title
    body = str(soup.body or soup)
    return title, body


def _split_onenote_html_into_pages(soup: BeautifulSoup, filename: str):
    base_title = filename.rsplit(".", 1)[0]
    page_divs = soup.find_all("div", style=lambda v: v and "border-width:100%" in v)

    pages: list[tuple[str, str]] = []

    if not page_divs:
        title, body_html = html_to_body(str(soup), base_title)
        pages.append((title, body_html))
        return pages

    for idx, div in enumerate(page_divs, start=1):
        title_p = div.find("p")
        title_text = title_p.get_text(strip=True) if title_p else ""
        if not title_text:
            title_text = f"{base_title} {idx}"
        body_html = str(div)
        pages.append((title_text, body_html))

    return pages


def parse_mht_to_pages(data: bytes, filename: str):
    """
    ✅ Per-page sanitize: санитизация делается на уровне каждой страницы
    """
    msg = email.message_from_bytes(data)
    html_part = None
    resources: list[tuple[str, bytes, str | None, str | None]] = []

    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/html" and html_part is None:
            charset = part.get_content_charset() or "utf-8"
            html_part = part.get_payload(decode=True).decode(charset, errors="replace")
        else:
            cid = part.get("Content-ID")
            loc = part.get("Content-Location")
            payload = part.get_payload(decode=True) or b""
            if cid or loc:
                resources.append((ctype, payload, cid, loc))

    if not html_part:
        raise ValueError("В .mht не найден HTML-контент")

    def norm(val: str) -> str:
        val = urllib.parse.unquote(val or "").strip()
        val = val.replace("\\", "/")
        if val.lower().startswith("cid:"):
            val = "cid:" + val[4:]
        return val

    src_map: dict[str, str] = {}
    for ctype, content, cid, loc in resources:
        data_url = f"data:{ctype};base64,{base64.b64encode(content).decode()}"
        if cid:
            cid_clean = cid.strip("<>")
            for key in (f"cid:{cid_clean}", f"CID:{cid_clean}", cid_clean, norm(cid_clean)):
                src_map[key] = data_url
        if loc:
            loc_clean = loc.strip().strip("<>")
            normalized = norm(loc_clean)
            for key in (loc_clean, f"cid:{loc_clean}", f"CID:{loc_clean}", normalized):
                src_map[key] = data_url
            basename = os.path.basename(normalized)
            if basename:
                for key in (basename, f"cid:{basename}", f"CID:{basename}", norm(basename)):
                    src_map[key] = data_url

    soup = BeautifulSoup(html_part, "html.parser")

    for tag in soup.find_all(src=True):
        src_val = tag.get("src", "")
        lookup = norm(src_val)
        if lookup in src_map:
            tag["src"] = src_map[lookup]
        else:
            basename = os.path.basename(lookup)
            if basename in src_map:
                tag["src"] = src_map[basename]

    pages = _split_onenote_html_into_pages(soup, filename)

    safe_pages: list[tuple[str, str]] = []
    base_title = filename.rsplit(".", 1)[0]
    for title, body_html in pages:
        safe_title = (title or "").strip() or base_title
        safe_body = sanitize_html_xss(body_html or "")
        safe_pages.append((safe_title, safe_body))

    return safe_pages


def parse_mht_to_html(data: bytes, filename: str):
    pages = parse_mht_to_pages(data, filename)
    return pages[0]


def _safe_filename(title: str, ext: str) -> str:
    base = (title or "page").strip()
    base = re.sub(r"[^\w\-. ]+", "_", base)
    if not base:
        base = "page"
    return f"{base}.{ext}"


def export_html_to_docx_bytes(html: str, title: str) -> io.BytesIO:
    doc = Document()

    normal_style = doc.styles["Normal"]
    nf = normal_style.paragraph_format
    nf.space_before = Pt(0)
    nf.space_after = Pt(0)
    nf.line_spacing_rule = WD_LINE_SPACING.SINGLE

    if title:
        h = doc.add_heading(title, level=1)
        h.paragraph_format.space_before = Pt(0)
        h.paragraph_format.space_after = Pt(6)

    soup = BeautifulSoup(html or "", "html.parser")
    body = soup.body or soup

    for br in body.find_all("br"):
        br.replace_with(" ")

    for text_node in body.find_all(string=True):
        cleaned = re.sub(r"[\r\n]+", " ", str(text_node))
        if cleaned != text_node:
            text_node.replace_with(cleaned)

    def _update_flags_from_style(tag: Tag, bold: bool, italic: bool, underline: bool):
        style = (tag.get("style") or "").lower()
        if "font-weight" in style and ("bold" in style or "700" in style or "600" in style):
            bold = True
        if "font-style" in style and "italic" in style:
            italic = True
        if "text-decoration" in style and "underline" in style:
            underline = True
        return bold, italic, underline

    def add_image_from_tag(tag: Tag):
        src = (tag.get("src") or "").strip()
        if not src:
            return

        img_bytes: bytes | None = None

        if src.startswith("data:image"):
            try:
                _, b64data = src.split(",", 1)
                img_bytes = base64.b64decode(b64data)
            except Exception:
                return
        elif src.startswith("http://") or src.startswith("https://"):
            try:
                resp = requests.get(src, timeout=5)
                resp.raise_for_status()
                img_bytes = resp.content
            except Exception:
                return
        else:
            return

        if not img_bytes:
            return

        img_stream = io.BytesIO(img_bytes)
        img_stream.seek(0)
        try:
            doc.add_picture(img_stream, width=Inches(5))
        except Exception as e:
            logger.warning("Не удалось вставить картинку в DOCX: %s", e)

    def add_inline(node, paragraph, bold=False, italic=False, underline=False):
        if isinstance(node, NavigableString):
            text = re.sub(r"[\r\n]+", " ", str(node))
            if not text:
                return
            run = paragraph.add_run(text)
            run.bold = bold
            run.italic = italic
            run.underline = underline
            return

        if not isinstance(node, Tag):
            return

        name = node.name.lower()

        if name == "img":
            add_image_from_tag(node)
            return

        tb, ti, tu = bold, italic, underline
        if name in ("b", "strong"):
            tb = True
        if name in ("i", "em"):
            ti = True
        if name == "u":
            tu = True

        tb, ti, tu = _update_flags_from_style(node, tb, ti, tu)

        for child in node.children:
            add_inline(child, paragraph, tb, ti, tu)

    def make_paragraph(tag: Tag, style: str | None = None):
        text_raw = tag.get_text("", strip=False)
        has_any_chars = bool(text_raw and text_raw.strip("\r\n\t "))
        has_visible_text = bool(tag.get_text("", strip=True))
        has_img = bool(tag.find("img"))
        has_br = bool(tag.find("br"))

        if not has_any_chars and not has_img and not has_br:
            return

        p = doc.add_paragraph(style=style)
        pf = p.paragraph_format
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        pf.line_spacing_rule = WD_LINE_SPACING.SINGLE

        if not has_visible_text and not has_img:
            return

        for child in tag.children:
            add_inline(child, p)

    def add_table_from_tag(table_tag: Tag):
        rows = table_tag.find_all("tr")
        if not rows:
            return
        max_cols = 0
        row_cells = []
        for tr in rows:
            cells = tr.find_all(["td", "th"], recursive=False)
            row_cells.append(cells)
            max_cols = max(max_cols, len(cells))

        t = doc.add_table(rows=len(rows), cols=max_cols)
        t.style = "Table Grid"
        for i, cells in enumerate(row_cells):
            for j, cell_tag in enumerate(cells):
                cell = t.cell(i, j)
                cell.text = cell_tag.get_text(" ", strip=True)

    def handle_block(node):
        if isinstance(node, NavigableString):
            if str(node).strip():
                p = doc.add_paragraph(str(node).strip())
                pf = p.paragraph_format
                pf.space_before = Pt(0)
                pf.space_after = Pt(0)
                pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
            return

        if not isinstance(node, Tag):
            return

        name = node.name.lower()

        if name in ("h1", "h2", "h3"):
            level = int(name[1])
            make_paragraph(node, style=f"Heading {level}")
        elif name == "p":
            make_paragraph(node)
        elif name in ("ul", "ol"):
            bullet = name == "ul"
            for li in node.find_all("li", recursive=False):
                style = "List Bullet" if bullet else "List Number"
                make_paragraph(li, style=style)
        elif name == "img":
            add_image_from_tag(node)
        elif name == "table":
            add_table_from_tag(node)
        else:
            for child in node.children:
                handle_block(child)

    for child in body.children:
        handle_block(child)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


# =========================
# FIX: сохраняем отступы/пробелы для .sql экспорта + корректный текст для буфера
# =========================
def _html_to_plain_preserving_layout(html: str, indent_spaces: int = 4) -> str:
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    body = soup.body or soup

    block_tags = {"p", "div", "li", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
    out_lines: list[str] = []

    def _norm_newlines(s: str) -> str:
        return s.replace("\r\n", "\n").replace("\r", "\n")

    def _indent_level(tag: Tag) -> int:
        cur: Tag | None = tag
        while isinstance(cur, Tag):
            classes = cur.get("class") or []
            for c in classes:
                m = re.match(r"ql-indent-(\d+)", str(c))
                if m:
                    return int(m.group(1))
            if cur is body:
                break
            cur = cur.parent if isinstance(cur.parent, Tag) else None
        return 0

    def _text_of(tag: Tag) -> str:
        parts: list[str] = []
        for elem in tag.descendants:
            if isinstance(elem, NavigableString):
                parts.append(str(elem))
        raw = "".join(parts)
        raw = _norm_newlines(raw).replace("\xa0", " ")
        return "\n".join(line.rstrip() for line in raw.split("\n"))

    def _has_desc_block(t: Tag) -> bool:
        # если внутри есть вложенные блоки, текст контейнера будет дублировать их текст
        for child in t.find_all(list(block_tags), recursive=True):
            if child is not t:
                return True
        return False

    blocks_all = body.find_all(list(block_tags))
    # берем "листовые" блоки: те, у которых нет вложенных блоков
    blocks = [t for t in blocks_all if not _has_desc_block(t)]
    if blocks:
        for el in blocks:
            txt = _text_of(el)

            if txt.replace("\n", "").strip() == "":
                out_lines.append("")
                continue

            lvl = _indent_level(el)
            prefix = (" " * (lvl * indent_spaces)) if lvl > 0 else ""

            for line in txt.split("\n"):
                if line == "":
                    out_lines.append("")
                else:
                    out_lines.append(prefix + line)
    else:
        txt = _text_of(body)
        out_lines.extend(txt.split("\n"))

    result = "\n".join(out_lines)
    result = _norm_newlines(result)
    result = re.sub(r"\n{3,}", "\n\n", result).rstrip()
    return result


def _html_to_plain_for_email(html: str, indent_spaces: int = 4) -> str:
    """
    В отличие от `_html_to_plain_preserving_layout`, нормализует "служебные"
    переносы/пробелы из HTML (OneNote/Word часто вставляет \\n внутри текста),
    чтобы в письме не получалось "по слову на строку".
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    for bad in soup.find_all(["script", "style"]):
        bad.decompose()

    br_token = "\u0000"
    for br in soup.find_all("br"):
        br.replace_with(br_token)

    body = soup.body or soup
    block_tags = {"p", "li", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}

    def _norm_newlines(s: str) -> str:
        return s.replace("\r\n", "\n").replace("\r", "\n")

    def _indent_level(tag: Tag) -> int:
        cur: Tag | None = tag
        while isinstance(cur, Tag):
            classes = cur.get("class") or []
            for c in classes:
                m = re.match(r"ql-indent-(\d+)", str(c))
                if m:
                    return int(m.group(1))
            if cur is body:
                break
            cur = cur.parent if isinstance(cur.parent, Tag) else None
        return 0

    def _has_desc_block(t: Tag) -> bool:
        for child in t.find_all(list(block_tags), recursive=True):
            if child is not t:
                return True
        return False

    def _text_of(tag: Tag) -> str:
        if tag.name and tag.name.lower() == "pre":
            raw = tag.get_text()
            raw = raw.replace(br_token, "\n").replace("\xa0", " ")
            raw = _norm_newlines(raw)
            return "\n".join(line.rstrip() for line in raw.split("\n")).rstrip()

        raw = tag.get_text(separator=" ", strip=False)
        raw = raw.replace(br_token, "\n").replace("\xa0", " ")
        raw = _norm_newlines(raw)

        # Любые переносы строк/табуляции из исходного HTML считаем пробелами
        # (реальные переводы строк должны приходить только из <br> или <pre>).
        raw = raw.replace("\n", " ")
        raw = re.sub(r"[ \t\f\v]+", " ", raw).strip()
        raw = re.sub(r" *\n *", "\n", raw)
        return raw

    blocks_all = body.find_all(list(block_tags))
    blocks = [t for t in blocks_all if not _has_desc_block(t)]

    out_lines: list[str] = []
    for el in blocks:
        txt = _text_of(el)
        if not txt:
            continue

        lvl = _indent_level(el)
        prefix = (" " * (lvl * indent_spaces)) if lvl > 0 else ""

        if el.name and el.name.lower() == "li":
            for line in txt.split("\n"):
                line = line.strip()
                if not line:
                    continue
                out_lines.append(prefix + "- " + line)
            continue

        for line in txt.split("\n"):
            if line.strip() == "":
                out_lines.append("")
            else:
                out_lines.append(prefix + line.rstrip())

    result = "\n".join(out_lines)
    result = _norm_newlines(result)
    result = re.sub(r"\n{3,}", "\n\n", result).rstrip()
    return result


def _sql_text_from_html(html: str, title: str) -> str:
    plain = _html_to_plain_preserving_layout(html or "")
    header = f"-- {title or ''}".rstrip()
    return header + ("\n\n" + plain if plain else "\n")


def export_html_to_sql_bytes(html: str, title: str, encoding: str = "utf-8") -> bytes:
    full_text = _sql_text_from_html(html or "", title or "")
    return full_text.encode(encoding, errors="replace")


def render_copy_sql_button(sql_text: str, btn_key: str) -> None:
    payload = json.dumps(sql_text or "")
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", btn_key)

    components.html(
        f"""
        <div style="display:flex; justify-content:flex-end; align-items:center; width:100%; margin:0; padding:0;">
          <button id="copy_btn_{safe_id}"
                  style="
                    cursor:pointer;
                    padding:0.35rem 0.75rem;
                    border:1px solid rgba(49,51,63,.2);
                    border-radius:0.5rem;
                    background: white;
                    font-size:14px;
                    line-height:1.2;
                  ">
            📋 Копировать
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
                ok("Скопировано");
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
                ok("SQL скопирован в буфер обмена.");
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
        height=42,
        scrolling=False,
    )


def _close_edit_dialog_state():
    # закрыть редактор и убрать любые "авто-открывалки"
    st.session_state["edit_dialog_page_id"] = None
    st.session_state.pop("force_edit_page_id", None)
    st.session_state.pop("force_edit_page_id_once", None)


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



HELP_SEARCH_MD = r"""
## 🔎 Расширенный поиск

Поиск работает по **названию** и **содержимому** страницы, а если начать запрос с `#` то поиск работает по **тегам**.

### Основные правила
- **Пробел = AND** (пробел между словами в поиске - оба слова должны присутствовать в тексте/заголовке/теге)
- **`|` = OR** (символ | между словами в поиске - одно из слов должно присутствовать)
- **Скобки** `(...)` группируют выражения, т.е. можно сочитать условия AND, OR, NOT ...
- **`+`** — обязательное слово (аналогично просто слову, но явнее)
- **`-`** — исключить слово/фразу (NOT)
- **Кавычки** `"..."` — точная фраза
- **`*`** — любой набор символов (wildcard)

### Приоритеты (важно!)
`AND` сильнее чем `OR`, как в Google.

Пример:
`a | b c`  трактуется как `a OR (b AND c)`

### Примеры
- `(ноутбук | компьютер) + dell - asus`  
  → (ноутбук ИЛИ компьютер) И обязательно dell И НЕТ asus

- `"искусственный интеллект" | "машинное обучение"`  
  → одна из точных фраз

- `лучший*в*мире + ресторан - дорогой`  
  → wildcard + AND + NOT

### Поиск по тегам
- Чтобы искать **только по тегам**, начни запрос с `#`  
  Пример: `#1969 | 258`
"""


@st.dialog("Справка: Расширенный поиск", width="large")
def help_search_dialog() -> None:
    st.markdown(HELP_SEARCH_MD)


def main():
    st.set_page_config(
        layout="wide",
        page_title="ДФИП_Notes",
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

    ensure_db_credentials()
    owned_notebooks_df = pd.DataFrame()

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
    selected_department_id = "00"
    dept_records = list(departments_df.itertuples(index=False)) if not departments_df.empty else []
    is_user_department_selected = False
    forced_department_id = st.session_state.pop("force_department_id", None)

    if dept_records:
        if forced_department_id:
            forced_row = next((r for r in dept_records if str(r.department_id) == str(forced_department_id)), None)
            if forced_row is not None:
                st.session_state["department_selector"] = forced_row

        if "department_selector" not in st.session_state and not forced_department_id:
            has_owned_books = not owned_notebooks_df.empty
            default_dep_id = "99" if has_owned_books else "00"
            default_row = next((r for r in dept_records if str(r.department_id) == default_dep_id), None)
            if default_row is None:
                default_row = dept_records[0]
            st.session_state["department_selector"] = default_row

        selected_department = st.sidebar.selectbox(
            "Поиск по подразделению",
            dept_records,
            format_func=lambda row: row.name_department,
            key="department_selector",
        )
        selected_department_id = str(selected_department.department_id)
        user_dep_id = user_dept_map.get(selected_login)
        is_user_department_selected = bool(user_dep_id) and (str(selected_department_id) == str(user_dep_id))
    else:
        selected_department_id = "00"

    st.session_state["current_department_id"] = selected_department_id

    # --- поиск страниц ---
    def _clear_page_search():
        st.session_state["page_search"] = ""



    # уникальный префикс (можно оставить константой)
    SEARCH_UI_PREFIX = "pages_search_ui"

    search_col, clear_col = st.sidebar.columns([14, 2])

    with search_col:
        search_raw = st.text_input(
            label="",
            key="page_search",
            placeholder="Поиск страниц по #tag или тексту",
            label_visibility="collapsed",
        )

    with clear_col:
        st.button(
            "×",
            key=f"{SEARCH_UI_PREFIX}_clear_btn",
            help="Очистить поиск",
            on_click=_clear_page_search,
            use_container_width=True,
        )





    search_raw = (search_raw or "").strip()
    search_tags_only = search_raw.startswith("#")
    search_text = search_raw[1:].strip() if search_tags_only else search_raw

    current_user_can_create_notebook = selected_login in registered_users

    # --- список книг ---
    notebooks_df = get_notebooks(selected_login, user_dep_id)
    filtered_notebooks_df = notebooks_df.copy()
    # ✅ Все книги, доступные пользователю по правилам get_notebooks (owners/00/99/подразделения)
    visible_notebook_ids = notebooks_df["id"].astype(int).tolist() if not notebooks_df.empty else []
   
    current_department_id: str = st.session_state.get("current_department_id", "00")



    if current_department_id != "00" and not filtered_notebooks_df.empty:
        dep_col = filtered_notebooks_df["department_id"].fillna("00").astype(str)

        if str(current_department_id) == "99":
            owned_ids = set(owned_notebooks_df["id"].astype(int).tolist()) if not owned_notebooks_df.empty else set()
            filtered_notebooks_df = filtered_notebooks_df[filtered_notebooks_df["id"].astype(int).isin(owned_ids)]
        else:
            prefix = str(current_department_id).strip()
            prefix_like = prefix + "."
            mask = (dep_col == "00") | (dep_col == prefix) | dep_col.str.startswith(prefix_like)
            filtered_notebooks_df = filtered_notebooks_df[mask]

    selected_notebook_id: int | None = None
    selected_section_id: int | None = None
    selected_notebook_row: pd.Series | None = None
    can_edit_notebook: bool = False

    notebook_records = list(filtered_notebooks_df.itertuples(index=False))
    top_col1, top_col2, top_col3 = st.columns([5, 1, 5])

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
                st.success("Книга создана")
                st.rerun()

    # --- верхняя панель: книга + кнопки (ВЕРНУЛИ 🔐) ---
    with top_col1:
        select_col, plus_col, info_col = st.columns([14, 2, 2])

        with select_col:
            if notebook_records:
                forced_nb_id = st.session_state.pop("force_notebook_id", None)
                if forced_nb_id is not None:
                    target_row = next((r for r in notebook_records if int(r.id) == int(forced_nb_id)), None)
                    if target_row is not None:
                        st.session_state["notebook_selector"] = target_row

                def _nb_label(row):
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
                    options=notebook_records,
                    format_func=_nb_label,
                    key="notebook_selector",
                    label_visibility="collapsed",
                )

                selected_notebook_id = int(selected_notebook.id)
                selected_notebook_row = filtered_notebooks_df[filtered_notebooks_df["id"] == selected_notebook_id].iloc[0]
                can_edit_notebook = bool(is_notebook_owner(selected_notebook_id, selected_login))
            else:
                st.info("Нет доступных книг")

        # ✅ диалог "Права доступа на книгу" (вернули)
        if selected_notebook_id is not None and can_edit_notebook:

            @st.dialog("Права доступа на книгу", width="small")
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

                with st.form(f"access_form_{selected_notebook_id}"):
                    dept_records_all = list(departments_df.itertuples(index=False)) if not departments_df.empty else []
                    current_nb_dept = str(dept_id or "00")

                    current_dept_row = next(
                        (r for r in dept_records_all if str(r.department_id) == current_nb_dept),
                        None,
                    )
                    if current_dept_row is None and dept_records_all:
                        current_dept_row = dept_records_all[0]

                    selected_dept_row = st.selectbox(
                        "Область видимости книги (подразделение)",
                        options=dept_records_all,
                        index=dept_records_all.index(current_dept_row) if current_dept_row in dept_records_all else 0,
                        format_func=lambda r: f"{r.name_department}",
                        key=f"nb_department_{selected_notebook_id}",
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

                    submitted = st.form_submit_button("Сохранить доступы")
                    if submitted:
                        set_notebook_department(selected_notebook_id, new_department_id)

                        if new_owner_login:
                            add_notebook_owner(selected_notebook_id, new_owner_login)

                        if remove_owner_login and remove_owner_login != selected_login:
                            remove_notebook_owner(selected_notebook_id, remove_owner_login)

                        st.success("Доступы обновлены")
                        st.rerun()

        if current_user_can_create_notebook:
            with plus_col:
                st.markdown("###### ")
                st.markdown("###### ")
                if st.button("➕", key="open_new_notebook_dialog", help="Создать новую книгу", use_container_width=True):
                    new_notebook_dialog()

        # ✅ КНОПКА 🔐 (вернули)
        if selected_notebook_id is not None and can_edit_notebook:
            with info_col:
                st.markdown("###### ")
                st.markdown("###### ")
                if st.button("🔐", key="open_notebook_access_dialog", help="Права доступа на книгу", use_container_width=True):
                    notebook_access_dialog()

    # --- диалог "Новый раздел" (вернули) ---
    if selected_notebook_id is not None and can_edit_notebook:

        @st.dialog("Новый раздел", width="small")
        def new_section_dialog():
            new_section_name = st.text_input("Название раздела", key="new_section_name_modal")
            create_clicked = st.button("Создать раздел", key="create_section_btn_modal")
            if create_clicked:
                create_section(selected_notebook_id, new_section_name, selected_login)
                st.success("Раздел создан")
                st.rerun()

        @st.dialog("Переименовать или удалить раздел", width="small")
        def section_manage_dialog(section_row):
            section_id_local = int(section_row.id)
            st.caption(f"Текущий раздел: **{section_row.name}**")

            new_name = st.text_input(
                "Новое название раздела",
                value=section_row.name,
                key=f"rename_section_name_{section_id_local}",
            )
            pages_cnt = get_section_pages_count(section_id_local)

            col_rename, col_delete = st.columns(2)
            with col_rename:
                if st.button("Сохранить название", key=f"btn_rename_section_{section_id_local}", use_container_width=True):
                    rename_section(section_id_local, new_name)
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
                        st.success("Раздел удалён")
                        st.rerun()

    # --- список разделов + кнопки ➕/✎ (вернули) ---
    sections_df = pd.DataFrame()
    section_records: list = []
    if selected_notebook_id is not None:
        sections_df = get_sections(selected_notebook_id)
        section_records = list(sections_df.itertuples(index=False))

    with top_col3:
        select_col2, plus_col2, manage_col2 = st.columns([14, 2, 2])

        selected_section = None
        with select_col2:
            st.markdown("###### ")
            st.markdown("###### Раздел")
            if section_records:
                selected_section = st.selectbox(
                    label="",
                    options=section_records,
                    format_func=lambda row: row.name,
                    key="section_selector",
                    label_visibility="collapsed",
                )
                selected_section_id = int(selected_section.id)
            else:
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
        # ✅ без поиска показываем страницы выбранного раздела выбранной книги
        search_notebook_id = selected_notebook_id
        search_section_id = selected_section_id
        search_allowed_ids = dept_notebook_ids

    pages_df = load_pages_df(
        search_notebook_id,
        search_section_id,
        search_allowed_ids,
        search_text or None,
        search_tags_only,
    )


    # ✅ защита: load_pages_df / run_fetch_df должны возвращать DataFrame
    if pages_df is None:
        pages_df = pd.DataFrame()


    if pages_df.empty:
        pages_df = pd.DataFrame(
            columns=[
                "id",
                "title",
                "status",
                "tag",
                "body_html",
                "created_at",
                "updated_at",
                "created_by",
                "section_id",
                "section_name",
                "notebook_id",
                "notebook_name",
                "notebook_department_id",
            ]
        )

    # ---------- Кнопки "Новая страница" и "Импорт страниц" ----------
    new_page_clicked = False

    if can_edit_notebook:

        @st.dialog("Импорт .mht страниц", width="large")
        def import_mht_dialog():
            if not selected_section_id:
                st.info("Выберите раздел для импорта.")
                return

            uploaded = st.file_uploader(
                "Загрузите .mht файлы",
                type=["mht"],
                accept_multiple_files=True,
                key="mht_files_modal",
            )

            if uploaded and st.button("Импортировать .mht", key="import_mht_btn_modal"):
                imported = 0
                errors: list[str] = []
                for file in uploaded:
                    try:
                        pages = parse_mht_to_pages(file.getvalue(), file.name)  # ✅ sanitized per page
                        for title, body_html in pages:
                            insert_page_with_content(selected_section_id, title, body_html, selected_login)
                            imported += 1
                    except Exception as exc:
                        errors.append(f"{file.name}: {exc}")

                if imported:
                    st.success(f"Импортировано {imported} страниц")
                    st.rerun()
                if errors:
                    st.warning(";\n".join(errors))

        btn_col_new, btn_col_import = st.sidebar.columns([1, 1])
        with btn_col_new:
            new_page_clicked = st.button("Новая страница", use_container_width=True)
        with btn_col_import:
            import_clicked = st.button("Импорт страниц", use_container_width=True)

        if import_clicked:
            import_mht_dialog()

    if new_page_clicked:
        if not selected_section_id:
            st.sidebar.warning("Сначала создайте и выберите раздел.")
        else:
            new_page_id = create_page(section_id=selected_section_id, user_login=selected_login, title=None)
            st.session_state["current_page_id"] = new_page_id
            st.session_state["force_page_id"] = new_page_id
            # ✅ открыть редактор ровно один раз на следующем ререндере
            st.session_state["open_editor_once_for_page"] = new_page_id
            st.rerun()

    # ---------- Список страниц ----------
    # ✅ добавили статус в отображаемый df
    df_display = pages_df[["id", "title", "status"]].copy().reset_index(drop=True)

    # нормализация на всякий случай (если NULL)
    if "status" in df_display.columns:
        df_display["status"] = df_display["status"].fillna("#FFFFFF").astype(str)

    # снимок для сравнения изменений (чтобы поймать, какие status реально поменялись)
    if "pages_grid_prev_df" not in st.session_state:
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
        minWidth=160,
        resizable=True,
    )
    gb.configure_column(
        "status",
        header_name="",
        width=44,
        minWidth=44,
        maxWidth=44,
        sortable=False,
        filter=False,
        editable=False,
        resizable=False,
        valueFormatter=STATUS_VALUE_FORMATTER,
        cellStyle=STATUS_CELL_STYLE,
        suppressSizeToFit=True,
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
        # ✅ dblclick по кружку только владельцам
        onCellDoubleClicked=(STATUS_DBLCLICK_HANDLER if can_edit_notebook else None),
    )

    list_container = st.sidebar.container()
    with list_container:
        grid_response = AgGrid(
            df_display,
            gridOptions=gb.build(),
            enable_enterprise_modules=False,
            # ✅ важно: чтобы изменения из JS (setDataValue) возвращались в python
            update_mode=GridUpdateMode.MODEL_CHANGED,
            data_return_mode=DataReturnMode.AS_INPUT,
            height=750,
            fit_columns_on_grid_load=True,
            allow_unsafe_jscode=True,
        )


    # --- безопасно достаём data из grid_response (может быть list или DataFrame) ---
    raw_data = grid_response.get("data", [])
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
                    new_status = str(row.status_new or "#FFFFFF").strip()

                    # защита: разрешаем только заданные цвета, иначе ставим белый
                    new_status = new_status.upper()
                    allowed = {"#FFFFFF", "#FFF59D", "#90CAF9", "#A5D6A7", "#EF9A9A", "#CE93D8"}
                    if new_status not in allowed:
                        new_status = "#FFFFFF"


                    run_execute(
                        f"""
                        UPDATE {PAGES_TABLE}
                        SET status = '{_escape(new_status)}'
                        WHERE id = {int(page_id_upd)}
                        """
                    )

                # обновляем "предыдущее" состояние, чтобы не обновлять повторно
                st.session_state["pages_grid_prev_df"] = df_after.copy()

                # перечитать/перерисовать, чтобы pages_df внизу был синхронен
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

    selected_rows = grid_response.get("selected_rows", [])
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

    # ---------- Просмотр / редактирование выбранной страницы ----------
    if page_id is not None:
        current_page = pages_df[pages_df["id"] == page_id].iloc[0]
        current_title = current_page.get("title", "")
        current_html = current_page.get("body_html") or ""
        current_tag = current_page.get("tag") or ""

        dept_id_for_page = current_page.get("notebook_department_id")
        dept_name_for_page = department_map.get(dept_id_for_page, "") if dept_id_for_page else ""
        dept_prefix = f"[{dept_name_for_page}] " if dept_name_for_page else ""

        safe_title = current_title or f"Страница_{page_id}"
        page_path = f"{current_page['notebook_name']} > {current_page['section_name']} > {current_page['title']}"
        info_left, info_right = st.columns([12, 3])
        with info_left:
            st.caption(
                f"{dept_prefix}  {current_page['notebook_name']}  >  "
                f"{current_page['section_name']}  >  {current_page['title']}"
            )
            if current_tag:
                st.caption(f"Tag: {current_tag}")

        with info_right:
            sql_text = _sql_text_from_html(current_html or "", safe_title)
            render_copy_sql_button(sql_text, btn_key=f"copy_sql_utf8_{page_id}")

        safe_preview_html = sanitize_html_xss(current_html or "")
        preview_html = f"""
        <style>
        .page-preview-wrapper {{
            border: 1px solid #d0d4da;
            border-radius: 6px;
            padding: 16px 18px;
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
        </style>

        <div class="page-preview-wrapper">
            <div class="preview-body">
                {safe_preview_html or "<p><em>Нет содержимого</em></p>"}
            </div>
        </div>
        """
        components.html(preview_html, height=580, scrolling=True)

        # ---------------- Диалог редактирования (вызываем ТОЛЬКО по явному действию) ----------------
        @st.dialog("Редактирование страницы", width="large")
        def edit_page_dialog(page_id_local: int, title: str, html_body: str, tag: str):
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


            col_l, col_r = st.columns([2, 2])
            with col_l:
                new_title = st.text_input("Название страницы", value=title, key=f"dlg_title_{page_id_local}")

            with col_r:
                tag_col, help_col = st.columns([13,1.5])

                with tag_col:
                    new_tag = st.text_input(
                        "Теги",
                        value=tag or "",
                        key=f"dlg_tag_{page_id_local}",
                        placeholder="Введите тег(и) через запятую без символа #",
                    )

                with help_col:
                    st.markdown("###")  # выравнивание кнопки по высоте
                    with st.popover("❓", use_container_width=True):
                        st.markdown(
                            """
            ### 📝 Справка по редактору (Quill)

            **Форматирование**
            - **B** — жирный, *I* — курсив, <u>U</u> — подчёркивание, ~~S~~ — зачёркивание  
            - x₂ — нижний индекс, x² — верхний индекс  

            **Цвет**
            - A — цвет текста  
            - A (с фоном) — цвет подсветки (фон)

            **Списки и выравнивание**
            - Маркированный / нумерованный список  
            - Выравнивание: слева / центр / справа / по ширине

            **Заголовки**
            - H1 / H2 — заголовки  
            - Normal — обычный текст

            **Дополнительно**
            - fx — формулы  
            - “ ” — цитата  
            - </> — код / блок кода  
            - Tx — очистить форматирование

            **Вставка**
            - 🔗 — ссылка  
            - 🖼 — изображение  

            ℹ️ Вставка из Word/Excel может приносить таблицы. HTML дополнительно очищается от опасных тегов.
            """
                        )





            editable_html = html_body or ""

            quill_value = st_quill(
                value=editable_html,
                html=True,
                placeholder="Введите текст...",
                key=f"dlg_quill_{page_id_local}",
            )

            quill_html = editable_html if quill_value is None else (quill_value or "")

            c1, c2 = st.columns([1, 1])
            with c1:
                if st.button("Сохранить", key=f"dlg_save_{page_id_local}", use_container_width=True):
                    update_page(page_id_local, new_title, quill_html, new_tag)
                    st.success("Страница обновлена")

                    # ✅ закрыть редактор и убрать любые авто-открывалки
                    _close_edit_dialog_state()

                    st.session_state["current_page_id"] = page_id_local
                    st.session_state["force_page_id"] = page_id_local
                    st.rerun()

            with c2:
                if st.button("Отмена", key=f"dlg_cancel_{page_id_local}", use_container_width=True):
                    _close_edit_dialog_state()
                    st.rerun()

        # ✅ one-shot авто-открытие редактора (после создания/копирования), НЕ будет открываться после открытия вложений
        st.session_state.setdefault("open_editor_once_for_page", None)
        open_once = st.session_state.pop("open_editor_once_for_page", None)
        if can_edit_notebook and open_once == page_id:
            edit_page_dialog(page_id, current_title, current_html, current_tag)

        # --- кнопка редактирования + экспорт + вложения + перемещение ---
        col1, col2, col3, col4 = st.columns([1.8, 1.2, 3, 3])

        # ---------------- Редактировать ----------------
        with col1:
            if can_edit_notebook:
                if st.button("Редактировать страницу", key=f"open_edit_dialog_{page_id}", use_container_width=True):
                    edit_page_dialog(page_id, current_title, current_html, current_tag)
            else:
                st.caption("Просмотр (редактирование недоступно)")

        # ---------------- Экспорт ----------------
        with col2:
            exp_export_nonce_key = f"exp_export_nonce_{page_id}"
            st.session_state.setdefault(exp_export_nonce_key, 0)

            def _collapse_export():
                st.session_state[exp_export_nonce_key] += 1

            exp_export_label = "Экспорт" + ("\u200b" * int(st.session_state[exp_export_nonce_key]))

            with st.expander(exp_export_label, expanded=False):
                safe_title2 = current_title or f"Страница_{page_id}"

                docx_bytes = export_html_to_docx_bytes(current_html, safe_title2)
                st.download_button(
                    ".docx",
                    data=docx_bytes,
                    file_name=_safe_filename(safe_title2, "docx"),
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    on_click=_collapse_export,
                )

                st.download_button(
                    ".sql (utf-8)",
                    data=export_html_to_sql_bytes(current_html or "", safe_title2, encoding="utf-8"),
                    file_name=_safe_filename(safe_title2, "sql"),
                    mime="text/plain; charset=utf-8",
                    on_click=_collapse_export,
                )

                st.download_button(
                    ".sql (cp1251)",
                    data=export_html_to_sql_bytes(current_html or "", safe_title2, encoding="cp1251"),
                    file_name=_safe_filename(safe_title2, "sql"),
                    mime="text/plain; charset=windows-1251",
                    on_click=_collapse_export,
                )

                if st.button("eMail", key=f"open_email_dialog_{page_id}", use_container_width=True):
                    st.session_state["email_dialog_page_id"] = page_id

                if st.session_state.get("email_dialog_page_id") == page_id:
                    email_message_dialog(page_id, safe_title2, current_html or "", selected_login, page_path)
                    _collapse_export()

        # ---------------- Вложения ----------------
        with col3:
            exp_nonce_key = f"exp_files_nonce_{page_id}"
            up_nonce_files_key = f"uploader_nonce_files_{page_id}"
            up_nonce_links_key = f"uploader_nonce_links_{page_id}"

            st.session_state.setdefault(exp_nonce_key, 0)
            st.session_state.setdefault(up_nonce_files_key, 0)
            st.session_state.setdefault(up_nonce_links_key, 0)

            exp_label = "Файлы и ссылки" + ("\u200b" * int(st.session_state[exp_nonce_key]))

            with st.expander(exp_label, expanded=False):
                if can_edit_notebook:
                    uploader_key = f"files_uploader_{page_id}_{st.session_state[up_nonce_files_key]}"
                    uploaded_files = st.file_uploader(
                        "Загрузить файлы",
                        type=None,
                        accept_multiple_files=True,
                        key=uploader_key,
                    )

                    save_files_clicked = st.button("Сохранить файлы", key=f"save_files_btn_{page_id}")

                    if save_files_clicked:
                        if not uploaded_files:
                            st.warning("Сначала выберите файлы для загрузки.")
                        else:
                            try:
                                for uf in uploaded_files:
                                    save_file_attachment(page_id, uf, selected_login)

                                st.success(f"Сохранено файлов: {len(uploaded_files)}")

                                st.session_state[up_nonce_files_key] += 1
                                st.session_state[exp_nonce_key] += 1

                                st.rerun()
                            except Exception as e:
                                st.error(f"Ошибка при сохранении файлов: {e}")

                    st.markdown("---")

                    link_title_key = f"page_link_title_{page_id}"
                    link_url_key = f"page_link_url_{page_id}"

                    link_title = st.text_input("Подпись для ссылки", key=link_title_key)
                    link_url = st.text_input("URL", key=link_url_key)

                    if st.button("Сохранить ссылку", key=f"btn_save_link_{page_id}", use_container_width=True):
                        try:
                            save_link_attachment(page_id, link_url, link_title, selected_login)
                            st.success("Ссылка сохранена")
                            st.session_state[exp_nonce_key] += 1
                            st.rerun()
                        except ValueError as exc:
                            st.warning(str(exc))
                        except Exception as exc:
                            st.error(f"Не удалось сохранить ссылку: {exc}")
                else:
                    st.caption("Прикреплять файлы могут совладельцы блокнота.")

        # ---------------- Переместить/скопировать ----------------
        with col4:
            exp_move_nonce_key = f"exp_move_nonce_{page_id}"
            st.session_state.setdefault(exp_move_nonce_key, 0)

            def _collapse_move():
                st.session_state[exp_move_nonce_key] += 1

            exp_move_label = "Переместить или скопировать" + ("\u200b" * int(st.session_state[exp_move_nonce_key]))

            with st.expander(exp_move_label, expanded=False):
                st.write(f"Текущая страница: **{current_title or f'ID {page_id}'}**")

                if owned_notebooks_df.empty:
                    st.info("У вас нет записных книг, в которых вы являетесь владельцем. Копирование и перемещение недоступны.")
                else:
                    dest_nb_records = list(owned_notebooks_df.itertuples(index=False))

                    cur_nb_id = int(current_page["notebook_id"])
                    default_nb = next((r for r in dest_nb_records if int(r.id) == cur_nb_id), dest_nb_records[0])
                    nb_index = dest_nb_records.index(default_nb)

                    dest_notebook = st.selectbox(
                        "Записная книга (для копирования/перемещения)",
                        dest_nb_records,
                        format_func=lambda r: r.name,
                        index=nb_index,
                        key=f"move_nb_{page_id}",
                    )

                    dest_sections_df = get_sections(int(dest_notebook.id))
                    dest_sec_records = list(dest_sections_df.itertuples(index=False))
                    if not dest_sec_records:
                        st.warning("В выбранной книге нет разделов. Сначала создайте раздел.")
                    else:
                        cur_sec_id = int(current_page["section_id"])
                        default_sec = next((r for r in dest_sec_records if int(r.id) == cur_sec_id), dest_sec_records[0])
                        sec_index = dest_sec_records.index(default_sec)

                        dest_section = st.selectbox(
                            "Раздел",
                            dest_sec_records,
                            format_func=lambda r: r.name,
                            index=sec_index,
                            key=f"move_sec_{page_id}",
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
                                st.caption("Перемещение недоступно\n(только владельцы книги)")

                        with col_copy:
                            copy_clicked = st.button("Копировать", key=f"btn_copy_{page_id}", use_container_width=True)

                        with col_cancel:
                            cancel_clicked = st.button("Отмена", key=f"btn_cancel_{page_id}", use_container_width=True)

                        if move_clicked and can_edit_notebook:
                            run_execute(
                                f"""
                                UPDATE {PAGES_TABLE}
                                SET section_id = {int(dest_section.id)}, updated_at = NOW()
                                WHERE id = {int(page_id)}
                                """
                            )
                            st.success("Страница перемещена.")
                            st.session_state["current_page_id"] = page_id
                            st.session_state["force_page_id"] = page_id

                            _collapse_move()
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
                            if new_page_id is None:
                                st.error("Не удалось создать копию страницы.")
                            else:
                                new_page_id = int(new_page_id)
                                st.success("Страница скопирована.")
                                st.session_state["current_page_id"] = new_page_id
                                st.session_state["force_page_id"] = new_page_id
                                st.session_state["open_editor_once_for_page"] = new_page_id
                                _collapse_move()
                                st.rerun()

                        if cancel_clicked:
                            _collapse_move()
                            st.rerun()

        # --- дальше у тебя идёт таблица вложений, удаление страницы и т.д. ---

        # --- таблица вложений ---
        attachments_df = get_page_attachments(page_id)
        if not attachments_df.empty:
            att_display = attachments_df.copy()
            att_display["Размер"] = att_display["file_size"].apply(_format_file_size)
            att_display["Создано"] = pd.to_datetime(att_display["created_at"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
            att_display["Тип"] = att_display["attachment_type"].map({"file": "Файл", "link": "Ссылка"}).fillna(att_display["attachment_type"])
            att_display["Название"] = att_display["file_name"]
            att_display["Автор"] = att_display["created_by"]
            att_display["URL"] = att_display["url"].fillna("")
            grid_df = att_display[["id", "Тип", "Название", "Размер", "Создано", "Автор", "URL"]]

            links_data = []
            for row in attachments_df.itertuples(index=False):
                if row.attachment_type == "file":
                    payload = get_attachment_file(row.id)
                    if payload:
                        file_bytes, file_name, mime_type = payload
                        b64 = base64.b64encode(file_bytes).decode("ascii")
                        links_data.append({"id": int(row.id), "name": str(file_name), "href": f"data:{mime_type};base64,{b64}"})

            if links_data:
                js_payload = json.dumps(links_data, ensure_ascii=False)
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

            gb_att = GridOptionsBuilder.from_dataframe(grid_df)
            gb_att.configure_selection("single", use_checkbox=False)
            gb_att.configure_column("id", hide=True)
            gb_att.configure_column("URL", hide=True)
            gb_att.configure_column("Название", width=240)
            gb_att.configure_column("Размер", width=90)
            gb_att.configure_column("Тип", width=80)

            row_doubleclick_js = JsCode(
                """
                function (e) {
                    var d = e.data || {};
                    var id = d.id;
                    var type = d["Тип"];
                    var url = d["URL"] || "";

                    if (!id) { return; }

                    if (type === "Файл") {
                        var a = window.parent.document.getElementById("att_dl_" + id);
                        if (a) { a.click(); }
                    } else if (type === "Ссылка" && url) {
                        window.parent.open(url, "_blank");
                    }
                }
                """
            )
            gb_att.configure_grid_options(onRowDoubleClicked=row_doubleclick_js)

            grid_response = AgGrid(
                grid_df,
                gridOptions=gb_att.build(),
                enable_enterprise_modules=False,
                update_on=["selectionChanged"],
                height=220,
                fit_columns_on_grid_load=True,
                allow_unsafe_jscode=True,
            )

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
        col_a, col_b, col_c, col_d = st.columns([1.8, 1.2, 3, 3])

        with col_d:
            exp_help_nonce_key = f"exp_help_nonce_{page_id}"
            st.session_state.setdefault(exp_help_nonce_key, 0)

            def _open_help(topic: str) -> None:
                st.session_state[f"help_topic_{page_id}"] = topic
                st.session_state[exp_help_nonce_key] += 1

            exp_help_label = "Справка ?" + ("\u200b" * int(st.session_state[exp_help_nonce_key]))

            with st.expander(exp_help_label, expanded=False):
                st.button(
                    "🔎 Расширенный поиск",
                    key=f"help_search_open_{page_id}",
                    help="Правила AND/OR/NOT, теги, wildcard и приоритеты",
                    on_click=_open_help,
                    args=("search",),
                    use_container_width=True,
                )

            help_topic = st.session_state.pop(f"help_topic_{page_id}", None)
            if help_topic == "search":
                help_search_dialog()

        if can_edit_notebook:
            attachments_df_for_delete = get_page_attachments(page_id)
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
                if has_attachments:
                    st.error("Нельзя удалить страницу, пока к ней прикреплены вложения.")
                    st.stop()

                if confirm_delete:
                    delete_page(page_id)
                    st.success("Страница удалена")

                    if st.session_state.get("edit_dialog_page_id") == page_id:
                        st.session_state["edit_dialog_page_id"] = None

                    st.rerun()
                else:
                    st.warning("Поставьте галочку для подтверждения.")
        else:
            st.info("У вас права только на просмотр этой записной книжки.")

    st.logo("assets/logo.png", size="medium")


if __name__ == "__main__":
    main()
