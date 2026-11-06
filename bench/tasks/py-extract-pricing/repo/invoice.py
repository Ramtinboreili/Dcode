def invoice_total(lines, customer_type, region):
    subtotal = 0
    for line in lines:
        subtotal += line["amount"]
    if customer_type == "wholesale":
        subtotal *= 0.8
    elif customer_type == "member":
        subtotal *= 0.95
    rate = {"eu": 0.2, "us": 0.07}.get(region, 0.0)
    return round(subtotal * (1 + rate), 2)
