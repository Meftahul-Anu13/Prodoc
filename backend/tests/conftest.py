"""Legacy regression cases explicitly exercise the offline comparison provider."""
import pytest


@pytest.fixture(autouse=True)
def offline_regression_defaults(monkeypatch):
    # The application default is AI. Tests must never inherit permission to bill.
    monkeypatch.setenv('EXTRACTION_PROVIDER', 'baseline')
    monkeypatch.setenv('ALLOW_CLOUD_EXTRACTION', 'false')
    monkeypatch.setenv('SEMANTIC_ENABLED', 'false')
    # Existing contract tests use tiny synthetic sources and mocked parsers; the
    # guardrail, extraction cache and PDF text-layer fast path have their own tests.
    monkeypatch.setenv('GUARDRAIL_RELEVANCE', 'off')
    monkeypatch.setenv('EXTRACTION_CACHE', 'off')
    monkeypatch.setenv('PDF_TEXT_LAYER', 'off')
