def caesar(text, shift):
    out = ''
    for ch in text:
        if ch.islower():
            out += chr(ord(ch) + shift)
        else:
            out += ch
    return out
