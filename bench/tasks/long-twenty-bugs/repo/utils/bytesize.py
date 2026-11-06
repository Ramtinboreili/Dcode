def human_bytes(n):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if n <= 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'
