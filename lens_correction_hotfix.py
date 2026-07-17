"""Hotfix for stale manual lens distortion values.

Lightroom catalogs may already contain LensManualDistortionAmount from an old
edit or a previous buggy preset application. The normal XMP merge intentionally
preserves keys absent from a preset, so that stale value could survive forever.

Rule: honor LensManualDistortionAmount when explicitly present in the XMP;
otherwise normalize it to 0 while applying a preset.
"""
from __future__ import annotations

import core

_INSTALLED = False
_ORIGINAL_PARSE = core.parse_xmp_preset


def _parse_xmp_preset_without_stale_manual_distortion(xmp_path):
    settings = _ORIGINAL_PARSE(xmp_path)
    settings.setdefault("LensManualDistortionAmount", "0")
    return settings


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    core.parse_xmp_preset = _parse_xmp_preset_without_stale_manual_distortion
    _INSTALLED = True
