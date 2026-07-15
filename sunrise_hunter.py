import os
import sys
import csv
import re
import requests
import traceback
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ⚙️ 環境変数の読み込み
CSV_URL = os.environ.get("CONFIG_CSV_URL")
LINE_TOKEN = os.environ.get("LINE_ACCESS_TOKEN")
LINE_USER = os.environ.get("LINE_USER_ID")
LINE_GROUP = os.environ.get("LINE_GROUP_ID")

def send_line(message):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"}
    for to_id in [LINE_USER, LINE_GROUP]:
        if to_id:
            payload = {"to": to_id, "messages": [{"type": "text", "text": message}]}
            requests.post(url, headers=headers, json=payload)

def get_target_config():
    try:
        res = requests.get(CSV_URL)
        res.encoding = 'utf-8'
        lines = res.text.splitlines()
        reader = list(csv.reader(lines))
        if len(reader) < 2:
            print("⚠️ スプレッドシートにデータがありません。")
            sys.exit(0)
        latest = reader[-1]
        raw_date = latest[1].replace("/", "-")
        dt = datetime.strptime(raw_date, "%Y-%m-%d")
        return {
            "year": str(dt.year), "month": str(dt.month), "day": str(dt.day),
            "dep": latest[2].strip(), "arr": latest[3].strip()
        }
    except Exception as e:
        print(f"CSV読み込み失敗: {e}")
        sys.exit(0)

def is_within_active_hours():
    """⏰ 日本時間の 5:29〜23:51 の間だけ動く秒速判定センサー"""
    jst = timezone(timedelta(hours=9)) # 日本時間 (UTC+9)
    now_jst = datetime.now(jst).time()
    
    start_time = datetime.strptime("05:29", "%H:%M").time()
    end_time = datetime.strptime("23:51", "%H:%M").time()
    
    return start_time <= now_jst <= end_time

def is_e5489_error(page_content):
    error_keywords = [
        "20100801", "99990110", "00604087", 
        "処理中にエラーが発生しました", "混雑中ですが"
    ]
    return any(k in page_content for k in error_keywords)

def parse_mark(td):
    """セルの文字を綺麗なマークに変換する"""
    text = td.get_text().strip()
    if "○" in text or "内車" in text:
        return "○"
    elif "△" in text:
        return "△"
    elif "◇" in text:
        return "◇"  # 日付またぎ特殊空席マーク
    elif "×" in text:
        return "×"
    else:
        return "--"

