from benchcheck import exists, finish, load_json, ok

WANT = {'checkout_failures': 93, 'payments_timeout': 49, 'orders_db': 22, 'stock_conflict': 22, 'slowest_ok_checkout': '72b96b88b319'}
ok(exists("answer.json"), "answer.json exists")
got = load_json("answer.json") if exists("answer.json") else {}
for key, value in WANT.items():
    ok(isinstance(got, dict) and got.get(key) == value, f"{key} is right")
finish()
