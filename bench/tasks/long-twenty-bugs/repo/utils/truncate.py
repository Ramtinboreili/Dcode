def truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + '...'
