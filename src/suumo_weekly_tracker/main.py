import os
import re
import time
import unicodedata
from datetime import datetime
from typing import Any, Dict, List

import gspread
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

SUUMO_SEARCH_URL = os.getenv("SUUMO_SEARCH_URL")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
}


# ==========================================
# 1. クレンジング・正規化ユーティリティ
# ==========================================
def normalize_text(text: str) -> str:
    """NFKC正規化と余分な空白除去"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_for_key(name: str) -> str:
    """名寄せキー用の物件名正規化（スペースや中黒・ハイフンをすべて除去）"""
    name = normalize_text(name)
    return re.sub(r"[\s・･\-‐ー]", "", name)


def parse_price_man(price_str: str) -> float:
    """'1億2000万円' や '4980万円' を万円単位のfloatに変換（未定などは0）"""
    s = normalize_text(price_str).replace(",", "")
    total = 0.0
    oku_match = re.search(r"(\d+)億", s)
    man_match = re.search(r"(\d+(?:\.\d+)?)万", s)
    if oku_match:
        total += float(oku_match.group(1)) * 10000
    if man_match:
        total += float(man_match.group(1))
    return total


def parse_area(area_str: str) -> float:
    """'82.45m2（壁芯）' などから面積(float)を抽出"""
    s = normalize_text(area_str)
    m = re.search(r"(\d+(?:\.\d+)?)\s*m", s)
    return float(m.group(1)) if m else 0.0


def parse_walk_minutes(station_str: str) -> int:
    """'ＪＲ東北本線「仙台」徒歩7分' から徒歩分数(int)を抽出"""
    s = normalize_text(station_str)
    m = re.search(r"歩\s*(\d+)\s*分", s)
    return int(m.group(1)) if m else 999


# ==========================================
# 2. SUUMOスクレイピング処理
# ==========================================
def fetch_all_properties(base_url: str) -> List[Dict[str, Any]]:
    """検索結果ページを巡回し、全物件の生データを抽出する"""
    if not base_url:
        raise ValueError("環境変数 SUUMO_SEARCH_URL が設定されていません。")

    raw_items: List[Dict[str, Any]] = []
    page = 1

    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        while True:
            url = f"{base_url}&page={page}" if "?" in base_url else f"{base_url}?page={page}"
            resp = client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            units = soup.select("div.property_unit")
            if not units:
                break

            for unit in units:
                title_el = unit.select_one("h2.property_unit-title a")
                if not title_el:
                    continue
                name = normalize_text(title_el.text)
                href = title_el.get("href", "")
                detail_url = f"https://suumo.jp{href}" if href.startswith("/") else href

                info_map: Dict[str, str] = {}
                for dl in unit.select("div.dottable-line dl"):
                    dt = dl.select_one("dt")
                    dd = dl.select_one("dd")
                    if dt and dd:
                        info_map[normalize_text(dt.text)] = normalize_text(dd.text)

                price_man = parse_price_man(info_map.get("販売価格", ""))
                area_m2 = parse_area(info_map.get("専有面積", ""))
                if price_man <= 0 or area_m2 <= 0:
                    continue

                station_raw = info_map.get("沿線・駅", "")
                walk_min = parse_walk_minutes(station_raw)
                layout = info_map.get("間取り", "")
                built_date = info_map.get("築年月", "")
                address = info_map.get("所在地", "")

                # 単価計算
                m2_price = round(price_man / area_m2, 1)
                tsubo_price = round(price_man / (area_m2 * 0.3025), 1)

                # 名寄せ用ユニークID
                unique_id = f"{normalize_for_key(name)}_{area_m2:.2f}_{layout}_{built_date}"

                raw_items.append(
                    {
                        "unique_id": unique_id,
                        "name": name,
                        "price_man": price_man,
                        "tsubo_price": tsubo_price,
                        "m2_price": m2_price,
                        "area_m2": area_m2,
                        "layout": layout,
                        "station": station_raw,
                        "walk_min": walk_min,
                        "built_date": built_date,
                        "address": address,
                        "url": detail_url,
                    }
                )

            next_link = soup.select_one("div.pagination p.pagination-parts a:-soup-contains('次へ')")
            if not next_link:
                break

            page += 1
            time.sleep(2.5)

    return raw_items


# ==========================================
# 3. 名寄せ・前回差分検知・ランキング算出
# ==========================================
def deduplicate_and_rank(
    raw_items: List[Dict[str, Any]], prev_prices: Dict[str, float]
) -> List[Dict[str, Any]]:
    """同一住戸を最安値で名寄せし、前回価格との比較と坪単価ランキングを行う"""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in raw_items:
        grouped.setdefault(item["unique_id"], []).append(item)

    deduped: List[Dict[str, Any]] = []
    for uid, group in grouped.items():
        group.sort(key=lambda x: x["price_man"])
        rep = group[0].copy()
        rep["listing_count"] = len(group)

        if uid not in prev_prices:
            rep["status"] = "🆕 新着"
            rep["price_diff"] = 0.0
        else:
            diff = round(rep["price_man"] - prev_prices[uid], 1)
            rep["price_diff"] = diff
            if diff < 0:
                rep["status"] = "📉 値下げ"
            elif diff > 0:
                rep["status"] = "📈 値上げ"
            else:
                rep["status"] = "➖ 据置"

        deduped.append(rep)

    # 第1キー: 坪単価(昇順), 第2キー: 徒歩分数(昇順)
    deduped.sort(key=lambda x: (x["tsubo_price"], x["walk_min"]))

    for idx, item in enumerate(deduped, start=1):
        item["rank"] = idx

    return deduped


# ==========================================
# 4. Googleスプレッドシート連携
# ==========================================
def sync_with_spreadsheet(today_str: str, raw_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Raw履歴から前回データを取得し、名寄せ後にSummaryとRawタブを更新する"""
    if not SPREADSHEET_ID:
        raise ValueError("環境変数 SPREADSHEET_ID が設定されていません。")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SPREADSHEET_ID)

    try:
        ws_raw = sh.worksheet("Raw_History")
    except gspread.WorksheetNotFound:
        ws_raw = sh.add_worksheet(title="Raw_History", rows=2000, cols=15)

    try:
        ws_summary = sh.worksheet("Summary_Ranking")
    except gspread.WorksheetNotFound:
        ws_summary = sh.add_worksheet(title="Summary_Ranking", rows=200, cols=15)

    all_raw_rows = ws_raw.get_all_values()
    raw_header = [
        "取得日",
        "ユニークID",
        "物件名",
        "販売価格(万円)",
        "坪単価(万円)",
        "平米単価(万円)",
        "専有面積(㎡)",
        "間取り",
        "所在地",
        "沿線・駅",
        "徒歩(分)",
        "築年月",
        "掲載枠数",
        "SUUMO URL",
    ]

    prev_prices: Dict[str, float] = {}
    kept_history_rows = [raw_header]

    if len(all_raw_rows) > 1:
        past_dates = sorted({r[0] for r in all_raw_rows[1:] if r[0] and r[0] < today_str})
        latest_past_date = past_dates[-1] if past_dates else None

        for r in all_raw_rows[1:]:
            if not r or r[0] == today_str:
                continue
            kept_history_rows.append(r)
            if latest_past_date and r[0] == latest_past_date:
                uid = r[1]
                try:
                    price = float(r[3])
                    if uid not in prev_prices or price < prev_prices[uid]:
                        prev_prices[uid] = price
                except ValueError:
                    pass

    ranked_items = deduplicate_and_rank(raw_items, prev_prices)

    for item in ranked_items:
        kept_history_rows.append(
            [
                today_str,
                item["unique_id"],
                item["name"],
                item["price_man"],
                item["tsubo_price"],
                item["m2_price"],
                item["area_m2"],
                item["layout"],
                item["address"],
                item["station"],
                item["walk_min"],
                item["built_date"],
                item["listing_count"],
                item["url"],
            ]
        )
    ws_raw.clear()
    ws_raw.update(values=kept_history_rows, range_name="A1")

    summary_header = [
        "順位",
        "ステータス",
        "物件名",
        "販売価格(万円)",
        "前回比(万円)",
        "坪単価(万円)",
        "平米単価(万円)",
        "専有面積(㎡)",
        "間取り",
        "最寄駅・徒歩",
        "築年月",
        "掲載枠数",
        "SUUMO URL",
        "更新日",
    ]
    summary_rows = [summary_header]
    for item in ranked_items:
        diff_str = f"{item['price_diff']:+.1f}" if item["price_diff"] != 0 else "0"
        summary_rows.append(
            [
                item["rank"],
                item["status"],
                item["name"],
                item["price_man"],
                diff_str,
                item["tsubo_price"],
                item["m2_price"],
                item["area_m2"],
                item["layout"],
                item["station"],
                item["built_date"],
                item["listing_count"],
                item["url"],
                today_str,
            ]
        )
    ws_summary.clear()
    ws_summary.update(values=summary_rows, range_name="A1")

    return {
        "ranked_items": ranked_items,
        "summary_url": f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={ws_summary.id}",
        "raw_url": f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={ws_raw.id}",
    }


