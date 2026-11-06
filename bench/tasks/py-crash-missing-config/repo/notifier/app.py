from .mail import make_mailer


class App:
    def __init__(self, channel, mailer):
        self.channel = channel
        self.mailer = mailer

    def notify(self, text):
        sent = [f"posted to {self.channel}: {text}"]
        sent.append(self.mailer.send(text))
        return sent


def build(config):
    mailer = make_mailer(config)
    return App(config["channel"], mailer)
