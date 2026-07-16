import os
import sys
import csv
import re
import time
import random
import requests
import traceback
import urllib.parse
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ⚙️ 環境変数の読み込み
CSV_URL = os.environ.get("CONFIG_CSV_URL")
LINE_TOKEN = os.environ.get("LINE_ACCESS_TOKEN")
LINE_USER = os.environ.get("LINE_USER_ID")
LINE_GROUP = os.environ.get("LINE_GROUP_ID")

@dataclass
class SunRiseStatus:
    nobinobi: str = "--"
    solo: str = "--"
    single_kinyen: str = "--"
    single_kitsuyen: str = "--"
    single_twin_kinyen: str = "--"
    single_twin_kitsuyen: str = "--"
    single_dx_kinyen: str = "--"
    single_dx_kitsuyen: str = "--"
    sunrise_twin_kinyen: str = "--"
    sunrise_twin_kitsuyen: str = "--"

    def to_dict(self):
        return {
            "ノビノビ禁煙": self.nobinobi,
            "ソロ禁煙": self.solo,
            "シングル禁煙": self.single_kinyen,
            "シングル喫煙": self.single_kitsuyen,
            "シングルツイン禁煙": self.single_twin_kinyen,
            "シングルツイン喫煙": self.single_twin_kitsuyen,
            "シングルデラックス禁煙": self.single_dx_kinyen,
            "シングルデラックス喫煙": self.single_dx_kitsuyen,
            "サンライズツイン禁煙": self.sunrise_twin_kinyen,
            "サンライズツイン喫煙": self.sunrise_twin_kitsuyen
        }

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
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst).time()
    start_time = datetime.strptime("05:29", "%H:%M").time()
    end_time = datetime.strptime("23:51", "%H:%M").time()
    return start_time <= now_jst <= end_time

def is_e5489_error(page_title, page_url, html_content):
    try:
        if "ご案内" in page_title and not any(k in page_title for k in ["経路・設備選択", "列車の変更"]):
            return True
        if any(k in page_url for k in ["/Error/", "/Guide/", "/Message/"]):
            return True
        error_keywords = [
            "20100801", "99990110", "00604087", 
            "処理中にエラーが発生しました", "混雑中ですが", "大変混み合っております"
        ]
        return any(k in html_content for k in error_keywords)
    except:
        return True

def parse_mark_str(text):
    if "○" in text or "内車" in text or "空席あり" in text or "vacant" in text:
        return "○"
    elif "△" in text or "残りわずか" in text or "almost" in text:
        return "△"
    elif "◇" in text or "事前申込" in text or "undefined" in text:
        return "◇"
    elif "×" in text or "残席なし" in text or "unavailable" in text:
        return "×"
    return "--"

def scrape_page1_table(soup, status: SunRiseStatus):
    tables = soup.find_all("table", class_="seat-status-table")
    if not tables:
        return False
        
    print("    📊 [解析] 1ページ目の通常設備テーブルを検出しました。")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            row_text = "".join(row.stripped_strings)
            is_smoking = "喫煙" in row_text
            
            mark = "--"
            td = row.find("td")
            if td:
                img = td.find("img")
                if img:
                    mark = parse_mark_str(img.get("alt", ""))
                    if mark == "--":
                        mark = parse_mark_str(img.get("src", ""))
            
            if "普通" in row_text or "ノビノビ" in row_text:
                status.nobinobi = mark
            elif "シングルツイン" in row_text:
                if is_smoking: status.single_twin_kitsuyen = mark
                else: status.single_twin_kinyen = mark
            elif "デラックス" in row_text or "ＤＸ" in row_text:
                if is_smoking: status.single_dx_kitsuyen = mark
                else: status.single_dx_kinyen = mark
    return True

