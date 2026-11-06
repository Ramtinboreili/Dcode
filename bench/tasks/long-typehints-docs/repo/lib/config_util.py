def get_bool(value):
    return value.lower() in ('1', 'true', 'yes')


def get_list(value, sep=','):
    return [part.strip() for part in value.split(sep) if part.strip()]


def merge(base, extra):
    return {**base, **extra}


def env_name(key, prefix='APP'):
    return f'{prefix}_{key}'.upper().replace('.', '_')


def redact(value):
    return value[:2] + '*' * max(len(value) - 2, 0)
