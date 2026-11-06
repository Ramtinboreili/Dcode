def is_weekend(day):
    return day in (5, 6)


def days_in_month(month, leap=False):
    return 29 if month == 2 and leap else [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]


def to_minutes(hours, minutes):
    return hours * 60 + minutes


def format_hm(minutes):
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def quarter(month):
    return (month - 1) // 3 + 1
