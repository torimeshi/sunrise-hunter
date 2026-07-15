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

def scrape_train_status(page_content, trains_status):
    """🛡️ とりめしさん提供のHTML構造に完全特化した無敵のスキャンエンジン"""
    soup = BeautifulSoup(page_content, "html.parser")
    
    # 💡 パターン1：キャンペーン用カードリスト構造（スマホ・レスポンシブ画面）
    lists = soup.find_all("ul", class_="changing-train-list")
    if lists:
        print("📱 [解析] サンライズ専用カードリスト構造を検出しました。スキャンを開始します。")
        for u_list in lists:
            items = u_list.find_all("li", recursive=False)
            for item in items:
                header_train = item.find(class_="train-info-heading__train")
                if not header_train:
                    continue
                header_text = header_train.get_text().strip().replace(" ", "").replace("\n", "")
                
                # 列車名と個室カテゴリ（ソロ・シングル等）を動的に分離
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
                
                # 各設備ボックスをループ
                boxes = item.find_all(class_="changing-train-box")
                for box in boxes:
                    box_text = box.get_text().strip().replace(" ", "").replace("\n", "")
                    is_smoking = "喫煙" in box_text
                    
                    # 記号の判定（画像alt属性およびファイル名から二重判定）
                    mark = "--"
                    status_div = box.find(class_="changing-train-box__status")
                    if status_div:
                        img = status_div.find("img")
                        if img:
                            alt_text = img.get("alt", "")
                            src_text = img.get("src", "")
                            if "空席あり" in alt_text or "○" in alt_text or "vacant" in src_text:
                                mark = "○"
                            elif "残りわずか" in alt_text or "△" in alt_text or "almost" in src_text:
                                mark = "△"
                            elif "残席なし" in alt_text or "×" in alt_text or "unavailable" in src_text:
                                mark = "×"
                            elif "◇" in alt_text or "事前申込" in alt_text or "undefined" in src_text:
                                mark = "◇"
                    
                    # 万が一画像が読めなくても、disabledクラスがあれば満席判定にする
                    if mark == "--" and "disabled" in "".join(box.get("class", [])):
                        mark = "×"
                        
                    # 内部キーへのマッピング
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
                        trains_status[base_name][facility_key] = mark
        return

    # 💡 パターン2：クラシックテーブル構造（念のためのPCデスクトップ用フォールバック）
    tables = soup.find_all("table")
    if tables:
        print("💻 [解析] デスクトップ用テーブル構造を検出しました。")
        for table in tables:
            if "サンライズ" not in table.get_text():
                continue
            facility_headers = []
            for tr in table.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                row_text = "".join([c.get_text() for c in cells])
                if any(k in row_text for k in ["ノビノビ", "シングル", "ソロ", "ツイン", "デラックス", "ＤＸ"]) and not any(k in row_text for k in ["サンライズ"]):
                    for c in cells:
                        c_text = c.get_text().strip().replace("\n", "").replace(" ", "")
                        if any(k in c_text for k in ["選択", "列車", "発着", "時間", "月日", "おとな", "設備"]):
                            continue
                        facility_headers.append(c_text)
                    break
            if not facility_headers:
                continue
            
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
                    
            rows = table.find_all("tr")
            for row in rows:
                tds = row.find_all(["td", "th"])
                if not tds:
                    continue
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
                right_tds = tds[train_cell_idx + 1:]
                base_name = re.sub(r'（.+?）|\(.+?\)', '', train_raw_name).strip().replace("特急", "")
                
                if base_name not in trains_status:
                    trains_status[base_name] = {
                        "ノビノビ禁煙": "--", "ソロ禁煙": "--", "single禁煙": "--", "single喫煙": "--",
                        "シングルツイン禁煙": "--", "シングルツイン喫煙": "--",
                        "シングルデラックス禁煙": "--", "シングルデラックス喫煙": "--",
                        "サンライズツイン禁煙": "--", "サンライズツイン喫煙": "--"
                    }
                for col_idx, facility_key in col_map.items():
                    if col_idx < len(right_tds):
                        text = right_tds[col_idx].get_text().strip()
                        mark = "--"
                        if "○" in text or "内車" in text: mark = "○"
                        elif "△" in text: mark = "△"
                        elif "◇" in text: mark = "◇"
                        elif "×" in text: mark = "×"
                        if mark != "--":
                            trains_status[base_name][facility_key] = mark

def main():
    if not is_within_active_hours():
        print("💤 現在は稼働時間外（5:29〜23:51）のため、何もせずに即時終了します。")
        return

    config = get_target_config()
    print(f"🎯 ステルス直行巡回開始: {config['year']}年{config['month']}月{config['day']}日 | {config['dep']} ➡️ {config['arr']}")

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
                
                # 1️⃣ セッションを確立
                page.goto("https://e5489.jr-odekake.net/e5489/cspc/CBTopMenuPC")
                page.wait_for_load_state("networkidle")

                # 2️⃣ 直接結果画面へワープ！
                page.goto(direct_url)
                page.wait_for_load_state("networkidle")

                if is_e5489_error(page.content()):
                    print("⚠️ エラーまたは混雑を検知。次の30秒後チェックに期待します。")
                    continue

                try:
                    # 🎯 列車の一覧（カードリストまたはテーブル）が出現するまで最大20秒待機
                    page.wait_for_selector(".changing-train-list, table, text=特急サンライズ", timeout=20000)
                except Exception as e:
                    print("⚠️ 列車一覧画面のロードに失敗したか、タイムアウトしました。")
                    print(f"   現在の表示URL: {page.url}")
                    print(f"   現在のページタイトル: {page.title()}")
                    continue

                trains_status = {}

                # 📸 直接画面を解析！
                print("📸 画面をスキャンして空席データを解析中...")
                scrape_train_status(page.content(), trains_status)

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
