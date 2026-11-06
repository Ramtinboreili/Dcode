from httpkit import fetch

URL = "https://api.internal/events"


def load():
    """Load events."""
    data = fetch(URL, False)
    return data
