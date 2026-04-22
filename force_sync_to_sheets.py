import json
import glob
from sheets_helper import append_holdings_to_sheets

def main():
    date_to_sync = "2026-04-22"
    for f in sorted(glob.glob("data_*.json")):
        if "index" in f:
            continue
        etf_code = f.replace("data_", "").replace(".json", "")
        print(f"Syncing {etf_code} with date {date_to_sync} ...")
        try:
            with open(f, "r", encoding="utf-8") as file:
                data = json.load(file)
            holdings = data.get("holdings", [])
            append_holdings_to_sheets(etf_code, date_to_sync, holdings)
            print(f"  -> Success: {len(holdings)} holdings appended.")
        except Exception as e:
            print(f"  -> Failed {etf_code}: {e}")

if __name__ == "__main__":
    main()
