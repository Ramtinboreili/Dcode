import httpkit as hk

URL = "https://api.internal/leads"


def load():
    """Load leads."""
    data = hk.fetch(URL, True)
    return data
