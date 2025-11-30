import base64
import email
import os
import urllib.parse
import streamlit.components.v1 as components
from datetime import datetime

import duckdb
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from st_aggrid import AgGrid, GridOptionsBuilder
from streamlit_quill import st_quill

DB_PATH = "onenote.duckdb"
DEFAULT_USER_LOGIN = "owner"
DEFAULT_USER_NAME = "Owner"


@st.cache_resource
def get_connection():
    con = duckdb.connect(DB_PATH)
    init_db(con)
    return con


def init_db(con: duckdb.DuckDBPyConnection) -> None:
    """Create the OneNote-like schema, apply simple migrations, and seed demo data."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            login TEXT PRIMARY KEY,
            full_name TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS notebooks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT REFERENCES users(login),
            closed BOOLEAN DEFAULT FALSE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY,
            notebook_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT REFERENCES users(login)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY,
            section_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            body_html TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT REFERENCES users(login)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS notebook_owners (
            notebook_id INTEGER NOT NULL,
            user_login TEXT NOT NULL,
            PRIMARY KEY (notebook_id, user_login),
            FOREIGN KEY (notebook_id) REFERENCES notebooks(id),
            FOREIGN KEY (user_login) REFERENCES users(login)
        )
        """
    )

    # Lightweight migrations for existing databases.
    con.execute("ALTER TABLE notebooks ADD COLUMN IF NOT EXISTS closed BOOLEAN DEFAULT FALSE")
    con.execute("ALTER TABLE notebooks ADD COLUMN IF NOT EXISTS created_by TEXT")
    con.execute("ALTER TABLE sections ADD COLUMN IF NOT EXISTS created_by TEXT")
    con.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS created_by TEXT")

    con.execute(
        "INSERT OR IGNORE INTO users (login, full_name) VALUES (?, ?)",
        [DEFAULT_USER_LOGIN, DEFAULT_USER_NAME],
    )
    con.execute(
        "UPDATE notebooks SET created_by = ? WHERE created_by IS NULL",
        [DEFAULT_USER_LOGIN],
    )
    con.execute("UPDATE notebooks SET closed = FALSE WHERE closed IS NULL")
    con.execute(
        "UPDATE sections SET created_by = ? WHERE created_by IS NULL",
        [DEFAULT_USER_LOGIN],
    )
    con.execute(
        "UPDATE pages SET created_by = ? WHERE created_by IS NULL",
        [DEFAULT_USER_LOGIN],
    )
    con.execute(
        """
        INSERT OR IGNORE INTO notebook_owners (notebook_id, user_login)
        SELECT id, COALESCE(created_by, ?) FROM notebooks
        """,
        [DEFAULT_USER_LOGIN],
    )

    if con.execute("SELECT COUNT(*) FROM notebooks").fetchone()[0] > 0:
        con.commit()
        return

    now = datetime.now()
    notebooks = ["Личное", "Работа"]
    notebook_ids: dict[str, int] = {}
    for idx, name in enumerate(notebooks, start=1):
        con.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at, created_by, closed) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [idx, name, now, now, DEFAULT_USER_LOGIN, False],
        )
        notebook_ids[name] = idx
        con.execute(
            "INSERT OR IGNORE INTO notebook_owners (notebook_id, user_login) VALUES (?, ?)",
            [idx, DEFAULT_USER_LOGIN],
        )

    sections = [
        ("Идеи", notebook_ids["Личное"]),
        ("Путешествия", notebook_ids["Личное"]),
        ("Проекты", notebook_ids["Работа"]),
        ("Встречи", notebook_ids["Работа"]),
    ]
    section_ids: dict[str, int] = {}
    for idx, (name, nb_id) in enumerate(sections, start=1):
        con.execute(
            "INSERT INTO sections (id, notebook_id, name, created_at, updated_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [idx, nb_id, name, now, now, DEFAULT_USER_LOGIN],
        )
        section_ids[name] = idx

    pages = [
        (
            section_ids["Идеи"],
            "Планы на отпуск",
            "<h2>Планы на отпуск</h2><p>Подборка мест и активности для июля.</p>",
        ),
        (
            section_ids["Путешествия"],
            "Прага",
            "<p><strong>Маршрут</strong>: Старый город → Карлов мост → Пражский град.</p>",
        ),
        (
            section_ids["Проекты"],
            "Dashboard v2",
            "<h3>Задачи</h3><ul><li>Подключить DuckDB</li><li>Перенести UI на Streamlit</li></ul>",
        ),
        (
            section_ids["Встречи"],
            "Команда маркетинга",
            "<p>Пункты обсуждения:<ol><li>Запуск рекламной кампании</li><li>Новые метрики</li></ol></p>",
        ),
    ]

    for idx, (section_id, title, html) in enumerate(pages, start=1):
        con.execute(
            "INSERT INTO pages (id, section_id, title, body_html, created_at, updated_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [idx, section_id, title, html, now, now, DEFAULT_USER_LOGIN],
        )

    con.commit()


