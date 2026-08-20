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
