---
name: artifact-creator
description: Producing polished standalone deliverables (HTML pages, dashboards, SVG diagrams, reports) as artifacts.
---
# Artifact Creator

- Use `artifact_save` for anything the user will view on its own: web pages, dashboards, diagrams, reports, tables, long code.
- Pick the right kind: `html` (interactive pages), `svg` (diagrams, charts), `markdown` (docs, reports), `csv` (tables), `code` (+ language).
- HTML must be fully self-contained: inline CSS and JS, no CDNs, fonts or images from the internet (it renders offline, sandboxed).
- Design it well: clear title, readable typography, spacing, a coherent color scheme, works in light and dark.
- SVG: set a `viewBox`, use readable font sizes, label everything, keep it simple.
- To change an artifact, call `artifact_get` then `artifact_save` with the same `id` (keeps version history).
- In chat, reply with a one-line summary — don't paste the artifact content.
