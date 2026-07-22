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

def parse_mark_str(html_str):
    if not html_str: return "--"
    if "○" in html_str or "内車" in html_str or "空席あり" in html_str or "vacant" in html_str: return "○"
    elif "△" in html_str or "残りわずか" in html_str or "almost" in html_str: return "△"
    elif "◇" in html_str or "事前申込" in html_str or "undefined" in html_str: return "◇"
    elif "×" in html_str or "残席なし" in html_str or "unavailable" in html_str: return "×"
    return "--"

def is_data_acquired(status: SunRiseStatus, target_keys: list):
    current_data = status.to_dict()
    return any(current_data[k] != "--" for k in target_keys)

def scrape_page1_table(soup, status: SunRiseStatus):
    print("    📊 [解析] 1ページ目の設備テーブル解析中...")
    tables = soup.find_all("table")
    for table in tables:
        headers = []
        for tr in table.find_all("tr"):
            th_tds = tr.find_all(["th", "td"])
            texts = [x.get_text(strip=True) for x in th_tds]
            
            # 動画00:16の通り、列のヘッダー（普通・A寝台・B寝台）を記憶する
            if any("普通" in t or "寝台" in t for t in texts):
                headers = texts
                continue
            
            # ヘッダーが取得済みで、記号（○△×）が含まれる行ならデータをマッピング
            if headers and any(parse_mark_str(str(x)) != "--" for x in th_tds):
                for i, cell in enumerate(th_tds):
                    if i < len(headers):
                        h_text = headers[i]
                        mark = parse_mark_str(str(cell))
                        if mark == "--": continue
                        
                        is_smoking = "喫煙" in h_text
                        if "普通" in h_text or "ノビノビ" in h_text:
                            status.nobinobi = mark
                        elif "B寝台" in h_text or "シングルツイン" in h_text:
                            if is_smoking: status.single_twin_kitsuyen = mark
                            else: status.single_twin_kinyen = mark
                        elif "A寝台" in h_text or "デラックス" in h_text:
                            if is_smoking: status.single_dx_kitsuyen = mark
                            else: status.single_dx_kinyen = mark
    return True

def scrape_page2_list(soup, status: SunRiseStatus):
    print("    📊 [解析] 2ページ目の個室テーブル解析中...")
    tables = soup.find_all("table")
    for table in tables:
        headers = []
        for tr in table.find_all("tr"):
            th_tds = tr.find_all(["th", "td"])
            texts = [x.get_text(strip=True) for x in th_tds]
            
            # 動画00:23の通り、列のヘッダー（禁煙・喫煙）を記憶する
            if any("禁煙" in t or "喫煙" in t for t in texts):
                headers = texts
                continue
            
            row_text = tr.get_text()
            if "（ソロ）" in row_text or "（シングル）" in row_text or "（サツイン）" in row_text:
                category = ""
                if "（ソロ）" in row_text: category = "ソロ"
                elif "（シングル）" in row_text: category = "シングル"
                elif "（サツイン）" in row_text or "サンライズツイン" in row_text: category = "サンライズツイン"
                
                for i, cell in enumerate(th_tds):
                    mark = parse_mark_str(str(cell))
                    if mark == "--": continue
                    
                    is_smoking = False
                    if headers and i < len(headers):
                        if "喫煙" in headers[i]: is_smoking = True
                    else:
                        # ヘッダーが見つからなかった場合のフォールバック (0:列車名, 1:禁煙, 2:喫煙)
                        if i == 2: is_smoking = True
                        
                    if category == "ソロ":
                        status.solo = mark
                    elif category == "シングル":
                        if is_smoking: status.single_kitsuyen = mark
                        else: status.single_kinyen = mark
                    elif category == "サンライズツイン":
                        if is_smoking: status.sunrise_twin_kitsuyen = mark
                        else: status.sunrise_twin_kinyen = mark
    return True

