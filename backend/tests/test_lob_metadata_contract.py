"""The official pre-2024 and current Lob specs require deepObject query metadata."""

from urllib.parse import parse_qs, urlsplit

import pytest

from parkingbreaker.lob_submission import MetadataLookup
from test_lob_client import _client, _WireServer


@pytest.mark.parametrize("token", ["synthetic-token", "synthetic:+/ ?&=bracket[]"])
def test_metadata_filter_uses_exact_deep_object_wire(token: str) -> None:
    server = _WireServer(
        lambda *_: (200, {"object": "list", "data": [], "count": 0, "next_url": None})
    )
    try:
        result = _client(server).find_by_metadata(token)
    finally:
        server.close()
    assert result == MetadataLookup(None, authoritative=False)
    method, path, _headers, _body = server.requests[0]
    assert method == "GET"
    assert urlsplit(path).path == "/v1/letters"
    assert parse_qs(urlsplit(path).query) == {
        "limit": ["100"],
        "metadata[submission_token]": [token],
    }
    assert len(server.requests) == 1
