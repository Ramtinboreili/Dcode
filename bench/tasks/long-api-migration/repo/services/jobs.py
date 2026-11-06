from httpkit import fetch

URL = "https://api.internal/jobs"


def load():
    """Load jobs."""
    data = fetch(URL)
    return data
