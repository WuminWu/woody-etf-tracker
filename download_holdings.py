"""
Download 00981A holdings from ezmoney.com.tw
Navigate to Fund Info page -> click 基金投資組合 tab -> click 匯出XLSX檔
"""
import os
import time
from playwright.sync_api import sync_playwright
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))

FUND_URL = "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode=49YTW"
HOLDINGS_DIR = "holdings"

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)

def download_holdings():
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        
        print(f"Navigating to {FUND_URL}...")
        page.goto(FUND_URL, wait_until="networkidle")
        time.sleep(3)
        
        # Click 基金投資組合 tab
        print("Looking for 基金投資組合 tab...")
        portfolio_link = page.locator("a:has-text('基金投資組合')")
        if portfolio_link.count() > 0:
            portfolio_link.first.click()
            print("Clicked 基金投資組合 tab")
            page.wait_for_timeout(5000)
        else:
            print("Could not find 基金投資組合 tab, trying anchor link...")
            page.goto(FUND_URL + "#asset", wait_until="networkidle")
            page.wait_for_timeout(5000)
        
        # Scroll to bottom to find the export button
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)
        
        # Look for the export XLSX button
        print("Looking for 匯出XLSX檔 button...")
        export_btn = page.locator("button:has-text('匯出XLSX')")
        if export_btn.count() == 0:
            export_btn = page.locator("button:has-text('匯出')")
        if export_btn.count() == 0:
            export_btn = page.locator("a:has-text('匯出XLSX')")
        
        if export_btn.count() > 0:
            print(f"Found export button! Count: {export_btn.count()}")
            # Wait for download
            with page.expect_download(timeout=30000) as download_info:
                export_btn.first.click()
                print("Clicked export button, waiting for download...")
            
            download = download_info.value
            # Save to holdings directory with date
            save_path = os.path.join(HOLDINGS_DIR, f"00981A_holdings_{today_str}.xlsx")
            download.save_as(save_path)
            print(f"Successfully downloaded holdings to: {save_path}")
            print(f"Original filename: {download.suggested_filename}")
        else:
            print("ERROR: Could not find export button!")
            # Debug: print all buttons
            buttons = page.query_selector_all("button")
            print(f"Found {len(buttons)} buttons on page:")
            for btn in buttons:
                print(f"  - {btn.inner_text().strip()}")
        
        browser.close()

if __name__ == "__main__":
    download_holdings()
