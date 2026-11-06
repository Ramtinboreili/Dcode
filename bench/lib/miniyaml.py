"""A small YAML subset parser, so checks need no PyYAML. Standard library only.

Supported: block mappings and sequences, multiple documents, comments, quoted and plain scalars, flow ``[...]`` and
``{...}`` (on one line), block scalars (``|``, ``>`` with ``-``/``+``), and null, booleans, ints and floats.

Not supported, and raised as MiniYamlError rather than guessed: anchors, aliases, tags, merge keys, complex keys and
plain scalars that continue on the next line. Unlike YAML 1.1 parsers, ``on``, ``yes`` and ``no`` stay strings, which is
what a reader of a GitHub Actions file or a Kubernetes manifest expects. A value starting with ``{{`` is a plain string,
since templates (Helm, Ansible) are often read before they are rendered.
"""

from __future__ import annotations

import re
from typing import Any


class MiniYamlError(ValueError):
    pass


_INT = re.compile(r"^[-+]?(0|[1-9][0-9]*)$")
_FLOAT = re.compile(r"^[-+]?((\d+\.\d*|\.\d+)([eE][-+]?\d+)?|\d+[eE][-+]?\d+)$")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/", "a": "\a", "b": "\b", "e": "\x1b"}


def _strip_comment(text: str) -> str:
    """Remove a trailing comment, leaving quoted text alone."""
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 1
            elif char == quote:
                quote = ""
        elif char in "\"'" and (index == 0 or text[index - 1] in " \t[{,:-"):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
        index += 1
    return text.rstrip()


def _unquote(text: str) -> str:
    if text[0] == "'":
        if len(text) < 2 or text[-1] != "'":
            raise MiniYamlError(f"unterminated string: {text}")
        return text[1:-1].replace("''", "'")
    if len(text) < 2 or text[-1] != '"':
        raise MiniYamlError(f"unterminated string: {text}")
    out, index, body = [], 0, text[1:-1]
    while index < len(body):
        char = body[index]
        if char == "\\":
            index += 1
            if index >= len(body) or body[index] not in _ESCAPES:
                raise MiniYamlError(f"unsupported escape in {text}")
            out.append(_ESCAPES[body[index]])
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _scalar(text: str) -> Any:
    text = text.strip()
    if not text or text in ("~", "null", "Null", "NULL"):
        return None
    if text[0] in "\"'":
        return _unquote(text)
    if text.startswith("{{"):
        return text
    if text[0] in "&*!" or text.startswith(("? ", "<<:")):
        raise MiniYamlError(f"anchors, aliases and tags are not supported: {text}")
    if text[0] in "[{":
        return _flow(text)
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    return text


def _flow(text: str) -> Any:
    value, end = _flow_value(text, 0)
    if text[end:].strip():
        raise MiniYamlError(f"unexpected text after a flow value: {text}")
    return value


