def parse_kv(line):
    out = {}
    for part in line.split(';'):
        key, value = part.split('=')
        out[key.strip()] = value.strip()
    return out
