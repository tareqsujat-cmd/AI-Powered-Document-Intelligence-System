from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.enums import DocumentStatus, FileType
from app.db.models import Document
from app.pipeline.processor import process_document
from app.search.embeddings import chunk_page_text, chunk_pages
from app.search.service import (
    MIN_SIMILARITY,
    SearchFilters,
    SearchMode,
    hybrid_search,
    keyword_search,
    search,
    semantic_search,
)
from tests.pipeline_helpers import FakeEmbedder, FakeLLM, field, make_document, make_pdf


def lines_for(number: str, supplier: str, item: str) -> list[str]:
    return [
        f"Invoice Number: {number}",
        "Invoice Date: 2026-05-01",
        f"Supplier: {supplier}",
        "Bill To: Contoso Retail",
        f"{item} 2 500.00 1,000.00",
        "Subtotal 1,000.00",
        "Tax 100.00",
        "Total GBP 1,100.00",
    ]


def answer_for(number: str, supplier: str, item: str) -> dict:
    return {
        "invoice_number": field(number),
        "invoice_date": field("2026-05-01"),
        "supplier_name": field(supplier),
        "customer_name": field("Contoso Retail"),
        "subtotal": field("1,000.00"),
        "tax": field("100.00"),
        "total": field("1,100.00"),
        "currency": field("GBP"),
        "line_items": [
            {
                "description": item,
                "quantity": "2",
                "unit_price": "500.00",
                "amount": "1,000.00",
                "confidence": 0.9,
            }
        ],
    }


@pytest.fixture
def corpus(db: Session, tmp_path: Path, settings: Settings) -> dict[str, Document]:
    """Three processed invoices with distinct subject matter."""
    documents: dict[str, Document] = {}
    embedder = FakeEmbedder()
    for name, number, supplier, item in (
        ("bearings", "INV-1001", "Northwind Traders", "Industrial hydraulic bearing assembly"),
        ("cloud", "INV-2002", "Fabrikam Software", "Annual cloud hosting subscription"),
        ("catering", "XYZ-3003", "Adventure Foods", "Office lunch catering platter"),
    ):
        path = make_pdf(tmp_path / f"{name}.pdf", pages=[lines_for(number, supplier, item)])
        document = make_document(db, path)
        process_document(
            db,
            document.id,
            settings=settings,
            get_llm_client=lambda n=number, s=supplier, i=item: FakeLLM(answer_for(n, s, i)),
            embedder=embedder,
        )
        documents[name] = document
    return documents


@pytest.mark.db
class TestKeywordSearch:
    def test_finds_a_word_on_the_right_document(self, db: Session, corpus: dict) -> None:
        hits = keyword_search(db, "hydraulic", SearchFilters(), 10)

        assert [hit.document_id for hit in hits] == [corpus["bearings"].id]
        assert hits[0].filename == "bearings.pdf"
        assert hits[0].page_number == 1
        assert hits[0].matched_by == ["keyword"]
        assert "hydraulic" in hits[0].snippet.lower()

    def test_stemming_matches_word_forms(self, db: Session, corpus: dict) -> None:
        hits = keyword_search(db, "subscriptions", SearchFilters(), 10)

        assert [hit.document_id for hit in hits] == [corpus["cloud"].id]

    def test_exact_invoice_number_ignores_formatting_and_ranks_first(
        self, db: Session, corpus: dict
    ) -> None:
        for query in ("INV-1001", "inv 1001", "INV1001"):
            hits = keyword_search(db, query, SearchFilters(), 10)
            assert hits[0].document_id == corpus["bearings"].id, query
            assert hits[0].score == 1.0, query

    def test_supplier_name_substring(self, db: Session, corpus: dict) -> None:
        hits = keyword_search(db, "Fabrikam", SearchFilters(), 10)

        assert hits[0].document_id == corpus["cloud"].id

    def test_a_document_appears_once(self, db: Session, corpus: dict) -> None:
        hits = keyword_search(db, "Contoso", SearchFilters(), 10)

        assert len({hit.document_id for hit in hits}) == len(hits) == 3

    def test_no_match_is_empty(self, db: Session, corpus: dict) -> None:
        assert keyword_search(db, "zebrafish", SearchFilters(), 10) == []

    @pytest.mark.parametrize(
        "query",
        [
            "'; DROP TABLE documents; --",
            '" OR 1=1 --',
            "100%",
            "a_b",
            "\\",
            "(((",
            "&&& |||",
            ":*",
        ],
    )
    def test_hostile_queries_are_harmless(self, db: Session, corpus: dict, query: str) -> None:
        keyword_search(db, query, SearchFilters(), 10)

        assert db.get(Document, corpus["cloud"].id) is not None

    def test_like_wildcards_are_matched_literally(self, db: Session, corpus: dict) -> None:
        assert keyword_search(db, "%", SearchFilters(), 10) == []
        assert keyword_search(db, "North_ind", SearchFilters(), 10) == []

    def test_snippet_has_no_highlight_markup(self, db: Session, corpus: dict) -> None:
        hit = keyword_search(db, "hydraulic", SearchFilters(), 10)[0]

        assert "<b>" not in hit.snippet


