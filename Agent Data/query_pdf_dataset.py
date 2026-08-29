#!/usr/bin/env python3
"""Search or export content from a PDF SQLite knowledge base."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def search_database(database: str | Path, query: str, limit: int = 8) -> list[dict]:
    """Return ranked, page-cited chunks suitable for insertion in an LLM prompt."""
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT c.id, c.page_number, c.chunk_index, c.text, bm25(chunk_search) AS score
               FROM chunk_search
               JOIN chunks c ON c.id = chunk_search.chunk_id
               WHERE chunk_search MATCH ?
               ORDER BY score LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)

    page = subparsers.add_parser("page")
    page.add_argument("number", type=int)

    image = subparsers.add_parser("image")
    image.add_argument("id", type=int)
    image.add_argument("output", type=Path)

    args = parser.parse_args()
    if args.command == "search":
        print(
            json.dumps(
                search_database(args.database, args.query, args.limit),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    conn = sqlite3.connect(args.database)
    conn.row_factory = sqlite3.Row
    if args.command == "page":
        row = conn.execute(
            "SELECT page_number, text FROM pages WHERE page_number = ?", (args.number,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"Page {args.number} was not found")
        print(row["text"])
    else:
        row = conn.execute(
            "SELECT mime_type, image_data FROM images WHERE id = ?", (args.id,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"Image {args.id} was not found")
        args.output.write_bytes(row["image_data"])
        print(f"Wrote {args.output} ({row['mime_type']})")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
