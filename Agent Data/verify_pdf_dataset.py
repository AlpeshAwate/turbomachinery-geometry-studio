#!/usr/bin/env python3
"""Run structural and content checks on a generated PDF knowledge database."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    conn = sqlite3.connect(args.database)
    checks = {
        "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "documents": conn.execute("SELECT count(*) FROM documents").fetchone()[0],
        "pages": conn.execute("SELECT count(*) FROM pages").fetchone()[0],
        "empty_pages": conn.execute(
            "SELECT count(*) FROM pages WHERE trim(text) = ''"
        ).fetchone()[0],
        "text_characters": conn.execute(
            "SELECT coalesce(sum(length(text)), 0) FROM pages"
        ).fetchone()[0],
        "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
        "search_rows": conn.execute("SELECT count(*) FROM chunk_search").fetchone()[0],
        "images": conn.execute("SELECT count(*) FROM images").fetchone()[0],
        "image_placements": conn.execute("SELECT count(*) FROM page_images").fetchone()[0],
        "bad_png_headers": conn.execute(
            "SELECT count(*) FROM images WHERE hex(substr(image_data, 1, 8)) != '89504E470D0A1A0A'"
        ).fetchone()[0],
        "private_glyphs": conn.execute(
            "SELECT count(*) FROM pages WHERE text GLOB '*[\ue000-\uf8ff]*'"
        ).fetchone()[0],
    }
    source_blob, source_hash = conn.execute(
        "SELECT source_pdf, sha256 FROM documents LIMIT 1"
    ).fetchone()
    checks["source_hash_matches"] = hashlib.sha256(source_blob).hexdigest() == source_hash
    checks["database_bytes"] = args.database.stat().st_size
    checks["largest_image"] = conn.execute(
        "SELECT id, width, height, length(image_data) FROM images ORDER BY width * height DESC LIMIT 1"
    ).fetchone()
    for key, value in checks.items():
        print(f"{key}: {value}")
    failed = (
        checks["integrity"] != "ok"
        or checks["documents"] != 1
        or checks["pages"] == 0
        or checks["chunks"] != checks["search_rows"]
        or checks["bad_png_headers"] != 0
        or checks["private_glyphs"] != 0
        or not checks["source_hash_matches"]
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
