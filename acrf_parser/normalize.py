"""Text normalization shared by field and annotation extraction (Phases 3-5)."""
import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def clean(text: str) -> str:
    """Light cleanup: unicode fold, collapse whitespace. Keeps case + punctuation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(" ", " ").replace("\r", "\n")
    return _WS.sub(" ", text).strip()


def normalize(text: str) -> str:
    """Full normalization: lowercase, drop punctuation, collapse whitespace."""
    return _WS.sub(" ", _PUNCT.sub(" ", clean(text).lower())).strip()


_TOKEN = re.compile(r"[A-Za-z0-9]+")


def statement_key(text: str) -> tuple[str, ...]:
    """Identity of an annotation *statement*: its word set, upper-cased.

    Annotators re-order and re-qualify the same mapping without changing what it
    says - "SUPPDM.QVAL when QNAM=RACEOR" and "QVAL when SUPPDM.QNAM = RACEOR"
    are one statement written twice, and comparing strings would call them two.
    Used wherever the question is "have we already got this one?": collecting the
    annotation set for a field, catching a sibling row that repeats another, and
    de-duplicating what gets drawn on the page.
    """
    return tuple(sorted({t.upper() for t in _TOKEN.findall(text or "")}))
