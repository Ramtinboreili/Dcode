from httpkit import fetch

URL = "https://api.internal/devices"


def load():
    """Load devices."""
    data = fetch(URL, False, 2, 30)
    return data
