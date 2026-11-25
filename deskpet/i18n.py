"""Language selection for desktop UI strings and model-facing preferences."""

from typing import Literal


Language = Literal["zh", "en"]
LANGUAGES = ("zh", "en")
DEFAULT_LANGUAGE: Language = "zh"
_language: Language = DEFAULT_LANGUAGE


def set_language(value: Language) -> None:
    if value not in LANGUAGES:
        raise ValueError("Unsupported desktop language")
    global _language
    _language = value


def language() -> Language:
    return _language


def tr(chinese: str, english: str) -> str:
    return english if _language == "en" else chinese
