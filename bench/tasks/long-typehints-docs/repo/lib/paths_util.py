def extension(path):
    return path.rsplit('.', 1)[-1] if '.' in path else ''


def join_url(base, path):
    return base.rstrip('/') + '/' + path.lstrip('/')


def depth(path):
    return len([p for p in path.split('/') if p])


def stem(path):
    return path.rsplit('/', 1)[-1].split('.')[0]


def is_hidden(path):
    return path.rsplit('/', 1)[-1].startswith('.')
