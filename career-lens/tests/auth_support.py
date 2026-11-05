"""Explicit real credentials for API tests."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_lens.auth.service import create_api_key
from career_lens.store import db


def customer_client(application: FastAPI, app_url: str) -> TestClient:
    connection = db.connect(app_url)
    try:
        issued = create_api_key(connection, "test customer", datetime.now(UTC))
    finally:
        connection.close()
    return TestClient(application, headers={
        "Authorization": f"Bearer {issued.token}",
    })


operator_client = customer_client
