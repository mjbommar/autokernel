"""Module name + source path → ordered list of candidate ``CONFIG_*`` symbols.

Drives the resolver's "given a module, what symbol enables it" question.
We do not parse Kbuild Makefiles or kernel sources (which the user may not
have installed) — instead we encode the kernel tree's naming conventions as
an ordered prefix table and confirm matches against the running ``.config``.

For each module we produce a list of candidate symbol names; the caller
checks them against the running config in order and returns the first hit.
On no hit, the module is reported *unresolved*, and the policy layer treats
it as load-bearing for safety.

The table is **incomplete by design**: it covers the high-traffic subsystems
where the naive ``CONFIG_<UPPER>`` mapping is wrong. For paths it doesn't
cover, we fall back to the bare name; if that misses, the conservative fail-
safe applies.
"""

from __future__ import annotations

from collections.abc import Iterable

# Ordered: most specific path prefix first.
# Each entry: (path_prefix, [config_prefix_or_template, ...]).
# A template containing ``{name}`` substitutes the normalized module name.
# A bare prefix string means ``CONFIG_<PREFIX>_{name}``.
_PATH_TABLE: list[tuple[str, list[str]]] = [
    # ── sound subsystems (more specific than generic 'sound/') ───────────
    ("sound/soc/sof/intel/", ["SND_SOC_SOF_INTEL_{name}", "SND_SOC_SOF_{name}", "SND_SOC_{name}"]),
    ("sound/soc/sof/amd/",   ["SND_SOC_SOF_AMD_{name}", "SND_SOC_SOF_{name}"]),
    ("sound/soc/sof/",       ["SND_SOC_SOF_{name}", "SND_SOC_{name}"]),
    ("sound/soc/intel/",     ["SND_SOC_INTEL_{name}", "SND_SOC_{name}"]),
    ("sound/soc/",           ["SND_SOC_{name}"]),
    ("sound/pci/hda/",       ["SND_HDA_{name}", "SND_HDA_CODEC_{name}", "SND_{name}"]),
    ("sound/pci/",           ["SND_{name}"]),
    ("sound/usb/",           ["SND_USB_{name}", "SND_{name}"]),
    ("sound/",               ["SND_{name}"]),

    # ── drivers/gpu/drm: names like 'drivers/gpu/drm/i915/i915' ──────────
    ("drivers/gpu/drm/",     ["DRM_{name}"]),

    # ── drivers/usb subsystems ───────────────────────────────────────────
    ("drivers/usb/storage/", ["USB_{name}", "USB_STORAGE_{name}", "USB_STORAGE"]),
    ("drivers/usb/serial/",  ["USB_SERIAL_{name}", "USB_SERIAL"]),
    ("drivers/usb/host/",    ["USB_{name}"]),
    ("drivers/usb/gadget/",  ["USB_GADGET_{name}", "USB_GADGET"]),
    ("drivers/usb/musb/",    ["USB_MUSB_{name}", "USB_MUSB_HDRC"]),
    ("drivers/usb/dwc2/",    ["USB_DWC2"]),
    ("drivers/usb/dwc3/",    ["USB_DWC3"]),
    ("drivers/usb/core/",    ["USB"]),
    ("drivers/usb/",         ["USB_{name}"]),

    # ── drivers/net subsystems ───────────────────────────────────────────
    ("drivers/net/wireless/", ["{name}"]),  # most are direct: CONFIG_IWLWIFI, CONFIG_ATH10K_PCI
    ("drivers/net/ethernet/", ["{name}"]),  # most direct: CONFIG_R8169, CONFIG_E1000E
    ("drivers/net/usb/",      ["USB_{name}"]),
    ("drivers/net/dsa/",      ["NET_DSA_{name}"]),
    ("drivers/net/can/",      ["CAN_{name}"]),
    ("drivers/net/ipa/",      ["IPA"]),
    ("drivers/net/phy/",      ["{name}_PHY", "{name}", "PHYLIB"]),
    ("drivers/net/mdio/",     ["MDIO_{name}"]),
    ("drivers/net/",          ["{name}"]),

    # ── storage / block ──────────────────────────────────────────────────
    ("drivers/nvme/host/",    ["BLK_DEV_NVME", "NVME_{name}", "{name}"]),
    ("drivers/nvme/target/",  ["NVME_TARGET", "NVME_TARGET_{name}"]),
    ("drivers/nvme/",         ["NVME_{name}", "{name}"]),
    ("drivers/scsi/",         ["SCSI_{name}", "{name}"]),
    ("drivers/block/zram/",   ["ZRAM"]),
    ("drivers/block/",        ["BLK_DEV_{name}", "{name}"]),
    ("drivers/md/",           ["{name}", "MD_{name}", "DM_{name}", "BCACHE"]),
    # drivers/ata/ and drivers/mmc/ are handled below in the storage and
    # mmc-host blocks (more specific entries first wins).
    ("drivers/mtd/",          ["MTD_{name}"]),

    # ── input / hid ──────────────────────────────────────────────────────
    ("drivers/hid/usbhid/",   ["USB_HID", "USB_HIDDEV"]),
    ("drivers/hid/",          ["HID_{name}", "{name}"]),
    ("drivers/input/keyboard/", ["KEYBOARD_{name}"]),
    ("drivers/input/mouse/",    ["MOUSE_{name}"]),
    ("drivers/input/touchscreen/", ["TOUCHSCREEN_{name}"]),
    ("drivers/input/joystick/", ["JOYSTICK_{name}"]),
    ("drivers/input/serio/",  ["SERIO_{name}"]),
    ("drivers/input/misc/",   ["INPUT_{name}"]),
    ("drivers/input/",        ["INPUT_{name}", "{name}"]),

    # ── i2c / spi / gpio / clk ───────────────────────────────────────────
    ("drivers/i2c/busses/",   ["I2C_{name}"]),
    ("drivers/i2c/muxes/",    ["I2C_MUX_{name}"]),
    ("drivers/i2c/",          ["I2C_{name}"]),
    ("drivers/spi/",          ["SPI_{name}"]),
    ("drivers/gpio/",         ["GPIO_{name}"]),
    ("drivers/clk/",          ["COMMON_CLK_{name}", "CLK_{name}"]),

    # ── tty / serial ─────────────────────────────────────────────────────
    ("drivers/tty/serial/8250/", ["SERIAL_8250_{name}", "SERIAL_8250"]),
    ("drivers/tty/serial/",   ["SERIAL_{name}"]),
    ("drivers/tty/",          ["{name}"]),

    # ── ACPI / power / thermal ───────────────────────────────────────────
    ("drivers/acpi/dptf/",    ["ACPI_DPTF", "ACPI_DPTF_{name}"]),
    ("drivers/acpi/",         ["ACPI_{name}", "ACPI"]),
    ("drivers/idle/",         ["INTEL_IDLE", "ACPI_PROCESSOR_IDLE", "{name}"]),
    ("drivers/thermal/intel/", ["INTEL_{name}_THERMAL", "INT3400_THERMAL", "INT340X_THERMAL"]),
    ("drivers/thermal/",      ["THERMAL_{name}", "{name}_THERMAL", "{name}"]),
    ("drivers/power/supply/", ["{name}_BATTERY", "CHARGER_{name}", "{name}"]),
    ("drivers/power/",        ["{name}"]),

    # ── storage / ata ────────────────────────────────────────────────────
    ("drivers/ata/",          ["SATA_{name}", "PATA_{name}", "ATA_{name}", "{name}"]),

    # ── hwmon / sensors ──────────────────────────────────────────────────
    ("drivers/hwmon/",        ["SENSORS_{name}"]),

    # ── bluetooth / wireless extras ──────────────────────────────────────
    ("drivers/bluetooth/",    ["BT_HCIBTUSB_{name}", "BT_{name}", "BT_HCIBTUSB"]),

    # ── leds / rtc / regulator / pwm / dma / iommu ───────────────────────
    ("drivers/leds/",         ["LEDS_{name}"]),
    ("drivers/rtc/",          ["RTC_DRV_{name}", "RTC_{name}"]),
    ("drivers/regulator/",    ["REGULATOR_{name}"]),
    ("drivers/pwm/",          ["PWM_{name}"]),
    ("drivers/dma/idxd/",     ["INTEL_IDXD"]),
    ("drivers/dma/",          ["{name}_DMAC", "{name}_DMA", "{name}"]),
    ("drivers/iommu/intel/",  ["INTEL_IOMMU", "INTEL_IOMMU_SVM"]),
    ("drivers/iommu/amd/",    ["AMD_IOMMU"]),
    ("drivers/iommu/",        ["{name}_IOMMU", "{name}"]),
    ("drivers/edac/",         ["EDAC_{name}"]),
    ("drivers/firmware/efi/", ["EFI_{name}", "EFI"]),
    ("drivers/firmware/",     ["{name}", "FW_{name}"]),
    ("drivers/extcon/",       ["EXTCON_{name}"]),
    ("drivers/cxl/",          ["CXL_{name}"]),
    ("drivers/perf/",         ["{name}"]),
    ("drivers/pinctrl/intel/", ["PINCTRL_INTEL", "PINCTRL_{name}"]),
    ("drivers/pinctrl/",      ["PINCTRL_{name}"]),
    ("drivers/auxdisplay/",   ["AUXDISPLAY", "{name}"]),
    ("drivers/mmc/host/",     ["MMC_{name}", "MMC_SDHCI_{name}"]),
    ("drivers/mmc/",          ["{name}"]),

    # ── platform x86 (Dell, ThinkPad, ASUS, etc.) ────────────────────────
    ("drivers/platform/x86/dell/", ["DELL_{name}", "{name}", "DELL_LAPTOP"]),
    ("drivers/platform/x86/intel/", ["INTEL_{name}", "{name}"]),
    ("drivers/platform/x86/amd/",  ["AMD_{name}", "{name}"]),
    ("drivers/platform/x86/",  ["{name}", "X86_{name}_LAPTOP"]),
    ("drivers/platform/",     ["{name}"]),

    # ── misc / char drivers ──────────────────────────────────────────────
    ("drivers/misc/eeprom/",  ["EEPROM_{name}"]),
    ("drivers/misc/mei/",     ["INTEL_MEI", "INTEL_MEI_{name}"]),
    ("drivers/misc/",         ["{name}"]),
    ("drivers/char/tpm/",     ["TCG_{name}", "TCG_TPM"]),
    ("drivers/char/",         ["{name}"]),
    ("drivers/watchdog/",     ["{name}_WDT", "{name}"]),

    # ── infiniband / iio / media ─────────────────────────────────────────
    ("drivers/infiniband/",   ["INFINIBAND_{name}", "{name}"]),
    ("drivers/iio/",          ["{name}"]),
    ("drivers/media/cec/",    ["CEC_CORE", "MEDIA_CEC_SUPPORT", "{name}"]),
    ("drivers/media/",        ["{name}", "MEDIA_{name}"]),
    ("drivers/staging/",      ["{name}"]),

    # ── crypto / virtio (firmware handled above) ─────────────────────────
    ("drivers/crypto/",       ["CRYPTO_DEV_{name}", "{name}"]),
    ("drivers/virtio/",       ["VIRTIO_{name}", "{name}"]),
    ("drivers/cpufreq/",      ["{name}_CPUFREQ", "{name}"]),
    ("drivers/cpuidle/",      ["CPU_IDLE_GOV_{name}", "{name}"]),

    # ── filesystems ──────────────────────────────────────────────────────
    # fs/<name>/<file> usually maps to CONFIG_<NAME>_FS. The {fsdir} placeholder
    # will be filled by the candidate generator to use the *directory* name,
    # not the module name (e.g. fs/ext4/ext4 → CONFIG_EXT4_FS).
    ("fs/nfsd/",              ["NFSD"]),
    ("fs/cifs/",              ["CIFS"]),
    ("fs/9p/",                ["9P_FS"]),
    ("fs/",                   ["{fsdir}_FS", "{name}_FS", "{name}"]),

    # ── networking core ──────────────────────────────────────────────────
    ("net/wireguard/",        ["WIREGUARD"]),
    ("net/bluetooth/",        ["BT", "BT_{name}"]),
    ("net/sched/",            ["NET_SCH_{name}"]),
    ("net/netfilter/",        ["NETFILTER_{name}", "{name}", "NF_{name}"]),
    ("net/ipv4/netfilter/",   ["IP_NF_{name}", "NF_{name}"]),
    ("net/ipv6/netfilter/",   ["IP6_NF_{name}", "NF_{name}"]),
    ("net/",                  ["{name}"]),

    # ── crypto / lib ─────────────────────────────────────────────────────
    ("crypto/",               ["CRYPTO_{name}", "{name}"]),
    ("lib/crypto/",           ["CRYPTO_LIB_{name}", "CRYPTO_{name}"]),
    ("lib/",                  ["{name}"]),

    # ── arch ─────────────────────────────────────────────────────────────
    ("arch/x86/kvm/",         ["KVM_{name}", "KVM"]),
    # arch/x86/crypto/aesni-intel → CONFIG_CRYPTO_AES_NI_INTEL — name needs the
    # AES_NI normalisation. The plain CRYPTO_{name} also covers CONFIG_CRYPTO_*
    # for entries like sha512-ssse3 → SHA512_SSSE3.
    ("arch/x86/crypto/",      ["CRYPTO_{name}", "CRYPTO_AES_NI_INTEL"]),
    ("arch/x86/events/intel/", ["PERF_EVENTS_INTEL_{name}", "PERF_EVENTS_INTEL_UNCORE"]),
    ("arch/x86/events/",      ["{name}_EVENTS"]),
    ("arch/x86/platform/",    ["{name}"]),
    ("arch/x86/",             ["X86_{name}", "{name}"]),
    ("arch/arm64/",           ["ARM64_{name}", "{name}"]),
    ("arch/",                 ["{name}"]),

    # ── kernel core ──────────────────────────────────────────────────────
    ("kernel/",               ["{name}"]),
]


