import httpkit as hk

URL = "https://api.internal/tickets"


def load():
    """Load tickets."""
    data = hk.fetch(URL, as_json=False, timeout=5)
    return data
