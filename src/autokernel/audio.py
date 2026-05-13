"""Audio evidence classification and keep-set helpers.

Audio is not a generic optional subsystem on user-facing machines. Modern
laptop audio usually spans PCI HDA, SOF DSP, SoundWire/HDA links, machine
drivers, codec/amplifier drivers, firmware, and userspace UCM policy. A
pure ``lsmod`` snapshot can miss late-bound codecs and USB/Bluetooth audio
paths, so this module turns several weak host signals into one explicit
``AudioContext`` for the resolver and LLM prompts.
"""

from __future__ import annotations

from collections.abc import Iterable

from autokernel.models import (
    AudioContext,
    LoadedModule,
    PciDevice,
    SoftwareFeature,
    SystemIdentity,
    UsbDevice,
)


_USER_FACING_CHASSIS = {
    # SMBIOS chassis enum values used by laptop-detect/systemd heuristics.
    3,  # desktop
    4,  # low profile desktop
    5,  # pizza box
    6,  # mini tower
    7,  # tower
    8,  # portable
    9,  # laptop
    10,  # notebook
    11,  # handheld
    14,  # sub notebook
    30,  # tablet
    31,  # convertible
    32,  # detachable
    35,  # mini PC
}

_AUDIO_MODULE_PREFIXES = ("snd", "soundwire")
_AUDIO_MODULE_FRAGMENTS = ("_sof", "sof_", "hda", "sdw", "sdca")
_AUDIO_SOFTWARE_FEATURES = {"audio", "desktop", "bluetooth"}
_AUDIO_USB_WORDS = ("audio", "headset", "speaker", "microphone", "mic", "dac")


# These are intentionally broader than the currently loaded module set. They
# cover the common late-bound pieces that make internal laptop audio and audio
# hotplug work after ``make localmodconfig``.
AUDIO_KEEP_MODULES: frozenset[str] = frozenset(
    {
        # ALSA core and common PCM plumbing
        "snd",
        "soundcore",
        "snd_pcm",
        "snd_timer",
        "snd_seq",
        "snd_seq_device",
        "snd_hrtimer",
        "snd_hwdep",
        "snd_compress",
        "snd_jack",
        # HDA controller and codecs
        "snd_hda_intel",
        "snd_hda_codec",
        "snd_hda_core",
        "snd_hda_ext_core",
        "snd_hda_codec_generic",
        "snd_hda_codec_hdmi",
        "snd_hda_codec_realtek",
        "snd_hda_codec_cirrus",
        "snd_hda_codec_conexant",
        "snd_hda_codec_idt",
        "snd_hda_codec_via",
        "snd_hda_codec_cs8409",
        "snd_hda_scodec_component",
        "snd_hda_scodec_cs35l41",
        "snd_hda_scodec_cs35l41_i2c",
        "snd_hda_scodec_cs35l41_spi",
        "snd_hda_scodec_cs35l56",
        "snd_hda_scodec_cs35l56_i2c",
        "snd_hda_scodec_cs35l56_spi",
        "snd_hda_scodec_tas2781_i2c",
        "snd_hda_scodec_tas2781_spi",
        # SOF controller/DSP paths
        "snd_sof",
        "snd_sof_pci",
        "snd_sof_pci_intel_mtl",
        "snd_sof_pci_intel_lnl",
        "snd_sof_intel_hda",
        "snd_sof_intel_hda_common",
        "snd_sof_intel_hda_generic",
        "snd_sof_intel_hda_mlink",
        "snd_sof_intel_hda_sdw_bpt",
        "snd_sof_xtensa_dsp",
        "snd_sof_utils",
        "snd_soc_hdac_hda",
        "snd_soc_intel_hda_dsp_common",
        "snd_soc_skl_hda_dsp",
        "snd_soc_sof_sdw",
        # SoundWire + SDCA
        "soundwire_bus",
        "soundwire_cadence",
        "soundwire_generic_allocation",
        "soundwire_intel",
        "snd_soc_sdw_utils",
        "snd_soc_sdca",
        "snd_soc_sdca_class",
        "snd_soc_sdca_class_function",
        # Common laptop codec/amplifier families.
        "snd_soc_cs35l41",
        "snd_soc_cs35l41_i2c",
        "snd_soc_cs35l41_spi",
        "snd_soc_cs35l56",
        "snd_soc_cs35l56_i2c",
        "snd_soc_cs35l56_spi",
        "snd_soc_cs35l56_sdw",
        "snd_soc_da7219",
        "snd_soc_max98357a",
        "snd_soc_max98363",
        "snd_soc_max98373",
        "snd_soc_max98373_i2c",
        "snd_soc_max98373_sdw",
        "snd_soc_max98927",
        "snd_soc_nau8825",
        "snd_soc_rt1011",
        "snd_soc_rt1015",
        "snd_soc_rt1015p",
        "snd_soc_rt1308",
        "snd_soc_rt1308_sdw",
        "snd_soc_rt1316_sdw",
        "snd_soc_rt1318_sdw",
        "snd_soc_rt1320_sdw",
        "snd_soc_rt5682",
        "snd_soc_rt5682_i2c",
        "snd_soc_rt5682_sdw",
        "snd_soc_rt5682s",
        "snd_soc_rt700",
        "snd_soc_rt700_sdw",
        "snd_soc_rt711",
        "snd_soc_rt711_sdca",
        "snd_soc_rt711_sdca_sdw",
        "snd_soc_rt711_sdw",
        "snd_soc_rt712_sdca",
        "snd_soc_rt712_sdca_dmic",
        "snd_soc_rt715",
        "snd_soc_rt715_sdca",
        "snd_soc_rt715_sdca_sdw",
        "snd_soc_rt715_sdw",
        "snd_soc_rt721_sdca",
        "snd_soc_rt721_sdca_sdw",
        "snd_soc_rt722_sdca",
        "snd_soc_rt722_sdca_sdw",
        # USB audio hotplug for headsets, docks, microphones, and DACs.
        "snd_usb_audio",
        "snd_usb_audio_qmi",
        "snd_usbmidi_lib",
    }
)