def _normalize(name: str) -> str:
    """Module name → CONFIG-style identifier fragment.

    Module names use ``-`` and lowercase; CONFIG_ symbols use ``_`` and
    uppercase. Some modules also have leading ``snd-`` etc. that carries
    over directly.
    """
    return name.upper().replace("-", "_")


def candidate_configs(
    module_name: str,
    source_path: str | None,
) -> list[str]:
    """Generate ordered candidate ``CONFIG_*`` symbol names for a module.

    Most-specific path-prefix candidates come first, followed by name-only
    fallbacks. De-duplicated, order preserved.
    """
    name_u = _normalize(module_name)
    out: list[str] = []

    if source_path:
        # `fs/ext4/ext4` → fsdir = "EXT4"
        parts = source_path.split("/")
        fsdir = _normalize(parts[1]) if len(parts) >= 2 and parts[0] == "fs" else ""

        path_with_slash = source_path + "/"  # so 'sound/soc/' matches 'sound/soc/foo/bar'
        for prefix, templates in _PATH_TABLE:
            if not path_with_slash.startswith(prefix):
                continue
            for tmpl in templates:
                rendered = tmpl.format(name=name_u, fsdir=fsdir)
                out.append(f"CONFIG_{rendered}")
            break  # only the first (most specific) prefix wins

    # Always also try the bare name as last resorts.
    out.append(f"CONFIG_{name_u}")
    if module_name.endswith("fs") or module_name.endswith("_fs"):
        out.append(f"CONFIG_{name_u}_FS")
    if module_name.endswith("_mod"):
        out.append(f"CONFIG_{name_u[:-4]}")

    # Dedupe, preserve order.
    seen: set[str] = set()
    deduped: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def resolve_module_to_config(
    module_name: str,
    source_path: str | None,
    running_config: dict[str, str],
    *,
    accept_values: Iterable[str] = ("y", "m"),
) -> str | None:
    """Walk candidates in order; return the first that exists in ``running_config``
    with an accepted value (default ``y`` or ``m``). Returns ``None`` if no
    candidate matches — caller should treat the module as load-bearing.
    """
    accept = set(accept_values)
    for cand in candidate_configs(module_name, source_path):
        if running_config.get(cand) in accept:
            return cand
    return None
