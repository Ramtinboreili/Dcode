from benchcheck import exists, finish, load_json, ok

WANT = {'pod': 'cart-88ab', 'time': '2026-05-29T00:12:56Z'}
ok(exists("answer.json"), "answer.json exists")
got = load_json("answer.json") if exists("answer.json") else {}
ok(isinstance(got, dict), "it is an object")
ok(got.get("pod") == WANT["pod"], "the right pod")
ok(got.get("time") == WANT["time"], "the right time")
finish()
