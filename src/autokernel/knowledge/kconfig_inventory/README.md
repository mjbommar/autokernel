# Kconfig Inventory Data

Versioned generated inventories live here when they are ready to commit:

```text
linux-X.Y/
  x86_64/
    manifest.json
    symbols.jsonl
    enrichments.jsonl  # optional until the LLM pass has completed
```

Generate or refresh with:

```bash
git clone --depth 1 --branch v7.0 https://github.com/torvalds/linux.git ~/.cache/autokernel/kernels/linux-torvalds-v7.0
autokernel inventory scan ~/.cache/autokernel/kernels/linux-torvalds-v7.0 --out src/autokernel/knowledge/kconfig_inventory/linux-7.0/x86_64
autokernel inventory enrich src/autokernel/knowledge/kconfig_inventory/linux-7.0/x86_64 --source-dir ~/.cache/autokernel/kernels/linux-torvalds-v7.0 --batch-size 20 --jobs 4 --model openai:gpt-5.4-mini --service-tier flex
```

The scanner output is deterministic. LLM enrichments must remain
evidence-cited and keyed by `fact_hash`. Enrichment is resumable: existing
`symbol + fact_hash` rows are skipped unless `--force` is passed.

This directory is a Python package on purpose. Files committed here are
included in PyPI wheels/sdists and can be read at runtime with:

```python
from importlib.resources import files

root = files("autokernel.knowledge.kconfig_inventory")
```
