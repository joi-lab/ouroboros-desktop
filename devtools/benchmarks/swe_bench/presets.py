"""Official SWE-bench dataset preset names used by devtools."""

from __future__ import annotations


PRESETS: dict[str, str] = {
    "full": "princeton-nlp/SWE-bench",
    "lite": "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
}

ALIASES: dict[str, str] = {
    "swe-bench": PRESETS["full"],
    "swebench": PRESETS["full"],
    "swe-bench-lite": PRESETS["lite"],
    "swebench-lite": PRESETS["lite"],
    "swe-bench-verified": PRESETS["verified"],
    "swebench-verified": PRESETS["verified"],
    "SWE-bench/SWE-bench_Verified": PRESETS["verified"],
}


def resolve_preset(name: str) -> str:
    key = str(name or "").strip()
    if key in PRESETS:
        return PRESETS[key]
    if key in ALIASES:
        return ALIASES[key]
    return key
