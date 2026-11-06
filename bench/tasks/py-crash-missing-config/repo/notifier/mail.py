class Mailer:
    def __init__(self, host, port):
        self.host = host
        self.port = port

    def send(self, text):
        return f"mailed via {self.host}:{self.port}: {text}"


def make_mailer(config):
    host = config["smtp"]["host"]
    port = config["smtp"]["port"]
    return Mailer(host, port)
