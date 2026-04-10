# SecondBrain Engine — Architectural Principles

Hard rules. Every pipeline component must enforce these.

---

## WHAT ENTERS (what gets into the system)

### W1: Value Pyramid
Priority of what to store (top = most valuable):
1. **Decisions + reasons** (WHY, not just WHAT)
2. **Project facts** (stack, people, status)
3. **Principles and preferences** (behavioral patterns)
4. **Concepts and definitions**
5. **Sources and notes**

Never enters: source code, logs, DB dumps, shopping lists, chat garbage.

### W2: Value Gate
Daemon asks LLM: "Does this have long-term value?"
No → reject with reason to rejected.log.
Better to miss something useful than to store garbage.

---

## HOW IT'S STORED (storage invariants)

### S1: Uniqueness (critical)
**One meaning = one place in the system. No contradictions.**

- One topic = one file in vault.
- One entity = one node in knowledge graph (LightRAG dedup).
- New information on an existing topic → merge into existing note, never create a duplicate.
- Before creating a new note: semantic search in LightRAG. Match >0.85 → merge.
- If new info contradicts existing info → flag for human review, do not silently overwrite.
- The graph must not contain two nodes that mean the same thing.

### S2: Closed Vocabulary
- Tags — ONLY from existing vault tags.
- Folders — ONLY from existing vault folders.
- New tag/folder is never created automatically.
- If nothing fits → inbox + `needs_review: true`.

### S3: Structure Ownership
- LLM decides: title, type, tags, folder, links.
- Human decides: creating new folders, new tag categories.
- Daemon NEVER creates folders.

---

## QUALITY CONTROL (enforcement layers)

### Q1: Garbage In = Garbage In Graph
Pipeline gates (in order):
- **L1**: Hash dedup — same file never processed twice
- **L2**: Size gate — <20 words reject, >5000 words reject
- **L3**: Content quality — code/logs/binary reject (>50% code lines)
- **L4**: Value gate — LLM: long-term value? yes/no
- **L5**: Title dedup — slug already exists globally
- **L6**: Semantic dedup — >0.85 similarity → merge into existing

### Q2: Daemon Never Guesses
- confidence < 0.7 → inbox + `needs_review: true`
- No matching folder → inbox + `needs_review: true`
- No matching tags → no tags + `needs_review: true`
- Better to ask than to be wrong.

### Q3: Everything Is Reversible
- Any node can be pruned.
- Any file can be moved.
- Reindex rebuilds the graph from vault in minutes.
- The brain is not a tattoo.

---

## SIMPLICITY (system hygiene)

### X1: Fewer Files, Not More
System strives for consolidation, not proliferation.
10 clean notes > 100 fragments.

### X2: Minimal Metadata
Frontmatter: title, type, tags, created, source, confidence.
Nothing else. No extra fields unless they serve a gate or query.

### X3: Single Data Flow
```
inbox → daemon → vault → LightRAG → graph
```
No side branches, no alternative paths, no parallel stores.
