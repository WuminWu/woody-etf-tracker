import yfinance as yf
t = yf.Ticker("00981A.TW")
h = t.history(period="ytd", timeout=10)
if len(h) >= 2:
    price = round(float(h["Close"].iloc[-1]), 2)
    ytd = round(((h["Close"].iloc[-1] - h["Close"].iloc[0]) / h["Close"].iloc[0]) * 100, 2)
    print(f"price={price}")
    print(f"ytd={ytd}")
else:
    print("NO DATA")
