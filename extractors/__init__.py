import importlib
import pkgutil
from pathlib import Path

from .base import BaseExtractor

_extractors: list[BaseExtractor] | None = None


def _load_extractors() -> list[BaseExtractor]:
    pkg_dir = Path(__file__).parent
    for _, module_name, _ in pkgutil.iter_modules([str(pkg_dir)]):
        if module_name in ("base",):
            continue
        importlib.import_module(f"extractors.{module_name}")

    return [cls() for cls in BaseExtractor.__subclasses__()]


def get_extractors() -> list[BaseExtractor]:
    global _extractors
    if _extractors is None:
        _extractors = _load_extractors()
    return _extractors


def find_extractor(doc_text: str) -> BaseExtractor | None:
    for ext in get_extractors():
        if ext.matches(doc_text):
            return ext
    return None