def scrape_page2_list(soup, status: SunRiseStatus):
    lists = soup.find_all("ul", class_="changing-train-list")
    if not lists:
        return False
        
    print("    📊 [解析] 2ページ目の個室アコーディオンリストを検出しました。")
    for u_list in lists:
        items = u_list.find_all("li", recursive=False)
        for item in items:
            header_train = item.find(class_="train-info-heading__train")
            if not header_train:
                continue
            header_text = "".join(header_train.stripped_strings)
            
            category_match = re.search(r'（(.+?)）|\((.+?)\)', header_text)
            if not category_match:
                continue
            category = category_match.group(1) or category_match.group(2)
            
            boxes = item.find_all(class_="changing-train-box")
            for box in boxes:
                box_text = "".join(box.stripped_strings)
                is_smoking = "喫煙" in box_text
                
                mark = "--"
                status_div = box.find(class_="changing-train-box__status")
                if status_div:
                    img = status_div.find("img")
                    if img:
                        mark = parse_mark_str(img.get("alt", ""))
                        if mark == "--":
                            mark = parse_mark_str(img.get("src", ""))
                
                if mark == "--" and "disabled" in "".join(box.get("class", [])):
                    mark = "×"
                    
                if "ソロ" in category:
                    status.solo = mark
                elif "サンライズツイン" in category or "サツイン" in category:
                    if is_smoking: status.sunrise_twin_kitsuyen = mark
                    else: status.sunrise_twin_kinyen = mark
                elif "シングル" in category and "ツイン" not in category:
                    if is_smoking: status.single_kitsuyen = mark
                    else: status.single_kinyen = mark
    return True

