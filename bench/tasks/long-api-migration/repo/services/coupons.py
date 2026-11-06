from httpkit import fetch

URL = "https://api.internal/coupons"


def load():
    """Load coupons."""
    data = fetch(URL, False)
    return data
