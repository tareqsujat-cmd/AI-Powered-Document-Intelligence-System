import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.search import embeddings
from app.search.embeddings import SentenceTransformerEmbedder, preload_embedder_in_background


class CountingEmbedder:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error
        self.loaded = threading.Event()

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.loaded.set()
        if self.error:
            raise self.error
        return [[0.0] for _ in texts]


def test_preloading_uses_the_model_once_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder = CountingEmbedder()
    monkeypatch.setattr(embeddings, "get_embedder", lambda: embedder)

    thread = preload_embedder_in_background()
    thread.join(timeout=5)

    assert embedder.calls == 1
    assert thread.daemon is True


def test_a_failed_preload_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder = CountingEmbedder(error=OSError("the model file is missing"))
    monkeypatch.setattr(embeddings, "get_embedder", lambda: embedder)

    thread = preload_embedder_in_background()
    thread.join(timeout=5)

    assert embedder.calls == 1
    assert not thread.is_alive()


@pytest.mark.parametrize("enabled", [True, False])
def test_the_api_preloads_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    started: list[bool] = []
    monkeypatch.setattr("app.main.preload_embedder_in_background", lambda: started.append(True))
    settings = Settings(_env_file=None, preload_embedding_model=enabled)

    with TestClient(create_app(settings)):
        pass

    assert started == ([True] if enabled else [])


def test_it_is_off_unless_asked_for() -> None:
    assert Settings(_env_file=None).preload_embedding_model is False


def test_simultaneous_first_uses_load_the_model_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[str] = []

    class FakeModel:
        def __init__(self, name: str, device: str) -> None:
            loads.append(name)

        def encode(self, texts: list[str], **_: Any) -> list[list[float]]:
            return [[1.0] for _ in texts]

    import sentence_transformers

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", FakeModel)
    embedder = SentenceTransformerEmbedder(Settings(_env_file=None))

    threads = [threading.Thread(target=embedder.encode, args=(["x"],)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(loads) == 1
