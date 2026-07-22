import os
import sys
import csv
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
            requests.post(url, headers=headers, json=payload, timeout=10)

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
        error_keywords = ["20100801", "99990110", "00604087", "処理中にエラーが発生しました", "混雑中ですが", "大変混み合っております"]
        return any(k in html_content for k in error_keywords)
    except:
        return True

def parse_mark_from_td(td_element):
    """ご提供いただいたHTML構造に基づき、imgのalt属性から正確に記号を判定する"""
    if not td_element: return "--"
    html_str = str(td_element)
    if "空席あり" in html_str: return "○"
    if "残りわずか" in html_str: return "△"
    if "事前申込" in html_str: return "◇"
    if "残席なし" in html_str: return "×"
    return "--"

def is_data_acquired(status: SunRiseStatus, target_keys: list):
    current_data = status.to_dict()
    return any(current_data[k] != "--" for k in target_keys)

def parse_table_data(soup, status: SunRiseStatus):
    """💡 提供された生HTML構造を完全に反映したテーブル解析エンジン"""
    tables = soup.find_all("table", class_="train-info-table")
    for table in tables:
        for tr in table.find_all("tr"):
            train_td = tr.find("td", class_="train-info-table__col-train")
            if not train_td: continue
            
            row_text = train_td.get_text(strip=True)
            tds = tr.find_all("td")
            
            # 構造: [0]発着, [1]列車名, [2]ノビノビ, [3]B寝台禁煙, [4]B寝台喫煙, [5]A寝台禁煙, [6]A寝台喫煙
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

def save_debug_files(page, attempt_num, state=""):
    try:
        base_path = f"debug_attempt_{attempt_num}_{state}"
        page.screenshot(path=f"{base_path}.png", full_page=True)
        with open(f"{base_path}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except:
        pass

def main():
    if not is_within_active_hours():
        print("💤 現在は稼働時間外のため即時終了します。")
        return

    config = get_target_config()
    
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    target_dt = datetime(int(config["year"]), int(config["month"]), int(config["day"]), 23, 59, 59, tzinfo=jst)
    if target_dt < now_jst:
        print(f"⚠️ 【自動停止】指定された乗車日は過去の日付です。")
        sys.exit(0)

    print(f"🎯 最終完全版(V9) Wスキャン開始: {config['year']}年{config['month']}月{config['day']}日 | {config['dep']} ➡️ {config['arr']}")

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

    max_attempts = 30
    full_scan_success = False
    status_obj = SunRiseStatus()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        try:
            for attempt in range(max_attempts):
                if not is_within_active_hours():
                    return
                current_attempt_num = attempt + 1
                if attempt > 0:
                    time.sleep(random.uniform(0.5, 1.5))

                print(f"💻 【PC標準窓】超速アタック {current_attempt_num} / {max_attempts} 回目...")
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
                )
                page = context.new_page()
                
                try:
                    page.goto("https://e5489.jr-odekake.net/e5489/cspc/CBTopMenuPC", timeout=15000)
                    page.goto(direct_url, referer=referer_url, timeout=15000)
                    
                    try:
                        page.locator("table.train-info-table").first.wait_for(timeout=10000, state="visible")
                    except:
                        pass
                    
                    html_p1 = page.content()
                    if is_e5489_error(page.title(), page.url, html_p1):
                        print(f"    ⚠️ [混雑・エラー検知] 1ページ目で弾かれました。")
                        save_debug_files(page, current_attempt_num, "err_p1")
                        continue

                    print("    📸 [STATE: PAGE1_SCAN] 1ページ目のデータを一瞬で回収...")
                    parse_table_data(BeautifulSoup(html_p1, "html.parser"), status_obj)

                    # 💡 「この列車を変更」ボタンを確実なセレクターでクリック
                    change_btn = page.locator("a.popup-link:has-text('この列車を変更')").first
                    try:
                        change_btn.wait_for(state="visible", timeout=10000)
                        print("    👉 『この列車を変更』をクリックします...")
                        change_btn.click(timeout=5000)
                    except:
                        raise Exception("「この列車を変更」ボタンが見つかりませんでした。")
                        
                    inner_success = False
                    # 💡 動画の通り、オレンジ色の「後の列車」ボタンを最大100回連打して混雑を突破
                    for inner_attempt in range(100):
                        try:
                            later_btn = page.locator("text=後の列車").first
                            later_btn.wait_for(state="visible", timeout=5000)
                            print(f"    👉 ポップアップ内の『後の列車』をクリック（内部連打 {inner_attempt+1}/100）...")
                            later_btn.click(timeout=5000)
                            
                            page.wait_for_load_state("networkidle", timeout=10000)
                            time.sleep(1)
                            
                            html_p2 = page.content()
                            
                            if is_e5489_error(page.title(), page.url, html_p2):
                                print("    ⚠️ 混雑エラー発生！動画の通り即座に『前のページに戻る』を押してリトライします。")
                                back_btn = page.locator("a:has-text('前のページに戻る')").first
                                if back_btn.is_visible():
                                    back_btn.click(timeout=5000)
                                    page.wait_for_load_state("networkidle", timeout=10000)
                                    change_btn.wait_for(state="visible", timeout=10000)
                                    change_btn.click(timeout=5000)
                                    continue
                                else:
                                    break
                            else:
                                inner_success = True
                                break
                        except Exception as e:
                            print(f"    ⚠️ ポップアップ操作でタイムアウト: {e}")
                            break
                            
                    if inner_success:
                        print("    📸 [STATE: PAGE2_SCAN] 2ページ目（個室一覧）の解析開始...")
                        parse_table_data(BeautifulSoup(page.content(), "html.parser"), status_obj)
                        
                        p2_keys = ["ソロ禁煙", "シングル禁煙", "シングル喫煙", "サンライズツイン禁煙", "サンライズツイン喫煙"]
                        if not is_data_acquired(status_obj, p2_keys):
                            raise Exception("ソロやシングルのデータが1件も取得できませんでした。")

                        print("    🎉 [STATE: SUCCESS] 全設備の完全踏破とデータ取得に成功！")
                        save_debug_files(page, current_attempt_num, "success")
                        full_scan_success = True
                        break
                        
                except Exception as attempt_err:
                    print("==================================================")
                    print(f"❌ アタック {current_attempt_num} 回目でエラー/検証失敗が発生しました。")
                    print(f"内容: {attempt_err}")
                    print("==================================================")
                    save_debug_files(page, current_attempt_num, "fail")
                finally:
                    context.close()

            if not full_scan_success:
                print("\n❌ 規定回数内で全設備の正確なデータ取得に到達できませんでした。")
                sys.exit(1)

            any_vacant = False
            status_text = ""
            for room_name, mark in status_obj.to_dict().items():
                alert = " 🎉空席!!" if mark in ["○", "△", "◇"] else ""
                if alert: any_vacant = True
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
                print(f"🎉 厳密な検証を通過し空席を検知！LINEへ通知します。")
                send_line(msg)
                return 

            print("\n📭 Wスキャンに完全成功！現時点ではすべて本当に「満席」でした。")

        except Exception as e:
            print(f"❌ 予期せぬクリティカルエラー発生: {e}")
            traceback.print_exc()
            sys.exit(1)
        finally:
            browser.close()

if __name__ == "__main__":
    main()
