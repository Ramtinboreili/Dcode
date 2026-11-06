def parse_ranges(spec):
    out = []
    for part in spec.split(','):
        if '-' in part:
            a, b = part.split('-')
            out.extend(range(int(a), int(b)))
        else:
            out.append(int(part))
    return out
