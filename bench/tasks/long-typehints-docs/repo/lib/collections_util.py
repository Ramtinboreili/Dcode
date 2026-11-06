def first(items, default=0):
    return items[0] if items else default


def group_by_len(words):
    out = {}
    for word in words:
        out.setdefault(len(word), []).append(word)
    return out


def pairs(items):
    return list(zip(items, items[1:]))


def top_n(scores, n):
    return sorted(scores, key=lambda k: -scores[k])[:n]


def compact(items):
    return [item for item in items if item]
