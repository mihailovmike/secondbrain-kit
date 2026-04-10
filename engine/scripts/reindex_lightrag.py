#!/usr/bin/env python3
"""Full vault reindex through LightRAG (Gemini Pro + Embedding 2).

Scans all note folders, strips frontmatter, inserts into LightRAG
for entity extraction + knowledge graph building.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.lightrag_engine import insert, shutdown  # noqa: E402

VAULT_PATH = Path(os.getenv("VAULT_PATH", "/app/vault"))
INBOX_DIR_NAME = os.getenv("INBOX_DIR_NAME", "_inbox")
_SKIP_DIRS = {"templates", ".obsidian", ".git", ".lightrag", ".entire", ".trash"}
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n*", re.DOTALL)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reindex vault into LightRAG")
    p.add_argument("--limit", type=int, default=0, help="Index first N files only (0=all)")
    p.add_argument("--dry-run", action="store_true", help="List files without indexing")
    p.add_argument("--folder", type=str, default="", help="Index only this folder")
    return p.parse_args()


def discover_notes(folder_filter: str = "") -> list[Path]:
    """Find all .md files in vault note folders."""
    files: list[Path] = []
    for d in sorted(VAULT_PATH.iterdir()):
        if not d.is_dir():
            continue
        if d.name in _SKIP_DIRS or d.name.startswith("."):
            continue
        if d.name == INBOX_DIR_NAME:
            continue
        if folder_filter and d.name != folder_filter:
            continue
        files.extend(sorted(d.rglob("*.md")))
    return files


def strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter from note."""
    if not text.startswith("---"):
        return text.strip()
    m = FRONTMATTER_RE.match(text)
    if m:
        return text[m.end():].strip()
    return text.strip()


def main() -> int:
    args = parse_args()
    files = discover_notes(args.folder)

    if args.limit > 0:
        files = files[:args.limit]

    print(f"Vault: {VAULT_PATH}")
    print(f"Notes discovered: {len(files)}")

    if args.dry_run:
        for f in files[:20]:
            print(f"  {f.relative_to(VAULT_PATH)}")
        if len(files) > 20:
            print(f"  ... and {len(files) - 20} more")
        return 0

    indexed = 0
    skipped = 0
    errors = 0
    t0 = time.time()

    for i, path in enumerate(files, 1):
        rel = path.relative_to(VAULT_PATH)
        raw = path.read_text(encoding="utf-8").strip()
        body = strip_frontmatter(raw)

        if len(body) < 20:
            print(f"  SKIP (too short): {rel}")
            skipped += 1
            continue

        try:
            insert(body, file_path=str(rel))
            indexed += 1
            elapsed = time.time() - t0
            rate = indexed / elapsed if elapsed > 0 else 0
            print(f"  [{i}/{len(files)}] OK: {rel} ({rate:.1f} notes/s)")
        except Exception as e:
            errors += 1
            print(f"  [{i}/{len(files)}] ERROR: {rel} — {e}")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s. Indexed={indexed}, skipped={skipped}, errors={errors}")

    shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
