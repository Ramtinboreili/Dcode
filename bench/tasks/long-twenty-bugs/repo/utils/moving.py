def moving_average(values, window):
    return [sum(values[i:i + window]) / window for i in range(len(values))]
