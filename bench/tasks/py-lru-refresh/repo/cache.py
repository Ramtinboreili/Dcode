"""A least-recently-used cache."""


class LRUCache:
    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.items = {}
        self.order = []
        self.hits = 0
        self.misses = 0

    def get(self, key, default=None):
        if key in self.items:
            self.hits += 1
            return self.items[key]
        self.misses += 1
        return default

    def put(self, key, value):
        if key not in self.items:
            self.order.append(key)
        self.items[key] = value
        if len(self.order) > self.capacity:
            oldest = self.order.pop()
            del self.items[oldest]

    def delete(self, key):
        del self.items[key]

    def __len__(self):
        return len(self.items)

    def keys(self):
        """Oldest first."""
        return list(self.order)
