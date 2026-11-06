def average(values):
    return sum(values) / len(values) if values else 0.0


def factorial(n):
    return 1 if n < 2 else n * factorial(n - 1)


def is_prime(n):
    return n > 1 and all(n % d for d in range(2, int(n ** 0.5) + 1))


def digits(n):
    return [int(ch) for ch in str(abs(n))]


def round_to(value, step):
    return round(value / step) * step
