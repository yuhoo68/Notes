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
import email
import io
import json
import re
import urllib.parse
import pandas as pd
import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from docx import Document
from docx.enum.text import WD_LINE_SPACING
from docx.shared import Inches, Pt
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode
from streamlit_quill import st_quill

import config
from src.database_utils_DRP import get_execute, get_fetch, test_connection


SCHEMA = "sbx_dfip_ocpp"
USERS_TABLE = f"{SCHEMA}.notes_users"
NOTEBOOKS_TABLE = f"{SCHEMA}.notes_notebooks"
SECTIONS_TABLE = f"{SCHEMA}.notes_sections"
PAGES_TABLE = f"{SCHEMA}.notes_pages"
OWNERS_TABLE = f"{SCHEMA}.notes_notebook_owners"
DEPARTMENTS_TABLE = f"{SCHEMA}.notes_departments"
ATTACHMENTS_TABLE = f"{SCHEMA}.notes_page_attachments"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("notes_app")


def _escape(val: str) -> str:
    """Минимальное экранирование строк для SQL."""
    return (val or "").replace("'", "''")


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


st.session_state.setdefault("edit_dialog_page_id", None)
st.session_state.setdefault("download_payload", None)      # tuple[bytes, str, str] | None
st.session_state.setdefault("download_att_id", None)       # int | None
st.session_state.setdefault("download_error", None)        # str | None


def _creds() -> tuple[str, str]:
    creds = ensure_db_credentials()
    return creds["user"], creds["password"]


def run_fetch_df(query: str) -> pd.DataFrame:
    user, pwd = _creds()
    return get_fetch(query, user, pwd)


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

#         ON CONFLICT (login) DO NOTHING


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
    - '99'          -> видна только создателю книги (created_by = текущий пользователь)
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
      - владелец книги видит её всегда (и редактировать может только владелец)
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

    # видимость по подразделению для НЕ-99
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
            -- 99: только владельцы
            (n.department_id = '99' AND ({owner_condition}))
            OR
            -- НЕ 99: владельцы ИЛИ видимость по подразделению/всем
            ((n.department_id IS NULL OR n.department_id <> '99')
                AND ( {owner_condition} OR {dept_visibility_non99} )
            )
        ORDER BY n.name
        """
    )


def get_owned_notebooks(user_login: str) -> pd.DataFrame:
    """Книги, в которых пользователь является владельцем (для назначения при копировании/перемещении)."""
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

    query = f"""
        SELECT
            p.id,
            p.title,
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

    if notebook_id:
        query += f" AND n.id = {int(notebook_id)}"
    if section_id:
        query += f" AND s.id = {int(section_id)}"

    if search_text:
        if search_tags_only:
            query += f" AND p.tag ILIKE '%{_escape(search_text)}%'"
        else:
            q = _escape(search_text)
            query += f" AND (p.title ILIKE '%{q}%' OR p.body_html ILIKE '%{q}%')"

    query += " ORDER BY p.updated_at DESC, p.id DESC"
    return run_fetch_df(query)


def create_notebook(name: str, user_login: str, department_id: str | None) -> int:
    cleaned = name.strip() or "Новая книга"
    logger.info(
        "Создание книги: %s пользователем %s (department_id=%s)",
        cleaned,
        user_login,
        department_id,
    )

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
    logger.info("Создание раздела: %s в книге %s пользователем %s", cleaned, notebook_id, user_login)
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
    logger.info("Создание страницы: section_id=%s user=%s", section_id, user_login)
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
    new_id = run_scalar(
        f"""
        INSERT INTO {PAGES_TABLE} (section_id, title, tag, body_html, created_by)
        VALUES ({int(section_id)},
                '{_escape(title.strip() or 'Untitled')}',
                '',
                '{_escape(body_html)}',
                '{_escape(user_login)}')
        RETURNING id
        """
    )
    if new_id is None:
        raise RuntimeError("Не удалось импортировать страницу")
    return int(new_id)


def update_page(page_id: int, title: str, body_html: str, tag: str) -> None:
    run_execute(
        f"""
        UPDATE {PAGES_TABLE}
        SET title = '{_escape(title.strip() or 'Без названия')}',
            tag = '{_escape(tag)}',
            body_html = '{_escape(body_html)}',
            updated_at = NOW()
        WHERE id = {int(page_id)}
        """
    )