def list_users(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(
        "SELECT login, full_name FROM users ORDER BY full_name"
    ).df()


def create_user(con: duckdb.DuckDBPyConnection, login: str, full_name: str) -> str:
    normalized = login.strip().lower()
    if not normalized:
        raise ValueError("Логин не может быть пустым")
    name = full_name.strip() or normalized
    con.execute(
        "INSERT OR IGNORE INTO users (login, full_name) VALUES (?, ?)",
        [normalized, name],
    )
    con.commit()
    return normalized


def add_notebook_owner(con: duckdb.DuckDBPyConnection, notebook_id: int, user_login: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO notebook_owners (notebook_id, user_login) VALUES (?, ?)",
        [notebook_id, user_login],
    )
    con.commit()


def set_notebook_closed(con: duckdb.DuckDBPyConnection, notebook_id: int, closed: bool) -> None:
    con.execute("UPDATE notebooks SET closed = ?, updated_at = ? WHERE id = ?", [closed, datetime.now(), notebook_id])
    con.commit()


def is_notebook_owner(con: duckdb.DuckDBPyConnection, notebook_id: int, user_login: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM notebook_owners WHERE notebook_id = ? AND user_login = ? LIMIT 1",
        [notebook_id, user_login],
    ).fetchone()
    return bool(row)


def get_notebook_owners(con: duckdb.DuckDBPyConnection, notebook_id: int) -> pd.DataFrame:
    return con.execute(
        """
        SELECT o.user_login AS login, u.full_name
        FROM notebook_owners o
        LEFT JOIN users u ON u.login = o.user_login
        WHERE o.notebook_id = ?
        ORDER BY COALESCE(u.full_name, o.user_login)
        """,
        [notebook_id],
    ).df()


def get_notebooks(con: duckdb.DuckDBPyConnection, user_login: str) -> pd.DataFrame:
    return con.execute(
        """
        SELECT id, name, created_at, updated_at, created_by, closed
        FROM notebooks
        WHERE closed = FALSE
           OR id IN (SELECT notebook_id FROM notebook_owners WHERE user_login = ?)
        ORDER BY name
        """,
        [user_login],
    ).df()


def get_sections(con: duckdb.DuckDBPyConnection, notebook_id: int | None) -> pd.DataFrame:
    query = "SELECT id, notebook_id, name, created_at, updated_at, created_by FROM sections"
    params: list[int] = []
    if notebook_id:
        query += " WHERE notebook_id = ?"
        params.append(notebook_id)
    query += " ORDER BY name"
    return con.execute(query, params).df()


def load_pages_df(
    con: duckdb.DuckDBPyConnection,
    notebook_id: int | None,
    section_id: int | None,
) -> pd.DataFrame:
    query = """
        SELECT
            p.id,
            p.title,
            p.body_html,
            p.created_at,
            p.updated_at,
            p.created_by,
            s.id AS section_id,
            s.name AS section_name,
            n.id AS notebook_id,
            n.name AS notebook_name,
            n.closed AS notebook_closed
        FROM pages p
        JOIN sections s ON p.section_id = s.id
        JOIN notebooks n ON s.notebook_id = n.id
        WHERE 1=1
    """
    params: list[int] = []
    if notebook_id:
        query += " AND n.id = ?"
        params.append(notebook_id)
    if section_id:
        query += " AND s.id = ?"
        params.append(section_id)

    query += " ORDER BY p.updated_at DESC, p.id DESC"
    return con.execute(query, params).df()


def create_notebook(con: duckdb.DuckDBPyConnection, name: str, user_login: str) -> int:
    cleaned = name.strip() or "Новая записная книжка"
    now = datetime.now()
    notebook_id = (
        con.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM notebooks"
        ).fetchone()[0]
    )
    con.execute(
        "INSERT INTO notebooks (id, name, created_at, updated_at, created_by, closed) VALUES (?, ?, ?, ?, ?, ?)",
        [notebook_id, cleaned, now, now, user_login, False],
    )
    add_notebook_owner(con, notebook_id, user_login)
    con.commit()
    return notebook_id


