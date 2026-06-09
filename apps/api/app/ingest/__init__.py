"""Polite, cached, rate-limited HTTP ingest layer shared by every data source."""

from app.ingest.http import HttpClient, HttpResponse

__all__ = ["HttpClient", "HttpResponse"]