def _skip(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _flow_value(text: str, index: int) -> tuple[Any, int]:
    index = _skip(text, index)
    if index >= len(text):
        raise MiniYamlError(f"unterminated flow value: {text}")
    char = text[index]
    if char == "[":
        items, index = [], _skip(text, index + 1)
        while True:
            if index >= len(text):
                raise MiniYamlError(f"unterminated list: {text}")
            if text[index] == "]":
                return items, index + 1
            item, index = _flow_value(text, index)
            items.append(item)
            index = _skip(text, index)
            if index < len(text) and text[index] == ",":
                index = _skip(text, index + 1)
    if char == "{":
        mapping, index = {}, _skip(text, index + 1)
        while True:
            if index >= len(text):
                raise MiniYamlError(f"unterminated mapping: {text}")
            if text[index] == "}":
                return mapping, index + 1
            key, index = _flow_token(text, index, stop=":,}")
            index = _skip(text, index)
            if index < len(text) and text[index] == ":":
                mapping[key], index = _flow_value(text, index + 1)
            else:
                mapping[key] = None
            index = _skip(text, index)
            if index < len(text) and text[index] == ",":
                index = _skip(text, index + 1)
    return _flow_token(text, index, stop=",]}")


def _flow_token(text: str, index: int, stop: str) -> tuple[Any, int]:
    index = _skip(text, index)
    if index < len(text) and text[index] in "\"'":
        quote, end = text[index], index + 1
        while end < len(text):
            if text[end] == "\\" and quote == '"':
                end += 2
                continue
            if text[end] == quote:
                if quote == "'" and text[end + 1:end + 2] == "'":
                    end += 2
                    continue
                return _unquote(text[index:end + 1]), end + 1
            end += 1
        raise MiniYamlError(f"unterminated string: {text}")
    end = index
    while end < len(text) and text[end] not in stop:
        end += 1
    return _scalar(text[index:end]), end


def _key_split(text: str) -> tuple[str, str] | None:
    """(key, rest) when this line opens a mapping entry, else None."""
    if text[0] in "\"'":
        quote = text[0]
        end = 1
        while end < len(text):
            if text[end] == "\\" and quote == '"':
                end += 2
                continue
            if text[end] == quote:
                break
            end += 1
        rest = text[end + 1:].lstrip()
        if end < len(text) and rest.startswith(":") and (len(rest) == 1 or rest[1] in " \t"):
            return _unquote(text[:end + 1]), rest[1:].strip()
        return None
    if text[0] in "[{":
        return None
    for index, char in enumerate(text):
        if char == ":" and (index + 1 == len(text) or text[index + 1] in " \t"):
            return text[:index].strip(), text[index + 1:].strip()
        if char == "#" and index and text[index - 1] in " \t":
            return None
    return None


class _Parser:
    def __init__(self, lines: list[tuple[int, str, str]]) -> None:
        self.lines = lines  # (number, indent as a count, text without comment); raw lines kept for block scalars
        self.index = 0

    def peek(self) -> tuple[int, int, str] | None:
        while self.index < len(self.lines):
            number, raw, text = self.lines[self.index]
            if text.strip():
                indent = len(raw) - len(raw.lstrip(" "))
                if raw[:indent + 1].endswith("\t"):
                    raise MiniYamlError(f"line {number}: a tab in the indentation")
                return number, indent, text.strip()
            self.index += 1
        return None

    def value(self, indent: int) -> Any:
        head = self.peek()
        if head is None or head[1] < indent:
            return None
        return self.block(head[1])

    def block(self, indent: int) -> Any:
        head = self.peek()
        assert head is not None
        number, _, text = head
        if text == "-" or text.startswith("- "):
            return self.sequence(indent)
        if _key_split(text) is not None:
            return self.mapping(indent)
        self.index += 1
        following = self.peek()
        if following is not None and following[1] > indent:
            raise MiniYamlError(f"line {number}: a plain scalar on several lines is not supported")
        return _scalar(text)

    def sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while True:
            head = self.peek()
            if head is None or head[1] != indent or not (head[2] == "-" or head[2].startswith("- ")):
                if head is not None and head[1] > indent:
                    raise MiniYamlError(f"line {head[0]}: bad indentation")
                return items
            number, _, text = head
            rest = text[1:].strip()
            if not rest:
                self.index += 1
                items.append(self.value(indent + 1))
                continue
            offset = indent + len(text) - len(text[1:].lstrip())  # the column the inline item starts at
            number, raw, stripped = self.lines[self.index]
            self.lines[self.index] = (number, " " * offset + rest + raw[len(raw):], " " * offset + rest)
            if rest in ("|", ">") or rest[0] in "|>" and re.fullmatch(r"[|>][-+]?", rest):
                self.index += 1
                items.append(self.block_scalar(rest, indent))
                continue
            items.append(self.block(offset))

    def mapping(self, indent: int) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        while True:
            head = self.peek()
            if head is None or head[1] < indent:
                return mapping
            number, column, text = head
            if column > indent:
                raise MiniYamlError(f"line {number}: bad indentation")
            if text == "-" or text.startswith("- "):
                return mapping
            split = _key_split(text)
            if split is None:
                raise MiniYamlError(f"line {number}: expected 'key: value', got {text!r}")
            key, rest = split
            if key == "<<" or key[:1] in ("&", "*", "!", "?"):
                raise MiniYamlError(f"line {number}: merge keys, anchors and complex keys are not supported")
            if key in mapping:
                raise MiniYamlError(f"line {number}: duplicate key {key!r}")
            self.index += 1
            if re.fullmatch(r"[|>][-+]?", rest):
                mapping[key] = self.block_scalar(rest, indent)
                continue
            if rest:
                following = self.peek()
                if following is not None and following[1] > indent:
                    raise MiniYamlError(f"line {following[0]}: a plain scalar on several lines is not supported")
                mapping[key] = _scalar(rest)
                continue
            following = self.peek()
            if following is None or following[1] < indent:
                mapping[key] = None
            elif following[1] == indent:
                # A sequence may sit at its key's own indent; anything else at that indent is the next key.
                is_item = following[2] == "-" or following[2].startswith("- ")
                mapping[key] = self.sequence(indent) if is_item else None
            else:
                mapping[key] = self.block(following[1])

    def block_scalar(self, header: str, parent: int) -> str:
        collected: list[str] = []
        block_indent = None
        while self.index < len(self.lines):
            _, raw, _ = self.lines[self.index]
            if raw.strip():
                current = len(raw) - len(raw.lstrip(" "))
                if block_indent is None:
                    if current <= parent:
                        break
                    block_indent = current
                if current < block_indent:
                    break
                collected.append(raw[block_indent:])
            else:
                collected.append("")
            self.index += 1
        trailing = 0
        while collected and collected[-1] == "":
            collected.pop()
            trailing += 1
        if header[0] == "|":
            body = "\n".join(collected)
        else:
            paragraphs: list[str] = []
            current_words: list[str] = []
            for line in collected:
                if line:
                    current_words.append(line)
                else:
                    paragraphs.append(" ".join(current_words))
                    current_words = []
            paragraphs.append(" ".join(current_words))
            body = "\n".join(paragraphs)
        if not collected:
            return ""
        if header.endswith("-"):
            return body
        if header.endswith("+"):
            return body + "\n" * (trailing + 1)
        return body + "\n"


def _documents(text: str) -> list[list[tuple[int, str, str]]]:
    documents: list[list[tuple[int, str, str]]] = [[]]
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if raw.startswith("---") and (stripped == "---" or stripped.startswith("--- #")):
            documents.append([])
            continue
        if stripped == "...":
            continue
        if raw.startswith("%"):
            raise MiniYamlError(f"line {number}: directives are not supported")
        documents[-1].append((number, raw.rstrip(), _strip_comment(raw)))
    return documents


def load_all(text: str) -> list[Any]:
    """Every non-empty document in the text."""
    out = []
    for lines in _documents(text):
        if not any(line[2].strip() for line in lines):
            continue
        parser = _Parser(lines)
        head = parser.peek()
        assert head is not None
        value = parser.block(head[1])
        left = parser.peek()
        if left is not None:
            raise MiniYamlError(f"line {left[0]}: unexpected content: {left[2]!r}")
        out.append(value)
    return out


def load(text: str) -> Any:
    """The single document in the text (None if empty); several documents are an error."""
    documents = load_all(text)
    if len(documents) > 1:
        raise MiniYamlError("expected one document, found several")
    return documents[0] if documents else None
