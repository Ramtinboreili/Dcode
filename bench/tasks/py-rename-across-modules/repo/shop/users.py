from dataclasses import dataclass


@dataclass
class UserData:
    id: int
    name: str
    tier: str = "basic"


_USERS = {1: UserData(1, "ana", "gold"), 2: UserData(2, "bo")}


def get_user_data(user_id):
    """Look a user up. Raises KeyError when there's none."""
    return _USERS[user_id]
