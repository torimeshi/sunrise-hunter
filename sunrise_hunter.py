import os
import sys
import csv
import time
import random
import requests
import urllib.parse
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ⚙️ 環境変数
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
    targets = [t.strip() for t in [LINE_USER, LINE_GROUP] if t and t.strip()]
    
    for to_id in targets:
        payload = {"to": to_id, "messages": [{"type": "text", "text": message}]}
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            print(f"📢 LINE送信 (宛先: {to_id}): ステータス {res.status_code}")
        except Exception as e:
            print(f"❌ LINE送信エラー: {e}")

def get_target_config():
    try:
        res = requests.get(CSV_URL, timeout=10)
        res.encoding = 'utf-8'
        lines = res.text.splitlines()
        reader = list(csv.reader(lines))
        if len(reader) < 2:
            print("⚠️ スプレッドシートにデータがありません。")
            sys.exit(0)
        latest = reader[-1]
        raw_date = latest[1].replace("/", "-")
        dt = datetime.strptime(raw_date, "%Y-%m-%d")
        
        target_facility = latest[4].strip() if len(latest) > 4 and latest[4].strip() else "全設備"
        last_status_str = latest[5].strip() if len(latest) > 5 else "RESET"

        return {
            "year": str(dt.year), "month": str(dt.month), "day": str(dt.day),
            "dep": latest[2].strip(), "arr": latest[3].strip(),
            "target_facility": target_facility,
            "last_status_str": last_status_str
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
        error_keywords = ["20100801", "99990110", "00604087", "処理中にエラーが発生しました", "混雑中ですが", "大変混み合っております"]
        return any(k in html_content for k in error_keywords)
    except:
        return True

def parse_mark_from_td(td_element):
    if not td_element: return "--"
    html_str = str(td_element)
    if "空席あり" in html_str: return "○"
    if "残りわずか" in html_str: return "△"
    if "事前申込" in html_str: return "◇"
    if "残席なし" in html_str: return "×"
    return "--"

def parse_table_data(soup, status: SunRiseStatus):
    tables = soup.find_all("table", class_="train-info-table")
    for table in tables:
        for tr in table.find_all("tr"):
            train_td = tr.find("td", class_="train-info-table__col-train")
            if not train_td: continue
            
            row_text = train_td.get_text(strip=True)
            tds = tr.find_all("td")
            
            if len(tds) >= 7:
                nobi_mark = parse_mark_from_td(tds[2])
                b_kinyen = parse_mark_from_td(tds[3])
                b_kitsuyen = parse_mark_from_td(tds[4])
                a_kinyen = parse_mark_from_td(tds[5])
                a_kitsuyen = parse_mark_from_td(tds[6])
                
                if row_text == "特急サンライズ瀬戸":
                    status.nobinobi = nobi_mark
                    status.single_twin_kinyen = b_kinyen
                    status.single_twin_kitsuyen = b_kitsuyen
                    status.single_dx_kinyen = a_kinyen
                    status.single_dx_kitsuyen = a_kitsuyen
                elif "（ソロ）" in row_text:
                    status.solo = b_kinyen if b_kinyen != "--" else b_kitsuyen
                elif "（シングル）" in row_text:
                    status.single_kinyen = b_kinyen
                    status.single_kitsuyen = b_kitsuyen
                elif "（サツイン）" in row_text or "サンライズツイン" in row_text:
                    status.sunrise_twin_kinyen = b_kinyen
                    status.sunrise_twin_kitsuyen = b_kitsuyen

def filter_status_by_target(status_dict, target_facility):
    if not target_facility or target_facility.strip() in ["", "全設備", "未選択"]:
        return status_dict
    
    targets = [t.strip() for t in target_facility.replace("、", ",").split(",") if t.strip()]
    if not targets:
        return status_dict

    filtered = {}
    for key, val in status_dict.items():
        matched = False
        for t in targets:
            if ("ノビノビ" in t) and ("ノビノビ" in key): matched = True
            elif (t == "ソロ") and ("ソロ" in key): matched = True
            elif ("シングルツイン" in t) and ("シングルツイン" in key): matched = True
            elif ("シングルデラックス" in t) and ("シングルデラックス" in key): matched = True
            elif ("サンライズツイン" in t) and ("サンライズツイン" in key): matched = True
            elif (t == "シングル") and ("シングル" in key) and ("シングルツイン" not in key) and ("シングルデラックス" not in key): matched = True

        if matched:
            filtered[key] = val

    return filtered if filtered else status_dict

def scan_once(page, direct_url, referer_url, status_obj):
    try:
        page.goto("https://e5489.jr-odekake.net/e5489/cspc/CBTopMenuPC", timeout=15000)
        page.goto(direct_url, referer=referer_url, timeout=15000)
        
        try:
            page.locator("table.train-info-table").first.wait_for(timeout=10000, state="visible")
        except:
            pass
        
        html_p1 = page.content()
        if is_e5489_error(page.title(), page.url, html_p1):
            return False

        parse_table_data(BeautifulSoup(html_p1, "html.parser"), status_obj)

        change_btn = page.locator("a.popup-link:has-text('この列車を変更')").first
        if not change_btn.is_visible():
            return False
        change_btn.click(timeout=5000)

        for inner_attempt in range(15):
            try:
                later_btn = page.locator("text=後の列車").first
                later_btn.wait_for(state="visible", timeout=5000)
                later_btn.click(timeout=5000)
                page.wait_for_load_state("networkidle", timeout=10000)
                time.sleep(1)
                
                html_p2 = page.content()
                if is_e5489_error(page.title(), page.url, html_p2):
                    back_btn = page.locator("a:has-text('前のページに戻る')").first
                    if back_btn.is_visible():
                        back_btn.click(timeout=5000)
                        page.wait_for_load_state("networkidle", timeout=10000)
                        change_btn.click(timeout=5000)
                        continue
                    else:
                        return False
                else:
                    parse_table_data(BeautifulSoup(html_p2, "html.parser"), status_obj)
                    return True
            except:
                return False
    except Exception as e:
        print(f"    ⚠️ スキャン中にエラーが発生: {e}")
        return False
    return False

def main():
    if not is_within_active_hours():
        print("💤 現在は稼働時間外のため即時終了します。")
        return

    config = get_target_config()
    print(f"🎯 厳密フィルタ搭載ハンター起動: {config['month']}月{config['day']}日 | {config['dep']} ➡️ {config['arr']} | 狙い: {config['target_facility']}")

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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = context.new_page()

        for loop_cnt in range(10):
            print(f"\n🔍 [巡回 {loop_cnt+1}/10 回目] e5489へアクセス中...")
            status_obj = SunRiseStatus()
            success = scan_once(page, direct_url, referer_url, status_obj)

            if success:
                all_status = status_obj.to_dict()
                target_status = filter_status_by_target(all_status, config["target_facility"])
                
                current_status_str = "|".join([f"{k}:{v}" for k, v in target_status.items()])
                last_status_str = config["last_status_str"]

                print(f"    📊 取得結果: {target_status}")

                if last_status_str == "RESET" or current_status_str != last_status_str:
                    status_text = ""
                    any_vacant = False
                    for room_name, mark in target_status.items():
                        alert = " 🎉空席!!" if mark in ["○", "△", "◇"] else ""
                        if alert: any_vacant = True
                        status_text += f"・{room_name} ➡️ [ {mark} ]{alert}\n"

                    title = "【🚨 サンライズ空席速報！！】" if any_vacant else "【ℹ️ サンライズ空席状況案内】"
                    msg = (
                        f"{title}\n"
                        f"[乗車日] {config['month']}月{config['day']}日 | {config['dep']} ➡️ {config['arr']}\n"
                        f"[希望設備] {config['target_facility']}\n\n"
                        f"🔥 現在のステータス:\n"
                        f"===============================\n"
                        f"{status_text}"
                        f"===============================\n"
                    )
                    print(f"    📢 前回から状態が変化したため（または初回）、LINE通知を送信します！")
                    send_line(msg)
                    break
                else:
                    print("    🔕 前回通知したステータスと変化がないため、LINE通知をスキップしました。")
            else:
                print("    ⚠️ 今回の巡回ではデータの完全取得に失敗しました（リトライします）。")
            
            if loop_cnt < 9:
                print("    ⏳ 15秒待機して次の巡回へ進みます...")
                time.sleep(15)

        browser.close()

if __name__ == "__main__":
    main()
