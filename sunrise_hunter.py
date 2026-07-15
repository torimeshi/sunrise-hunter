import os
import sys
import csv
import re
import requests
import traceback
import urllib.parse
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

def scrape_train_status(page_content, trains_status):
    """🛡️ テーブル構造を動的に解析して安全にすべての設備マークを取得するスキャンエンジン"""
    soup = BeautifulSoup(page_content, "html.parser")
    rows = soup.find_all("tr")

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
        if len(right_tds) < 1:
            continue

        base_name = re.sub(r'（.+?）|\(.+?\)', '', train_raw_name).strip()

        if base_name not in trains_status:
            trains_status[base_name] = {
                "ソロ禁煙": "--", "single禁煙": "--", "single喫煙": "--",
                "シングルツイン禁煙": "--", "シングルツイン喫煙": "--",
                "シングルデラックス禁煙": "--", "シングルデラックス喫煙": "--",
                "サンライズツイン禁煙": "--", "サンライズツイン喫煙": "--"
            }

        # 1. ソロ：[設備名, 禁煙] のため、right_tds[1] が禁煙マーク
        if "ソロ" in train_raw_name:
            if len(right_tds) >= 2:
                trains_status[base_name]["ソロ禁煙"] = parse_mark(right_tds[1])
                
        # 2. シングル：[設備名, 禁煙, 喫煙] のため、right_tds[1] が禁煙、right_tds[2] が喫煙
        elif "シングル" in train_raw_name and "ツイン" not in train_raw_name and "デラックス" not in train_raw_name:
            if len(right_tds) >= 3:
                trains_status[base_name]["single禁煙"] = parse_mark(right_tds[1])
                trains_status[base_name]["single喫煙"] = parse_mark(right_tds[2])
            elif len(right_tds) >= 2:
                trains_status[base_name]["single禁煙"] = parse_mark(right_tds[1])
                
        # 3. サンライズツイン：[設備名, 禁煙, 喫煙] のため、right_tds[1] が禁煙、right_tds[2] が喫煙
        elif "サツイン" in train_raw_name or "サンライズツイン" in train_raw_name:
            if len(right_tds) >= 3:
                trains_status[base_name]["サンライズツイン禁煙"] = parse_mark(right_tds[1])
                trains_status[base_name]["サンライズツイン喫煙"] = parse_mark(right_tds[2])
            elif len(right_tds) >= 2:
                trains_status[base_name]["サンライズツイン禁煙"] = parse_mark(right_tds[1])
                
        # 4. シングルツイン：[設備名, 禁煙, 喫煙]
        elif "シングルツイン" in train_raw_name:
            if len(right_tds) >= 3:
                trains_status[base_name]["シングルツイン禁煙"] = parse_mark(right_tds[1])
                trains_status[base_name]["シングルツイン喫煙"] = parse_mark(right_tds[2])
            elif len(right_tds) >= 2:
                trains_status[base_name]["シングルツイン禁煙"] = parse_mark(right_tds[1])
                
        # 5. シングルデラックス：[設備名, 禁煙, 喫煙]
        elif "デラックス" in train_raw_name or "シングルＤＸ" in train_raw_name:
            if len(right_tds) >= 3:
                trains_status[base_name]["シングルデラックス禁煙"] = parse_mark(right_tds[1])
                trains_status[base_name]["シングルデラックス喫煙"] = parse_mark(right_tds[2])
            elif len(right_tds) >= 2:
                trains_status[base_name]["シングルデラックス禁煙"] = parse_mark(right_tds[1])

