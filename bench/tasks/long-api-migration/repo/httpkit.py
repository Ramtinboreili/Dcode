"""HTTP helpers.

fetch() is deprecated, use request():
    fetch(url, True)            -> request("GET", url).json()
    fetch(url, False)           -> request("GET", url).text
    fetch(url, True, 3)         -> request("GET", url, retries=3).json()
    fetch(url, as_json=False, timeout=5) -> request("GET", url, timeout=5).text
"""
import json


class Response:
    def __init__(self, text):
        self.text = text

    def json(self):
        return json.loads(self.text)


def _send(method, url, timeout, retries):
    raise RuntimeError("no network here")


def request(method, url, retries=0, timeout=10):
    return Response(_send(method, url, timeout, retries))


def fetch(url, as_json=True, retries=0, timeout=10):
    response = request("GET", url, retries=retries, timeout=timeout)
    return response.json() if as_json else response.text
