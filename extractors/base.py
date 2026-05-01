from abc import ABC, abstractmethod


class BaseExtractor(ABC):
    name: str = ""

    @abstractmethod
    def matches(self, doc_text: str) -> bool:
        """Return True if this extractor can handle the given document."""

    @abstractmethod
    def extract(self, doc_text: str, filename: str) -> list[dict]:
        """Extract test cases and return list of row dicts."""
