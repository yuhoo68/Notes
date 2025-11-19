import streamlit.components.v1 as components
from datetime import datetime

import duckdb
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode
from streamlit_quill import st_quill

DB_PATH = "onenote.duckdb"


@st.cache_resource
def get_connection():
    con = duckdb.connect(DB_PATH)
    init_db(con)
    return con


def init_db(con: duckdb.DuckDBPyConnection) -> None:
    """Create the OneNote-like schema and seed demo data. """
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS notebooks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    if con.execute("SELECT COUNT(*) FROM notebooks").fetchone()[0] > 0:
        return

    now = datetime.now()
    notebooks = ["Личное", "Работа"]
    notebook_ids: dict[str, int] = {}
    for idx, name in enumerate(notebooks, start=1):
        con.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            [idx, name, now, now],
        )
        notebook_ids[name] = idx

    sections = [
        ("Идеи", notebook_ids["Личное"]),
        ("Путешествия", notebook_ids["Личное"]),
        ("Проекты", notebook_ids["Работа"]),
        ("Встречи", notebook_ids["Работа"]),
    ]
    section_ids: dict[str, int] = {}
    for idx, (name, nb_id) in enumerate(sections, start=1):
        con.execute(
            "INSERT INTO sections (id, notebook_id, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [idx, nb_id, name, now, now],
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
            "INSERT INTO pages (id, section_id, title, body_html, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [idx, section_id, title, html, now, now],
        )

    con.commit()


def get_notebooks(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(
        "SELECT id, name, created_at, updated_at FROM notebooks ORDER BY name"
    ).df()


def get_sections(con: duckdb.DuckDBPyConnection, notebook_id: int | None) -> pd.DataFrame:
    query = "SELECT id, notebook_id, name, created_at, updated_at FROM sections"
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
            s.id AS section_id,
            s.name AS section_name,
            n.id AS notebook_id,
            n.name AS notebook_name
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


def create_notebook(con: duckdb.DuckDBPyConnection, name: str) -> int:
    cleaned = name.strip() or "Новая записная книжка"
    now = datetime.now()
    notebook_id = (
        con.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM notebooks"
        ).fetchone()[0]
    )
    con.execute(
        "INSERT INTO notebooks (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        [notebook_id, cleaned, now, now],
    )
    con.commit()
    return notebook_id


def create_section(con: duckdb.DuckDBPyConnection, notebook_id: int, name: str) -> int:
    cleaned = name.strip() or "Новый раздел"
    now = datetime.now()
    section_id = (
        con.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM sections"
        ).fetchone()[0]
    )
    con.execute(
        "INSERT INTO sections (id, notebook_id, name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [section_id, notebook_id, cleaned, now, now],
    )
    con.commit()
    return section_id


def create_page(con: duckdb.DuckDBPyConnection, section_id: int) -> int:
    now = datetime.now()
    page_id = (
        con.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM pages"
        ).fetchone()[0]
    )
    con.execute(
        "INSERT INTO pages (id, section_id, title, body_html, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [page_id, section_id, "Новая страница", "", now, now],
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


def main():
    st.set_page_config(
        layout="wide",
        page_title="OneNote",
    )

    con = get_connection()

    notebooks_df = get_notebooks(con)

    selected_notebook_id: int | None = None
    selected_section_id: int | None = None

    notebook_records = list(notebooks_df.itertuples(index=False))
    if notebook_records:
        selected_notebook = st.sidebar.selectbox(
            "Записная книжка",
            notebook_records,
            format_func=lambda row: row.name,
        )
        selected_notebook_id = int(selected_notebook.id)
    else:
        st.sidebar.info("Создайте первую записную книжку ниже.")

    with st.sidebar.expander("➕ Новая книжка", expanded=False):
        new_nb_name = st.text_input("Название", key="new_notebook_name")
        if st.button("Создать записную книжку", key="create_notebook_btn"):
            create_notebook(con, new_nb_name)
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
            st.sidebar.warning("В книжке пока нет разделов.")

        with st.sidebar.expander("➕ Новый раздел", expanded=False):
            new_section_name = st.text_input("Название раздела", key="new_section_name")
            if st.button("Создать раздел", key="create_section_btn"):
                create_section(con, selected_notebook_id, new_section_name)
                st.rerun()

    pages_df = load_pages_df(con, selected_notebook_id, selected_section_id)

    if selected_section_id and st.sidebar.button("➕ Новая страница"):
        new_page_id = create_page(con, selected_section_id)
        st.sidebar.success(f"Создана страница ID = {new_page_id}")
        st.rerun()

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
            update_mode=GridUpdateMode.SELECTION_CHANGED,
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
            created_at = current_page.get("created_at")
            updated_at = current_page.get("updated_at")

            st.caption(
                f"{current_page['notebook_name']} › {current_page['section_name']} › {current_page['title']}"
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
                quill_html = st_quill(
                    value=current_html,
                    html=True,
                    placeholder="Начните писать...",
                    key=f"quill_{page_id}",
                ) or ""

                confirm_delete = st.checkbox(
                    "Подтверждаю удаление",
                    key=f"confirm_delete_{page_id}",
                )
                cols = st.columns([2, 1])
                if cols[0].button("💾 Сохранить изменения", key=f"save_{page_id}"):
                    update_page(con, page_id, new_title, quill_html)
                    st.success("Страница сохранена")
                    st.rerun()
                if cols[1].button("🗑 Удалить страницу", key=f"delete_{page_id}", type="secondary"):
                    if confirm_delete:
                        delete_page(con, page_id)
                        st.success("Страница удалена")
                        st.rerun()
                    else:
                        st.warning("Поставьте галочку для подтверждения.")
    else:
        st.info("Выберите страницу на боковой панели, чтобы просмотреть и отредактировать её.")


if __name__ == "__main__":
    main()
