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
        "処理中にエラーが発生しました", "混雑中ですが", "ご案内"
    ]
    return any(k in page_content for k in error_keywords)

def parse_mark_str(text):
    """セルの文字から空席マークを抽出する"""
    if "○" in text or "内車" in text or "空席あり" in text:
        return "○"
    elif "△" in text or "残りわずか" in text:
        return "△"
    elif "◇" in text or "事前申込" in text:
        return "◇"
    elif "×" in text or "残席なし" in text:
        return "×"
    return "--"

def scrape_train_status(page_content, trains_status):
    """🛡️ レイアウトに依存せず、文字の並びから動的にマークを取得する無敵のスキャンエンジン"""
    soup = BeautifulSoup(page_content, "html.parser")
    lists = soup.find_all("ul", class_="changing-train-list")
    
    for u_list in lists:
        items = u_list.find_all("li", recursive=False)
        for item in items:
            header_train = item.find(class_="train-info-heading__train")
            if not header_train:
                continue
            header_text = header_train.get_text().strip().replace(" ", "").replace("\n", "")
            
            base_name = re.sub(r'（.+?）|\(.+?\)', '', header_text).strip().replace("特急", "")
            category_match = re.search(r'（(.+?)）|\((.+?)\)', header_text)
            if not category_match:
                continue
            category = category_match.group(1) or category_match.group(2)
            
            if base_name not in trains_status:
                trains_status[base_name] = {
                    "ノビノビ禁煙": "--", "ソロ禁煙": "--", "single禁煙": "--", "single喫煙": "--",
                    "シングルツイン禁煙": "--", "シングルツイン喫煙": "--",
                    "シングルデラックス禁煙": "--", "シングルデラックス喫煙": "--",
                    "サンライズツイン禁煙": "--", "サンライズツイン喫煙": "--"
                }
            
            boxes = item.find_all(class_="changing-train-box")
            for box in boxes:
                box_text = box.get_text().strip().replace(" ", "").replace("\n", "")
                is_smoking = "喫煙" in box_text
                
                mark = "--"
                status_div = box.find(class_="changing-train-box__status")
                if status_div:
                    img = status_div.find("img")
                    if img:
                        alt_text = img.get("alt", "")
                        src_text = img.get("src", "")
                        mark = parse_mark_str(alt_text)
                        if mark == "--":
                            mark = parse_mark_str(src_text)
                
                if mark == "--" and "disabled" in "".join(box.get("class", [])):
                    mark = "×"
                    
                facility_key = None
                if "ソロ" in category:
                    facility_key = "ソロ禁煙"
                elif "シングルツイン" in category:
                    facility_key = "シングルツイン喫煙" if is_smoking else "シングルツイン禁煙"
                elif "デラックス" in category or "ＤＸ" in category:
                    facility_key = "シングルデラックス喫煙" if is_smoking else "シングルデラックス禁煙"
                elif "サツイン" in category or "サンライズツイン" in category:
                    facility_key = "サンライズツイン喫煙" if is_smoking else "サンライズツイン禁煙"
                elif "ノビノビ" in category:
                    facility_key = "ノビノビ禁煙"
                elif "シングル" in category:
                    facility_key = "single喫煙" if is_smoking else "single禁煙"
                    
                if facility_key and mark != "--":
                    if trains_status[base_name][facility_key] not in ["○", "△", "◇"]:
                        trains_status[base_name][facility_key] = mark

