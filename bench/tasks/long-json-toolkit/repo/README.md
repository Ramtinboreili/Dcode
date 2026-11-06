# jsonkit

We can't use the `json` module in this environment (long story), so we need our own. Nothing exists yet.

    from jsonkit import loads, dumps, JsonError
    from jsonkit.pointer import resolve, PointerError      # RFC 6901
    from jsonkit.patch import apply_patch, diff, PatchError  # RFC 6902

- `loads(text)`: strict JSON per RFC 8259. `JsonError` on anything else, with `.line` and `.col` (1-based) of the problem, `.pos` (0-based offset). No trailing commas, no comments, no leading zeros, no NaN, no control chars in strings. Handles `\uXXXX` and surrogate pairs. Lone surrogates are errors. Ints stay ints, a number with `.` or an exponent is a float. Duplicate keys: the last one wins. A very deep document (10000 levels) must raise JsonError, not RecursionError, or parse fine.
- `dumps(value, indent=None, sort_keys=False, ascii_only=False)`: compact is `{"a":1,"b":[1,2]}`, with indent it's like `{\n  "a": 1\n}`, and empty containers stay `{}` and `[]`. Bytes, sets, NaN and non-string keys raise `TypeError`/`ValueError`. Its output must always read back through `loads`.
- `resolve(doc, pointer)`: `~0`, `~1` handling, `-` is an error when reading, array indexes have no leading zeros. `PointerError` otherwise.
- `apply_patch(doc, ops)`: add, remove, replace, move, copy, test. Returns a new document, the input stays as it was. All or nothing: `PatchError` on any failure.
- `diff(a, b)`: ops such that `apply_patch(a, diff(a, b)) == b`, and `diff(x, x) == []`.

No imports of `json` anywhere in the package, and no `eval`.
