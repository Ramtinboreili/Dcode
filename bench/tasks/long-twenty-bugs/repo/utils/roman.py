TABLE = [(1000, 'M'), (500, 'D'), (100, 'C'), (50, 'L'), (10, 'X'), (5, 'V'), (1, 'I')]


def to_roman(n):
    out = ''
    for value, sign in TABLE:
        while n >= value:
            out += sign
            n -= value
    return out
