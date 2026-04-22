import os
import urllib.request
import urllib.parse
import json
from playwright.sync_api import sync_playwright

# Telegram config
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ETF, Manager, URL
MANAGERS = [
    ("00981A", "統一台股增長", "陳釧瑤", "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode=49YTW"),
    ("00980A", "野村智慧優選", "游景德", "https://www.nomurafunds.com.tw/ETF/FundOverview?FundID=00980A"),
    ("00985A", "野村台灣50", "林浩詳", "https://www.nomurafunds.com.tw/ETF/FundOverview?FundID=00985A"),
    ("00991A", "復華未來50", "呂宏宇", "https://www.fhtrust.com.tw/ETF/etf_info/00991A"),
    ("00992A", "群益科技創新", "陳朝政", "https://www.capitalfund.com.tw/etf/product/detail/500/overview"),
    ("00982A", "群益台灣強棒", "陳沅易", "https://www.capitalfund.com.tw/etf/product/detail/399/overview"),
    ("00987A", "台新台灣優勢成長", "魏永祥", "https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/00987A")
]

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured. Skipping notification.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Telegram API error: {e}")

def main():
    alerts = []
    
    with sync_playwright() as p:
        # Use firefox or webkit if chromium is blocked, but chromium is usually fine
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=True
        )
        
        for code, name, manager, url in MANAGERS:
            try:
                page = context.new_page()
                print(f"Checking {code} {name} ...")
                
                # Ignore HTTPS errors (Nomura has SSL issues sometimes)
                response = page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)
                
                html = page.content()
                
                # We simply check if the hardcoded manager name is still present ANYWHERE on the page
                # If it's not present, it likely means the manager was changed.
                if manager not in html:
                    alerts.append(f"⚠️ {code} {name} 的網頁中找不到原經理人「{manager}」，可能已更換！\n🔗 檢查網址：{url}")
                else:
                    print(f"  [OK] Found {manager}")
                
                page.close()
            except Exception as e:
                print(f"  [Error] {code}: {e}")
                alerts.append(f"❌ {code} {name} 經理人檢查網頁讀取失敗：{e}")
        
        browser.close()
        
    if alerts:
        msg = "🔔 **ETF 經理人異動警告** 🔔\n\n" + "\n\n".join(alerts)
        send_telegram(msg)
        print("Alerts sent.")
    else:
        print("All managers verified. No alerts needed.")

if __name__ == "__main__":
    main()
