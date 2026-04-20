"""Clean up <SEP>-aggregated entity descriptions in LightRAG.

Problem: LightRAG merges descriptions from multiple chunks using <SEP> as a
delimiter. Over time this accumulates duplicates and near-duplicates (the same
fact phrased 3-5 times). This module splits, deduplicates, and optionally
sends the residue to the LLM for a single coherent paraphrase.

Exports:
- scan_sep_entities(graphml_path) -> list of (name, description) needing cleanup
- compress_description(fragments: list[str]) -> str  — pure/test-friendly
- clean_sep_descriptions(dry_run=False, limit=None) -> report

The actual entity update goes through LightRAG WebUI's POST /graph/entity/edit,
configured via LIGHTRAG_WEBUI_URL + LIGHTRAG_WEBUI_API_KEY env.
"""
from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

SEP = "<SEP>"
GRAPHML_NS = {"g": "http://graphml.graphdrawing.org/xmlns"}
MIN_FRAGMENTS_FOR_LLM = 2
MIN_TOTAL_LEN_FOR_LLM = 500
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-pro")


def _graphml_path() -> Path:
    base = os.getenv("LIGHTRAG_WORKING_DIR", "/app/vault/.lightrag")
    return Path(base) / "graph_chunk_entity_relation.graphml"


def _webui_url() -> str:
    return os.getenv("LIGHTRAG_WEBUI_URL", "http://secondbrain-webui:9621").rstrip("/")


def _webui_headers() -> dict:
    key = os.getenv("LIGHTRAG_WEBUI_API_KEY") or os.getenv("SECONDBRAIN_API_KEY", "")
    h = {"Content-Type": "application/json"}
    if key:
        h["X-API-Key"] = key
    return h


def _dedup_fragments(fragments: list[str]) -> list[str]:
    """Drop exact and case-insensitive duplicates, keep order."""
    seen: set[str] = set()
    out: list[str] = []
    for f in fragments:
        k = re.sub(r"\s+", " ", f.strip().lower())
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(f.strip())
    return out


def compress_description(fragments: list[str]) -> str:
    """Merge fragments into one description. Dedup-only; LLM step is separate.

    Returns "" if no usable fragments.
    """
    unique = _dedup_fragments(fragments)
    if not unique:
        return ""
    if len(unique) == 1:
        return unique[0]
    # Join with ". " — LLM step will smooth this into prose if invoked.
    joined = ". ".join(s.rstrip(". ") for s in unique)
    return joined


def _llm_compress(entity_name: str, fragments: list[str]) -> str | None:
    """Use Gemini to paraphrase multiple fragments into a single coherent description."""
    try:
        from google import genai
    except Exception:
        logger.warning("google.genai not available — skipping LLM compression")
        return None
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    client = genai.Client(api_key=api_key)
    joined = "\n- ".join(fragments)
    prompt = (
        f"Сущность: «{entity_name}»\n\n"
        f"Ниже — несколько описаний этой сущности, агрегированных из разных "
        f"документов. В них много повторов и перефразировок одной и той же "
        f"информации. Сожми их в ОДНО связное описание на русском языке "
        f"(2-5 предложений), сохранив ВСЕ уникальные факты. Убери дубли, "
        f"«Человек, упомянутый в…»-заполнители, отсылки к документу. "
        f"Не добавляй новых фактов. Верни только текст описания без заголовков "
        f"и кавычек.\n\n"
        f"Описания:\n- {joined}"
    )
    try:
        resp = client.models.generate_content(model=LLM_MODEL, contents=prompt)
        text = (resp.text or "").strip()
        if text and len(text) < 3000:
            return text
    except Exception as e:
        logger.warning("LLM compression failed for %s: %s", entity_name, e)
    return None


def _iter_nodes(graphml_path: Path):
    """Yield (entity_name, description, description_key) tuples."""
    tree = ET.parse(graphml_path)
    root = tree.getroot()
    # Find which key id maps to 'description' attribute
    desc_keys: set[str] = set()
    for k in root.findall("g:key", GRAPHML_NS):
        if k.get("attr.name") == "description":
            desc_keys.add(k.get("id"))
    for node in root.findall(".//g:node", GRAPHML_NS):
        nid = node.get("id") or ""
        desc_text = None
        desc_key = None
        for d in node.findall("g:data", GRAPHML_NS):
            if d.get("key") in desc_keys and d.text:
                desc_text = d.text
                desc_key = d.get("key")
                break
        if desc_text:
            yield nid, desc_text, desc_key


def scan_sep_entities(graphml_path: Path | None = None) -> list[tuple[str, str]]:
    """Return [(entity_name, current_description), …] for entities with <SEP>."""
    graphml_path = graphml_path or _graphml_path()
    if not graphml_path.exists():
        logger.warning("GraphML not found at %s", graphml_path)
        return []
    out: list[tuple[str, str]] = []
    for name, desc, _ in _iter_nodes(graphml_path):
        if SEP in desc:
            out.append((name, desc))
    return out


def _update_entity(name: str, new_description: str) -> dict:
    url = f"{_webui_url()}/graph/entity/edit"
    payload = {
        "entity_name": name,
        "updated_data": {"description": new_description},
        "allow_rename": False,
        "allow_merge": False,
    }
    try:
        resp = requests.post(url, json=payload, headers=_webui_headers(), timeout=60)
        resp.raise_for_status()
        return {"status": "ok", "name": name}
    except Exception as e:
        logger.warning("Entity edit failed for %s: %s", name, e)
        return {"status": "error", "name": name, "error": str(e)}


def clean_sep_descriptions(
    dry_run: bool = False,
    limit: int | None = None,
    use_llm: bool = True,
) -> dict:
    """Clean <SEP>-aggregated descriptions in the entity graph.

    Returns a structured report with counters and a sample of changes.
    """
    candidates = scan_sep_entities()
    total = len(candidates)
    updated = 0
    skipped_noop = 0
    failed = 0
    sample_before: list[tuple[str, str, str]] = []

    if limit:
        candidates = candidates[:limit]

    for name, desc in candidates:
        fragments = [f.strip() for f in desc.split(SEP) if f.strip()]
        unique = _dedup_fragments(fragments)
        if len(unique) <= 1:
            # Dedup-only was enough; drop <SEP>
            new_desc = unique[0] if unique else ""
        elif (
            use_llm
            and len(unique) >= MIN_FRAGMENTS_FOR_LLM
            and sum(len(f) for f in unique) >= MIN_TOTAL_LEN_FOR_LLM
        ):
            llm_out = _llm_compress(name, unique)
            new_desc = llm_out if llm_out else compress_description(unique)
        else:
            new_desc = compress_description(unique)

        if not new_desc or new_desc == desc:
            skipped_noop += 1
            continue

        if dry_run:
            if len(sample_before) < 5:
                sample_before.append((name, desc[:200], new_desc[:200]))
            updated += 1
            continue

        res = _update_entity(name, new_desc)
        if res.get("status") == "ok":
            updated += 1
            if len(sample_before) < 5:
                sample_before.append((name, desc[:200], new_desc[:200]))
        else:
            failed += 1

    return {
        "total_with_sep": total,
        "processed": len(candidates),
        "updated": updated,
        "skipped_noop": skipped_noop,
        "failed": failed,
        "dry_run": dry_run,
        "sample": [
            {"entity": n, "before": b, "after": a} for n, b, a in sample_before
        ],
    }
