import base64
import email
import logging
import os
import urllib.parse
from datetime import datetime
import requests

import streamlit.components.v1 as components
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup, NavigableString, Tag
from st_aggrid import AgGrid, GridOptionsBuilder
from streamlit_quill import st_quill
import io
import re
from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_LINE_SPACING
from xhtml2pdf import pisa

import quopri
import uuid
# from reportlab.pdfbase import pdfmetrics
# from reportlab.pdfbase.ttfonts import TTFont


import config
from src.database_utils_DRP import get_execute, get_fetch, test_connection

SCHEMA = "sbx_dfip_ocpp"
USERS_TABLE = f"{SCHEMA}.notes_users"
NOTEBOOKS_TABLE = f"{SCHEMA}.notes_notebooks"
SECTIONS_TABLE = f"{SCHEMA}.notes_sections"
PAGES_TABLE = f"{SCHEMA}.notes_pages"
OWNERS_TABLE = f"{SCHEMA}.notes_notebook_owners"
DEPARTMENTS_TABLE = f"{SCHEMA}.notes_departments"

ACCESS_LABELS = {
    0: "Все",
    1: "Сотрудники подразделения",
    2: "Владельцы книги",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("notes_app")


# !!!Включить в Lunix системе
# PDF_FONT_REGISTERED = False

# def ensure_pdf_font():
#     """Регистрирует кириллический шрифт под Linux для xhtml2pdf."""
#     global PDF_FONT_REGISTERED
#     if PDF_FONT_REGISTERED:
#         return
#
#     # Наиболее надёжный путь для Linux:
#     possible_paths = [
#         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
#         "/usr/share/fonts/dejavu/DejaVuSans.ttf",
#         "/usr/local/share/fonts/DejaVuSans.ttf",
#     ]
#
#     font_path = None
#     for p in possible_paths:
#         if os.path.exists(p):
#             font_path = p
#             break
#
#     if font_path is None:
#         raise FileNotFoundError(
#             "Не найден DejaVuSans.ttf! Установите пакет fonts-dejavu: "
#             "sudo apt install fonts-dejavu-core"
#         )
#
#     pdfmetrics.registerFont(TTFont("MyCyrillic", font_path))
#     PDF_FONT_REGISTERED = True


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


def _creds() -> tuple[str, str]:
    creds = ensure_db_credentials()
    return creds["user"], creds["password"]


def run_fetch_df(query: str) -> pd.DataFrame:
    user, pwd = _creds()
    result = get_fetch(query, user, pwd)
    if not result:
        return pd.DataFrame()
    rows, columns = result
    return pd.DataFrame(rows, columns=columns)


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
    """Справочник подразделений."""
    return run_fetch_df(
        f"""
        SELECT department_id, name_department
        FROM {DEPARTMENTS_TABLE}
        ORDER BY department_id
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
        ON CONFLICT (login) DO NOTHING
        """
    )
    return normalized


def add_notebook_owner(notebook_id: int, user_login: str) -> None:
    logger.info("Добавление владельца: notebook_id=%s user=%s", notebook_id, user_login)
    run_execute(
        f"""
        INSERT INTO {OWNERS_TABLE} (notebook_id, user_login)
        VALUES ({int(notebook_id)}, '{_escape(user_login)}')
        ON CONFLICT DO NOTHING
        """
    )


def set_notebook_closed(notebook_id: int, closed: int) -> None:
    """
    closed:
      0 – читать могут все
      1 – читать могут только сотрудники подразделения
      2 – читать могут только владельцы
    """
    logger.info("Изменение режима доступа книги: id=%s closed=%s", notebook_id, closed)
    run_execute(
        f"""
        UPDATE {NOTEBOOKS_TABLE}
        SET closed = {int(closed)}, updated_at = NOW()
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

    closed = 0  -> видят все
    closed = 1  -> видят только пользователи того же department_id
    closed = 2  -> только владельцы

    Владелец книги видит её всегда, независимо от closed и department_id.
    """
    dept_condition = ""
    if user_department_id:
        dept_condition = f" OR (closed = 1 AND department_id = '{_escape(user_department_id)}')"

    return run_fetch_df(
        f"""
        SELECT id,
               name,
               department_id,
               created_at,
               updated_at,
               created_by,
               closed
        FROM {NOTEBOOKS_TABLE}
        WHERE
              id IN (
                  SELECT notebook_id
                  FROM {OWNERS_TABLE}
                  WHERE user_login = '{_escape(user_login)}'
              )
           OR closed = 0
           {dept_condition}
        ORDER BY name
        """
    )

def get_owned_notebooks(user_login: str) -> pd.DataFrame:
    """Книги, в которых пользователь является владельцем."""
    return run_fetch_df(
        f"""
        SELECT n.id,
               n.name,
               n.department_id,
               n.created_at,
               n.updated_at,
               n.created_by,
               n.closed
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
            n.closed AS notebook_closed,
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
        INSERT INTO {NOTEBOOKS_TABLE} (name, created_by, closed, department_id)
        VALUES ('{_escape(cleaned)}', '{_escape(user_login)}', 0, {dept_value})
        RETURNING id
        """
    )

    if new_id is None:
        raise RuntimeError("Не удалось создать книгу")
    add_notebook_owner(int(new_id), user_login)
    return int(new_id)


def create_section(notebook_id: int, name: str, user_login: str) -> int:
    cleaned = name.strip() or "Новый раздел"
    logger.info(
        "Создание раздела: %s в книге %s пользователем %s",
        cleaned,
        notebook_id,
        user_login,
    )
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


def create_page(section_id: int, user_login: str, title: str | None = None) -> int:
    """Создать пустую страницу в разделе."""
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


def insert_page_with_content(
    section_id: int, title: str, body_html: str, user_login: str
) -> int:
    new_id = run_scalar(
        f"""
        INSERT INTO {PAGES_TABLE} (section_id, title, tag, body_html, created_by)
        VALUES ({int(section_id)}, '{_escape(title.strip() or 'Untitled')}', '', '{_escape(body_html)}', '{_escape(user_login)}')
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


def html_to_body(text: str, fallback_title: str):
    """Извлекает title и body из HTML."""
    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else fallback_title
    body = str(soup.body or soup)
    return title, body


def _split_onenote_html_into_pages(soup: BeautifulSoup, filename: str):
    """
    Разбивает HTML из OneNote на «страницы».

    OneNote при экспорте делает по странице блок вида:
        <div style="direction:ltr;border-width:100%"> ... </div>

    Возвращает список (title, body_html) для каждой страницы.
    """
    base_title = filename.rsplit(".", 1)[0]

    # блоки верхнего уровня – каждая страница OneNote
    page_divs = soup.find_all("div", style=lambda v: v and "border-width:100%" in v)

    pages: list[tuple[str, str]] = []

    # если не нашли «страничные» div'ы — считаем весь документ одной страницей
    if not page_divs:
        title, body_html = html_to_body(str(soup), base_title)
        pages.append((title, body_html))
        return pages

    for idx, div in enumerate(page_divs, start=1):
        # Заголовок страницы — первый <p> внутри блока (как в OneNote)
        title_p = div.find("p")
        title_text = title_p.get_text(strip=True) if title_p else ""
        if not title_text:
            title_text = f"{base_title} {idx}"

        body_html = str(div)
        pages.append((title_text, body_html))

    return pages


def parse_mht_to_pages(data: bytes, filename: str):
    """
    Парсинг .mht, инлайн ресурсов, возврат списка страниц
    [(title, body_html), ...].
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
            for key in (
                f"cid:{cid_clean}",
                f"CID:{cid_clean}",
                cid_clean,
                norm(cid_clean),
            ):
                src_map[key] = data_url
        if loc:
            loc_clean = loc.strip().strip("<>")
            normalized = norm(loc_clean)
            for key in (
                loc_clean,
                f"cid:{loc_clean}",
                f"CID:{loc_clean}",
                normalized,
            ):
                src_map[key] = data_url
            basename = os.path.basename(normalized)
            if basename:
                for key in (
                    basename,
                    f"cid:{basename}",
                    f"CID:{basename}",
                    norm(basename),
                ):
                    src_map[key] = data_url

    soup = BeautifulSoup(html_part, "html.parser")

    # Подменяем src на data: URL внутри всего документа
    for tag in soup.find_all(src=True):
        src_val = tag.get("src", "")
        lookup = norm(src_val)
        if lookup in src_map:
            tag["src"] = src_map[lookup]
        else:
            basename = os.path.basename(lookup)
            if basename in src_map:
                tag["src"] = src_map[basename]

    # Разбиваем HTML на логические страницы
    return _split_onenote_html_into_pages(soup, filename)


# Старый интерфейс на всякий случай оставим —
# он вернёт только первую страницу из файла.
def parse_mht_to_html(data: bytes, filename: str):
    pages = parse_mht_to_pages(data, filename)
    return pages[0]


def strip_data_uri_images(html: str) -> str:
    """Убираем data-uri картинки для облегчения веса."""
    soup = BeautifulSoup(html or "", "html.parser")
    changed = False
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src.startswith("data:"):
            img["src"] = ""
            changed = True
    return str(soup) if changed else html


def _safe_filename(title: str, ext: str) -> str:
    """Простейшая очистка имени файла от недопустимых символов."""
    base = (title or "page").strip()
    base = re.sub(r"[^\w\-. ]+", "_", base)
    if not base:
        base = "page"
    return f"{base}.{ext}"


def export_html_to_docx_bytes(html: str, title: str) -> io.BytesIO:
    """
    Экспорт HTML -> .docx с сохранением базового форматирования,
    интервалов между абзацами и картинок (data:image/... и http/https).
    """
    doc = Document()

    # базовый стиль
    normal_style = doc.styles["Normal"]
    nf = normal_style.paragraph_format
    nf.space_before = Pt(0)
    nf.space_after = Pt(0)
    nf.line_spacing_rule = WD_LINE_SPACING.SINGLE

    # заголовок документа
    if title:
        h = doc.add_heading(title, level=1)
        h.paragraph_format.space_before = Pt(0)
        h.paragraph_format.space_after = Pt(6)

    soup = BeautifulSoup(html or "", "html.parser")
    body = soup.body or soup

    # 1. убираем <br> (чтобы не плодить лишние переносы)
    for br in body.find_all("br"):
        br.replace_with(" ")

    # 2. чистим \r и \n внутри текстовых узлов
    for text_node in body.find_all(string=True):
        cleaned = re.sub(r"[\r\n]+", " ", str(text_node))
        if cleaned != text_node:
            text_node.replace_with(cleaned)

    # ---------- вспомогательные функции ----------

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
        """
        Вставляет картинку из тега <img>:
        - data:image/...;base64,...
        - http(s)://...
        """
        src = (tag.get("src") or "").strip()
        if not src:
            return

        img_bytes: bytes | None = None

        # 1) data:image/...
        if src.startswith("data:image"):
            try:
                header, b64data = src.split(",", 1)
                img_bytes = base64.b64decode(b64data)
            except Exception:
                return

        # 2) обычный URL
        elif src.startswith("http://") or src.startswith("https://"):
            try:
                resp = requests.get(src, timeout=5)
                resp.raise_for_status()
                img_bytes = resp.content
            except Exception:
                # если не удалось скачать – просто пропускаем картинку
                return
        else:
            # относительные/другие схемы сейчас не поддерживаем
            return

        if not img_bytes:
            return

        img_stream = io.BytesIO(img_bytes)
        img_stream.seek(0)
        # ширину можно подкорректировать по вкусу
        try:
            doc.add_picture(img_stream, width=Inches(5))
        except Exception as e:
            # Логируем и просто пропускаем проблемное изображение (например, битый TIFF)
            logger.warning("Не удалось вставить картинку в DOCX: %s", e)
            return


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

        # картинка внутри абзаца
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
        """
        Создаёт один абзац Word из HTML-тега.
        Пустые абзацы (&nbsp;/пробелы) оставляем пустыми как визуальные интервалы.
        """
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

        # только «пустой» интервал – оставляем абзац пустым
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

    # ---------- основной проход ----------
    for child in body.children:
        handle_block(child)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def export_html_to_pdf_bytes(html: str, title: str) -> io.BytesIO:
    """HTML -> PDF через xhtml2pdf с корректной кириллицей."""
    # ensure_pdf_font()

    full_html = f"""
    <html>
      <head>
        <meta charset="utf-8" />
        <style>
          * {{
            font-family: "MyCyrillic";
          }}
          body {{
            font-family: "MyCyrillic";
          }}
          p, span, div {{
            font-family: "MyCyrillic";
          }}
        </style>
        <title>{title}</title>
      </head>
      <body>
        {html}
      </body>
    </html>
    """

    buf = io.BytesIO()
    pisa.CreatePDF(full_html, dest=buf, encoding="utf-8")
    buf.seek(0)
    return buf


def export_html_to_mht_bytes(html: str, title: str) -> io.BytesIO:
    """
    Экспорт страницы в MHTML (.mht), максимально совместимый с IE/Word.
    Один multipart/related, HTML в cp1251 (если не получится — в utf-8).
    """
    base_template = """<!DOCTYPE html>
<html>
  <head>
    <meta http-equiv="Content-Type" content="text/html; charset={charset}" />
    <title>{title}</title>
  </head>
  <body>
  {body}
  </body>
</html>
"""

    # сначала пытаемся сделать cp1251 — так IE/Word читают без проблем
    try:
        charset = "windows-1251"
        full_html = base_template.format(
            charset=charset,
            title=title or "",
            body=html or "",
        )
        html_bytes = full_html.encode(charset)
    except UnicodeEncodeError:
        # если вдруг есть символы вне cp1251 — падаем обратно на utf-8
        charset = "utf-8"
        full_html = base_template.format(
            charset=charset,
            title=title or "",
            body=html or "",
        )
        html_bytes = full_html.encode(charset)

    # quoted-printable
    qp_html = quopri.encodestring(html_bytes).decode("ascii")

    boundary = "----=_NextPart_" + uuid.uuid4().hex[:8]

    lines = [
        "MIME-Version: 1.0",
        f'Content-Type: multipart/related; boundary="{boundary}"',
        "",
        f"--{boundary}",
        f'Content-Type: text/html; charset="{charset}"',
        "Content-Transfer-Encoding: quoted-printable",
        "",
        qp_html,
        f"--{boundary}--",
        "",
    ]

    mht_str = "\r\n".join(lines)
    buf = io.BytesIO(mht_str.encode("ascii"))
    buf.seek(0)
    return buf


def main():
    st.set_page_config(
        layout="wide",
        page_title="ДФИП_Notes",
        initial_sidebar_state="expanded",   # ← добавили
    )


    # Убираем отступы сверху

    # st.markdown(
    #     """
    #     <style>
    #     /* Убираем верхний отступ основной области */
    #     .block-container {
    #         padding-top: 0rem !important;
    #     }
    #     </style>
    #     """,
    #     unsafe_allow_html=True
    # )

    # st.markdown(
    #     """
    #     <style>
    #     .block-container {
    #         padding-top: 0 !important;
    #         margin-top: 0 !important;
    #     }
    #     header[data-testid="stHeader"] {
    #         height: 0px !important;
    #     }
    #     </style>
    #     """,
    #     unsafe_allow_html=True
    # )


    st.markdown(
        """
        <style>
        /* Чуть уменьшаем верхний отступ, но не до нуля,
        чтобы контент не залезал под header */
        .block-container {
            padding-top: 0.4rem !important;
        }

        /* Делаем header более компактным, но не трогаем min-height */
        header[data-testid="stHeader"] {
            padding-top: 0;
            padding-bottom: 0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )




    # Кастомная ширина сайдбара
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] {
            width: 330px;         /* начальная ширина */
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    ensure_db_credentials()

    users_df = list_users()
    user_records = list(users_df.itertuples(index=False))
    user_map = {row.login: row.full_name for row in user_records}
    user_dept_map = {row.login: row.department_id for row in user_records}
    registered_users = {row.login for row in user_records}
    login_options = [row.login for row in user_records]

    stored_login = st.session_state.get("current_user_login")
    db_user = st.session_state.get("db_credentials", {}).get("user")
    preferred_login: str | None = (
        stored_login or db_user or (login_options[0] if login_options else None)
    )

    # --- выбор текущего пользователя ---
    selected_login: str | None = preferred_login
    if selected_login:
        st.session_state["current_user_login"] = selected_login
    else:
        st.sidebar.info("Нет доступных пользователей.")
        return


    departments_df = get_departments()
    department_map = {
        row.department_id: row.name_department
        for row in departments_df.itertuples(index=False)
    }

    selected_department_id = "00"
    dept_records = (
        list(departments_df.itertuples(index=False)) if not departments_df.empty else []
    )

    forced_department_id = st.session_state.pop("force_department_id", None)

    if dept_records:
        # 1) если только что создавали книгу – форсим подразделение книги
        if forced_department_id:
            forced_row = next(
                (r for r in dept_records if r.department_id == forced_department_id),
                None,
            )
            if forced_row is not None:
                st.session_state["department_selector"] = forced_row

        # 2) если никакого форса нет и selectbox ещё не инициализирован –
        #    ставим по умолчанию подразделение пользователя,
        #    а если его нет – строку "Все" (id == "00")
        if "department_selector" not in st.session_state and not forced_department_id:
            default_row = None

            # подразделение пользователя из таблицы notes_users
            user_dep_id = user_dept_map.get(selected_login)

            if user_dep_id:
                default_row = next(
                    (r for r in dept_records if r.department_id == user_dep_id),
                    None,
                )

            # если пользователя нет в notes_users или dep_id не найден —
            # берём строку с department_id == "00" (Все),
            # а если вдруг её нет – первый элемент
            if default_row is None:
                default_row = next(
                    (r for r in dept_records if str(r.department_id) == "00"),
                    dept_records[0],
                )

            st.session_state["department_selector"] = default_row

        selected_department = st.sidebar.selectbox(
            "Подразделение",
            dept_records,
            format_func=lambda row: row.name_department,
            key="department_selector",
        )
        selected_department_id = selected_department.department_id

    st.session_state["current_department_id"] = selected_department_id


    # --- поле поиска + кнопка очистки ---

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
        st.button(
            "✕",
            key="clear_page_search",
            help="Очистить поиск",
            on_click=_clear_page_search,
        )

    search_raw = (search_raw or "").strip()
    search_tags_only = search_raw.startswith("#")
    search_text = search_raw[1:].strip() if search_tags_only else search_raw

    # имя + отчество
    welcome_name = _name_patronymic(user_map.get(selected_login), selected_login)

    # подразделение пользователя
    user_dep_id = user_dept_map.get(selected_login)
    user_dep_name = department_map.get(user_dep_id, "") if user_dep_id else ""

    dep_prefix = f"[{user_dep_name}] " if user_dep_name else ""

    #st.markdown(f"**Добро пожаловать:** {welcome_name}{'   '}{dep_prefix}")

    current_user_can_create_notebook = selected_login in registered_users

    # --- список книг пользователя ---
    notebooks_df = get_notebooks(selected_login, user_dep_id)

    # книги, где пользователь является владельцем (для диалога перемещения/копирования)
    owned_notebooks_df = get_owned_notebooks(selected_login)


    # Отдельный DataFrame для фильтрации поиска по department_id
    filtered_notebooks_df = notebooks_df.copy()

    current_department_id: str = st.session_state.get("current_department_id", "00")

    if current_department_id != "00":
        prefix = current_department_id.strip()
        if prefix:
            if "." not in prefix:
                prefix = prefix + "."
            mask = filtered_notebooks_df["department_id"].astype(str).str.startswith(
                prefix
            )
            filtered_notebooks_df = filtered_notebooks_df[mask]

    selected_notebook_id: int | None = None
    selected_section_id: int | None = None
    selected_notebook_row: pd.Series | None = None
    can_edit_notebook: bool = False

    notebook_records = list(filtered_notebooks_df.itertuples(index=False))
    top_col1, top_col2, top_col3 = st.columns([5, 1, 5])
#, top_col4

    # --- диалог "Новая книга" ---
    if current_user_can_create_notebook:

        @st.dialog("Новая книга", width="small")
        def new_notebook_dialog():
            new_nb_name = st.text_input(
                "Название новой книги",
                key="new_notebook_name_modal",
            )
            create_clicked = st.button(
                "Создать книгу",
                key="create_notebook_btn_modal",
            )
            if create_clicked:
                user_department_id = user_dept_map.get(selected_login)
                new_nb_id = create_notebook(
                    new_nb_name, selected_login, user_department_id
                )

                if user_department_id:
                    st.session_state["force_department_id"] = user_department_id
                st.session_state["force_notebook_id"] = new_nb_id

                st.success("Книга создана")
                st.rerun()

    # ---------- COL1: выбор книги + кнопки "+" и "!" ----------
    with top_col1:
        select_col, plus_col, info_col = st.columns([14, 2, 2])

        with select_col:
            if notebook_records:

                forced_nb_id = st.session_state.pop("force_notebook_id", None)
                if forced_nb_id is not None:
                    target_row = next(
                        (r for r in notebook_records if r.id == forced_nb_id), None
                    )
                    if target_row is not None:
                        st.session_state["notebook_selector"] = target_row

                def _nb_label(row):
                    dept_id = getattr(row, "department_id", None)
                    dept_name = department_map.get(dept_id, "") if dept_id else ""
                    dept_txt = f" [{dept_name}]" if dept_name else ""

                    mode = ACCESS_LABELS.get(int(getattr(row, "closed", 0)), "Все")
                    if mode == "Все":
                        closed_txt = ""
                    elif mode == "Сотрудники подразделения":
                        closed_txt = " (только подразделение)"
                    else:
                        closed_txt = " (только владельцы)"

                    return f"{row.name}{dept_txt}{closed_txt}"
                
                st.markdown("###### ")
                st.markdown("###### Записная книга")

                # st.markdown(
                #     """
                #     <div style="
                #         position: relative;
                #         z-index: 1;
                #         font-weight: 600;
                #         font-size: 0.9rem;
                #         margin-bottom: 0.25rem;
                #         color: #111111;
                #     ">
                #         Записная книга
                #     </div>
                #     """,
                #     unsafe_allow_html=True,
                # )
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

                # права на редактирование книги
                can_edit_notebook = bool(
                    is_notebook_owner(selected_notebook_id, selected_login)
                )
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

                dept_id = selected_notebook_row.get("department_id", "")
                dept_value = department_map.get(dept_id, "не задано")

                closed_mode_current = int(selected_notebook_row.closed or 0)
                st.caption(f"Подразделение: {dept_value}")
                st.caption(f"Владельцы: {owners_text}")
                st.caption(
                    "Доступ на чтение: "
                    + ACCESS_LABELS.get(closed_mode_current, "Все")
                )

                owner_logins = set(owners_df["login"].tolist())

                with st.form(f"access_form_{selected_notebook_id}"):
                    labels = list(ACCESS_LABELS.values())
                    current_label = ACCESS_LABELS.get(
                        closed_mode_current, "Все"
                    )
                    try:
                        current_index = labels.index(current_label)
                    except ValueError:
                        current_index = 0

                    selected_label = st.selectbox(
                        "Доступ на чтение книги",
                        options=labels,
                        index=current_index,
                        key=f"closed_mode_{selected_notebook_id}",
                    )

                    new_closed_value = next(
                        k for k, v in ACCESS_LABELS.items() if v == selected_label
                    )

                    selectable_users = [
                        login for login in login_options
                        if login not in owner_logins
                    ]
                    new_owner_login = st.selectbox(
                        "Добавить владельца книги",
                        options=[""] + selectable_users,
                        format_func=lambda login: "—"
                        if login == ""
                        else f"{user_map.get(login, login)} ({login})",
                        key=f"add_owner_{selected_notebook_id}",
                    )

                    submitted = st.form_submit_button("Сохранить доступы")
                    if submitted:
                        set_notebook_closed(selected_notebook_id, new_closed_value)
                        if new_owner_login:
                            add_notebook_owner(selected_notebook_id, new_owner_login)
                        st.success("Доступы обновлены")
                        st.rerun()

        # кнопка "+" — создать новую книгу
        if current_user_can_create_notebook:
            with plus_col:
                st.markdown("###### ")
                st.markdown("###### ")
                if st.button(
                    "➕",
                    key="open_new_notebook_dialog",
                    help="Создать новую книгу",
                    use_container_width=True,
                ):
                    new_notebook_dialog()

        # кнопка "!" — права доступа на книгу
        if selected_notebook_id is not None and can_edit_notebook:
            with info_col:
                st.markdown("###### ")
                st.markdown("###### ")
                if st.button(
                    "!",
                    key="open_notebook_access_dialog",
                    help="Права доступа на книгу",
                    use_container_width=True,
                ):
                    notebook_access_dialog()




    # флаг прав на редактирование книги
    can_edit_notebook = bool(
        selected_notebook_id
        and is_notebook_owner(selected_notebook_id, selected_login)
    )

    # --- диалог "Новый раздел" ---
    if can_edit_notebook and selected_notebook_id is not None:

        @st.dialog("Новый раздел", width="small")
        def new_section_dialog():
            new_section_name = st.text_input(
                "Название раздела",
                key="new_section_name_modal",
            )
            create_clicked = st.button(
                "Создать раздел",
                key="create_section_btn_modal",
            )
            if create_clicked:
                create_section(
                    selected_notebook_id,
                    new_section_name,
                    selected_login,
                )
                st.success("Раздел создан")
                st.rerun()



    # ---------- COL2 + COL3: разделы ----------
    sections_df = pd.DataFrame()
    if selected_notebook_id is not None:
        sections_df = get_sections(selected_notebook_id)
        section_records = list(sections_df.itertuples(index=False))

        with top_col3:
            # поле "Раздел" + кнопка "+"
            select_col2, plus_col2 = st.columns([14, 2])

            with select_col2:
                st.markdown("###### ")
                st.markdown("###### Раздел")

                # st.markdown(
                #     """
                #     <div style="
                #         position: relative;
                #         z-index: 1;
                #         font-weight: 600;
                #         font-size: 0.9rem;
                #         margin-bottom: 0.25rem;
                #         color: #111111;
                #     ">
                #         Раздел
                #     </div>
                #     """,
                #     unsafe_allow_html=True,
                # )
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
                    if st.button(
                        "➕",
                        key="open_new_section_dialog",
                        help="Создать новый раздел",
                        use_container_width=True,
                    ):
                        new_section_dialog()



    # ---------- Загрузка страниц ----------
    dept_notebook_ids = filtered_notebooks_df["id"].astype(int).tolist()

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
                "notebook_closed",
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
                            insert_page_with_content(
                                selected_section_id, title, body_html, selected_login
                            )
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
            import_clicked = st.button(
                "Импорт страниц", use_container_width=True
            )

        if import_clicked:
            import_mht_dialog()

    if new_page_clicked:
        if not selected_section_id:
            st.sidebar.warning("Сначала создайте и выберите раздел.")
        else:
            new_page_id = create_page(
                section_id=selected_section_id,
                user_login=selected_login,
                title=None,
            )

            st.session_state["current_page_id"] = new_page_id
            st.session_state["force_page_id"] = new_page_id
            st.session_state["force_edit_page_id"] = new_page_id

            st.rerun()

    # ---------- Список страниц ----------
    df_display = pages_df[["id", "title"]].copy()
    df_display = df_display.reset_index(drop=True)

    gb = GridOptionsBuilder.from_dataframe(df_display)
    gb.configure_selection("single", use_checkbox=False)
    gb.configure_column("title", header_name="Страница", width=220)
    gb.configure_column("id", header_name="ID", width=60, hide=True)

    force_page_id = st.session_state.pop("force_page_id", None)
    if force_page_id is not None and not df_display.empty:
        try:
            row_index = df_display.index[df_display["id"] == force_page_id][0]
            row_index = int(row_index)
            gb.configure_selection("single", pre_selected_rows=[row_index])
        except Exception:
            pass

    list_container = st.sidebar.container()
    with list_container:
        grid_response = AgGrid(
            df_display,
            gridOptions=gb.build(),
            enable_enterprise_modules=False,
            update_on=["selectionChanged"],
            height=500,
            fit_columns_on_grid_load=True,
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
        dept_name_for_page = (
            department_map.get(dept_id_for_page, "") if dept_id_for_page else ""
        )
        dept_prefix = f"[{dept_name_for_page}] " if dept_name_for_page else ""

        st.caption(
            f"{dept_prefix}{current_page['notebook_name']} - "
            f"{current_page['section_name']} - {current_page['title']}"
        )

        if current_tag:
            st.caption(f"Tag: {current_tag}")

        preview_html = f"""
        <style>
        .preview-body *,
        .preview-body p,
        .preview-body li {{
            line-height: 1.15 !important;
        }}
        .preview-body p {{
            margin: 0.2em 0 !important;
        }}
        </style>
        <div class="preview-body">
            {current_html or "<p><em>Нет содержимого</em></p>"}
        </div>
        """
        components.html(
            preview_html,
            height=520,
            scrolling=True,
        )





        forced_edit_page_id = st.session_state.pop("force_edit_page_id", None)
        edit_key = f"edit_mode_{page_id}"

        if can_edit_notebook and forced_edit_page_id == page_id:
            st.session_state[edit_key] = True

        # --- кнопка редактирования + экспорт + перемещение/копирование ---
        col1, col2, col3, col4 = st.columns([2, 1, 2, 2])

        # col1 — чекбокс "Редактировать страницу"
        edit_mode = False
        with col1:
            if can_edit_notebook:
                edit_mode = st.checkbox(
                    "Редактировать страницу",
                    value=st.session_state.get(edit_key, False),
                    key=edit_key,
                )
            else:
                st.caption("Просмотр (редактирование недоступно)")

        # col2 — пока пустая

        # col3 — Expander "Экспорт"
        with col3:
            with st.expander("Экспорт", expanded=False):
                safe_title = current_title or f"Страница_{page_id}"

                docx_bytes = export_html_to_docx_bytes(current_html, safe_title)
                st.download_button(
                    "Экспорт в .docx",
                    data=docx_bytes,
                    file_name=_safe_filename(safe_title, "docx"),
                    mime=(
                        "application/"
                        "vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ),
                )

                pdf_bytes = export_html_to_pdf_bytes(current_html, safe_title)
                st.download_button(
                    "Экспорт в .pdf",
                    data=pdf_bytes,
                    file_name=_safe_filename(safe_title, "pdf"),
                    mime="application/pdf",
                )

        # col4 — Expander "Переместить или скопировать"
        with col4:
            # expander теперь виден ВСЕГДА, даже при отсутствии прав на редактирование
            with st.expander("Переместить или скопировать", expanded=False):
                st.write(f"Текущая страница: **{current_title or f'ID {page_id}'}**")

                # Книги, где текущий пользователь является владельцем (ограничение назначения)
                if owned_notebooks_df.empty:
                    st.info(
                        "У вас нет записных книг, в которых вы являетесь владельцем. "
                        "Копирование и перемещение недоступны."
                    )
                else:
                    dest_nb_records = list(owned_notebooks_df.itertuples(index=False))

                    # по умолчанию — текущая книга, если она тоже в owned_notebooks;
                    # иначе — первая из списка владельца
                    cur_nb_id = int(current_page["notebook_id"])
                    default_nb = next(
                        (r for r in dest_nb_records if int(r.id) == cur_nb_id),
                        dest_nb_records[0],
                    )
                    nb_index = dest_nb_records.index(default_nb)

                    dest_notebook = st.selectbox(
                        "Записная книга (для копирования/перемещения)",
                        dest_nb_records,
                        format_func=lambda r: r.name,
                        index=nb_index,
                        key=f"move_nb_{page_id}",
                    )

                    # разделы выбранной книги
                    dest_sections_df = get_sections(int(dest_notebook.id))
                    dest_sec_records = list(dest_sections_df.itertuples(index=False))
                    if not dest_sec_records:
                        st.warning(
                            "В выбранной книге нет разделов. "
                            "Сначала создайте раздел."
                        )
                    else:
                        # по умолчанию — раздел с таким же id, если он есть в этой книге,
                        # иначе первый раздел книги
                        cur_sec_id = int(current_page["section_id"])
                        default_sec = next(
                            (r for r in dest_sec_records if int(r.id) == cur_sec_id),
                            dest_sec_records[0],
                        )
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
                                move_clicked = st.button(
                                    "Переместить",
                                    type="primary",
                                    key=f"btn_move_{page_id}",
                                    use_container_width=True,
                                )
                            else:
                                st.caption("Перемещение недоступно\n(только владельцы книги)")

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

                        # --- ЛОГИКА ПЕРЕМЕЩЕНИЯ (только владельцы книги) ---
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

                        # --- ЛОГИКА КОПИРОВАНИЯ (доступно всем пользователям) ---
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






        if can_edit_notebook and edit_mode:
            st.markdown("### Редактирование")

            title_key = f"title_{page_id}"
            new_title = st.text_input(
                "Название страницы",
                value=current_title,
                key=title_key,
            )

            if forced_edit_page_id == page_id:
                components.html(
                    """
                    <script>
                    setTimeout(function () {
                      const doc = window.parent.document;
                      const labels = Array.from(doc.querySelectorAll('label'));
                      const label = labels.find(
                        l => l.textContent.trim() === 'Название страницы'
                      );
                      if (label) {
                        const input = label.parentElement.querySelector('input');
                        if (input) {
                          input.focus();
                          input.select();
                        }
                      }
                    }, 50);
                    </script>
                    """,
                    height=0,
                )

            new_tag = st.text_input(
                "Тег",
                value=current_page.get("tag") or "",
                key=f"tag_{page_id}",
            )
            editable_html = strip_data_uri_images(current_html)
            quill_html = (
                st_quill(
                    value=editable_html,
                    html=True,
                    placeholder="Введите текст...",
                    key=f"quill_{page_id}",
                )
                or ""
            )

            confirm_delete = st.checkbox(
                "Подтвердить удаление",
                key=f"confirm_delete_{page_id}",
            )
            cols = st.columns([2, 1])
            if cols[0].button("Сохранить изменения", key=f"save_{page_id}"):
                update_page(page_id, new_title, quill_html, new_tag)
                st.success("Страница обновлена")
                st.rerun()
            if cols[1].button(
                "Удалить страницу", key=f"delete_{page_id}", type="secondary"
            ):
                if confirm_delete:
                    delete_page(page_id)
                    st.success("Страница удалена")
                    st.rerun()
                else:
                    st.warning("Поставьте галочку для подтверждения.")

        elif not can_edit_notebook:
            st.info("У вас права только на просмотр этой записной книжки.")


if __name__ == "__main__":
    main()
