from __future__ import annotations

import argparse
from pathlib import Path

DATA_FILE = Path.cwd() / "notes.txt"


def list_notes() -> list[str]:
    if not DATA_FILE.exists():
        return []
    return [line.strip() for line in DATA_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def add_note(text: str) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Минимальный CLI для заметок")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Добавить новую заметку")
    add_parser.add_argument("text", help="Текст заметки")

    subparsers.add_parser("list", help="Показать все заметки")

    args = parser.parse_args(argv)

    if args.command == "add":
        add_note(args.text)
        print("Заметка сохранена в", DATA_FILE)
    elif args.command == "list":
        notes = list_notes()
        if not notes:
            print("Пока нет заметок")
        else:
            for idx, note in enumerate(notes, start=1):
                print(f"{idx}. {note}")


if __name__ == "__main__":
    main()