def main():
    if not is_within_active_hours():
        print("💤 現在は稼働時間外（5:29〜23:51）のため、何もせずに即時終了します。")
        return

    config = get_target_config()
    print(f"🎯 ステルス巡回開始: {config['year']}年{config['month']}月{config['day']}日 | {config['dep']} ➡️ {config['arr']}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        page = context.new_page()

        try:
            # 💡 1回のアクション内で、30秒待機を挟んで3回チェックを繰り返す
            for attempt in range(3):
                if not is_within_active_hours():
                    print("⏰ ループ中に稼働時間を過ぎたため、終了します。")
                    return

                if attempt > 0:
                    print("⏳ 間隔を短くするため、30秒待機して再チェックします...")
                    page.wait_for_timeout(30000)

                print(f"🔄 チェック {attempt + 1} 回目 実行中...")
                
                page.goto("https://e5489.jr-odekake.net/e5489/cspc/CBTopMenuPC")
                page.click("text=新規予約")
                page.wait_for_load_state("networkidle")

                if is_e5489_error(page.content()):
                    print("⚠️ エラーまたは混雑を検知。次へ進みます。")
                    continue

                page.click("text=駅名を入力")
                page.fill("input[id='txtStnNameFrom']", config["dep"])
                page.fill("input[id='txtStnNameTo']", config["arr"])
                page.select_option("select[name='selMonth']", config["month"])
                page.select_option("select[name='selDay']", config["day"])

                # 出発時間の自動セット
                if config["dep"] == "三ノ宮":
                    page.select_option("select[name='selHour']", "23")
                    page.select_option("select[name='selMinute']", "50")
                else:
                    page.select_option("select[name='selHour']", "18")
                    page.select_option("select[name='selMinute']", "00")

                page.uncheck("input[id='chkShinkansen']")
                page.check("input[id='chkLimitedExpress']")
                page.click("text=検索する（新規予約）")
                page.wait_for_load_state("networkidle")

                if is_e5489_error(page.content()):
                    print("⚠️ 検索エラーまたは混雑を検知。次へ進みます。")
                    continue

                change_buttons = page.locator("text=この列車を変更")
                if change_buttons.count() == 0:
                    print("📭 サンライズ号が見つかりません。")
                    continue

                change_buttons.first.click()
                page.wait_for_selector("text=後の列車", timeout=5000)
                page.click("text=後の列車")
                page.wait_for_load_state("networkidle")

                html = page.content()
                soup = BeautifulSoup(html, "html.parser")
                rows = soup.find_all("tr")

                trains_status = {}
                for row in rows:
                    tds = row.find_all(["td", "th"])
                    sunrise_cell_idx = -1
                    train_raw_name = ""
                    
                    for idx, td in enumerate(tds):
                        if "サンライズ" in td.get_text():
                            sunrise_cell_idx = idx
                            train_raw_name = td.get_text().strip().replace("\n", "").replace(" ", "")
                            break
                    
                    if sunrise_cell_idx == -1:
                        continue

                    right_tds = tds[sunrise_cell_idx + 1:]
                    if len(right_tds) < 5:
                        continue

                    base_name = re.sub(r'（.+?）|\(.+?\)', '', train_raw_name).strip()

                    if base_name not in trains_status:
                        trains_status[base_name] = {
                            "ソロ禁煙": "--", "single禁煙": "--", "single喫煙": "--",
                            "シングルツイン禁煙": "--", "シングルツイン喫煙": "--",
                            "シングルデラックス禁煙": "--", "シングルデラックス喫煙": "--",
                            "サンライズツイン禁煙": "--", "サンライズツイン喫煙": "--"
                        }

                    if "ソロ" in train_raw_name:
                        trains_status[base_name]["ソロ禁煙"] = parse_mark(right_tds[1])
                    elif "シングル" in train_raw_name and "ツイン" not in train_raw_name and "デラックス" not in train_raw_name:
                        trains_status[base_name]["single禁煙"] = parse_mark(right_tds[1])
                        trains_status[base_name]["single喫煙"] = parse_mark(right_tds[2])
                    elif "サツイン" in train_raw_name or "サンライズツイン" in train_raw_name:
                        trains_status[base_name]["サンライズツイン禁煙"] = parse_mark(right_tds[1])
                        trains_status[base_name]["サンライズツイン喫煙"] = parse_mark(right_tds[2])
                    else:
                        trains_status[base_name]["シングルツイン禁煙"] = parse_mark(right_tds[1])
                        trains_status[base_name]["シングルツイン喫煙"] = parse_mark(right_tds[2])
                        trains_status[base_name]["シングルデラックス禁煙"] = parse_mark(right_tds[3])
                        trains_status[base_name]["シングルデラックス喫煙"] = parse_mark(right_tds[4])

                any_vacant = False
                status_text = ""

                for t_name, rooms in trains_status.items():
                    status_text += f"◆ {t_name}\n-------------------------------\n"
                    order = [
                        "ソロ禁煙", "シングル禁煙", "シングル喫煙", 
                        "シングルツイン禁煙", "シングルツイン喫煙", 
                        "シングルデラックス禁煙", "シングルデラックス喫煙", 
                        "サンライズツイン禁煙", "サンライズツイン喫煙"
                    ]
                    for key in order:
                        lookup_key = "single禁煙" if key == "シングル禁煙" else ("single喫煙" if key == "シングル喫煙" else key)
                        mark = rooms[lookup_key]
                        
                        alert = ""
                        if mark in ["○", "△"]:
                            alert = " 🎉空席!!"
                            any_vacant = True
                        elif mark == "◇":
                            alert = " 🎉空席(◇)!!"
                            any_vacant = True
                        
                        status_text += f"・{key} ➡️ [ {mark} ]{alert}\n"
                    status_text += "===============================\n"

                if any_vacant:
                    msg = (
                        f"【🚨 サンライズ空席速報！！】\n"
                        f"お目当てのキャンセルが放流されました！\n\n"
                        f"[乗車日(始発駅基準)] {config['month']}月{config['day']}日\n"
                        f"[区間] {config['dep']} ➡️ {config['arr']}\n\n"
                        f"🔥 現在の全設備ステータス:\n"
                        f"===============================\n"
                        f"{status_text}"
                    )
                    print(f"🎉 空席検知！LINEへ送信して巡回を終了します。")
                    send_line(msg)
                    return 

            print("📭 今回の連続チェック（約1.5分間）を完了。すべて「満席」でした。")

        except Exception as e:
            print(f"❌ エラー発生: {e}")
            traceback.print_exc()
        finally:
            browser.close()

if __name__ == "__main__":
    main()
