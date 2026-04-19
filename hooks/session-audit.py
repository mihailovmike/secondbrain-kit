#!/usr/bin/env python3
"""SessionEnd audit — reads metrics + transcript, writes actionable report.

Chained after ``secondbrain-session-end.py``. Reads
``~/.claude/metrics/<sid>*.json`` (metrics collector output) and the
session transcript to produce a short markdown report at
``<vault>/archives/sessions/audit-YYYY-MM-DD-<sid8>.md`` as Layer 2.

Runs silently: no stdout output, always exits 0.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

METRICS_DIR = Path(os.path.expanduser("~/.claude/metrics"))
REPEAT_THRESHOLD = 3
TOP_TOKENS_N = 5
VAULT_PATH = Path(os.environ.get("VAULT_PATH", os.path.expanduser("~/coding/SecondBrain")))
KNOWN_MCPS = [
    "secondbrain", "perplexity", "tavily", "jina", "context7",
    "youtube-search", "youtube-transcript", "notebooklm",
    "chrome-devtools", "n8n", "n8n-live", "shadcn-ui", "stitch",
]


def _load_metrics(sid: str) -> dict | None:
    if not METRICS_DIR.exists():
        return None
    files = list(METRICS_DIR.glob(f"{sid}*.json"))
    if not files:
        return None
    merged: dict = {
        "tools": {}, "violations": [], "repeat_hashes": {},
        "started_at": None, "last_at": None,
    }
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for name, v in d.get("tools", {}).items():
            e = merged["tools"].setdefault(name, {"count": 0, "out_bytes": 0})
            e["count"] += v.get("count", 0)
            e["out_bytes"] += v.get("out_bytes", 0)
        merged["violations"].extend(d.get("violations", []))
        for h, c in d.get("repeat_hashes", {}).items():
            merged["repeat_hashes"][h] = merged["repeat_hashes"].get(h, 0) + c
        for k in ("started_at", "last_at"):
            if d.get(k) is not None:
                if merged[k] is None:
                    merged[k] = d[k]
                else:
                    merged[k] = min(merged[k], d[k]) if k == "started_at" else max(merged[k], d[k])
    return merged


def _transcript_text(path: str | None) -> str:
    if not path or not Path(path).exists():
        return ""
    chunks: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                msg = entry.get("message", {}) or {}
                content = msg.get("content", "")
                if isinstance(content, list):
                    parts: list[str] = []
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            parts.append(b.get("text", ""))
                    content = "\n".join(parts)
                if isinstance(content, str) and content:
                    chunks.append(content)
    except Exception:
        return ""
    return "\n".join(chunks)


def _build_report(sid: str, cwd: str, metrics: dict, transcript: str) -> tuple[str, int]:
    """Return (report_markdown, findings_count)."""
    sections: list[str] = []
    findings = 0

    tools = metrics.get("tools", {})
    violations = metrics.get("violations", [])
    repeats = metrics.get("repeat_hashes", {})

    # 1. Rule violations
    if violations:
        findings += len(violations)
        uniq = list(dict.fromkeys(violations))[:10]
        lines = ["## ⚠️ Нарушения cc-routing.md", ""]
        for v in uniq:
            lines.append(f"- `{v}`")
        sections.append("\n".join(lines))

    # 2. MCP hygiene
    used_mcps = {
        name.split("__", 2)[1] for name in tools.keys() if name.startswith("mcp__")
    }
    unused = [m for m in KNOWN_MCPS if m not in used_mcps]
    if unused:
        lines = ["## 🧹 MCP-гигиена (не вызывались)", ""]
        for m in unused:
            lines.append(f"- `{m}` → кандидат на off")
        sections.append("\n".join(lines))
        if unused:
            findings += 1

    # 3. Top tools by output volume (proxy for tokens)
    if tools:
        top = sorted(tools.items(), key=lambda kv: kv[1].get("out_bytes", 0), reverse=True)[:TOP_TOKENS_N]
        lines = ["## 📊 Top tools by output bytes", ""]
        for name, v in top:
            kb = v.get("out_bytes", 0) / 1024
            lines.append(f"- `{name}` — {v.get('count', 0)} calls, {kb:.1f} KB")
        sections.append("\n".join(lines))

    # 4. Repeat patterns
    repeated = [(h, c) for h, c in repeats.items() if c >= REPEAT_THRESHOLD]
    if repeated:
        findings += len(repeated)
        lines = ["## 🔁 Repeat patterns (≥3)", ""]
        for h, c in sorted(repeated, key=lambda kv: -kv[1])[:5]:
            lines.append(f"- hash `{h}` × {c} → кандидат на скилл/хук")
        sections.append("\n".join(lines))

    # 5. Vault opportunity (heuristic: non-saved insight markers)
    markers = ["вывод:", "решили", "инсайт", "понял что", "принципиально"]
    t_lower = transcript.lower()
    hits = [m for m in markers if m in t_lower]
    if hits and "/save-as-knowledge" not in transcript and "/save-to-archive" not in transcript:
        findings += 1
        sections.append(
            "## 📥 Vault-opportunity\n\n"
            f"В сессии найдены маркеры инсайта ({', '.join(hits)}), но "
            "ни `/save-as-knowledge`, ни `/save-to-archive` не вызывался.\n"
            "Предложение: зафиксировать ключевой вывод в vault."
        )

    # Header
    started = metrics.get("started_at")
    last = metrics.get("last_at")
    dur = ""
    if started and last:
        mins = (last - started) / 60
        dur = f"{mins:.1f} min"
    total_calls = sum(v.get("count", 0) for v in tools.values())
    header = [
        f"## Session Audit — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"- session: `{sid[:8]}`",
        f"- cwd: `{cwd}`",
        f"- duration: {dur or 'n/a'}",
        f"- tool calls: {total_calls}",
        f"- findings: **{findings}**",
        "",
    ]

    if not sections:
        sections.append("## ✅ Clean session\n\nНарушений и паттернов не найдено.")

    return "\n".join(header) + "\n\n" + "\n\n".join(sections) + "\n", findings


def main() -> None:
    if os.environ.get("CLAUDE_INVOKED_BY"):
        return
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        return

    sid = hook_input.get("session_id") or ""
    cwd = hook_input.get("cwd") or ""
    transcript_path = hook_input.get("transcript_path")

    if not sid:
        return

    metrics = _load_metrics(sid)
    if metrics is None:
        return  # no data collected → nothing to audit

    transcript = _transcript_text(transcript_path)
    body, findings = _build_report(sid, cwd, metrics, transcript)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    fname = f"audit-{date_str}-{sid[:8]}.md"

    frontmatter = (
        "---\n"
        f'title: "Session Audit: {date_str} ({sid[:8]})"\n'
        "type: source\n"
        "tags: [session-audit, observability]\n"
        f"created: {date_str}\n"
        "source: session-audit-hook\n"
        "layer: 2\n"
        "processing: vector\n"
        f"findings: {findings}\n"
        "---\n\n"
    )

    target_dir = VAULT_PATH / "archives" / "sessions"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / fname).write_text(frontmatter + body, encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
