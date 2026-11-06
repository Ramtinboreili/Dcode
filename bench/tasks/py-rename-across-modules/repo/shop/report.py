import shop.users


def tiers(ids):
    return {i: shop.users.get_user_data(i).tier for i in ids}


LOOKUP = "get_user_data"


def lookup_by_name(name):
    return getattr(shop.users, LOOKUP)
