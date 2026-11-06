import httpkit as hk

URL = "https://api.internal/partners"


def load():
    """Load partners."""
    data = hk.fetch(URL, False, 2, 30)
    return data
