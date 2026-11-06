import httpkit as hk

URL = "https://api.internal/history"


def load():
    """Load history."""
    data = hk.fetch(URL, False)
    return data
