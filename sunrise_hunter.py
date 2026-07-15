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
    tables = soup.find_all("table")
    
    for table in tables:
        if "サンライズ" not in table.get_text():
            continue
            
        # 1. 設備の列ヘッダー（TH）を動的に探す
        facility_headers = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            row_text = "".join([c.get_text() for c in cells])
            # ノビノビ・シングル等のいずれかを含み、かつ「サンライズ」という文字を含まない行＝ヘッダー行
            if any(k in row_text for k in ["ノビノビ", "シングル", "ソロ", "ツイン", "デラックス", "ＤＸ"]) and not any(k in row_text for k in ["サンライズ"]):
                for c in cells:
                    c_text = c.get_text().strip().replace("\n", "").replace(" ", "")
                    # 余分な列をスキップ
                    if any(k in c_text for k in ["選択", "列車", "発着", "時間", "月日", "おとな", "設備"]):
                        continue
                    facility_headers.append(c_text)
                break
                
        if not facility_headers:
            continue
            
        print(f"📊 解析されたヘッダー列: {facility_headers}")
        
        # 2. 列のインデックスと内部キーのマッピングを作成
        col_map = {}
        for idx, h_text in enumerate(facility_headers):
            is_smoking = "喫煙" in h_text
            facility_key = None
            
            if "ノビノビ" in h_text:
                facility_key = "ノビノビ禁煙"
            elif "シングルツイン" in h_text:
                facility_key = "シングルツイン喫煙" if is_smoking else "シングルツイン禁煙"
            elif "デラックス" in h_text or "ＤＸ" in h_text:
                facility_key = "シングルデラックス喫煙" if is_smoking else "シングルデラックス禁煙"
            elif "サンライズツイン" in h_text or "サツイン" in h_text:
                facility_key = "サンライズツイン喫煙" if is_smoking else "サンライズツイン禁煙"
            elif "ソロ" in h_text:
                facility_key = "ソロ禁煙"
            elif "シングル" in h_text:
                facility_key = "single喫煙" if is_smoking else "single禁煙"
            
            if facility_key:
                col_map[idx] = facility_key
                
        print(f"⚙️ 生成されたマッピング: {col_map}")
        
        # 3. データの読み取りとマージ
        rows = table.find_all("tr")
        for row in rows:
            tds = row.find_all(["td", "th"])
            if not tds:
                continue
            
            # 「サンライズ」が含まれるセルを特定
            train_cell_idx = -1
            train_raw_name = ""
            for idx, td in enumerate(tds):
                td_text = td.get_text().strip().replace("\n", "").replace(" ", "")
                if "サンライズ" in td_text:
                    train_cell_idx = idx
                    train_raw_name = td_text
                    break
            
            if train_cell_idx == -1:
                continue
                
            # 列車名より右側にある、空席記号のセルを抽出
            right_tds = tds[train_cell_idx + 1:]
            base_name = re.sub(r'（.+?）|\(.+?\)', '', train_raw_name).strip()
            base_name = base_name.replace("特急", "").strip()
            
            if base_name not in trains_status:
                trains_status[base_name] = {
                    "ノビノビ禁煙": "--", "ソロ禁煙": "--", "single禁煙": "--", "single喫煙": "--",
                    "シングルツイン禁煙": "--", "シングルツイン喫煙": "--",
                    "シングルデラックス禁煙": "--", "シングルデラックス喫煙": "--",
                    "サンライズツイン禁煙": "--", "サンライズツイン喫煙": "--"
                }
                
            for col_idx, facility_key in col_map.items():
                if col_idx < len(right_tds):
                    mark = parse_mark(right_tds[col_idx])
                    # すでに他のスキャンで有効なマークが入っている場合は上書きしない
                    if mark != "--":
                        trains_status[base_name][facility_key] = mark

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

                trains_status = {}

                # 💡 【ダブルスキャン：第1波】ノビノビ・シングルツイン・DX等
                print("📸 [スキャン①] 最初の画面を解析中...")
                scrape_train_status(page.content(), trains_status)

                change_buttons = page.locator("text=この列車を変更")
                if change_buttons.count() == 0:
                    print("📭 サンライズ号が見つかりません。")
                    continue

                # 「この列車を変更」をクリック
                change_buttons.first.click()
                
                # 💡 【ダブルスキャン：第2波】ソロ・シングル・サンライズツイン等
                has_after_button = False
                try:
                    page.wait_for_selector("text=後の列車", timeout=5000)
                    print("👉 '後の列車' ボタンを発見。画面2へ進みます...")
                    page.click("text=後の列車")
                    has_after_button = True
                except Exception as e:
                    print("ℹ️ '後の列車' ボタンはありません。最初の画面のみでチェックを続行します。")

                if has_after_button:
                    try:
                        # 確実にページが切り替わるのを3秒だけ待機
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(3000)
                        print("📸 [スキャン②] 2番目の画面を解析中...")
                        scrape_train_status(page.content(), trains_status)
                    except Exception as e:
                        print("⚠️ 2番目の設備画面のロードに失敗しました。")

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