@pytest.mark.db
class TestFilters:
    def test_file_type(self, db: Session, corpus: dict) -> None:
        assert keyword_search(db, "Contoso", SearchFilters(file_type=FileType.CSV), 10) == []
        assert len(keyword_search(db, "Contoso", SearchFilters(file_type=FileType.PDF), 10)) == 3

    def test_status(self, db: Session, corpus: dict) -> None:
        assert keyword_search(db, "Contoso", SearchFilters(status=DocumentStatus.FAILED), 10) == []

    def test_needs_review(self, db: Session, corpus: dict) -> None:
        corpus["cloud"].needs_review = True
        db.commit()

        hits = keyword_search(db, "Contoso", SearchFilters(needs_review=True), 10)

        assert [hit.document_id for hit in hits] == [corpus["cloud"].id]

    def test_semantic_search_respects_filters(self, db: Session, corpus: dict) -> None:
        embedder = FakeEmbedder()

        assert (
            semantic_search(db, "cloud", embedder, SearchFilters(file_type=FileType.CSV), 10) == []
        )

    def test_limit(self, db: Session, corpus: dict) -> None:
        assert len(keyword_search(db, "Contoso", SearchFilters(), 2)) == 2


@pytest.mark.db
class TestSemanticSearch:
    def test_ranks_the_closest_passage_first(self, db: Session, corpus: dict) -> None:
        hits = semantic_search(
            db, "cloud hosting subscription", FakeEmbedder(), SearchFilters(), 10
        )

        assert hits[0].document_id == corpus["cloud"].id
        assert hits[0].matched_by == ["semantic"]
        assert hits[0].score >= MIN_SIMILARITY
        assert hits[0].page_number == 1

    def test_scores_descend(self, db: Session, corpus: dict) -> None:
        hits = semantic_search(db, "Contoso Retail invoice", FakeEmbedder(), SearchFilters(), 10)

        assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)

    def test_unrelated_query_returns_nothing(self, db: Session, corpus: dict) -> None:
        assert semantic_search(db, "zebrafish quantum", FakeEmbedder(), SearchFilters(), 10) == []

    def test_vectors_from_another_model_are_ignored(self, db: Session, corpus: dict) -> None:
        class OtherModel(FakeEmbedder):
            model_name = "another-model"

        assert semantic_search(db, "cloud hosting", OtherModel(), SearchFilters(), 10) == []


@pytest.mark.db
class TestHybridSearch:
    def test_finds_what_either_method_finds(self, db: Session, corpus: dict) -> None:
        by_number = hybrid_search(db, "XYZ-3003", FakeEmbedder(), SearchFilters(), 10)
        by_words = hybrid_search(db, "lunch catering", FakeEmbedder(), SearchFilters(), 10)

        assert by_number[0].document_id == corpus["catering"].id
        assert by_words[0].document_id == corpus["catering"].id

    def test_an_exact_invoice_number_returns_only_that_invoice(
        self, db: Session, corpus: dict
    ) -> None:
        hits = hybrid_search(db, "inv 1001", FakeEmbedder(), SearchFilters(), 10)

        assert [hit.document_id for hit in hits] == [corpus["bearings"].id]
        assert hits[0].matched_by == ["keyword"]

    def test_a_match_by_both_methods_says_so_and_ranks_first(
        self, db: Session, corpus: dict
    ) -> None:
        hits = hybrid_search(db, "catering platter", FakeEmbedder(), SearchFilters(), 10)

        assert hits[0].document_id == corpus["catering"].id
        assert set(hits[0].matched_by) == {"keyword", "semantic"}

    def test_each_document_once_and_scores_descend(self, db: Session, corpus: dict) -> None:
        hits = hybrid_search(db, "Contoso Retail", FakeEmbedder(), SearchFilters(), 10)

        assert len({hit.document_id for hit in hits}) == len(hits)
        assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)

    def test_limit_applies_after_merging(self, db: Session, corpus: dict) -> None:
        assert len(hybrid_search(db, "Contoso Retail", FakeEmbedder(), SearchFilters(), 1)) == 1

    @pytest.mark.parametrize("mode", list(SearchMode))
    def test_dispatch(self, db: Session, corpus: dict, mode: SearchMode) -> None:
        hits = search(db, "cloud hosting", mode, FakeEmbedder(), SearchFilters(), 10)

        assert hits[0].document_id == corpus["cloud"].id


