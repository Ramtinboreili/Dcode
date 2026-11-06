def reverse_words(text):
    return ' '.join(reversed(text.split()))


def count_vowels(text):
    return sum(ch in 'aeiou' for ch in text.lower())


def is_palindrome(text):
    cleaned = [ch for ch in text.lower() if ch.isalnum()]
    return cleaned == cleaned[::-1]


def pad_left(text, width, fill=' '):
    return text.rjust(width, fill)


def initials(name):
    return ''.join(part[0].upper() for part in name.split())
