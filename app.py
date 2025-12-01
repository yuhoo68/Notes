import base64
import email
import os
import urllib.parse
from datetime import datetime

import streamlit.components.v1 as components
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from st_aggrid import AgGrid, GridOptionsBuilder
from streamlit_quill import st_quill

import config
from src.database_utils_DRP import get_execute, get_fetch, test_connection

SCHEMA = "sbx_dfip_ocpp"
USERS_TABLE = f"{SCHEMA}.notes_users"
NOTEBOOKS_TABLE = f"{SCHEMA}.notes_notebooks"
SECTIONS_TABLE = f"{SCHEMA}.notes_sections"
PAGES_TABLE = f"{SCHEMA}.notes_pages"
OWNERS_TABLE = f"{SCHEMA}.notes_notebook_owners"


def _escape(val: str) -> str:
    """Minimal escaping for SQL string literals."""
    return (val or "").replace("'", "''")


def ensure_db_credentials() -> dict[str, str]:
    """Request DB creds once per session via modal dialog."""
    creds = st.session_state.get("db_credentials")
    if creds and creds.get("user") and creds.get("password"):
        st.session_state.setdefault("current_user_login", creds["user"])
        return creds

    @st.dialog("Подключение к базе", width="small")
    def _ask_credentials():
        st.write("Введите логин и пароль PostgreSQL.")
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
        SELECT login, full_name
        FROM {USERS_TABLE}
        ORDER BY COALESCE(full_name, login)
        """
    )


def create_user(login: str, full_name: str) -> str:
    normalized = login.strip().lower()
    if not normalized:
        raise ValueError("Логин не может быть пустым")
    name = full_name.strip() or normalized
    run_execute(
        f"""
        INSERT INTO {USERS_TABLE} (login, full_name)
        VALUES ('{_escape(normalized)}', '{_escape(name)}')
        ON CONFLICT (login) DO NOTHING
        """
    )
    return normalized


def add_notebook_owner(notebook_id: int, user_login: str) -> None:
    run_execute(
        f"""
        INSERT INTO {OWNERS_TABLE} (notebook_id, user_login)
        VALUES ({int(notebook_id)}, '{_escape(user_login)}')
        ON CONFLICT DO NOTHING
        """
    )


def set_notebook_closed(notebook_id: int, closed: bool) -> None:
    run_execute(
        f"""
        UPDATE {NOTEBOOKS_TABLE}
        SET closed = {'TRUE' if closed else 'FALSE'}, updated_at = NOW()
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


def get_notebooks(user_login: str) -> pd.DataFrame:
    return run_fetch_df(
        f"""
        SELECT id, name, created_at, updated_at, created_by, closed
        FROM {NOTEBOOKS_TABLE}
        WHERE closed = FALSE
           OR id IN (
                SELECT notebook_id
                FROM {OWNERS_TABLE}
                WHERE user_login = '{_escape(user_login)}'
           )
        ORDER BY name
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
            n.closed AS notebook_closed
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


def create_notebook(name: str, user_login: str) -> int:
    cleaned = name.strip() or "Новый блокнот"
    new_id = run_scalar(
        f"""
        INSERT INTO {NOTEBOOKS_TABLE} (name, created_by, closed)
        VALUES ('{_escape(cleaned)}', '{_escape(user_login)}', FALSE)
        RETURNING id
        """
    )
    if new_id is None:
        raise RuntimeError("Не удалось создать блокнот")
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


def create_page(section_id: int, user_login: str) -> int:
    new_id = run_scalar(
        f"""
        INSERT INTO {PAGES_TABLE} (section_id, title, tag, body_html, created_by)
        VALUES ({int(section_id)}, 'Новая страница', '', '', '{_escape(user_login)}')
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
    run_execute(f"DELETE FROM {PAGES_TABLE} WHERE id = {int(page_id)}")
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
        raise ValueError("Логин не может быть пустым")

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

    ensure_db_credentials()

    users_df = list_users()
    user_records = list(users_df.itertuples(index=False))
    user_map = {row.login: row.full_name for row in user_records}
    login_options = [row.login for row in user_records]

    stored_login = st.session_state.get("current_user_login")
    db_user = st.session_state.get("db_credentials", {}).get("user")
    if stored_login and stored_login not in login_options:
        stored_login = None
    if not stored_login and db_user in login_options:
        stored_login = db_user

    search_raw = st.sidebar.text_input("Поиск страниц", key="page_search", placeholder="#tag или текст").strip()
    search_tags_only = search_raw.startswith("#")
    search_text = search_raw[1:].strip() if search_tags_only else search_raw

    selected_login: str | None = None
    if login_options:
        selected_login = stored_login or login_options[0]
        st.session_state["current_user_login"] = selected_login
        st.sidebar.caption(f"User: {user_map.get(selected_login, selected_login)}")
    else:
        st.sidebar.info("No users available in the database.")
        return

    notebooks_df = get_notebooks(selected_login)
    allowed_notebook_ids = notebooks_df["id"].astype(int).tolist()
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
            create_notebook(new_nb_name, selected_login)
            st.rerun()

    can_edit_notebook = False
    owners_df = pd.DataFrame()
    if selected_notebook_id is not None:
        can_edit_notebook = is_notebook_owner(selected_notebook_id, selected_login)
        owners_df = get_notebook_owners(selected_notebook_id)
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
                        set_notebook_closed(selected_notebook_id, closed_value)
                        if new_owner_login:
                            add_notebook_owner(selected_notebook_id, new_owner_login)
                        st.success("Права обновлены")
                        st.rerun()

    sections_df = pd.DataFrame()
    if selected_notebook_id:
        sections_df = get_sections(selected_notebook_id)
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
                    create_section(selected_notebook_id, new_section_name, selected_login)
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
                            insert_page_with_content(selected_section_id, title, body_html, selected_login)
                            imported += 1
                        except Exception as exc:
                            errors.append(f"{file.name}: {exc}")
                    if imported:
                        st.success(f"Импортировано {imported} страниц")
                        st.rerun()
                    if errors:
                        st.warning(";\n".join(errors))

    pages_df = load_pages_df(
        selected_notebook_id,
        selected_section_id,
        allowed_notebook_ids,
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
            ]
        )

    if selected_section_id and can_edit_notebook and st.sidebar.button("? Новая страница"):
        new_page_id = create_page(selected_section_id, selected_login)
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
            f"{current_page['notebook_name']} - {current_page['section_name']} - {current_page['title']}"
        )
        if current_page.get("tag"):
            st.caption(f"Tag: {current_page['tag']}")
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
                new_tag = st.text_input(
                    "Тег",
                    value=current_page.get("tag") or "",
                    key=f"tag_{page_id}",
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
                    update_page(page_id, new_title, quill_html, new_tag)
                    st.success("Страница обновлена")
                    st.rerun()
                if cols[1].button("?? Удалить страницу", key=f"delete_{page_id}", type="secondary"):
                    if confirm_delete:
                        delete_page(page_id)
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
