from httpkit import fetch

URL = "https://api.internal/discounts"


def load():
    """Load discounts."""
    data = fetch(URL, True)
    return data
