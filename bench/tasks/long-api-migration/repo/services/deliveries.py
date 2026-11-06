from httpkit import fetch

URL = "https://api.internal/deliveries"


def load():
    """Load deliveries."""
    data = fetch(URL)
    return data