def create_section(con: duckdb.DuckDBPyConnection, notebook_id: int, name: str, user_login: str) -> int:
    cleaned = name.strip() or "Новый раздел"
    now = datetime.now()
    section_id = (
        con.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM sections"
        ).fetchone()[0]
    )
    con.execute(
        "INSERT INTO sections (id, notebook_id, name, created_at, updated_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [section_id, notebook_id, cleaned, now, now, user_login],
    )
    con.commit()
    return section_id


def create_page(con: duckdb.DuckDBPyConnection, section_id: int, user_login: str) -> int:
    now = datetime.now()
    page_id = (
        con.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM pages"
        ).fetchone()[0]
    )
    con.execute(
        "INSERT INTO pages (id, section_id, title, body_html, created_at, updated_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [page_id, section_id, "Новая страница", "", now, now, user_login],
    )
    con.commit()
    return page_id


def insert_page_with_content(
    con: duckdb.DuckDBPyConnection, section_id: int, title: str, body_html: str, user_login: str
) -> int:
    """Insert a page with provided content into a section."""
    now = datetime.now()
    page_id = (
        con.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM pages"
        ).fetchone()[0]
    )
    con.execute(
        "INSERT INTO pages (id, section_id, title, body_html, created_at, updated_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [page_id, section_id, title.strip() or "Untitled", body_html, now, now, user_login],
    )
    con.commit()
    return page_id


def update_page(
    con: duckdb.DuckDBPyConnection, page_id: int, title: str, body_html: str
) -> None:
    now = datetime.now()
    con.execute(
        "UPDATE pages SET title = ?, body_html = ?, updated_at = ? WHERE id = ?",
        [title.strip() or "Без названия", body_html, now, page_id],
    )
    con.commit()


def delete_page(con: duckdb.DuckDBPyConnection, page_id: int) -> None:
    con.execute("DELETE FROM pages WHERE id = ?", [page_id])
    con.commit()


def html_to_body(text: str, fallback_title: str):
    """Extract title and body HTML from raw HTML text."""
    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else fallback_title
    body = str(soup.body or soup)
    return title, body


def parse_mht_to_html(data: bytes, filename: str):
    """Parse .mht, inline referenced resources, return (title, body_html)."""
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
        raise ValueError(f"No HTML part found in {filename}")

    def norm(val: str) -> str:
        val = urllib.parse.unquote(val or "").strip()
        val = val.replace("\\", "/")
        if val.lower().startswith("cid:"):
            val = "cid:" + val[4:]
        return val

    # Build map from possible src values to data URLs
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
    for tag in soup.find_all(src=True):
        src_val = tag.get("src", "")
        lookup = norm(src_val)
        if lookup in src_map:
            tag["src"] = src_map[lookup]
        else:
            # Try basename match
            basename = os.path.basename(lookup)
            if basename in src_map:
                tag["src"] = src_map[basename]

    title, body_html = html_to_body(str(soup), filename.rsplit(".", 1)[0])
    return title, body_html


def strip_data_uri_images(html: str) -> str:
    """Remove data URI image sources to avoid huge payloads in the editor."""
    soup = BeautifulSoup(html or "", "html.parser")
    changed = False
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src.startswith("data:"):
            img["src"] = ""
            changed = True
    return str(soup) if changed else html


