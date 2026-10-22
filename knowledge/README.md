# Knowledge Store

Persistent memory for WAT. Indexed by graphify into `../graphify-out/`.

- `sources/` — raw inputs: docs, PDFs, images, fetched URLs. Don't edit; add.
- `notes/` — learnings written during work. One topic per file, frontmatter (`title`, `created`, `tags`, `source`).

Add: `python tools/knowledge.py note|add-url ...`
Query: `python tools/knowledge.py query "..."`
Rebuild after adding: `/graphify . --update`

SOP: `workflows/knowledge_base.md`. No secrets here.
