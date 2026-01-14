"""
Basic JDBC configuration for PostgreSQL.

Update jdbc_url and driver_jar to match your environment before use.
"""

# Example JDBC URL: jdbc:postgresql://<host>:<port>/<database>
jdbc_url = "jdbc:postgresql://localhost:5432/postgres"

# Path to the PostgreSQL JDBC driver JAR (download from https://jdbc.postgresql.org/)
# Example: r"C:\drivers\postgresql-42.7.3.jar" or "drivers/postgresql-42.7.3.jar"
driver_jar = r"C:\Program Files (x86)\PostgreSQL\pgJDBC\postgresql-42.7.2.jar"

# Optional: schema and prefix for table names
pg_schema = "sbx_dfip_ocpp"
pg_table_prefix = "notes_"

# Relative path (from project root / app.py location) for temporary email attachments
temp_attachments_dir = "docs"

# Public base URL of the Streamlit app (used to build deep-links to pages).
# Example: "https://notes.company.ru" or "https://notes.company.ru/Notes"
app_base_url = "http://localhost:8501"
