import httpkit as hk

URL = "https://api.internal/exports"


def load():
    """Load exports."""
    data = hk.fetch(URL, True, 3)
    return data
