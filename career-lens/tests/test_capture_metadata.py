"""Small, browser-free tests for navigation metadata capture."""

from career_lens.capture import page as capture_page


class Request:
    method = "GET"
    headers = {"accept": "text/html", "cookie": "li_at=secret"}


class Response:
    status = 200
    request = Request()
    headers = {
        "content-type": "text/html",
        "set-cookie": "session=secret",
        "location": "https://example.com/next?token=secret",
    }


def test_navigation_metadata_keeps_useful_headers_and_redacts_credentials():
    metadata = getattr(capture_page, "navigation_metadata", None)
    assert metadata is not None

    method, request_headers, response_headers = metadata(Response())

    assert method == "GET"
    assert request_headers["accept"] == "text/html"
    assert request_headers["cookie"].startswith("sha256:")
    assert response_headers["set-cookie"].startswith("sha256:")
    assert "secret" not in response_headers["location"]
