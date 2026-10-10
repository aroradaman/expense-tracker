def allocate(line_prices, bill_total):
    """Spread (bill_total - sum(line_prices)) across items proportionally to price.

    All amounts are integer paise. The result always sums exactly to bill_total
    (largest-remainder rounding). Free items (price 0) get no share unless every
    item is free, in which case the total is split equally.
    """
    if not line_prices:
        raise ValueError("no items to allocate")
    subtotal = sum(line_prices)
    weights = line_prices if subtotal > 0 else [1] * len(line_prices)
    weight_sum = sum(weights)
    extra = bill_total - sum(line_prices)

    shares, remainders = [], []
    for w in weights:
        q, r = divmod(extra * w, weight_sum)
        shares.append(q)
        remainders.append(r)

    leftover = extra - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        shares[i] += 1

    return [p + s for p, s in zip(line_prices, shares)]
