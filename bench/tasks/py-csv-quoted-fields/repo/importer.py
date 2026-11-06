"""Reads our export format: comma separated, # starts a comment line, double quotes around fields, "" is a literal quote."""


def parse_line(line):
    return [field.strip().strip('"') for field in line.split(",")]


def parse(text):
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        rows.append(parse_line(line))
    return rows


def to_dicts(text):
    rows = parse(text)
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]
