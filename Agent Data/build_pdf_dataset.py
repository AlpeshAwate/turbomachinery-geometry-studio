#!/usr/bin/env python3
"""Convert a PDF into a self-contained, searchable SQLite knowledge base."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import struct
import sys
import zlib
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import ContentStream


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE dataset_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    title TEXT,
    author TEXT,
    subject TEXT,
    page_count INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    source_pdf BLOB NOT NULL
);

CREATE TABLE pages (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    page_number INTEGER NOT NULL,
    label TEXT,
    width REAL NOT NULL,
    height REAL NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(document_id, page_number)
);

CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    page_id INTEGER NOT NULL REFERENCES pages(id),
    page_number INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    UNIQUE(page_id, chunk_index)
);

CREATE VIRTUAL TABLE chunk_search USING fts5(
    text,
    chunk_id UNINDEXED,
    page_number UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE images (
    id INTEGER PRIMARY KEY,
    object_number INTEGER NOT NULL UNIQUE,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    color_space TEXT,
    bits_per_component INTEGER,
    mime_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    image_data BLOB NOT NULL,
    mask_image_id INTEGER REFERENCES images(id)
);

CREATE TABLE page_images (
    page_id INTEGER NOT NULL REFERENCES pages(id),
    image_id INTEGER NOT NULL REFERENCES images(id),
    resource_name TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(page_id, image_id, resource_name)
);

CREATE INDEX idx_chunks_page ON chunks(page_number, chunk_index);
CREATE INDEX idx_page_images_page ON page_images(page_id);
CREATE INDEX idx_images_sha256 ON images(sha256);
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pdf_value(value) -> str | None:
    return None if value is None else str(value)


def normalize_text(text: str) -> str:
    text = text.replace("\x00", "")
    # PDF icon fonts often map bullets to Unicode's private-use area. These
    # glyphs are visual decoration, not useful language-model content.
    text = re.sub(r"[\ue000-\uf8ff\ufffd]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_chunks(text: str, target: int = 1800, overlap: int = 250):
    if not text:
        return
    start = 0
    index = 0
    while start < len(text):
        hard_end = min(start + target, len(text))
        end = hard_end
        if hard_end < len(text):
            candidates = [
                text.rfind("\n\n", start + target // 2, hard_end),
                text.rfind("\n", start + target // 2, hard_end),
                text.rfind(". ", start + target // 2, hard_end),
                text.rfind(" ", start + target // 2, hard_end),
            ]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + (2 if text[boundary : boundary + 2] == ". " else 0)
        piece = text[start:end].strip()
        if piece:
            yield index, piece, start, end
            index += 1
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def make_png(width: int, height: int, pixels: bytes, channels: int) -> bytes:
    color_types = {1: 0, 2: 4, 3: 2, 4: 6}
    expected = width * height * channels
    if len(pixels) != expected:
        raise ValueError(f"expected {expected} pixel bytes, found {len(pixels)}")
    stride = width * channels
    scanlines = b"".join(
        b"\x00" + pixels[row * stride : (row + 1) * stride]
        for row in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, color_types[channels], 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(scanlines, 7))
        + png_chunk(b"IEND", b"")
    )


def merge_alpha(pixels: bytes, alpha: bytes, channels: int) -> bytes:
    if len(alpha) * channels != len(pixels):
        raise ValueError("soft mask dimensions do not match image")
    output = bytearray(len(alpha) * (channels + 1))
    source = memoryview(pixels)
    for i, opacity in enumerate(alpha):
        source_start = i * channels
        target_start = i * (channels + 1)
        output[target_start : target_start + channels] = source[
            source_start : source_start + channels
        ]
        output[target_start + channels] = opacity
    return bytes(output)


def object_number(reference, obj) -> int:
    number = getattr(reference, "idnum", None)
    if number is None:
        reference = getattr(obj, "indirect_reference", None)
        number = getattr(reference, "idnum", None)
    if number is None:
        raise ValueError("image does not have an indirect object number")
    return int(number)


def image_to_png(obj) -> bytes:
    width = int(obj["/Width"])
    height = int(obj["/Height"])
    color_space = str(obj.get("/ColorSpace"))
    channels = 3 if color_space == "/DeviceRGB" else 1 if color_space == "/DeviceGray" else 0
    if int(obj.get("/BitsPerComponent", 8)) != 8 or not channels:
        raise ValueError(f"unsupported image format: {color_space}")
    pixels = obj.get_data()
    mask_reference = obj.get("/SMask")
    if mask_reference is not None:
        mask = mask_reference.get_object()
        alpha = mask.get_data()
        pixels = merge_alpha(pixels, alpha, channels)
        channels += 1
    return make_png(width, height, pixels, channels)


def do_resource_names(page, reader: PdfReader) -> dict[str, int]:
    counts: dict[str, int] = {}
    contents = page.get_contents()
    if contents is None:
        return counts
    for operands, operator in ContentStream(contents, reader).operations:
        if operator == b"Do" and operands:
            name = str(operands[0])
            counts[name] = counts.get(name, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    pdf_path = args.pdf.resolve()
    output_path = args.output.resolve()
    temp_path = output_path.with_suffix(output_path.suffix + ".building")
    if not pdf_path.is_file():
        parser.error(f"PDF not found: {pdf_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if temp_path.exists():
        temp_path.unlink()

    pdf_data = pdf_path.read_bytes()
    reader = PdfReader(str(pdf_path))
    metadata = {str(k): pdf_value(v) for k, v in (reader.metadata or {}).items()}
    conn = sqlite3.connect(temp_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO dataset_info(key, value) VALUES (?, ?)",
            [
                ("format", "pdf-llm-sqlite-v1"),
                ("source_sha256", sha256_bytes(pdf_data)),
                ("chunk_target_chars", "1800"),
                ("chunk_overlap_chars", "250"),
            ],
        )
        cursor = conn.execute(
            """INSERT INTO documents
               (filename, sha256, title, author, subject, page_count, metadata_json, source_pdf)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pdf_path.name,
                sha256_bytes(pdf_data),
                metadata.get("/Title"),
                metadata.get("/Author"),
                metadata.get("/Subject"),
                len(reader.pages),
                json.dumps(metadata, ensure_ascii=False),
                pdf_data,
            ),
        )
        document_id = cursor.lastrowid
        image_ids: dict[int, int] = {}
        for page_number, page in enumerate(reader.pages, 1):
            text = normalize_text(page.extract_text() or "")
            box = page.mediabox
            page_id = conn.execute(
                """INSERT INTO pages
                   (document_id, page_number, label, width, height, text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    document_id,
                    page_number,
                    str(page_number),
                    float(box.width),
                    float(box.height),
                    text,
                ),
            ).lastrowid
            for chunk_index, chunk, start, end in split_chunks(text):
                chunk_id = conn.execute(
                    """INSERT INTO chunks
                       (document_id, page_id, page_number, chunk_index, text, char_start, char_end)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (document_id, page_id, page_number, chunk_index, chunk, start, end),
                ).lastrowid
                conn.execute(
                    "INSERT INTO chunk_search(text, chunk_id, page_number) VALUES (?, ?, ?)",
                    (chunk, chunk_id, page_number),
                )

            resources = page.get("/Resources") or {}
            xobjects = resources.get("/XObject") if hasattr(resources, "get") else None
            if xobjects:
                xobjects = xobjects.get_object()
                for name, count in do_resource_names(page, reader).items():
                    reference = xobjects.get(name)
                    if reference is None:
                        continue
                    obj = reference.get_object()
                    if obj.get("/Subtype") != "/Image":
                        continue
                    number = object_number(reference, obj)
                    image_id = image_ids.get(number)
                    if image_id is None:
                        png = image_to_png(obj)
                        image_id = conn.execute(
                            """INSERT INTO images
                               (object_number, width, height, color_space, bits_per_component,
                                mime_type, sha256, image_data, mask_image_id)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                            (
                                number,
                                int(obj["/Width"]),
                                int(obj["/Height"]),
                                str(obj.get("/ColorSpace")),
                                int(obj.get("/BitsPerComponent", 8)),
                                "image/png",
                                sha256_bytes(png),
                                png,
                            ),
                        ).lastrowid
                        image_ids[number] = image_id
                    conn.execute(
                        """INSERT OR REPLACE INTO page_images
                           (page_id, image_id, resource_name, occurrence_count)
                           VALUES (?, ?, ?, ?)""",
                        (page_id, image_id, name, count),
                    )
            if page_number % 25 == 0 or page_number == len(reader.pages):
                conn.commit()
                print(f"Processed {page_number}/{len(reader.pages)} pages", flush=True)

        conn.execute("INSERT INTO dataset_info(key, value) VALUES (?, ?)", ("indexed_images", str(len(image_ids))))
        conn.execute("INSERT INTO dataset_info(key, value) VALUES (?, ?)", ("status", "complete"))
        conn.commit()
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {result}")
    except Exception:
        conn.close()
        if temp_path.exists():
            temp_path.unlink()
        raise
    else:
        conn.close()

    if output_path.exists():
        output_path.unlink()
    temp_path.replace(output_path)
    print(f"Created {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
