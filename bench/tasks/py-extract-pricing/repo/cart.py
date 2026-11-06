def cart_total(items, customer_type, region):
    subtotal = sum(price * qty for price, qty in items)
    if customer_type == "wholesale":
        subtotal *= 0.8
    elif customer_type == "member" and subtotal >= 100:
        subtotal *= 0.95
    rate = {"eu": 0.2, "us": 0.07, "uk": 0.2}.get(region, 0.0)
    return round(subtotal * (1 + rate), 2)
