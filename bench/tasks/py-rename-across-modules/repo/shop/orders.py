from .users import UserData, get_user_data


def discount_for(user: UserData) -> float:
    return 0.1 if user.tier == "gold" else 0.0


def price_for(user_id, amount):
    user = get_user_data(user_id)
    return round(amount * (1 - discount_for(user)), 2)