# ==========================================
# 5. Slack通知処理
# ==========================================
def send_slack_notification(today_str: str, total_raw: int, result: Dict[str, Any]) -> None:
    """Slack Incoming Webhookで日次サマリーを送信"""
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL が未設定のためSlack通知をスキップします。")
        return

    items = result["ranked_items"]
    new_items = [x for x in items if x["status"] == "🆕 新着"]
    down_items = [x for x in items if x["status"] == "📉 値下げ"]
    top5_items = items[:5]

    lines = [
        f"*🏠 仙台市 中古マンション日次レポート ({today_str})*",
        f"該当：{len(items)}棟/室（延べ掲載 {total_raw}枠 / 80㎡以上・徒歩10分以内・築15年以内）\n",
    ]

    lines.append(f"*🆕 前回更新からの新着物件 ({len(new_items)}件)*")
    if new_items:
        for x in new_items[:5]:
            lines.append(
                f"• <{x['url']}|{x['name']}>：*{x['price_man']:,.0f}万円* "
                f"（坪 {x['tsubo_price']}万 / {x['area_m2']}㎡・{x['layout']} / 歩{x['walk_min']}分）"
            )
        if len(new_items) > 5:
            lines.append(f"  _他 {len(new_items) - 5}件はシート参照_")
    else:
        lines.append("• なし")

    lines.append(f"\n*📉 前回更新からの値下げ物件 ({len(down_items)}件)*")
    if down_items:
        for x in down_items[:5]:
            old_p = x["price_man"] - x["price_diff"]
            lines.append(
                f"• <{x['url']}|{x['name']}>：{old_p:,.0f}万円 → *{x['price_man']:,.0f}万円* "
                f"(*{x['price_diff']:+.0f}万* / 坪 {x['tsubo_price']}万)"
            )
    else:
        lines.append("• なし")

    lines.append("\n*🏆 坪単価ランキング TOP5*")
    for x in top5_items:
        lines.append(
            f"{x['rank']}位 <{x['url']}|{x['name']}>：*坪 {x['tsubo_price']}万円* "
            f"（{x['price_man']:,.0f}万円 / {x['area_m2']}㎡ / 歩{x['walk_min']}分 / {x['built_date']}）"
        )

    lines.append(
        f"\n📊 <{result['summary_url']}|*最新ランキングシートを開く*> ｜ "
        f"🗂️ <{result['raw_url']}|*Raw履歴シートを開く*>"
    )

    payload = {"text": "\n".join(lines)}
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    with httpx.Client(transport=transport, timeout=30.0) as client:
        client.post(SLACK_WEBHOOK_URL, json=payload).raise_for_status()


def main() -> None:
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        raw_items = fetch_all_properties(SUUMO_SEARCH_URL)
        if not raw_items:
            raise RuntimeError(
                "SUUMOから物件が1件も取得できませんでした（URLまたはHTML構造を確認してください）。"
            )

        result = sync_with_spreadsheet(today_str, raw_items)
        send_slack_notification(today_str, len(raw_items), result)
        print(
            f"[{today_str}] 完了: 名寄せ前 {len(raw_items)}件 -> "
            f"名寄せ後 {len(result['ranked_items'])}件"
        )

    except Exception as e:
        if SLACK_WEBHOOK_URL:
            try:
                transport = httpx.HTTPTransport(local_address="0.0.0.0")
                with httpx.Client(transport=transport, timeout=30.0) as client:
                    client.post(
                        SLACK_WEBHOOK_URL,
                        json={
                            "text": f"⚠️ *SUUMO日次クローラー エラー発生 ({today_str})*\n```{e}```"
                        },
                    )
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