def save_debug_files(page, attempt_num, prefix=""):
    try:
        page.screenshot(path=f"debug_{prefix}attempt_{attempt_num}.png", full_page=True)
        with open(f"debug_{prefix}attempt_{attempt_num}.html", "w", encoding="utf-8") as f:
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

    print(f"🎯 デスクトップ特化・超堅牢Wスキャン開始: {config['year']}年{config['month']}月{config['day']}日 | {config['dep']} ➡️ {config['arr']}")

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
                    sleep_time = backoff_base[attempt] + random.uniform(0.5, 2.0)
                    print(f"⏳ サーバー混雑中... {sleep_time:.2f}秒後に再突撃します...")
                    time.sleep(sleep_time)

                print(f"💻 【PC標準窓】アタック {current_attempt_num} / {max_attempts} 回目...")
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
                )
                page = context.new_page()
                
                try:
                    page.goto("https://e5489.jr-odekake.net/e5489/cspc/CBTopMenuPC", timeout=15000)
                    page.goto(direct_url, referer=referer_url, timeout=15000)
                    
                    try:
                        page.locator("img").first.wait_for(timeout=10000, state="visible")
                    except:
                        pass
                    
                    html_p1 = page.content()
                    title_p1 = page.title()
                    url_p1 = page.url
                    
                    if is_e5489_error(title_p1, url_p1, html_p1):
                        print(f"    ⚠️ [混雑・エラー検知] 1ページ目で弾かれました。")
                        save_debug_files(page, current_attempt_num, "err_p1_")
                        continue

                    print("    📸 [STATE: PAGE1_SCAN] 1ページ目の解析開始...")
                    soup_p1 = BeautifulSoup(html_p1, "html.parser")
                    scrape_page1_table(soup_p1, status_obj)

                    p1_keys = ["ノビノビ禁煙", "シングルツイン禁煙", "シングルツイン喫煙", "シングルデラックス禁煙", "シングルデラックス喫煙"]
                    if not is_data_acquired(status_obj, p1_keys):
                        raise Exception("1ページ目の座席データ(○△×)取得に失敗しました。")

                    # 💡 動画00:16 「この列車を変更」をクリック
                    change_btn = page.locator("a:has-text('この列車を変更')").first
                    if change_btn.is_visible():
                        print("    👉 「この列車を変更」をクリックします...")
                        change_btn.click(timeout=5000)
                        
                        inner_success = False
                        # 💡 動画00:19〜00:22 の「エラー ➡️ 戻る ➡️ 変更 ➡️ 後の列車」神業ループ
                        for inner_attempt in range(5):
                            later_btn = page.locator("text=後の列車").first
                            later_btn.wait_for(state="visible", timeout=5000)
                            print(f"    👉 ポップアップ内の「後の列車」をクリックします（内部試行 {inner_attempt+1}/5）...")
                            later_btn.click(timeout=5000)
                            
                            page.wait_for_load_state("networkidle", timeout=10000)
                            time.sleep(1)
                            
                            html_p2 = page.content()
                            title_p2 = page.title()
                            
                            # 混雑エラー(20100801)が出たら「前のページに戻る」を押して即復活
                            if is_e5489_error(title_p2, page.url, html_p2):
                                print("    ⚠️ 2ページ目遷移で混雑エラー発生！動画の通り『戻る』を押してリトライします。")
                                back_btn = page.locator("a:has-text('前のページに戻る')").first
                                if back_btn.is_visible():
                                    back_btn.click(timeout=5000)
                                    page.wait_for_load_state("networkidle", timeout=10000)
                                    time.sleep(1)
                                    # 1ページ目に戻ったので再度ポップアップを開く
                                    change_btn.click(timeout=5000)
                                    continue
                                else:
                                    break
                            else:
                                inner_success = True
                                break
                                
                        if inner_success:
                            # 💡 遷移後、「（シングル）」の文字が画面に出現するのを絶対的証拠として待機
                            try:
                                print("    ⏳ 個室一覧の展開（シングルの文字出現）を待機中...")
                                page.locator("text=（シングル）").first.wait_for(timeout=10000, state="visible")
                            except:
                                print("    ⚠️ 展開待ちタイムアウト。そのまま解析へ進みます。")
                            
                            print("    📸 [STATE: PAGE2_SCAN] 2ページ目（個室一覧）の解析開始...")
                            soup_p2 = BeautifulSoup(page.content(), "html.parser")
                            scrape_page2_list(soup_p2, status_obj)
                            
                            p2_keys = ["ソロ禁煙", "シングル禁煙", "シングル喫煙", "サンライズツイン禁煙", "サンライズツイン喫煙"]
                            if not is_data_acquired(status_obj, p2_keys):
                                raise Exception("2ページ目に遷移しましたが、ソロやシングルのデータが1件も取得できませんでした。")

                            print("    🎉 [STATE: SUCCESS] 全設備の完全踏破とデータ取得に成功！")
                            save_debug_files(page, current_attempt_num, "success_")
                            full_scan_success = True
                            break
                    else:
                        raise Exception("「この列車を変更」リンクが画面上に見つかりませんでした。")
                            
                except Exception as attempt_err:
                    print("==================================================")
                    print(f"❌ アタック {current_attempt_num} 回目でエラー/検証失敗が発生しました。")
                    print(f"内容: {attempt_err}")
                    print("==================================================")
                    save_debug_files(page, current_attempt_num, "fail_")
                finally:
                    context.close()

            if not full_scan_success:
                print("\n❌ 規定回数内で全設備の正確なデータ取得に到達できませんでした。")
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
