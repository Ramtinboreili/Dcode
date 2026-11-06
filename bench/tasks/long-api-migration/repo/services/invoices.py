from httpkit import fetch

URL = "https://api.internal/invoices"


def load():
    """Load invoices."""
    data = fetch(URL, as_json=False, timeout=5)
    return data
