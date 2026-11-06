def rle(text):
    out, count = [], 0
    for i, ch in enumerate(text):
        count += 1
        if i + 1 < len(text) and text[i + 1] != ch:
            out.append((ch, count))
            count = 0
    return out
