from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel
from typer.testing import CliRunner

from autokernel.cli import app
from autokernel.inventory import (
    InventoryTools,
    build_inventory,
    read_inventory,
    write_inventory,
)
from autokernel.inventory_agent import (
    enrich_records,
    offline_enrichment,
    validate_enrichment,
)
from autokernel.kconfig_walk import SymbolType


runner = CliRunner()


def _make_large_synthetic_kernel(tmp_path: Path, count: int = 150) -> Path:
    src = tmp_path / "linux"
    src.mkdir()
    (src / "Makefile").write_text("# synthetic kernel\n")
    arch = src / "arch" / "x86"
    arch.mkdir(parents=True)
    (arch / "Kconfig").write_text(
        "config X86\n"
        "\tdef_bool y\n"
        "\nconfig PCI\n"
        '\tbool "PCI support"\n'
        "\tdefault y\n"
        "\nconfig NET\n"
        '\tbool "Networking support"\n'
        "\tdefault y\n"
    )
    (src / "Kconfig").write_text(
        'mainmenu "synthetic kernel"\n'
        'source "arch/$(SRCARCH)/Kconfig"\n'
        'source "drivers/test/Kconfig"\n'
    )
    drivers = src / "drivers" / "test"
    drivers.mkdir(parents=True)

    kconfig_lines = ['menu "Synthetic test drivers"\n']
    makefile_lines = []
    for i in range(count):
        name = f"TEST_DRIVER_{i:03d}"
        obj = f"test_driver_{i:03d}"
        kconfig_lines.append(
            f"\nconfig {name}\n"
            f'\ttristate "Synthetic test driver {i:03d}"\n'
            "\tdepends on PCI && NET\n"
            "\thelp\n"
            f"\t  Builds a synthetic PCI network-style driver number {i:03d}.\n"
            "\t  Used by tests to validate inventory scale and source search.\n"
        )
        makefile_lines.append(f"obj-$(CONFIG_{name}) += {obj}.o\n")
        (drivers / f"{obj}.c").write_text(
            "/* Synthetic driver header.\n"
            f" * Driver index: {i:03d}\n"
            " * Pretends to support a PCI device table and optional firmware.\n"
            " */\n"
            "#include <linux/module.h>\n"
            "#include <linux/pci.h>\n"
            f"#ifdef CONFIG_{name}\n"
            f"static const struct pci_device_id {obj}_ids[] = {{\n"
            f"    {{ PCI_DEVICE(0x1af4, 0x{i:04x}) }},\n"
            "    { 0, }\n"
            "};\n"
            f"MODULE_DEVICE_TABLE(pci, {obj}_ids);\n"
            f'MODULE_FIRMWARE("synthetic/{obj}.bin");\n'
            "#endif\n"
        )
    kconfig_lines.append("\nendmenu\n")
    (drivers / "Kconfig").write_text("".join(kconfig_lines))
    (drivers / "Makefile").write_text("".join(makefile_lines))
    return src


def test_inventory_scans_150_kconfig_entries_with_source_evidence(tmp_path):
    src = _make_large_synthetic_kernel(tmp_path, 150)

    dataset = build_inventory(src, arch="x86_64")

    driver_records = [r for r in dataset.symbols if r.name.startswith("TEST_DRIVER_")]
    assert len(driver_records) == 150

    rec = next(r for r in driver_records if r.name == "TEST_DRIVER_042")
    assert rec.type == SymbolType.TRISTATE
    assert rec.prompt == "Synthetic test driver 042"
    assert rec.depends_symbols == ["NET", "PCI"]
    assert rec.kbuild
    assert "test_driver_042" in rec.modules
    assert rec.hardware.buses == ["pci"]
    assert "synthetic/test_driver_042.bin" in rec.hardware.firmware
    assert rec.source_refs.usage_count >= 1
    assert rec.fact_hash


def test_inventory_jsonl_roundtrip_and_tools_search_read(tmp_path):
    src = _make_large_synthetic_kernel(tmp_path, 150)
    out = tmp_path / "inventory"
    write_inventory(build_inventory(src, arch="x86_64"), out)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source_dir"] = str(tmp_path / "missing-linux")
    manifest_path.write_text(json.dumps(manifest))

    dataset = read_inventory(out)
    tools = InventoryTools.from_dir(out, source_dir=src)

    assert len(dataset.symbols) >= 150
    assert "CONFIG_TEST_DRIVER_042" in tools.search_symbols("driver_042")
    assert "CONFIG_TEST_DRIVER_042" in tools.search_kconfig_text("driver number 042")
    assert tools.search_kbuild_usages("CONFIG_TEST_DRIVER_042")
    assert tools.search_config_usages("CONFIG_TEST_DRIVER_042")

    head = tools.read_file_head("drivers/test/test_driver_042.c", max_lines=4)
    assert "Synthetic driver header" in head.text

    usage = tools.search_config_usages("CONFIG_TEST_DRIVER_042")[0]
    excerpt = tools.read_file_around_match(usage.path, line=usage.line, context=2)
    assert "CONFIG_TEST_DRIVER_042" in excerpt.text


