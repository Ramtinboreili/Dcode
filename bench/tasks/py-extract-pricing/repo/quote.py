def quote_total(amount, customer_type, region):
    subtotal = amount
    if customer_type == "wholesale":
        subtotal *= 0.75
    elif customer_type == "member" and subtotal >= 100:
        subtotal *= 0.95
    rate = {"eu": 0.2, "us": 0.07, "uk": 0.2}.get(region, 0.0)
    return round(subtotal + subtotal * rate)