def delete_page(page_id: int) -> None:
    logger.info("Удаление страницы: id=%s", page_id)
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
    logger.info("Удаление вложения: id=%s", attachment_id)
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

    return _split_onenote_html_into_pages(soup, filename)


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
    """
    Преобразует HTML в plain-text так, чтобы:
      - сохранялись переносы строк
      - сохранялись пробелы/табы (включая отступы в начале строк)
      - &nbsp; превращался в обычный пробел
      - восстанавливались отступы Quill по классам ql-indent-N (N * indent_spaces)
      - не появлялись "пустые строки между каждым абзацем" при копировании/экспорте
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # <br> -> перенос строки
    for br in soup.find_all("br"):
        br.replace_with("\n")

    body = soup.body or soup

    block_tags = {"p", "div", "li", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
    out_lines: list[str] = []

    def _norm_newlines(s: str) -> str:
        return s.replace("\r\n", "\n").replace("\r", "\n")

    def _indent_level(tag: Tag) -> int:
        classes = tag.get("class") or []
        for c in classes:
            m = re.match(r"ql-indent-(\d+)", str(c))
            if m:
                return int(m.group(1))
        return 0

    def _text_of(tag: Tag) -> str:
        parts: list[str] = []
        for elem in tag.descendants:
            if isinstance(elem, NavigableString):
                parts.append(str(elem))
        raw = "".join(parts)
        raw = _norm_newlines(raw).replace("\xa0", " ")
        # сохраняем ведущие пробелы (важно для SQL), но убираем хвосты справа
        return "\n".join(line.rstrip() for line in raw.split("\n"))

    # Берём блоки как строки (а не get_text всего body), чтобы не получать “двойные” пустые строки
    blocks = body.find_all(list(block_tags))
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
        # fallback, если почему-то блоки не найдены
        txt = _text_of(body)
        out_lines.extend(txt.split("\n"))

    result = "\n".join(out_lines)
    result = _norm_newlines(result)
    # максимум 2 пустых строки подряд
    result = re.sub(r"\n{3,}", "\n\n", result).rstrip()
    return result


def _sql_text_from_html(html: str, title: str) -> str:
    plain = _html_to_plain_preserving_layout(html or "")
    header = f"-- {title or ''}".rstrip()
    return header + ("\n\n" + plain if plain else "\n")


def export_html_to_sql_bytes(html: str, title: str, encoding: str = "utf-8") -> bytes:
    full_text = _sql_text_from_html(html or "", title or "")
    return full_text.encode(encoding, errors="replace")


def _clipboard_write_text_js(text: str) -> str:
    """
    JS-скрипт, который кладёт text в буфер обмена.
    С fallback на execCommand.
    """
    js_text = json.dumps(text)  # безопасно экранирует для JS
    return f"""
    <script>
    (async function() {{
        try {{
            await navigator.clipboard.writeText({js_text});
        }} catch (e) {{
            const ta = document.createElement('textarea');
            ta.value = {js_text};
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        }}
    }})();
    </script>
    """


def render_copy_sql_button(sql_text: str, btn_key: str) -> None:
    """
    Рендерит кнопку копирования SQL через components.html так,
    чтобы копирование выполнялось в обработчике клика (user gesture),
    иначе браузер блокирует доступ к clipboard.
    """
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
              // Основной способ
              if (navigator.clipboard && navigator.clipboard.writeText) {{
                await navigator.clipboard.writeText(text);
                ok("Скопировано");
                return;
              }}
            }} catch (e) {{
              // упадём в fallback ниже
            }}

            // Fallback (часто работает там, где clipboard API запрещён)
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
        height=42,           # ✅ маленькая фиксированная высота → не появится “пустая область”
        scrolling=False,
    )



def main():
    st.set_page_config(
        layout="wide",
        page_title="ДФИП_Notes",
        initial_sidebar_state="expanded",
    )

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
        [data-testid="stSidebar"] { min-width: 330px; }
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
    department_map = {
        row.department_id: row.name_department
        for row in departments_df.itertuples(index=False)
    }

    # --- Подразделение пользователя ---
    user_dep_id = user_dept_map.get(selected_login)

    # --- Левый сайдбар: выбор подразделения (включая 99) ---
    selected_department_id = "00"
    dept_records = list(departments_df.itertuples(index=False)) if not departments_df.empty else []

    is_user_department_selected = False

    forced_department_id = st.session_state.pop("force_department_id", None)

    if dept_records:
        # 1) если только что создавали книгу – форсим подразделение книги
        if forced_department_id:
            forced_row = next(
                (r for r in dept_records if str(r.department_id) == str(forced_department_id)),
                None,
            )
            if forced_row is not None:
                st.session_state["department_selector"] = forced_row

        # 2) если selector ещё не задан — ставим дефолт:
        #    - 99 (Владельцы), если у пользователя есть свои книги (owners)
        #    - иначе 00 (Все)
        if "department_selector" not in st.session_state and not forced_department_id:
            has_owned_books = not owned_notebooks_df.empty
            default_dep_id = "99" if has_owned_books else "00"

            default_row = next(
                (r for r in dept_records if str(r.department_id) == default_dep_id),
                None,
            )
            if default_row is None:
                # fallback на первый элемент, если вдруг справочник не содержит 99/00
                default_row = dept_records[0]

            st.session_state["department_selector"] = default_row

        selected_department = st.sidebar.selectbox(
            "Подразделение",
            dept_records,
            format_func=lambda row: row.name_department,
            key="department_selector",
        )
        selected_department_id = str(selected_department.department_id)

        # подразделение пользователя (из notes_users)
        user_dep_id = user_dept_map.get(selected_login)

        # выбран ли сейчас отдел пользователя
        is_user_department_selected = bool(user_dep_id) and (str(selected_department_id) == str(user_dep_id))
    else:
        selected_department_id = "00"
        is_user_department_selected = False

    st.session_state["current_department_id"] = selected_department_id

    # --- поиск страниц ---
    def _clear_page_search():
        st.session_state["page_search"] = ""

    search_col, clear_col = st.sidebar.columns([12, 2])
    with search_col:
        search_raw = st.text_input(
            label="",
            key="page_search",
            placeholder="Поиск страниц по #tag или тексту",
            label_visibility="collapsed",
        )
    with clear_col:
        st.button("✕", key="clear_page_search", help="Очистить поиск", on_click=_clear_page_search)

    search_raw = (search_raw or "").strip()
    search_tags_only = search_raw.startswith("#")
    search_text = search_raw[1:].strip() if search_tags_only else search_raw

    current_user_can_create_notebook = selected_login in registered_users

    # --- список книг пользователя (по новой ролевой модели + 99) ---
    notebooks_df = get_notebooks(selected_login, user_dep_id)

    filtered_notebooks_df = notebooks_df.copy()
    current_department_id: str = st.session_state.get("current_department_id", "00")

    # фильтр списка книг по выбранному подразделению слева
    if current_department_id != "00" and not filtered_notebooks_df.empty:
        dep_col = filtered_notebooks_df["department_id"].fillna("00").astype(str)

        # ✅ 99 = (Владельцы): показываем ВСЕ книги, где текущий пользователь владелец,
        # независимо от department_id книги
        if str(current_department_id) == "99":
            owned_ids = (
                set(owned_notebooks_df["id"].astype(int).tolist())
                if not owned_notebooks_df.empty
                else set()
            )
            filtered_notebooks_df = filtered_notebooks_df[
                filtered_notebooks_df["id"].astype(int).isin(owned_ids)
            ]
        else:
            prefix = str(current_department_id).strip()
            prefix_like = prefix + "."
            mask = (
                (dep_col == "00")                  # "Все" остаётся видимым
                | (dep_col == prefix)              # точное совпадение
                | dep_col.str.startswith(prefix_like)  # подчинённые
            )
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

    # --- верхняя панель: выбор книги + кнопки ---
    with top_col1:
        select_col, plus_col, info_col = st.columns([14, 2, 2])

        with select_col:
            if notebook_records:
                forced_nb_id = st.session_state.pop("force_notebook_id", None)
                if forced_nb_id is not None:
                    target_row = next((r for r in notebook_records if r.id == forced_nb_id), None)
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
                selected_notebook_row = filtered_notebooks_df[
                    filtered_notebooks_df["id"] == selected_notebook_id
                ].iloc[0]

                can_edit_notebook = bool(is_notebook_owner(selected_notebook_id, selected_login))
            else:
                st.info("Нет доступных книг")

        # --- диалог "Права доступа на книгу" ---
        if selected_notebook_id is not None and can_edit_notebook:

            @st.dialog("Права доступа на книгу", width="small")
            def notebook_access_dialog():
                owners_df = get_notebook_owners(selected_notebook_id)
                owners_text = (
                    ", ".join(
                        f"{row.full_name or row.login} ({row.login})"
                        for row in owners_df.itertuples(index=False)
                    )
                    or "Нет владельцев"
                )

                dept_id = selected_notebook_row.get("department_id", None)
                dept_id = "00" if dept_id is None or str(dept_id).strip() == "" else str(dept_id)
                dept_value = department_map.get(dept_id, "не задано")

                st.caption(f"Подразделение (видимость): {dept_value}")
                st.caption(f"Владельцы: {owners_text}")

                owner_logins = set(owners_df["login"].tolist()) if not owners_df.empty else set()

                with st.form(f"access_form_{selected_notebook_id}"):

                    # ✅ область видимости книги: выпадающий список ТОЛЬКО name_department (без id)
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
                        format_func=lambda r: f"{r.name_department}",  # ✅ только name_department
                        key=f"nb_department_{selected_notebook_id}",
                    )
                    new_department_id = str(selected_dept_row.department_id) if selected_dept_row else "00"

                    # добавление владельца
                    selectable_users = [login for login in login_options if login not in owner_logins]
                    new_owner_login = st.selectbox(
                        "Добавить владельца книги",
                        options=[""] + selectable_users,
                        format_func=lambda login: "—" if login == "" else f"{user_map.get(login, login)} ({login})",
                        key=f"add_owner_{selected_notebook_id}",
                    )

                    # удаление владельца (кроме текущего пользователя)
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

        if selected_notebook_id is not None and can_edit_notebook:
            with info_col:
                st.markdown("###### ")
                st.markdown("###### ")
                if st.button("🔐", key="open_notebook_access_dialog", help="Права доступа на книгу", use_container_width=True):
                    notebook_access_dialog()

    can_edit_notebook = bool(selected_notebook_id and is_notebook_owner(selected_notebook_id, selected_login))

    # --- диалог "Новый раздел" ---
    if can_edit_notebook and selected_notebook_id is not None:

        @st.dialog("Новый раздел", width="small")
        def new_section_dialog():
            new_section_name = st.text_input("Название раздела", key="new_section_name_modal")
            create_clicked = st.button("Создать раздел", key="create_section_btn_modal")
            if create_clicked:
                create_section(selected_notebook_id, new_section_name, selected_login)
                st.success("Раздел создан")
                st.rerun()

    # --- диалог "Переименовать / удалить раздел" ---
    if can_edit_notebook and selected_notebook_id is not None:

        @st.dialog("Переименовать или удалить раздел", width="small")
        def section_manage_dialog(section_row):
            section_id_local = int(section_row.id)
            st.caption(f"Текущий раздел: **{section_row.name}**")

            new_name = st.text_input("Новое название раздела", value=section_row.name, key=f"rename_section_name_{section_id_local}")
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
                    if st.button("Удалить раздел", key=f"btn_delete_section_{section_id_local}", use_container_width=True, type="secondary"):
                        delete_section(section_id_local)
                        st.success("Раздел удалён")
                        st.rerun()

    # --- список разделов ---
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
        search_notebook_id = None
        search_section_id = None
        search_allowed_ids = dept_notebook_ids
    else:
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

    if pages_df.empty:
        pages_df = pd.DataFrame(
            columns=[
                "id",
                "title",
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

    # ---------- Кнопки "Новая страница" и "Импорт страниц" в сайдбаре ----------
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
                        pages = parse_mht_to_pages(file.getvalue(), file.name)
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
            st.session_state["force_edit_page_id"] = new_page_id
            st.session_state["edit_dialog_page_id"] = new_page_id
            st.rerun()

    # ---------- Список страниц ----------
    df_display = pages_df[["id", "title"]].copy().reset_index(drop=True)

    gb = GridOptionsBuilder.from_dataframe(df_display)
    gb.configure_selection("single", use_checkbox=False)
    gb.configure_column("title", header_name="Страница", flex=1, minWidth=160)
    gb.configure_column("id", header_name="ID", width=60, hide=True)

    force_page_id = st.session_state.pop("force_page_id", None)
    if force_page_id is not None and not df_display.empty:
        try:
            row_index = int(df_display.index[df_display["id"] == force_page_id][0])
            gb.configure_selection("single", pre_selected_rows=[row_index])
        except Exception:
            pass

    list_container = st.sidebar.container()

    on_grid_ready = JsCode("function(params) { params.api.sizeColumnsToFit(); }")
    on_grid_size_changed = JsCode("function(params) { params.api.sizeColumnsToFit(); }")
    gb.configure_grid_options(onGridReady=on_grid_ready, onGridSizeChanged=on_grid_size_changed)

    with list_container:
        grid_response = AgGrid(
            df_display,
            gridOptions=gb.build(),
            enable_enterprise_modules=False,
            update_on=["selectionChanged"],
            height=650,
            fit_columns_on_grid_load=False,
            allow_unsafe_jscode=True,
        )

    selected_rows = grid_response.get("selected_rows", [])
    if isinstance(selected_rows, pd.DataFrame):
        selected_rows = selected_rows.to_dict("records")

    page_id: int | None = None
    if selected_rows:
        row = selected_rows[0]
        page_id = int(row["id"])
        st.session_state["current_page_id"] = page_id
    else:
        stored_page_id = st.session_state.get("current_page_id")
        if stored_page_id is not None and not pages_df.empty:
            if (pages_df["id"] == stored_page_id).any():
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

        # ======= строка: информация о странице (слева) + кнопка "Скопировать" (справа) =======
        safe_title = current_title or f"Страница_{page_id}"
        info_left, info_right = st.columns([12, 3])
        with info_left:
            st.caption(
                f"{dept_prefix}  {current_page['notebook_name']}  =>  "
                f"{current_page['section_name']}  =>  {current_page['title']}"
            )
            if current_tag:
                st.caption(f"Tag: {current_tag}")

        with info_right:
            sql_text = _sql_text_from_html(current_html or "", safe_title)
            render_copy_sql_button(sql_text, btn_key=f"copy_sql_utf8_{page_id}")


        preview_html = f"""
        <style>
        .page-preview-wrapper {{
            border: 1px solid #d0d4da;
            border-radius: 6px;
            padding: 16px 18px;
            background-color: #ffffff;
            box-shadow: 0 1px 2px rgba(0,0,0,0.04);
            min-height: 580px;
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
                {current_html or "<p><em>Нет содержимого</em></p>"}
            </div>
        </div>
        """
        components.html(preview_html, height=600, scrolling=True)

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
                </style>
                """,
                unsafe_allow_html=True,
            )

            col_l, col_r = st.columns([2, 2])
            with col_l:
                new_title = st.text_input("Название страницы", value=title, key=f"dlg_title_{page_id_local}")
            with col_r:
                new_tag = st.text_input("Теги", value=tag or "", key=f"dlg_tag_{page_id_local}")

            editable_html = html_body or ""

            quill_value = st_quill(
                value=editable_html,
                html=True,
                placeholder="Введите текст...",
                key=f"dlg_quill_{page_id_local}",
            )

            # st_quill иногда возвращает None (например, без изменений) — в этом случае
            # сохраняем исходный HTML, иначе можно случайно затереть контент.
            quill_html = editable_html if quill_value is None else (quill_value or "")

            c1, c2 = st.columns([1, 1])
            with c1:
                if st.button("Сохранить", key=f"dlg_save_{page_id_local}", use_container_width=True):
                    update_page(page_id_local, new_title, quill_html, new_tag)
                    st.success("Страница обновлена")
                    st.session_state["edit_dialog_page_id"] = None
                    st.session_state["current_page_id"] = page_id_local
                    st.session_state["force_page_id"] = page_id_local
                    st.rerun()

            with c2:
                if st.button("Отмена", key=f"dlg_cancel_{page_id_local}", use_container_width=True):
                    st.session_state["edit_dialog_page_id"] = None
                    st.rerun()

        dlg_pid = st.session_state.get("edit_dialog_page_id")
        if can_edit_notebook and dlg_pid == page_id:
            edit_page_dialog(page_id, current_title, current_html, current_tag)

        # --- кнопка редактирования + экспорт + вложения + перемещение ---
        col1, col2, col3, col4 = st.columns([1.5, 1.5, 3, 3])

        with col1:
            if can_edit_notebook:
                if st.button("Редактировать страницу", key=f"open_edit_dialog_{page_id}", use_container_width=True):
                    st.session_state["edit_dialog_page_id"] = page_id
                    st.rerun()
            else:
                st.caption("Просмотр (редактирование недоступно)")

        with col2:
            with st.expander("Экспорт", expanded=False):
                docx_bytes = export_html_to_docx_bytes(current_html, safe_title)
                st.download_button(
                    ".docx",
                    data=docx_bytes,
                    file_name=_safe_filename(safe_title, "docx"),
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )

                st.download_button(
                    ".sql (utf-8)",
                    data=export_html_to_sql_bytes(current_html or "", safe_title, encoding="utf-8"),
                    file_name=_safe_filename(safe_title, "sql"),
                    mime="text/plain; charset=utf-8",
                )

                st.download_button(
                    ".sql (cp1251)",
                    data=export_html_to_sql_bytes(current_html or "", safe_title, encoding="cp1251"),
                    file_name=_safe_filename(safe_title, "sql"),
                    mime="text/plain; charset=windows-1251",
                )

        # --- Вложения ---
        with col3:
            exp_key = f"attachments_expanded_{page_id}"
            expanded_state = st.session_state.get(exp_key, False)

            with st.expander("Файлы и ссылки", expanded=expanded_state):
                if can_edit_notebook:
                    uploaded_files = st.file_uploader(
                        "Прикрепить файлы к странице",
                        accept_multiple_files=True,
                        key=f"page_attachments_{page_id}",
                    )
                    if uploaded_files and st.button("Сохранить файлы", key=f"btn_save_attachments_{page_id}", use_container_width=True):
                        saved = 0
                        errors: list[str] = []
                        for file in uploaded_files:
                            try:
                                save_file_attachment(page_id, file, selected_login)
                                saved += 1
                            except Exception as exc:
                                errors.append(f"{file.name}: {exc}")
                        if saved:
                            st.success(f"Прикреплено: {saved}")
                            st.rerun()
                        if errors:
                            st.warning("; ".join(errors))

                    link_title = st.text_input("Подпись для ссылки", key=f"page_link_title_{page_id}")
                    link_url = st.text_input("URL", key=f"page_link_url_{page_id}")
                    if st.button("Сохранить ссылку", key=f"btn_save_link_{page_id}", use_container_width=True):
                        try:
                            save_link_attachment(page_id, link_url, link_title, selected_login)
                            st.success("Ссылка сохранена")
                            st.rerun()
                        except ValueError as exc:
                            st.warning(str(exc))
                        except Exception as exc:
                            st.error(f"Не удалось сохранить ссылку: {exc}")
                else:
                    st.caption("Прикреплять файлы могут совладельцы блокнота.")

        # --- Перемещение/копирование ---
        with col4:
            with st.expander("Переместить или скопировать", expanded=False):
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

                        move_clicked = False
                        with col_move:
                            if can_edit_notebook:
                                move_clicked = st.button("Переместить", type="primary", key=f"btn_move_{page_id}", use_container_width=True)
                            else:
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
                                st.session_state["force_edit_page_id"] = new_page_id
                                st.rerun()

                        if cancel_clicked:
                            st.rerun()

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

            # скрытые ссылки для dblclick по файлу
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

                # --- Файл ---
                if att_type == "Файл":
                    row_meta = attachments_df[attachments_df["id"].astype(int) == int(att_id)]
                    file_name_meta = ""
                    mime_meta = "application/octet-stream"
                    file_size_meta = None

                    if not row_meta.empty:
                        file_name_meta = str(row_meta.iloc[0].get("file_name") or "")
                        mime_meta = str(row_meta.iloc[0].get("mime_type") or "application/octet-stream")
                        file_size_meta = row_meta.iloc[0].get("file_size")

                    del_col, _ = st.columns([1,  4])

                    if can_edit_notebook:
                        with del_col:
                            if st.button("Удалить вложение", key=f"delete_attachment_{att_id}", use_container_width=True):
                                delete_attachment(att_id)
                                if st.session_state.get("download_att_id") == att_id:
                                    st.session_state["download_att_id"] = None
                                    st.session_state["download_payload"] = None
                                    st.session_state["download_error"] = None
                                st.success("Вложение удалено")
                                st.rerun()

                # --- Ссылка ---
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

        # --- Удаление страницы (с проверкой вложений) ---
        if can_edit_notebook:
            attachments_df_for_delete = get_page_attachments(page_id)
            has_attachments = not attachments_df_for_delete.empty

            if has_attachments:
                st.warning("Удаление страницы запрещено: сначала удалите все прикреплённые файлы и ссылки.")

            col_a, col_b, col_c, col_d = st.columns([1.5, 1.5, 3, 3])

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


if __name__ == "__main__":
    main()