AUDIO_KEEP_CONFIGS: frozenset[str] = frozenset(
    {
        "CONFIG_SOUND",
        "CONFIG_SND",
        "CONFIG_SND_PCM",
        "CONFIG_SND_TIMER",
        "CONFIG_SND_HRTIMER",
        "CONFIG_SND_HWDEP",
        "CONFIG_SND_COMPRESS",
        "CONFIG_SND_JACK",
        "CONFIG_SND_DYNAMIC_MINORS",
        "CONFIG_SND_HDA_INTEL",
        "CONFIG_SND_HDA_CODEC",
        "CONFIG_SND_HDA_CODEC_GENERIC",
        "CONFIG_SND_HDA_CODEC_HDMI",
        "CONFIG_SND_HDA_CODEC_REALTEK",
        "CONFIG_SND_HDA_CODEC_CIRRUS",
        "CONFIG_SND_HDA_CODEC_CONEXANT",
        "CONFIG_SND_HDA_CODEC_IDT",
        "CONFIG_SND_HDA_CODEC_VIA",
        "CONFIG_SND_HDA_CODEC_CS8409",
        "CONFIG_SND_HDA_EXT_CORE",
        "CONFIG_SND_SOC",
        "CONFIG_SND_SOC_SOF",
        "CONFIG_SND_SOC_SOF_PCI",
        "CONFIG_SND_SOC_SOF_PCI_DEV",
        "CONFIG_SND_SOC_SOF_INTEL_TOPLEVEL",
        "CONFIG_SND_SOC_SOF_INTEL_MTL",
        "CONFIG_SND_SOC_SOF_INTEL_LNL",
        "CONFIG_SND_SOC_SOF_HDA",
        "CONFIG_SND_SOC_SOF_HDA_COMMON",
        "CONFIG_SND_SOC_SOF_HDA_GENERIC",
        "CONFIG_SND_SOC_SOF_HDA_MLINK",
        "CONFIG_SND_SOC_INTEL_HDA_DSP_COMMON",
        "CONFIG_SND_SOC_INTEL_SOUNDWIRE_SOF_MACH",
        "CONFIG_SOUNDWIRE",
        "CONFIG_SOUNDWIRE_INTEL",
        "CONFIG_SOUNDWIRE_CADENCE",
        "CONFIG_SOUNDWIRE_GENERIC_ALLOCATION",
        "CONFIG_SND_SOC_SDCA",
        "CONFIG_SND_SOC_SDCA_CLASS",
        "CONFIG_SND_SOC_SDW_UTILS",
        "CONFIG_SND_SOC_CS35L41",
        "CONFIG_SND_SOC_CS35L41_I2C",
        "CONFIG_SND_SOC_CS35L41_SPI",
        "CONFIG_SND_SOC_CS35L56",
        "CONFIG_SND_SOC_CS35L56_I2C",
        "CONFIG_SND_SOC_CS35L56_SPI",
        "CONFIG_SND_SOC_CS35L56_SDW",
        "CONFIG_SND_SOC_DA7219",
        "CONFIG_SND_SOC_MAX98357A",
        "CONFIG_SND_SOC_MAX98363",
        "CONFIG_SND_SOC_MAX98373",
        "CONFIG_SND_SOC_MAX98373_I2C",
        "CONFIG_SND_SOC_MAX98373_SDW",
        "CONFIG_SND_SOC_MAX98927",
        "CONFIG_SND_SOC_NAU8825",
        "CONFIG_SND_SOC_RT1011",
        "CONFIG_SND_SOC_RT1015",
        "CONFIG_SND_SOC_RT1015P",
        "CONFIG_SND_SOC_RT1308",
        "CONFIG_SND_SOC_RT1308_SDW",
        "CONFIG_SND_SOC_RT1316_SDW",
        "CONFIG_SND_SOC_RT1318_SDW",
        "CONFIG_SND_SOC_RT1320_SDW",
        "CONFIG_SND_SOC_RT5682",
        "CONFIG_SND_SOC_RT5682_I2C",
        "CONFIG_SND_SOC_RT5682_SDW",
        "CONFIG_SND_SOC_RT5682S",
        "CONFIG_SND_SOC_RT700",
        "CONFIG_SND_SOC_RT700_SDW",
        "CONFIG_SND_SOC_RT711",
        "CONFIG_SND_SOC_RT711_SDCA_SDW",
        "CONFIG_SND_SOC_RT711_SDW",
        "CONFIG_SND_SOC_RT712_SDCA_SDW",
        "CONFIG_SND_SOC_RT712_SDCA_DMIC_SDW",
        "CONFIG_SND_SOC_RT715",
        "CONFIG_SND_SOC_RT715_SDCA_SDW",
        "CONFIG_SND_SOC_RT715_SDW",
        "CONFIG_SND_SOC_RT721_SDCA_SDW",
        "CONFIG_SND_SOC_RT722_SDCA_SDW",
        "CONFIG_SND_USB_AUDIO",
        "CONFIG_SND_USB_AUDIO_USE_MEDIA_CONTROLLER",
    }
)