def main():
    if not is_within_active_hours():
        print("💤 現在は稼働時間外（5:29〜23:51）のため、何もせずに即時終了します。")
        return

    config = get_target_config()
    print(f"🎯 独立クリーン巡回開始: {config['year']}年{config['month']}月{config['day']}日 | {config['dep']} ➡️ {config['arr']}")

    dep_st = "高松（香川県）" if config["dep"] == "高松" else config["dep"]
    arr_st = "高松（香川県）" if config["arr"] == "高松" else config["arr"]

    encoded_dep = urllib.parse.quote(dep_st.encode("cp932"))
    encoded_arr = urllib.parse.quote(arr_st.encode("cp932"))

    is_seto = "高松" in dep_st or "高松" in arr_st
    facility_id = "%BB%BE%C4%20%20000" if is_seto else "%BB%B2%BD%D3%20%20000"

    target_date = f"{config['year']}{int(config['month']):02d}{int(config['day']):02d}"

    if config["dep"] == "三ノ宮":
        hour, minute = "23", "50"
    else:
        hour, minute = "18", "00"

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
    referer_url = "https://www.jr-odekake.net/goyoyaku/campaign/sunriseseto_izumo/form.html"

    max_attempts = 15
    retry_delay_ms = 3000
    
    success_scrape_at_least_once = False
    trains_status = {}

    with sync_playwright() as p:
        # ブラウザ自体は1起動
        browser = p.chromium.launch(headless=True)

        try:
            for attempt in range(max_attempts):
                if not is_within_active_hours():
                    return

                if attempt > 0:
                    page.wait_for_timeout(retry_delay_ms)

                print(f"🔄 【完全独立ウィンドウ】アタック {attempt + 1} / {max_attempts} 回目 発射...")
                
                # 💡 【超重要】1回ごとにコンテキスト（クッキー・セッション）を「完全リセット」して使い捨てる！！
                context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
                page = context.new_page()
                
                try:
                    # 余計なトップメニューは一切踏まない。真っ新な状態でいきなり直行！
                    page.goto(direct_url, referer=referer_url)
                    page.wait_for_load_state("networkidle")

                    current_html = page.content()
                    if is_e5489_error(current_html):
                        print("    ⚠️ 混雑画面（ご案内）を検知。この使い捨てブラウザを即座に破棄します。")
                        continue

                    try:
                        page.wait_for_selector(".changing-train-list, text=この列車を変更", timeout=8000)
                    except Exception as e:
                        print("    ⚠️ 画面の応答が遅延したため、破棄して次へ進みます。")
                        continue

                    # 📸 【Wスキャン①】最初の画面を解析
                    print("    📸 [スキャン①] 最初の画面を解析中...")
                    scrape_train_status(page.content(), trains_status)
                    success_scrape_at_least_once = True # 本物の画面に到達できた証明

                    change_buttons = page.locator("text=この列車を変更")
                    if change_buttons.count() == 0:
                        continue

                    change_buttons.first.click()
                    
                    has_after_button = False
                    try:
                        page.wait_for_selector("text=後の列車", timeout=4000)
                        print("    👉 '後の列車' ボタンを発見。画面2へ進みます...")
                        page.click("text=後の列車")
                        has_after_button = True
                    except Exception as e:
                        pass

                    if has_after_button:
                        page.wait_for_load_state("networkidle")
                        
                        is_loaded_correctly = False
                        for check_sec in range(6):
                            current_html = page.content()
                            if is_e5489_error(current_html):
                                print("    ⚠️ 画面2への遷移中に混雑を検知しました。")
                                break
                            if any(k in current_html for k in ["（ソロ）", "（シングル）", "（サツイン）"]):
                                print(f"    ✅ 画面2の完全同期を確認（{check_sec}秒待機）。")
                                is_loaded_correctly = True
                                break
                            page.wait_for_timeout(1000)

                        # 📸 【Wスキャン②】画面2を解析
                        if is_loaded_correctly:
                            print("    📸 [スキャン②] 2番目の画面（ソロ・シングル等）を解析中...")
                            scrape_train_status(page.content(), trains_status)
                            
                except Exception as attempt_err:
                    print(f"    ⚠️ 個別アタック中に通信エラーが発生しました。")
                finally:
                    # 💡 使い終わったクッキーをこの瞬間にこの世から完全に消滅させる
                    context.close()

            # 💡 15回完全に独立して突撃しても全滅した場合の最終仕分け
            if not success_scrape_at_least_once:
                print("\n❌ 【真実のログ】15回すべてがサーバー混雑（ご案内）に阻まれ、一度も空席テーブルに辿り着けませんでした。")
                print("    サーバーが限界を迎えています。この実行は『失敗（Fail）』とします。")
                sys.exit(1)

            # 1回でも開けていた場合のみ、空席判定へ
            any_vacant = False
            status_text = ""

            for t_name, rooms in trains_status.items():
                status_text += f"◆ {t_name}\n-------------------------------\n"
                order = [
                    "ノビノビ禁煙", "ソロ禁煙", "single禁煙", "single喫煙", 
                    "シングルツイン禁煙", "シングルツイン喫煙", 
                    "シングルデラックス禁煙", "シングルデラックス喫煙", 
                    "サンライズツイン禁煙", "サンライズツイン喫煙"
                ]
                for key in order:
                    disp_key = "ノビノビ禁煙" if key == "ノビノビ禁煙" else (
                        "ソロ禁煙" if key == "ソロ禁煙" else (
                            "シングル禁煙" if key == "single禁煙" else (
                                "シングル喫煙" if key == "single喫煙" else (
                                    "シングルツイン禁煙" if key == "シングルツイン禁煙" else (
                                        "シングルツイン喫煙" if key == "シングルツイン喫煙" else (
                                            "シングルデラックス禁煙" if key == "シングルデラックス禁煙" else (
                                                "シングルデラックス喫煙" if key == "シングルデラックス喫煙" else (
                                                    "サンライズツイン禁煙" if key == "サンライズツイン禁煙" else "サンライズツイン喫煙"
                                                    )
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                    
                    mark = rooms.get(key, "--")
                    
                    alert = ""
                    if mark in ["○", "△"]:
                        alert = " 🎉空席!!"
                        any_vacant = True
                    elif mark == "◇":
                        alert = " 🎉空席(◇)!!"
                        any_vacant = True
                    
                    status_text += f"・{disp_key} ➡️ [ {mark} ]{alert}\n"
                status_text += "===============================\n"

            if any_vacant:
                msg = (
                    f"【🚨 サンライズ空席速報！！】\n"
                    f"とりめしさん、ついに混雑の壁を完全に突破して空席を検知しました！\n\n"
                    f"[乗車日(始発駅基準)] {config['month']}月{config['day']}日\n"
                    f"[区間] {config['dep']} ➡️ {config['arr']}\n\n"
                    f"🔥 現在の全設備ステータス:\n"
                    f"===============================\n"
                    f"{status_text}"
                )
                print(f"🎉 混雑をすり抜け、本物の画面で空席を検知！LINEへ通知します。")
                send_line(msg)
                return 

            print("\n📭 混雑の隙間を突いて画面の取得に成功しましたが、現時点ではすべて本当に「満席」でした。")

        except Exception as e:
            print(f"❌ エラー発生: {e}")
            traceback.print_exc()
            sys.exit(1)
        finally:
            browser.close()

if __name__ == "__main__":
    main()
