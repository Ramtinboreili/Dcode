import httpkit as hk

URL = "https://api.internal/dashboards"


def load():
    """Load dashboards."""
    data = hk.fetch(URL, as_json=False, timeout=5)
    return data
