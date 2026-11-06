from httpkit import fetch

URL = "https://api.internal/payments"


def load():
    """Load payments."""
    data = fetch(URL, True)
    return data