def _is_audio_module(name: str) -> bool:
    lower = name.lower()
    return lower.startswith(_AUDIO_MODULE_PREFIXES) or any(
        frag in lower for frag in _AUDIO_MODULE_FRAGMENTS
    )


def _is_audio_pci(device: PciDevice) -> bool:
    class_id = (device.class_id or "").lower()
    description = (device.description or "").lower()
    return class_id.startswith(("0401", "0403")) or "audio" in description


def _is_audio_usb(device: UsbDevice) -> bool:
    description = (device.description or "").lower()
    return any(word in description for word in _AUDIO_USB_WORDS)


def _chassis_is_user_facing(system: SystemIdentity) -> bool:
    return system.chassis_type in _USER_FACING_CHASSIS


def build_audio_context(
    *,
    system: SystemIdentity,
    pci: Iterable[PciDevice],
    usb: Iterable[UsbDevice],
    loaded_modules: Iterable[LoadedModule],
    software_features: Iterable[SoftwareFeature],
    asound_cards: Iterable[str] = (),
    asound_pcm: Iterable[str] = (),
    dev_snd: Iterable[str] = (),
    sys_class_sound: Iterable[str] = (),
) -> AudioContext:
    """Classify whether audio should be treated as useful for this host."""

    pci_audio = [d for d in pci if _is_audio_pci(d)]
    usb_audio = [d for d in usb if _is_audio_usb(d)]
    modules = sorted({m.name for m in loaded_modules if _is_audio_module(m.name)})
    userspace = sorted(
        {
            f"{s.source}:{s.name}"
            for s in software_features
            if s.feature in _AUDIO_SOFTWARE_FEATURES
        }
    )
    cards = [
        line.strip()
        for line in asound_cards
        if line.strip() and "no soundcards" not in line.lower()
    ]
    pcms = [line.strip() for line in asound_pcm if line.strip()]
    snd_nodes = [
        line.strip()
        for line in dev_snd
        if any(token in line for token in ("controlC", "pcmC", "seq", "timer"))
    ]
    sound_nodes = [line.strip() for line in sys_class_sound if line.strip()]

    evidence: list[str] = []
    if _chassis_is_user_facing(system):
        evidence.append(f"user-facing chassis_type={system.chassis_type}")
    for d in pci_audio[:6]:
        evidence.append(
            f"PCI audio {d.slot} {d.vendor_id}:{d.device_id} driver={d.driver or '-'}"
        )
    for d in usb_audio[:4]:
        evidence.append(f"USB audio-ish {d.vendor_id}:{d.product_id} {d.description}")
    if cards:
        evidence.append(f"ALSA cards present={len(cards)}")
    if pcms:
        evidence.append(f"ALSA PCM devices present={len(pcms)}")
    if snd_nodes:
        evidence.append("/dev/snd exposes ALSA device nodes")
    if sound_nodes:
        evidence.append("/sys/class/sound has devices")
    if modules:
        evidence.append(f"audio modules loaded={', '.join(modules[:8])}")
    if userspace:
        evidence.append(f"audio userspace={', '.join(userspace[:8])}")

    role = "unused"
    confidence = 0.0
    useful = False

    if cards or pcms or snd_nodes:
        useful = True
        confidence = 0.98
        role = "internal-sof" if any("sof" in m for m in modules) else "internal"
    elif pci_audio and modules:
        useful = True
        confidence = 0.95
        role = "internal-sof" if any("sof" in m for m in modules) else "internal"
    elif pci_audio and _chassis_is_user_facing(system):
        useful = True
        confidence = 0.90
        role = "internal"
    elif userspace and (pci_audio or usb_audio or _chassis_is_user_facing(system)):
        useful = True
        confidence = 0.82
        role = "userspace"
    elif usb_audio and _chassis_is_user_facing(system):
        useful = True
        confidence = 0.75
        role = "usb-hotplug"
    elif userspace:
        # Weak: a server can have pipewire libraries installed incidentally.
        useful = True
        confidence = 0.55
        role = "userspace"

    return AudioContext(
        useful=useful,
        confidence=confidence,
        role=role,
        evidence=evidence,
        cards=cards[:12],
        pcms=pcms[:24],
        modules=modules,
        userspace=userspace,
    )


def audio_keep_modules(ctx: AudioContext) -> set[str]:
    if not ctx.useful:
        return set()
    return set(AUDIO_KEEP_MODULES) | set(ctx.modules)


def audio_keep_configs(ctx: AudioContext) -> set[str]:
    if not ctx.useful:
        return set()
    return set(AUDIO_KEEP_CONFIGS)


def render_audio_summary(ctx: AudioContext) -> str:
    if not ctx.useful:
        return "# Audio: not detected as useful"
    evidence = "; ".join(ctx.evidence[:6])
    return (
        f"# Audio: useful role={ctx.role} confidence={ctx.confidence:.2f}; "
        f"protect internal codec/SOF/SoundWire/HDA/USB audio paths"
        + (f"; evidence: {evidence}" if evidence else "")
    )
