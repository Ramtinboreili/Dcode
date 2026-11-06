def invert(mapping):
    out = {}
    for key, value in mapping.items():
        out[value] = [key]
    return out
