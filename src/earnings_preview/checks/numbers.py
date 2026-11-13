"""Numeric provenance checks for generated previews."""
import re


def _numbers(text: str) -> set[str]:
    """Numeric tokens, normalised (strip %, $, commas, trailing zeros)."""
    out = set()
    for tok in re.findall(r"\d[\d,]*\.?\d*", text):
        tok = tok.replace(",", "").rstrip(".")
        if "." in tok:
            tok = tok.rstrip("0").rstrip(".")
        out.add(tok)
    return out


def check_numbers(note: str, tool_outputs: list[str]) -> list[str]:
    """Return numbers in the note that never appear in any tool output.

    Years and tiny integers are ignored to keep false positives down. Derived
    figures (a difference you computed) will be flagged too; treat the result
    as 'please verify', not as proof of error.
    """
    seen = set()
    for out in tool_outputs:
        seen |= _numbers(out)
    flagged = []
    for n in sorted(_numbers(note)):
        if n in seen:
            continue
        if re.fullmatch(r"(19|20)\d\d", n) or (n.isdigit() and int(n) <= 12):
            continue
        flagged.append(n)
    return flagged

