from httpkit import fetch

URL = "https://api.internal/metrics"


def load():
    """Load metrics."""
    data = fetch(URL, False)
    return data