class TestChunking:
    def test_short_text_is_one_chunk(self) -> None:
        assert list(chunk_page_text("hello\nworld")) == ["hello\nworld"]

    def test_empty_text_gives_nothing(self) -> None:
        assert list(chunk_page_text("")) == []
        assert list(chunk_page_text("  \n ")) == []

    def test_long_text_splits_at_lines_and_loses_nothing(self) -> None:
        lines = [f"row {index} widget 10.00" for index in range(200)]

        chunks = list(chunk_page_text("\n".join(lines), max_chars=300))

        assert len(chunks) > 1
        assert all(len(chunk) <= 300 for chunk in chunks)
        joined = "\n".join(chunks)
        assert all(line in joined for line in lines)

    def test_a_single_enormous_line_is_split(self) -> None:
        chunks = list(chunk_page_text("x" * 5000, max_chars=1000))

        assert all(len(chunk) <= 1000 for chunk in chunks)
        assert sum(len(chunk) for chunk in chunks) >= 5000

    def test_chunks_are_numbered_across_pages_and_keep_their_page(self) -> None:
        chunks = list(chunk_pages(iter([(3, "first"), (7, "second")]), limit=10))

        assert [(chunk.index, chunk.page_number, chunk.content) for chunk in chunks] == [
            (0, 3, "first"),
            (1, 7, "second"),
        ]

    def test_the_chunk_limit_is_respected(self) -> None:
        chunks = list(chunk_pages(iter([(page, "text") for page in range(1, 100)]), limit=5))

        assert len(chunks) == 5


@pytest.mark.embedding
class TestRealModel:
    def test_real_embeddings_are_384_dimensions_and_unit_length(self, settings: Settings) -> None:
        from app.search.embeddings import SentenceTransformerEmbedder

        vectors = SentenceTransformerEmbedder(settings).encode(["invoice for cloud hosting"])

        assert len(vectors[0]) == 384
        assert sum(value * value for value in vectors[0]) == pytest.approx(1.0, abs=1e-4)

    def test_meaning_is_matched_with_no_shared_words(self, settings: Settings) -> None:
        from app.search.embeddings import SentenceTransformerEmbedder

        embedder = SentenceTransformerEmbedder(settings)
        query, related, unrelated = embedder.encode(
            [
                "server rental fees",
                "Annual cloud hosting subscription",
                "Office lunch catering platter",
            ]
        )

        def similarity(first: list[float], second: list[float]) -> float:
            return sum(a * b for a, b in zip(first, second, strict=True))

        assert similarity(query, related) > similarity(query, unrelated)

    def test_the_similarity_cutoff_separates_related_from_unrelated_queries(
        self, settings: Settings
    ) -> None:
        from app.search.embeddings import SentenceTransformerEmbedder

        embedder = SentenceTransformerEmbedder(settings)
        names = {
            "bearings": ("INV-1001", "Northwind Traders", "Industrial hydraulic bearing assembly"),
            "cloud": ("INV-2002", "Fabrikam Software", "Annual cloud hosting subscription"),
            "catering": ("XYZ-3003", "Adventure Foods", "Office lunch catering platter"),
        }
        pages = embedder.encode(["\n".join(lines_for(*details)) for details in names.values()])
        related = {
            "server rental fees": "cloud",
            "machine parts": "bearings",
            "mechanical components": "bearings",
            "sandwiches for a meeting": "catering",
        }
        unrelated = ["zebrafish quantum", "football match results", "the weather in Paris"]

        def scores(query: str) -> dict[str, float]:
            vector = embedder.encode([query])[0]
            return {
                name: sum(a * b for a, b in zip(vector, page, strict=True))
                for name, page in zip(names, pages, strict=True)
            }

        for query, expected in related.items():
            result = scores(query)
            assert max(result, key=result.__getitem__) == expected, query
            assert result[expected] >= MIN_SIMILARITY, query
        for query in unrelated:
            assert max(scores(query).values()) < MIN_SIMILARITY, query
