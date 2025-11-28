import base64
import glob
import pathlib
import email
import os
import urllib.parse
import duckdb
from bs4 import BeautifulSoup

# DB path and target section id
DB_PATH = "onenote.duckdb"
SECTION_ID = 1  # set your target section id


def html_to_body(text: str, fallback_title: str):
    """Parse HTML and return (title, body_html)."""
    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else fallback_title
    body = str(soup.body or soup)
    return title, body


def load_mht(path: pathlib.Path):
    """Read .mht, inline referenced resources, return (title, body_html)."""
    msg = email.message_from_bytes(path.read_bytes())
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
        raise ValueError(f"No HTML part found in {path}")

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
            basename = os.path.basename(lookup)
            if basename in src_map:
                tag["src"] = src_map[basename]

    title, body_html = html_to_body(str(soup), path.stem)
    return title, body_html


def load_html_file(path: pathlib.Path):
    return html_to_body(path.read_text(encoding="utf-8"), path.stem)


def main():
    con = duckdb.connect(DB_PATH)

    files = list(glob.glob(r"exported_pages/*.mht")) + list(glob.glob(r"exported_pages/*.html"))
    for file in files:
        p = pathlib.Path(file)
        if p.suffix.lower() == ".mht":
            title, body = load_mht(p)
        else:
            title, body = load_html_file(p)

        con.execute(
            """
            INSERT INTO pages (id, section_id, title, body_html)
            VALUES ((SELECT coalesce(max(id),0)+1 FROM pages), ?, ?, ?)
            """,
            [SECTION_ID, title, body],
        )

    con.close()


if __name__ == "__main__":
    main()
