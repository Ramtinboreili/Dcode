import sys

from notifier.app import build
from notifier.config import load


def main(path):
    app = build(load(path))
    print(app.notify("deploy finished"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config/staging.json")
