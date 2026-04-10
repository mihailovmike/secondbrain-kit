# SecondBrain Vault — Agent Instructions

## Structure

| Folder | Content | Who writes | Who creates |
|--------|---------|-----------|-------------|
| _inbox/ | Raw unprocessed notes | Anyone | Always exists |
| knowledge/definitions/ | Terms, glossary | Daemon, user | User only |
| goals/ | Objectives, OKRs | Daemon, user | User only |
| templates/ | Note templates | Manual | Manual |

## Folder Rules

- All folders **lowercase**. No files in vault root.
- Agent **never** creates root folders — user decides.
- Note doesn't fit → stays in inbox/. Agent may suggest a folder, user approves.

## Note Rules

- **Atomic**: 1 idea = 1 file (150-500 words)
- **Frontmatter**: title, type, tags, created, source (all required)
- **Wiki-links**: `[[Note Title]]` for connections. Reuse existing tags.
- **Filenames**: kebab-case, English. Date in frontmatter, not filename.
- **Content**: Your language. No duplicates (similarity threshold 0.85).

## Note Types

concept · project · person · decision · goal · source

## Logical Consistency

Every note must pass four checks before entering the vault:

1. **Identity** — one term = one meaning. No silent redefinitions across notes.
2. **Non-contradiction** — no conflicting facts between notes. Conflict found → resolve before saving, or flag `contradiction: true` in frontmatter, keep in inbox/.
3. **Excluded middle** — every claim is confirmed or explicitly uncertain (`[?]`, `confidence: 0.x`). No vague "maybe" without a marker.
4. **Sufficient reason** — every claim has a source. `source:` in frontmatter is mandatory.

## Frontmatter

```yaml
title: "Note title"
type: concept | project | person | decision | goal | source
tags: [tag1, tag2]
created: YYYY-MM-DD
source: telegram | youtube | manual | scan | deep-research
confidence: 0.0-1.0
```
