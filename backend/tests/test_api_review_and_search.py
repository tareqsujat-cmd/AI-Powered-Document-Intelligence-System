from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import DocumentStatus, ProcessingEvent
from app.db.models import Document, Embedding, ProcessingHistory, ValidationError
from app.pipeline.review import refresh_needs_review
from app.pipeline.service import prepare_reprocess
from app.search.embeddings import get_embedder
from tests.api_helpers import API, Api, add_result, make_document, make_invoice
from tests.pipeline_helpers import FakeEmbedder

pytest_plugins = ["tests.api_helpers"]
pytestmark = pytest.mark.db


def flagged_document(db: Session) -> tuple[Document, list[ValidationError]]:
    """A document whose only problem is one low-confidence value, so it needs review."""
    document = make_document(db)
    invoice = make_invoice()
    invoice.tax.confidence = 0.3
    add_result(db, document, invoice)
    refresh_needs_review(db, document)
    db.commit()
    errors = list(db.scalars(select(ValidationError)))
    return document, errors


def review_url(document: Document, error: ValidationError) -> str:
    return f"{API}/documents/{document.id}/validation-errors/{error.id}"


class TestReviewingAnIssue:
    def test_resolving_the_only_issue_clears_the_review_flag(self, api: Api, db: Session) -> None:
        document, errors = flagged_document(db)
        assert [error.code for error in errors] == ["low_confidence"]
        assert document.needs_review is True

        response = api.client.patch(
            review_url(document, errors[0]), json={"is_resolved": True, "actor": "maria"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["needs_review"] is False
        assert body["validation_error"]["is_resolved"] is True
        assert body["validation_error"]["code"] == "low_confidence"
        db.expire_all()
        assert db.get(Document, document.id).needs_review is False

    def test_the_decision_is_recorded_with_who_made_it(self, api: Api, db: Session) -> None:
        document, errors = flagged_document(db)

        api.client.patch(
            review_url(document, errors[0]), json={"is_resolved": True, "actor": "maria"}
        )

        entry = db.scalars(
            select(ProcessingHistory).where(
                ProcessingHistory.event == ProcessingEvent.MANUAL_CORRECTION
            )
        ).one()
        assert entry.actor == "maria"
        assert entry.before["is_resolved"] is False
        assert entry.after["is_resolved"] is True
        assert entry.after["code"] == "low_confidence"

    def test_reopening_flags_the_document_again(self, api: Api, db: Session) -> None:
        document, errors = flagged_document(db)
        url = review_url(document, errors[0])
        api.client.patch(url, json={"is_resolved": True})

        response = api.client.patch(url, json={"is_resolved": False})

        assert response.json()["needs_review"] is True

    def test_repeating_a_decision_writes_no_history(self, api: Api, db: Session) -> None:
        document, errors = flagged_document(db)
        url = review_url(document, errors[0])
        api.client.patch(url, json={"is_resolved": True})
        api.client.patch(url, json={"is_resolved": True})

        entries = db.scalars(select(ProcessingHistory)).all()
        assert len(entries) == 1

    def test_other_unresolved_issues_keep_the_document_flagged(self, api: Api, db: Session) -> None:
        document = make_document(db)
        invoice = make_invoice()
        invoice.tax.confidence = 0.3
        invoice.subtotal.confidence = 0.3
        add_result(db, document, invoice)
        refresh_needs_review(db, document)
        db.commit()
        first, second = db.scalars(select(ValidationError)).all()

        assert api.client.patch(review_url(document, first), json={"is_resolved": True}).json()[
            "needs_review"
        ]
        assert not api.client.patch(
            review_url(document, second), json={"is_resolved": True}
        ).json()["needs_review"]

    def test_a_resolved_issue_stays_resolved_after_an_unrelated_correction(
        self, api: Api, db: Session
    ) -> None:
        document, errors = flagged_document(db)
        api.client.patch(review_url(document, errors[0]), json={"is_resolved": True})
        results = api.client.get(f"{API}/documents/{document.id}/results").json()

        corrected = api.client.patch(
            f"{API}/documents/{document.id}/results/{results[0]['id']}",
            json={"customer_name": "Fabrikam"},
        )

        issues = corrected.json()["validation_errors"]
        assert [(issue["code"], issue["is_resolved"]) for issue in issues] == [
            ("low_confidence", True)
        ]
        db.expire_all()
        assert db.get(Document, document.id).needs_review is False

    def test_a_changed_problem_is_a_new_issue_and_must_be_reviewed_again(
        self, api: Api, db: Session
    ) -> None:
        document = make_document(db)
        add_result(db, document, make_invoice(total="999.00"))
        refresh_needs_review(db, document)
        db.commit()
        mismatch = db.scalars(
            select(ValidationError).where(ValidationError.code == "total_mismatch")
        ).one()
        api.client.patch(review_url(document, mismatch), json={"is_resolved": True})
        results = api.client.get(f"{API}/documents/{document.id}/results").json()

        corrected = api.client.patch(
            f"{API}/documents/{document.id}/results/{results[0]['id']}", json={"total": "555.00"}
        )

        issues = {issue["code"]: issue for issue in corrected.json()["validation_errors"]}
        assert issues["total_mismatch"]["is_resolved"] is False
        assert issues["total_mismatch"]["actual_value"] == "555.00"
        db.expire_all()
        assert db.get(Document, document.id).needs_review is True

    def test_info_issues_never_flag_a_document(self, api: Api, db: Session) -> None:
        document = make_document(db)
        add_result(db, document, make_invoice(line_items=[]))
        refresh_needs_review(db, document)
        db.commit()

        codes = [error.code for error in db.scalars(select(ValidationError))]

        assert "no_line_items" in codes
        assert document.needs_review is False

    def test_unknown_issue_is_404(self, api: Api, db: Session) -> None:
        document, _ = flagged_document(db)
        missing = "00000000-0000-0000-0000-000000000000"

        response = api.client.patch(
            f"{API}/documents/{document.id}/validation-errors/{missing}",
            json={"is_resolved": True},
        )

        assert response.status_code == 404

    def test_an_issue_on_another_document_is_404(self, api: Api, db: Session) -> None:
        _, errors = flagged_document(db)
        other = make_document(db)

        response = api.client.patch(review_url(other, errors[0]), json={"is_resolved": True})

        assert response.status_code == 404

    @pytest.mark.parametrize("status", [DocumentStatus.QUEUED, DocumentStatus.PROCESSING])
    def test_a_busy_document_cannot_be_reviewed(
        self, api: Api, db: Session, status: DocumentStatus
    ) -> None:
        document, errors = flagged_document(db)
        document.status = status
        db.commit()

        response = api.client.patch(review_url(document, errors[0]), json={"is_resolved": True})

        assert response.status_code == 409

    @pytest.mark.parametrize(
        "body", [{}, {"is_resolved": "maybe"}, {"is_resolved": True, "extra": 1}]
    )
    def test_invalid_bodies_are_422(self, api: Api, db: Session, body: dict) -> None:
        document, errors = flagged_document(db)

        assert api.client.patch(review_url(document, errors[0]), json=body).status_code == 422


class TestReprocessFixes:
    def test_full_reprocess_of_a_failed_document_does_not_break_the_reason_constraint(
        self, db: Session
    ) -> None:
        document = make_document(db, status=DocumentStatus.FAILED, failure_reason="bad file")

        prepare_reprocess(db, document, resume=False)
        db.flush()

        assert document.status is DocumentStatus.FAILED
        assert document.failure_reason == "bad file"

    def test_full_reprocess_restarts_the_processing_clock(self, db: Session) -> None:
        started = datetime(2020, 1, 1, tzinfo=UTC)
        document = make_document(db, processing_started_at=started)

        prepare_reprocess(db, document, resume=False)

        assert document.processing_started_at is None

    def test_resuming_keeps_the_clock_running(self, db: Session) -> None:
        started = datetime(2020, 1, 1, tzinfo=UTC)
        document = make_document(db, processing_started_at=started)

        prepare_reprocess(db, document, resume=True)

        assert document.processing_started_at == started

    def test_the_api_can_reprocess_a_failed_document_from_scratch(
        self, api: Api, db: Session
    ) -> None:
        document = make_document(db, status=DocumentStatus.FAILED, failure_reason="bad file")

        response = api.client.post(f"{API}/documents/{document.id}/reprocess")

        assert response.status_code == 202
        assert response.json()["status"] == "queued"
        assert response.json()["failure_reason"] is None


@pytest.fixture
def search_api(api: Api) -> Api:
    api.app.dependency_overrides[get_embedder] = FakeEmbedder
    return api


def add_indexed_text(db: Session, document: Document, content: str) -> None:
    from app.db.models import DocumentPage, ExtractedText

    page = DocumentPage(document_id=document.id, page_number=1)
    db.add(page)
    db.flush()
    db.add(ExtractedText(document_id=document.id, page_id=page.id, content=content))
    embedder = FakeEmbedder()
    db.add(
        Embedding(
            document_id=document.id,
            page_number=1,
            chunk_index=0,
            content=content,
            embedding=embedder.encode([content])[0],
            model=embedder.model_name,
        )
    )
    db.commit()


class TestSearchEndpoint:
    def corpus(self, db: Session) -> tuple[Document, Document]:
        cloud = make_document(db, original_filename="cloud.pdf")
        add_indexed_text(db, cloud, "Annual cloud hosting subscription for servers")
        food = make_document(db, original_filename="food.pdf")
        add_indexed_text(db, food, "Office lunch catering platter for the meeting")
        return cloud, food

    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    def test_each_mode_finds_the_right_document(
        self, search_api: Api, db: Session, mode: str
    ) -> None:
        cloud, _ = self.corpus(db)

        response = search_api.client.get(
            f"{API}/search", params={"q": "cloud hosting subscription", "mode": mode}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == mode
        assert body["query"] == "cloud hosting subscription"
        first = body["results"][0]
        assert first["document_id"] == str(cloud.id)
        assert first["filename"] == "cloud.pdf"
        assert first["page_number"] == 1
        assert first["snippet"]
        assert first["matched_by"]

    def test_hybrid_is_the_default(self, search_api: Api, db: Session) -> None:
        self.corpus(db)

        assert (
            search_api.client.get(f"{API}/search", params={"q": "cloud"}).json()["mode"] == "hybrid"
        )

    def test_filters_are_applied(self, search_api: Api, db: Session) -> None:
        self.corpus(db)

        response = search_api.client.get(
            f"{API}/search", params={"q": "cloud", "file_type": "csv", "mode": "hybrid"}
        )

        assert response.json()["results"] == []

    def test_limit_is_applied(self, search_api: Api, db: Session) -> None:
        self.corpus(db)

        response = search_api.client.get(
            f"{API}/search", params={"q": "for", "mode": "semantic", "limit": 1}
        )

        assert len(response.json()["results"]) <= 1

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"q": ""},
            {"q": "x" * 501},
            {"q": "x", "mode": "psychic"},
            {"q": "x", "limit": 0},
            {"q": "x", "limit": 101},
            {"q": "x", "file_type": "docx"},
        ],
    )
    def test_invalid_parameters_are_422(self, search_api: Api, params: dict) -> None:
        assert search_api.client.get(f"{API}/search", params=params).status_code == 422

    def test_no_results_is_an_empty_list(self, search_api: Api, db: Session) -> None:
        self.corpus(db)

        response = search_api.client.get(f"{API}/search", params={"q": "zebrafish"})

        assert response.status_code == 200
        assert response.json()["results"] == []

    @pytest.mark.parametrize("query", ["'; DROP TABLE documents; --", "%", '"(((', "a\\b"])
    def test_hostile_queries_are_harmless(self, search_api: Api, db: Session, query: str) -> None:
        cloud, _ = self.corpus(db)

        response = search_api.client.get(f"{API}/search", params={"q": query, "mode": "keyword"})

        assert response.status_code == 200
        assert db.get(Document, cloud.id) is not None

    def test_search_has_its_own_rate_limit(self, make_api: Callable[..., Api], db: Session) -> None:
        limited = make_api(rate_limit_searches_per_minute=1)
        limited.app.dependency_overrides[get_embedder] = FakeEmbedder

        assert limited.client.get(f"{API}/search", params={"q": "a"}).status_code == 200
        response = limited.client.get(f"{API}/search", params={"q": "a"})

        assert response.status_code == 429
        assert int(response.headers["retry-after"]) >= 1


def test_results_are_reported_as_decimal_strings(api: Api, db: Session) -> None:
    document = make_document(db)
    add_result(db, document, make_invoice(total="110.50"))

    body = api.client.get(f"{API}/documents/{document.id}/results").json()

    assert body[0]["fields"]["total"]["value"] == "110.50"
    assert Decimal(body[0]["fields"]["total"]["value"]) == Decimal("110.50")
