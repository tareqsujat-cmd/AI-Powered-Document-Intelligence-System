from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from app.core.config import Settings, get_settings

# all-MiniLM-L6-v2 reads about 256 tokens, which is roughly this many characters of invoice text.
CHUNK_CHARS = 1000
CHUNK_OVERLAP_CHARS = 100


class Embedder(Protocol):
    @property
    def model_name(self) -> str: ...

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """One unit-length vector per text."""
        ...


class SentenceTransformerEmbedder:
    """Loads the model on first use, as it takes time and hundreds of MB of memory."""

    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.embedding_model
        self._batch_size = settings.embedding_batch_size
        self._model: Any = None

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if self._model is None:
            # Imported here because it pulls in torch, which slows every process that imports it.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device="cpu")
        vectors = self._model.encode(
            list(texts),
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in vector] for vector in vectors]


@lru_cache
def get_embedder() -> SentenceTransformerEmbedder:
    return SentenceTransformerEmbedder(get_settings())


@dataclass(frozen=True, slots=True)
class TextChunk:
    page_number: int
    index: int
    content: str


def chunk_page_text(text: str, max_chars: int = CHUNK_CHARS) -> Iterator[str]:
    """Split at line boundaries where possible, with a little overlap to keep context."""
    current = ""
    for line in text.split("\n"):
        while len(line) > max_chars:
            if current:
                yield current
                current = ""
            yield line[:max_chars]
            line = line[max_chars - CHUNK_OVERLAP_CHARS :]
        if current and len(current) + len(line) + 1 > max_chars:
            yield current
            current = current[-CHUNK_OVERLAP_CHARS:]
            current = current[current.find("\n") + 1 :] if "\n" in current else ""
        current = f"{current}\n{line}" if current else line
    if current.strip():
        yield current


def chunk_pages(pages: Iterator[tuple[int, str]], limit: int) -> Iterator[TextChunk]:
    index = 0
    for page_number, text in pages:
        for content in chunk_page_text(text):
            if index >= limit:
                return
            yield TextChunk(page_number=page_number, index=index, content=content)
            index += 1
