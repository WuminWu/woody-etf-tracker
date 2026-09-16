# -*- coding: utf-8 -*-
"""
capitalfund.py — 群益 ETF 持股 xlsx 下載（00982A / 00992A / 00997A 共用）。

## 為什麼有這個檔案
這三支原本各有一份幾乎逐字相同的 download_xlsx（00982A 與 00992A 完全相同，
00997A 只差排版）。而它們共有一個問題：**來源當日尚未揭露時會直接拋例外**。

群益網站的規則是「輸入日期 D 會拿到 D 的前一個交易日的持股」，所以要抓 T 日
就得輸入 T+1。若輸入的日期網站還沒有資料，畫面會彈出錯誤對話框
（div.app-dialog-error）蓋在下載鈕上方攔截點擊事件，於是：

    playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.
      - <div class="app-dialog-error"> ... subtree intercepts pointer events

例外往上拋 → 腳本非零結束。其他 16 支爬蟲在這種情況是送出「持股尚未更新」後
正常退出，只有這三支不一樣。而且每輪要白等 30 秒逾時，一天 7 輪 × 3 支 = 10 分鐘。

現在改成：設好日期後先看對話框在不在，在就直接回 None（呼叫端 main() 本來就會
據此送出「尚未更新」並正常 return），完全不用等逾時。

對話框偵測已實地驗證雙向正確（2026-09-16）：
    2026/09/16（有資料）→ 對話框不可見，下載成功
    2026/09/17（無資料）→ 對話框可見，跳過
"""

import logging
import os
import time

from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

DATE_INPUT = "#condition-date"
DOWNLOAD_BTN = "button.buyback-search-section-btn"
ERROR_DIALOG = "div.app-dialog-error"


def download_holdings_xlsx(fund_url, date_str, out_path):
    """下載該基金在 date_str（格式 yyyy/mm/dd）查詢下的持股 xlsx。

    回傳存檔路徑；來源尚未揭露或任何下載失敗一律回傳 None（不拋例外）。
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_context(accept_downloads=True).new_page()
            log.info(f"Navigating to {fund_url} ...")
            page.goto(fund_url, wait_until="networkidle", timeout=30000)
            time.sleep(3)

            date_input = page.locator(DATE_INPUT)
            if not date_input.is_visible():
                log.error("找不到日期輸入框")
                return None

            # Angular 的日期元件是分段輸入，.type() 會填錯；必須用原生 setter 設值再派事件
            page.evaluate(f"""
                var input = document.querySelector('{DATE_INPUT}');
                var setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(input, '{date_str}');
                input.dispatchEvent(new Event('input', {{bubbles: true}}));
                input.dispatchEvent(new Event('change', {{bubbles: true}}));
            """)
            time.sleep(1.5)
            log.info(f"日期已設為 {date_input.input_value()}")

            # 先看有沒有「查無資料」對話框：有的話點下載鈕會被它攔截、白等 30 秒逾時
            err = page.locator(ERROR_DIALOG)
            if err.count() > 0 and err.first.is_visible():
                log.info(f"{date_str} 來源尚未揭露（網站顯示錯誤對話框），跳過。")
                return None

            btn = page.locator(DOWNLOAD_BTN)
            if btn.count() == 0:
                log.error("找不到下載按鈕")
                return None

            log.info(f"點擊下載（查詢日 {date_str}）...")
            try:
                with page.expect_download(timeout=30000) as dl_info:
                    btn.first.click(timeout=10000)
                dl = dl_info.value
            except Exception as e:
                # 對話框可能晚一步才出現，仍要正常收尾而不是拋例外
                log.warning(f"下載未完成（{type(e).__name__}），視為來源尚未揭露。")
                return None

            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            dl.save_as(out_path)
            log.info(f"已下載：{dl.suggested_filename}")
            return out_path
        except Exception as e:
            log.error(f"下載失敗：{type(e).__name__}: {e}")
            return None
        finally:
            browser.close()
