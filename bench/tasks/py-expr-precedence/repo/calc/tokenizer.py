import re

TOKEN = re.compile(r"\s*(\d+\.?\d*|[-+*/^()])")


def tokenize(text):
    tokens = []
    position = 0
    while position < len(text):
        match = TOKEN.match(text, position)
        if not match:
            break
        tokens.append(match.group(1))
        position = match.end()
    return tokens
