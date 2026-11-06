from .tokenizer import tokenize


class CalcError(Exception):
    pass


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.index = 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self):
        token = self.peek()
        self.index += 1
        return token

    def expression(self):
        value = self.atom()
        while self.peek() in ("+", "-", "*", "/", "^"):
            op = self.next()
            right = self.atom()
            if op == "+":
                value += right
            elif op == "-":
                value -= right
            elif op == "*":
                value *= right
            elif op == "/":
                value /= right
            else:
                value **= right
        return value

    def atom(self):
        token = self.next()
        if token == "(":
            value = self.expression()
            self.next()
            return value
        return float(token)


def evaluate(text):
    return Parser(tokenize(text)).expression()
