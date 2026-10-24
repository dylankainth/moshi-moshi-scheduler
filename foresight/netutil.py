import json
import urllib.request
from urllib.error import HTTPError


def http_json(method: str, url: str, body=None, headers=None, timeout=10.0):
    """Returns (status, parsed JSON or None). Raises on network errors, not on HTTP error codes."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None
