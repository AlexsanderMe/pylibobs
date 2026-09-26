"""
Unit tests for the libobs 32.2 API additions and the declaration invariants
they depend on. No libobs needed.
"""

from __future__ import annotations

import re

from pylibobs import EncoderCaps
from pylibobs import _declarations, _declarations_auto


def _real_declarations() -> str:
    """Every cdef string pylibobs feeds cffi, minus the void(void) stubs."""
    parts = [v for k, v in vars(_declarations).items() if k.isupper() and isinstance(v, str)]
    parts.append(_declarations_auto.AUTO_DECLS)
    return "\n".join(parts)


def _declared_signature(name: str) -> str | None:
    m = re.search(rf"^[^\n/]*\b{name}\s*\([^;]*\);", _real_declarations(), re.M)
    return m.group(0).strip() if m else None


def test_encoder_caps_match_obs_encoder_h():
    # OBS_ENCODER_CAP_* in libobs/obs-encoder.h (32.2.2)
    assert EncoderCaps.DEPRECATED == 1 << 0
    assert EncoderCaps.PASS_TEXTURE == 1 << 1
    assert EncoderCaps.DYN_BITRATE == 1 << 2
    assert EncoderCaps.INTERNAL == 1 << 3
    assert EncoderCaps.ROI == 1 << 4
    assert EncoderCaps.SCALING == 1 << 5
    assert EncoderCaps.MULTITRACK_DYN_BITRATE == 1 << 6


def test_themed_icon_functions_have_real_signatures():
    """The generator can't parse `const char *name(`, so these must be curated —
    a void(void) stub would be callable but pass no argument."""
    for name in ("obs_source_get_dark_icon", "obs_source_get_light_icon"):
        sig = _declared_signature(name)
        assert sig is not None, f"{name} is not declared"
        assert "const char *" in sig and "const char *id" in sig, sig
        assert name not in getattr(_declarations_auto, "STUB_DECLS", ""), f"{name} is a stub"


def test_functions_mentioned_in_comments_stay_declared():
    """_declarations.py mentions these in prose; regenerating _declarations_auto.py
    once counted those mentions as declarations and dropped all three."""
    for name in ("calldata_get_data", "gs_clear", "obs_source_output_video"):
        assert _declared_signature(name) is not None, f"{name} lost its declaration"


def test_generator_counts_only_real_declarations():
    """scripts/generate_cdef.py leaves out whatever _declarations.py declares.
    If it counted a mere mention (a comment, a docstring, an error message),
    the function would end up declared nowhere."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    try:
        import generate_cdef
    finally:
        sys.path.pop(0)

    curated = "\n".join(v for k, v in vars(_declarations).items()
                        if k.isupper() and isinstance(v, str)
                        and k not in ("PREAMBLE", "AUTO_DECLS", "STUB_DECLS"))
    phantom = sorted(name for name in generate_cdef.curated_names()
                     if not re.search(rf"^[^\n/]*\b{name}\s*\([^;]*\);", curated, re.M))
    assert not phantom, f"counted as declared but only mentioned: {phantom}"
