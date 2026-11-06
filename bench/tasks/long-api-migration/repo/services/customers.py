from httpkit import fetch

URL = "https://api.internal/customers"


def load():
    """Load customers."""
    data = fetch(URL, True, 3)
    return data