def main():
    st.set_page_config(
        layout="wide",
        page_title="OneNote",
    )

    con = get_connection()

    users_df = list_users(con)
    user_records = list(users_df.itertuples(index=False))
    user_map = {row.login: row.full_name for row in user_records}
    login_options = [row.login for row in user_records]

    stored_login = st.session_state.get("current_user_login")
    if stored_login and stored_login not in login_options:
        stored_login = None

    with st.sidebar.expander("Пользователь", expanded=True):
        selected_login: str | None = None
        if login_options:
            default_idx = login_options.index(stored_login) if stored_login in login_options else 0
            selected_login = st.selectbox(
                "Текущий пользователь",
                options=login_options,
                index=default_idx,
                format_func=lambda login: f"{user_map.get(login, login)} ({login})",
                key="current_user_selector",
            )
            st.session_state["current_user_login"] = selected_login
        else:
            st.info("Добавьте первого пользователя ниже.")

    with st.sidebar.expander("? Новый пользователь", expanded=not login_options):
        new_login = st.text_input("Логин", key="new_user_login")
        new_full_name = st.text_input("Полное имя", key="new_user_full_name")
        if st.button("Создать пользователя", key="create_user_btn"):
            try:
                created_login = create_user(con, new_login, new_full_name)
                st.session_state["current_user_login"] = created_login
                st.success("Пользователь создан")
                st.rerun()
            except ValueError as exc:
                st.warning(str(exc))

    if not selected_login:
        st.warning("Выберите или создайте пользователя для работы с записными книжками.")
        return

    notebooks_df = get_notebooks(con, selected_login)
    selected_notebook_id: int | None = None
    selected_section_id: int | None = None
    selected_notebook_row: pd.Series | None = None

    notebook_records = list(notebooks_df.itertuples(index=False))
    if notebook_records:
        selected_notebook = st.sidebar.selectbox(
            "Выберите книжку",
            notebook_records,
            format_func=lambda row: f"{row.name}{' (закрыта)' if row.closed else ''}",
        )
        selected_notebook_id = int(selected_notebook.id)
        selected_notebook_row = notebooks_df[notebooks_df["id"] == selected_notebook_id].iloc[0]
    else:
        st.sidebar.info("Пока записных книжек нет.")

    with st.sidebar.expander("? Новая книжка", expanded=False):
        new_nb_name = st.text_input("Название", key="new_notebook_name")
        if st.button("Создать записную книжку", key="create_notebook_btn"):
            create_notebook(con, new_nb_name, selected_login)
            st.rerun()

    can_edit_notebook = False
    owners_df = pd.DataFrame()
    if selected_notebook_id is not None:
        can_edit_notebook = is_notebook_owner(con, selected_notebook_id, selected_login)
        owners_df = get_notebook_owners(con, selected_notebook_id)
        owners_text = ", ".join(
            f"{row.full_name or row.login} ({row.login})" for row in owners_df.itertuples(index=False)
        ) or "нет владельцев"
        st.sidebar.caption(f"Владельцы: {owners_text}")
        st.sidebar.caption(f"Статус: {'закрыта' if selected_notebook_row.closed else 'открыта'}")

        if can_edit_notebook:
            owner_logins = set(owners_df["login"].tolist())
            with st.sidebar.expander("Права доступа", expanded=False):
                with st.form(f"access_form_{selected_notebook_id}"):
                    closed_value = st.checkbox(
                        "Закрытая",
                        value=bool(selected_notebook_row.closed),
                        key=f"closed_flag_{selected_notebook_id}",
                    )
                    selectable_users = [login for login in login_options if login not in owner_logins]
                    new_owner_login = st.selectbox(
                        "Добавить владельца",
                        options=[""] + selectable_users,
                        format_func=lambda login: "—" if login == "" else f"{user_map.get(login, login)} ({login})",
                        key=f"add_owner_{selected_notebook_id}",
                    )
                    submitted = st.form_submit_button("Сохранить настройки")
                    if submitted:
                        set_notebook_closed(con, selected_notebook_id, closed_value)
                        if new_owner_login:
                            add_notebook_owner(con, selected_notebook_id, new_owner_login)
                        st.success("Права обновлены")
                        st.rerun()

    sections_df = pd.DataFrame()
    if selected_notebook_id:
        sections_df = get_sections(con, selected_notebook_id)
        section_records = list(sections_df.itertuples(index=False))
        if section_records:
            selected_section = st.sidebar.selectbox(
                "Раздел",
                section_records,
                format_func=lambda row: row.name,
            )
            selected_section_id = int(selected_section.id)
        else:
            st.sidebar.warning("В книжке нет разделов.")

        if can_edit_notebook:
            with st.sidebar.expander("? Новый раздел", expanded=False):
                new_section_name = st.text_input("Название раздела", key="new_section_name")
                if st.button("Создать раздел", key="create_section_btn"):
                    create_section(con, selected_notebook_id, new_section_name, selected_login)
                    st.rerun()
        else:
            st.sidebar.info("Нет прав на создание разделов в этой книжке.")

        with st.sidebar.expander("Import .mht pages", expanded=False):
            if not selected_section_id:
                st.info("Выберите раздел для импорта.")
            elif not can_edit_notebook:
                st.info("Импорт доступен только владельцам книжки.")
            else:
                uploaded = st.file_uploader(
                    "Загрузите .mht файлы", type=["mht"], accept_multiple_files=True, key="mht_files"
                )
                if uploaded and st.button("Импортировать .mht", key="import_mht_btn"):
                    imported = 0
                    errors = []
                    for file in uploaded:
                        try:
                            title, body_html = parse_mht_to_html(file.getvalue(), file.name)
                            insert_page_with_content(con, selected_section_id, title, body_html, selected_login)
                            imported += 1
                        except Exception as exc:
                            errors.append(f"{file.name}: {exc}")
                    if imported:
                        st.success(f"Импортировано {imported} страниц")
                        st.rerun()
                    if errors:
                        st.warning(";\n".join(errors))

    pages_df = load_pages_df(con, selected_notebook_id, selected_section_id)

    if selected_section_id and can_edit_notebook and st.sidebar.button("? Новая страница"):
        new_page_id = create_page(con, selected_section_id, selected_login)
        st.sidebar.success(f"Новая страница ID = {new_page_id}")
        st.rerun()
    elif selected_section_id and not can_edit_notebook:
        st.sidebar.info("Нет прав на создание страниц в этой книжке.")

    df_display = pages_df[["id", "title"]].copy()
    gb = GridOptionsBuilder.from_dataframe(df_display)
    gb.configure_selection("single", use_checkbox=False)
    gb.configure_column("title", header_name="Страница", width=220)
    gb.configure_column("id", header_name="ID", width=60, hide=True)
    list_container = st.sidebar.container()
    with list_container:
        grid_response = AgGrid(
            df_display,
            gridOptions=gb.build(),
            enable_enterprise_modules=False,
            update_on=["selectionChanged"],
            height=400,
            fit_columns_on_grid_load=True,
        )

    selected_rows = grid_response.get("selected_rows", [])
    if isinstance(selected_rows, pd.DataFrame):
        selected_rows = selected_rows.to_dict("records")

    if selected_rows:
        row = selected_rows[0]
        page_id = int(row["id"])
        current_page = pages_df[pages_df["id"] == page_id].iloc[0]
        current_title = current_page.get("title", "")
        current_html = current_page.get("body_html") or ""
        st.caption(
            f"{current_page['notebook_name']} • {current_page['section_name']} • {current_page['title']}"
        )

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

        if can_edit_notebook:
            edit_mode = st.checkbox(
                "Редактировать страницу",
                value=False,
                key=f"edit_mode_{page_id}",
            )

            if edit_mode:
                st.markdown("### Редактирование")
                new_title = st.text_input(
                    "Название страницы",
                    value=current_title,
                    key=f"title_{page_id}",
                )
                editable_html = strip_data_uri_images(current_html)
                quill_html = st_quill(
                    value=editable_html,
                    html=True,
                    placeholder="Введите текст...",
                    key=f"quill_{page_id}",
                ) or ""

                confirm_delete = st.checkbox(
                    "Подтвердить удаление",
                    key=f"confirm_delete_{page_id}",
                )
                cols = st.columns([2, 1])
                if cols[0].button("?? Сохранить изменения", key=f"save_{page_id}"):
                    update_page(con, page_id, new_title, quill_html)
                    st.success("Страница обновлена")
                    st.rerun()
                if cols[1].button("?? Удалить страницу", key=f"delete_{page_id}", type="secondary"):
                    if confirm_delete:
                        delete_page(con, page_id)
                        st.success("Страница удалена")
                        st.rerun()
                    else:
                        st.warning("Поставьте галочку для подтверждения.")
        else:
            st.info("У вас права только на просмотр этой записной книжки.")
    else:
        st.info("Выберите страницу слева, чтобы просмотреть и редактировать её.")


if __name__ == "__main__":
    main()