def test_offline_enrichment_validates_for_150_records(tmp_path):
    src = _make_large_synthetic_kernel(tmp_path, 150)
    dataset = build_inventory(src, arch="x86_64")
    tools = InventoryTools(dataset)

    records = [r for r in dataset.symbols if r.name.startswith("TEST_DRIVER_")]
    enrichments = [offline_enrichment(r) for r in records]

    assert len(enrichments) == 150
    for rec, enrichment in zip(records, enrichments, strict=True):
        validate_enrichment(enrichment, rec, tools)
        assert enrichment.evidence_refs


def test_enrichment_agent_validates_typed_output_with_test_model(tmp_path):
    src = _make_large_synthetic_kernel(tmp_path, 2)
    dataset = build_inventory(src, arch="x86_64")
    tools = InventoryTools(dataset)
    rec = tools.get_symbol("CONFIG_TEST_DRIVER_000")
    extra = tools.get_symbol("CONFIG_TEST_DRIVER_001")
    model = TestModel(
        custom_output_args={
            "enrichments": [
                {
                    "symbol": rec.symbol,
                    "fact_hash": rec.fact_hash,
                    "summary": "Synthetic driver",
                    "functionality": "Builds the synthetic test driver.",
                    "disable_effect": "Removes the synthetic test driver.",
                    "keep_when": ["when matching hardware is present"],
                    "safe_to_disable_when": ["when matching hardware is absent"],
                    "proposal_guidance": "disable_if_absent",
                    "confidence": 0.8,
                    "evidence_refs": [
                        {
                            "kind": "kconfig",
                            "path": rec.locations[0].path,
                            "line": rec.locations[0].line,
                            "detail": "definition",
                        }
                    ],
                },
                {
                    "symbol": extra.symbol,
                    "fact_hash": extra.fact_hash,
                    "summary": "Extra synthetic driver",
                    "functionality": "Builds an unrequested synthetic test driver.",
                    "disable_effect": "Removes the unrequested synthetic test driver.",
                    "proposal_guidance": "disable_if_absent",
                    "confidence": 0.8,
                    "evidence_refs": [
                        {
                            "kind": "kconfig",
                            "path": extra.locations[0].path,
                            "line": extra.locations[0].line,
                            "detail": "definition",
                        }
                    ],
                },
            ]
        }
    )

    out = enrich_records([rec], tools, model=model, service_tier=None)

    assert len(out) == 1
    assert out[0].symbol == rec.symbol
    assert out[0].model == "test"


def test_enrichment_agent_fails_when_required_symbol_is_missing(tmp_path):
    src = _make_large_synthetic_kernel(tmp_path, 1)
    dataset = build_inventory(src, arch="x86_64")
    tools = InventoryTools(dataset)
    rec = tools.get_symbol("CONFIG_TEST_DRIVER_000")
    model = TestModel(custom_output_args={"enrichments": []})

    with pytest.raises(ValueError, match="missing enrichments"):
        enrich_records([rec], tools, model=model, service_tier=None, max_attempts=1)


def test_inventory_cli_scan_search_and_offline_enrich_150_entries(tmp_path):
    src = _make_large_synthetic_kernel(tmp_path, 150)
    out = tmp_path / "inventory"

    result = runner.invoke(
        app,
        ["inventory", "scan", str(src), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        app,
        ["inventory", "search", str(out), "driver_042"],
    )
    assert result.exit_code == 0, result.output
    assert "CONFIG_TEST_DRIVER_042" in result.output

    result = runner.invoke(
        app,
        [
            "inventory",
            "enrich",
            str(out),
            "--offline",
            "--limit",
            "150",
            "--batch-size",
            "25",
            "--jobs",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    enrichments = (out / "enrichments.jsonl").read_text().splitlines()
    assert len(enrichments) == 150

    result = runner.invoke(
        app,
        [
            "inventory",
            "enrich",
            str(out),
            "--offline",
            "--limit",
            "150",
            "--batch-size",
            "25",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "skipping 150 existing enrichment(s)" in result.output
    enrichments = (out / "enrichments.jsonl").read_text().splitlines()
    assert len(enrichments) == 150
