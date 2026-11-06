"""Daily sales totals."""
from datetime import date, timedelta


def days_between(start, end):
    """Every date from start to end, both included."""
    days = []
    current = start
    while current < end:
        days.append(current)
        current += timedelta(days=1)
    return days


def total(sales, start, end):
    """Sum of sales (a dict of date -> amount) over the range, both ends included."""
    return sum(sales.get(day, 0) for day in days_between(start, end))


def parse(text):
    year, month, day = text.split("-")
    return date(int(year), int(month), int(day))
