import pandas as pd
import json
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

df = pd.read_excel('holdings/00981A_holdings_2026-04-17.xlsx')

cols = df.columns.tolist()
print(f'First column (date header): {cols[0]}')
print(f'Shape: {df.shape}')

# Find the stock holdings section (starts around row 19)
stock_data = []
for idx in range(19, len(df)):
    row = df.iloc[idx]
    code = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ''
    name = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ''
    shares = str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else ''
    weight = str(row.iloc[3]).strip() if pd.notna(row.iloc[3]) else ''
    if code and code != 'nan' and len(code) >= 4:
        stock_data.append({
            'code': code,
            'name': name,
            'shares': shares,
            'weight': weight
        })

print(f'Total stocks found: {len(stock_data)}')

# Save as JSON
with open('holdings/00981A_holdings_2026-04-17.json', 'w', encoding='utf-8') as f:
    json.dump(stock_data, f, ensure_ascii=False, indent=2)

print('Saved JSON version')
print()
for s in stock_data[:10]:
    print(f"{s['code']}  {s['name']}  {s['shares']}  {s['weight']}")
print('...')
for s in stock_data[-5:]:
    print(f"{s['code']}  {s['name']}  {s['shares']}  {s['weight']}")
