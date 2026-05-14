# Kconfig Inventory

autokernel keeps LLM-facing Kconfig knowledge as a source-derived inventory,
not as free-form notes. The deterministic scanner is the source of truth; the
LLM enrichment layer summarizes those facts into guidance for proposal agents.

## Layout

Generated inventories are directory based:

```text
inventory/
  linux-7.0/
    x86_64/
      manifest.json
      symbols.jsonl
      enrichments.jsonl  # optional until the LLM pass has completed
```

`manifest.json` records schema/generator versions, source path, arch/srcarch,
optional `.config`, kconfiglib version, generation time, and symbol count.

`symbols.jsonl` has one `KconfigSymbolRecord` per symbol. Records are sorted by
`CONFIG_*` name for stable diffs and contain deterministic facts:

- Kconfig type, prompt, help text, definition locations, menu path
- dependencies, selected-by/implied-by relations, defaults, ranges, choice info
- Kbuild bindings from `obj-$(CONFIG_FOO)` and related Makefile lines
- source-code `CONFIG_FOO` usages
- derived module names, firmware refs, `MODULE_DEVICE_TABLE()` bus refs
- subsystem and risk tags
- `fact_hash`, computed from deterministic fields

`enrichments.jsonl` has one LLM or offline baseline enrichment per symbol when
the enrichment pass has been run. Each row includes the symbol's `fact_hash`,
summary, disable effect, keep/disable guidance, confidence, and evidence refs.
If deterministic facts change, the hash changes and the old enrichment is
stale.

The currently vendored deterministic inventory is generated from a shallow
clone of `torvalds/linux` tag `v7.0` at commit
`028ef9c96e96197026887c0f092424679298aae8` for `x86_64`.

## Commands

Build a deterministic inventory:

```bash
git clone --depth 1 --branch v7.0 https://github.com/torvalds/linux.git \
  ~/.cache/autokernel/kernels/linux-torvalds-v7.0

autokernel inventory scan ~/.cache/autokernel/kernels/linux-torvalds-v7.0 \
  --arch x86_64 \
  --out src/autokernel/knowledge/kconfig_inventory/linux-7.0/x86_64
```

Add `--config /path/to/.config` only when you intentionally want a
host-config-filtered inventory with current values from that config.

Search by name:

```bash
autokernel inventory search INVENTORY_DIR e1000
```

Search Kconfig prompt/help text:

```bash
autokernel inventory search INVENTORY_DIR "transparent hugepage" --text
```

Inspect a record:

```bash
autokernel inventory show INVENTORY_DIR CONFIG_E1000E
```

Read bounded source excerpts through the inventory path sandbox:

```bash
autokernel inventory read-file INVENTORY_DIR \
  drivers/net/ethernet/intel/e1000e/netdev.c \
  --source-dir ~/.cache/autokernel/kernels/linux-torvalds-v7.0 \
  --head 80
```

Generate LLM enrichments. The default model is `openai:gpt-5.4-mini` with
`--service-tier flex`. Existing `(symbol, fact_hash)` rows are skipped by
default, so the command is safe to resume on a headless server:

```bash
autokernel inventory enrich INVENTORY_DIR \
  --source-dir ~/.cache/autokernel/kernels/linux-torvalds-v7.0 \
  --batch-size 20 \
  --jobs 4 \
  --model openai:gpt-5.4-mini \
  --service-tier flex
```

Use `--limit N` for smoke tests and `--force` only when intentionally replacing
already-current rows. The orchestrator rejects symbols outside the requested
batch, retries missing symbols, validates `fact_hash` and evidence paths, and
appends only accepted rows to `enrichments.jsonl`.

`--source-dir` is optional for freshly scanned local inventories whose
`manifest.json` already points at the right kernel source path. Pass it when
working from a vendored inventory on another machine.

For tests and bootstrapping, write deterministic baseline enrichments without a
model call:

```bash
autokernel inventory enrich INVENTORY_DIR --offline --limit 200
```

## Update Workflow

1. Fetch or update the kernel source tree.
2. Run `inventory scan` into a versioned `linux-X.Y/ARCH` directory.
3. Diff `symbols.jsonl` against the previous kernel version.
4. Reuse enrichments whose `symbol + fact_hash` still match.
5. Run `inventory enrich` for new or changed symbols. It skips current rows
   automatically.
6. Review diffs before committing. Large churn in unchanged symbols usually
   means the deterministic scanner changed, not Kconfig.

## Vendoring

Generated inventories that should ship with autokernel belong under
`src/autokernel/knowledge/kconfig_inventory/`. That directory is a Python
package, so committed `manifest.json`, `symbols.jsonl`, and
`enrichments.jsonl` files are included in PyPI wheels/sdists as package data.

Runtime code should load vendored inventories with `importlib.resources`:

```python
from importlib.resources import files

root = files("autokernel.knowledge.kconfig_inventory") / "linux-7.0" / "x86_64"
```

The longer design docs live in `docs/` for GitHub. The wheel carries the
runtime inventory data and the package-local README, not the full docs tree.

## Agent Tooling

The enrichment agent has read-only tools:

- `list_symbols`
- `search_symbols`
- `search_kconfig_text`
- `get_symbol`
- `get_symbol_relations`
- `search_config_usages`
- `search_kbuild_usages`
- `list_files`
- `read_file_head`
- `read_file_excerpt`
- `read_file_around_match`

The agent does not write files. The orchestrator validates output and appends
accepted rows to `enrichments.jsonl`.

## Guardrails

- Facts come from deterministic scanner fields, not the LLM.
- LLM claims must cite evidence refs.
- File reads are sandboxed under the kernel source root.
- Tool errors are returned as structured results so the model can recover from
  bad exploratory calls.
- Batch output is exact-symbol validated; extra rows are ignored and missing
  rows are retried before the command fails.
- Boot, storage, crypto, console, and network symbols should bias toward
  `keep_bias` or `never_auto`.
