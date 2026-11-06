"""Work out the order to install plugins in."""


def resolve(graph, root):
    """Return `root` and everything it needs, dependencies before what needs them."""
    order = []

    def visit(name):
        for dep in graph.get(name, []):
            visit(dep)
        order.append(name)

    visit(root)
    return order
