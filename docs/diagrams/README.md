# Workflow diagrams

All labels are in English. The main README embeds PNGs for Markdown preview compatibility; SVGs provide scalable versions, and `.dot` files are the editable Graphviz sources.

- `data-preparation`: the unified `run.py` entry point, sequential data preparation, expected data-gap handling, run snapshots, and manifest authorization.
- `preview-generation`: core-data eligibility, manifest-authorized input loading, OpenAI generation, one optional rewrite, numeric checks, and output saving by the wrapper.

These diagrams describe the unified wrapper. Standalone generation without `--manifest` uses the separate legacy input path. Expected data unavailability allows other preparation stages to continue; unexpected command failures and context or integrity mismatches stop the workflow.

Regenerate from the repository root with Graphviz installed:

```bash
dot -Tsvg docs/diagrams/data-preparation.dot -o docs/diagrams/data-preparation.svg
dot -Tpng -Gdpi=160 docs/diagrams/data-preparation.dot -o docs/diagrams/data-preparation.png
dot -Tsvg docs/diagrams/preview-generation.dot -o docs/diagrams/preview-generation.svg
dot -Tpng -Gdpi=160 docs/diagrams/preview-generation.dot -o docs/diagrams/preview-generation.png
```

The PNGs are static assets: Markdown previews do not need an extension, JavaScript, or a network connection to render them.