def main():
    if not is_within_active_hours():
        print("💤 現在は稼働時間外（5:29〜23:51）のため、何もせずに即時終了します。")
        return

    config = get_target_config()
    
    # 過去日付ガード
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    target_dt = datetime(int(config["year"]), int(config["month"]), int(config["day"]), 23, 59, 59, tzinfo=jst)
    if target_dt < now_jst:
        print(f"⚠️ 【自動停止】指定された乗車日（{config['month']}月{config['day']}日）は過去の日付です。")
        sys.exit(0)

    print(f"🎯 モバイル完全偽装Wスキャン開始: {config['year']}年{config['month']}月{config['day']}日 | {config['dep']} ➡️ {config['arr']}")

    dep_st = "高松（香川県）" if config["dep"] == "高松" else config["dep"]
    arr_st = "高松（香川県）" if config["arr"] == "高松" else config["arr"]

    encoded_dep = urllib.parse.quote(dep_st.encode("cp932"))
    encoded_arr = urllib.parse.quote(arr_st.encode("cp932"))
    facility_id = "%BB%BE%C4%20%20000" if "高松" in dep_st or "高松" in arr_st else "%BB%B2%BD%D3%20%20000"
    target_date = f"{config['year']}{int(config['month']):02d}{int(config['day']):02d}"

    hour, minute = ("23", "50") if config["dep"] == "三ノ宮" else ("18", "00")

    param = (
        f"inputDepartStName={encoded_dep}&inputArriveStName={encoded_arr}&inputType=0"
        f"&inputDate={target_date}&inputHour={hour}&inputMinute={minute}"
        f"&inputUniqueDepartSt=1&inputUniqueArriveSt=1&inputSearchType=1"
        f"&inputTransferDepartStName1={encoded_dep}&inputTransferArriveStName1={encoded_arr}"
        f"&inputTransferDepartStUnique1=1&inputTransferArriveStUnique1=1"
        f"&inputTransferTrainType1=0001&inputSpecificTrainType1=2"
        f"&inputSpecificBriefTrainKana1={facility_id}&SequenceType=0"
        f"&inputReturnUrl=goyoyaku/campaign/sunriseseto_izumo/form.html"
        f"&RTURL=https://www.jr-odekake.net/goyoyaku/campaign/sunriseseto_izumo/form.html"
    )

    direct_url = f"https://e5489.jr-odekake.net/e5489/cspc/CBDayTimeArriveSelRsvMyDiaPC?{param}"
    referer_url = "https://www.jr-odekake.net/goyoyaku/campaign/sunriseseto_izumo/form.html"

    max_attempts = 15
    backoff_base = [2, 3, 4, 6, 8, 11, 15, 15, 15, 15, 15, 15, 15, 15, 15]
    
    success_scrape_at_least_once = False
    status_obj = SunRiseStatus()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        try:
            for attempt in range(max_attempts):
                if not is_within_active_hours():
                    return

                if attempt > 0:
                    sleep_time = backoff_base[attempt] + random.uniform(0.5, 2.0)
                    print(f"⏳ サーバー混雑中... {sleep_time:.2f}秒後にモバイル再突撃します...")
                    time.sleep(sleep_time)

                print(f"📱 【iPhone13型独立窓】アタック {attempt + 1} / {max_attempts} 回目...")
                
                iphone_config = p.devices["iPhone 13"]
                context = browser.new_context(**iphone_config)
                page = context.new_page()
                
                try:
                    # 🎯 タイムアウトのみ指定。wait_untilのフライングを抑制
                    page.goto(direct_url, referer=referer_url, timeout=20000)
                    
                    # ⏳ 【修正点】HTMLやタイトルを引っこ抜く前に、まずテーブル要素の出現を「絶対待つ」
                    print("    ⏳ [DEBUG] 1ページ目の通常設備テーブルを待機中...")
                    page.locator(".seat-status-table").wait_for(timeout=10000)
                    
                    # 同期が完了したこの瞬間に初めて、各種情報を1回だけシリアライズ！
                    html_p1 = page.content()
                    title_p1 = page.title()
                    url_p1 = page.url
                    
                    if is_e5489_error(title_p1, url_p1, html_p1):
                        print("    ⚠️ [STATE: BLOCKED] 1ページ目で混雑画面を検知。セッションを破棄します。")
                        continue

                    print("    📸 [STATE: PAGE1_SCAN] 1ページ目の通常設備スキャン中...")
                    soup_p1 = BeautifulSoup(html_p1, "html.parser")
                    scrape_page1_table(soup_p1, status_obj)
                    success_scrape_at_least_once = True

                    # ポップアップ用の「この列車を変更」リンクを待ってクリック
                    popup_link = page.locator(".popup-link")
                    popup_link.wait_for(timeout=3000)
                    popup_link.first.click()
                    
                    # 2ページ目へ進む「後の列車」ボタンの出現を完璧に待つ！
                    next_btn = page.locator(".change-next-train-button")
                    next_btn.wait_for(timeout=4000)
                    next_btn.click()
                    
                    # 2ページ目のアコーディオンリストが画面に出現するまで完全同期！
                    print("    ⏳ [DEBUG] 2ページ目の個室リストの出現を待機中...")
                    page.locator(".changing-train-list").wait_for(timeout=10000)
                    
                    html_p2 = page.content()
                    if is_e5489_error(page.title(), page.url(), html_p2):
                        print("    ⚠️ [STATE: BLOCKED] 2ページ目への遷移中に混雑に阻まれました。")
                        continue

                    print("    📸 [STATE: PAGE2_SCAN] 2ページ目の個室アコーディオンを解析中...")
                    soup_p2 = BeautifulSoup(html_p2, "html.parser")
                    scrape_page2_list(soup_p2, status_obj)
                    print("    🎉 [STATE: SUCCESS] 全設備の完全踏破に成功！")
                    break
                            
                except Exception as attempt_err:
                    # 💡 【大修正】握りつぶしていた例外とスタックトレースをすべて可視化！！
                    print("==================================================")
                    print(f"❌ アタック {attempt + 1} 回目でエラーを検出しました。")
                    print(f"型: {type(attempt_err)}")
                    print(f"内容: {attempt_err}")
                    traceback.print_exc()
                    print("==================================================")
                finally:
                    context.close()

            if not success_scrape_at_least_once:
                print("\n❌ 【真実のログ】15回すべてがブロックまたは混雑に阻まれ、データに辿り着けませんでした。")
                sys.exit(1)

            # 空席判定
            any_vacant = False
            status_text = ""

            for room_name, mark in status_obj.to_dict().items():
                alert = ""
                if mark in ["○", "△"]:
                    alert = " 🎉空席!!"
                    any_vacant = True
                elif mark == "◇":
                    alert = " 🎉空席(◇)!!"
                    any_vacant = True
                status_text += f"・{room_name} ➡️ [ {mark} ]{alert}\n"

            if any_vacant:
                msg = (
                    f"【🚨 サンライズ空席速報！！】\n"
                    f"お目当てのキャンセルが放流されました！\n\n"
                    f"[乗車日(始発駅基準)] {config['month']}月{config['day']}日\n"
                    f"[区間] {config['dep']} ➡️ {config['arr']}\n\n"
                    f"🔥 現在の全設備ステータス:\n"
                    f"===============================\n"
                    f"{status_text}"
                    f"===============================\n"
                )
                print(f"🎉 混雑をすり抜け、本物の画面で空席を検知！LINEへ通知します。")
                send_line(msg)
                return 

            print("\n📭 スマホ版Wスキャンリレーに完全成功！現時点ではすべて本当に「満席」でした。")

        except Exception as e:
            print(f"❌ エラー発生: {e}")
            traceback.print_exc()
            sys.exit(1)
        finally:
            browser.close()

if __name__ == "__main__":
    main()
