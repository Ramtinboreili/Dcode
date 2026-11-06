from . import users as user_module


def greeting(user_id):
    # get_user_data raises for unknown ids, so callers should check first
    user = user_module.get_user_data(user_id)
    return f"Hello {user.name}"