def main():
    if not is_within_active_hours():
        print("💤 現在は稼働時間外（5:29〜23:51）のため、何もせずに即時終了します。")
        return

    config = get_target_config()
    print(f"🎯 ステルス直行巡回開始: {config['year']}年{config['month']}月{config['day']}日 | {config['dep']} ➡️ {config['arr']}")

    # 🔗 e5489直行ワープURLの自動組み立て
    dep_st = "高松（香川県）" if config["dep"] == "高松" else config["dep"]
    arr_st = "高松（香川県）" if config["arr"] == "高松" else config["arr"]

    encoded_dep = urllib.parse.quote(dep_st.encode("cp932"))
    encoded_arr = urllib.parse.quote(arr_st.encode("cp932"))

    # サンライズ瀬戸・出雲の判定
    is_seto = "高松" in dep_st or "高松" in arr_st
    facility_id = "%BB%BE%C4%20%20000" if is_seto else "%BB%B2%BD%D3%20%20000"

    target_date = f"{config['year']}{int(config['month']):02d}{int(config['day']):02d}"

    if config["dep"] == "三ノ宮":
        hour, minute = "23", "50"
    else:
        hour, minute = "18", "00"

    # パラメータ組み立て
    param = (
        f"inputDepartStName={encoded_dep}"
        f"&inputArriveStName={encoded_arr}"
        f"&inputType=0"
        f"&inputDate={target_date}"
        f"&inputHour={hour}"
        f"&inputMinute={minute}"
        f"&inputUniqueDepartSt=1"
        f"&inputUniqueArriveSt=1"
        f"&inputSearchType=1"
        f"&inputTransferDepartStName1={encoded_dep}"
        f"&inputTransferArriveStName1={encoded_arr}"
        f"&inputTransferDepartStUnique1=1"
        f"&inputTransferArriveStUnique1=1"
        f"&inputTransferTrainType1=0001"
        f"&inputSpecificTrainType1=2"
        f"&inputSpecificBriefTrainKana1={facility_id}"
        f"&SequenceType=0"
        f"&inputReturnUrl=goyoyaku/campaign/sunriseseto_izumo/form.html"
        f"&RTURL=https://www.jr-odekake.net/goyoyaku/campaign/sunriseseto_izumo/form.html"
    )

    direct_url = f"https://e5489.jr-odekake.net/e5489/cspc/CBDayTimeArriveSelRsvMyDiaPC?{param}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        page = context.new_page()

        try:
            for attempt in range(3):
                if not is_within_active_hours():
                    print("⏰ ループ中に稼働時間を過ぎたため、終了します。")
                    return

                if attempt > 0:
                    print("⏳ 間隔を短くするため、30秒待機して再チェックします...")
                    page.wait_for_timeout(30000)

                print(f"🔄 チェック {attempt + 1} 回目 実行中...")
                
                # 1️⃣ まずトップメニューにアクセスし、セッション（クッキー）を確立する！
                page.goto("https://e5489.jr-odekake.net/e5489/cspc/CBTopMenuPC")
                page.wait_for_load_state("networkidle")

                # 2️⃣ そのセッションを保持した状態で、直行ワープURLへアクセス！
                page.goto(direct_url)
                page.wait_for_load_state("networkidle")

                if is_e5489_error(page.content()):
                    print("⚠️ エラーまたは混雑を検知。次の30秒後チェックに期待します。")
                    continue

                try:
                    # 「この列車を変更」が出現するまで最大15秒待機
                    page.wait_for_selector("text=この列車を変更", timeout=15000)
                except Exception as e:
                    print("⚠️ 'この列車を変更' ボタンが見つかりませんでした。画面遷移に失敗した可能性があります。")
                    continue

                change_buttons = page.locator("text=この列車を変更")
                if change_buttons.count() == 0:
                    print("📭 サンライズ号が見つかりません。")
                    continue

                # 「この列車を変更」をクリック
                change_buttons.first.click()
                
                trains_status = {}

                # 💡 【ダブルスキャン：第1波】
                # 「後の列車」を押す前の最初の設備画面を解析
                try:
                    page.wait_for_selector("text=現在選択している列車", timeout=10000)
                    page.wait_for_load_state("networkidle")
                    print("📸 [スキャン①] 最初の画面（ノビノビ・ツイン・デラックス等）を解析中...")
                    scrape_train_status(page.content(), trains_status)
                except Exception as e:
                    print("⚠️ 最初の設備画面のロードに失敗しました。")

                # 💡 【ダブルスキャン：第2波】
                # 「後の列車」ボタンがあれば、それをクリックして2番目の設備画面へ進む
                has_after_button = False
                try:
                    page.wait_for_selector("text=後の列車", timeout=5000)
                    print("👉 '後の列車' ボタンを発見。画面2（ソロ・シングル等）へ進みます...")
                    page.click("text=後の列車")
                    page.wait_for_load_state("networkidle")
                    has_after_button = True
                except Exception as e:
                    print("ℹ️ '後の列車' ボタンはありません。最初の画面のみでチェックを続行します。")

                # 後の列車をクリックした場合、その画面もスキャンしてデータをマージ
                if has_after_button:
                    try:
                        page.wait_for_selector("text=現在選択している列車", timeout=10000)
                        page.wait_for_load_state("networkidle")
                        print("📸 [スキャン②] 2番目の画面を解析中...")
                        scrape_train_status(page.content(), trains_status)
                    except Exception as e:
                        print("⚠️ 2番目の設備画面のロードに失敗しました。")

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
