import ast
import unittest
from pathlib import Path

ORIGINAL = {'strings': "def reverse_words(text):\n    return ' '.join(reversed(text.split()))\n\n\ndef count_vowels(text):\n    return sum(ch in 'aeiou' for ch in text.lower())\n\n\ndef is_palindrome(text):\n    cleaned = [ch for ch in text.lower() if ch.isalnum()]\n    return cleaned == cleaned[::-1]\n\n\ndef pad_left(text, width, fill=' '):\n    return text.rjust(width, fill)\n\n\ndef initials(name):\n    return ''.join(part[0].upper() for part in name.split())\n", 'numbers': 'def average(values):\n    return sum(values) / len(values) if values else 0.0\n\n\ndef factorial(n):\n    return 1 if n < 2 else n * factorial(n - 1)\n\n\ndef is_prime(n):\n    return n > 1 and all(n % d for d in range(2, int(n ** 0.5) + 1))\n\n\ndef digits(n):\n    return [int(ch) for ch in str(abs(n))]\n\n\ndef round_to(value, step):\n    return round(value / step) * step\n', 'collections_util': 'def first(items, default=0):\n    return items[0] if items else default\n\n\ndef group_by_len(words):\n    out = {}\n    for word in words:\n        out.setdefault(len(word), []).append(word)\n    return out\n\n\ndef pairs(items):\n    return list(zip(items, items[1:]))\n\n\ndef top_n(scores, n):\n    return sorted(scores, key=lambda k: -scores[k])[:n]\n\n\ndef compact(items):\n    return [item for item in items if item]\n', 'paths_util': "def extension(path):\n    return path.rsplit('.', 1)[-1] if '.' in path else ''\n\n\ndef join_url(base, path):\n    return base.rstrip('/') + '/' + path.lstrip('/')\n\n\ndef depth(path):\n    return len([p for p in path.split('/') if p])\n\n\ndef stem(path):\n    return path.rsplit('/', 1)[-1].split('.')[0]\n\n\ndef is_hidden(path):\n    return path.rsplit('/', 1)[-1].startswith('.')\n", 'dates_util': "def is_weekend(day):\n    return day in (5, 6)\n\n\ndef days_in_month(month, leap=False):\n    return 29 if month == 2 and leap else [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]\n\n\ndef to_minutes(hours, minutes):\n    return hours * 60 + minutes\n\n\ndef format_hm(minutes):\n    return f'{minutes // 60:02d}:{minutes % 60:02d}'\n\n\ndef quarter(month):\n    return (month - 1) // 3 + 1\n", 'config_util': "def get_bool(value):\n    return value.lower() in ('1', 'true', 'yes')\n\n\ndef get_list(value, sep=','):\n    return [part.strip() for part in value.split(sep) if part.strip()]\n\n\ndef merge(base, extra):\n    return {**base, **extra}\n\n\ndef env_name(key, prefix='APP'):\n    return f'{prefix}_{key}'.upper().replace('.', '_')\n\n\ndef redact(value):\n    return value[:2] + '*' * max(len(value) - 2, 0)\n"}


def strip(tree):
    out = []
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            body = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
            params = [(a.arg) for a in fn.args.args]
            defaults = [ast.dump(d) for d in fn.args.defaults]
            out.append((fn.name, params, defaults, [ast.dump(s) for s in body]))
    return out


class Hidden(unittest.TestCase):
    def test_all_documented_and_typed(self):
        for mod in ORIGINAL:
            tree = ast.parse(Path(f"lib/{mod}.py").read_text())
            for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
                with self.subTest(f"{mod}.{fn.name}"):
                    self.assertTrue((ast.get_docstring(fn) or "").strip(), "docstring")
                    self.assertIsNotNone(fn.returns, "return type")
                    for arg in fn.args.args:
                        self.assertIsNotNone(arg.annotation, arg.arg)

    def test_code_unchanged(self):
        for mod, source in ORIGINAL.items():
            with self.subTest(mod):
                now = strip(ast.parse(Path(f"lib/{mod}.py").read_text()))
                self.assertEqual(now, strip(ast.parse(source)))
