import httpkit as hk

URL = "https://api.internal/reports"


def load():
    """Load reports."""
    data = hk.fetch(URL)
    return data
