#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fmkorea 뽀삐햄 새 글 알리미 — 클라우드(GitHub Actions)판
--------------------------------------------------------
GitHub 클라우드에서 진짜 크롬(Playwright)을 띄워 fmkorea에 접속,
뽀삐햄(뽀삐뽀삐보)이 새 글을 쓰면 텔레그램으로 알립니다.
컴퓨터를 꺼도 작동합니다.

환경변수(Secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import sys
import urllib.request
import urllib.parse

MEMBER_SRL = "7884592847"          # 뽀삐햄 회원번호

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

SEARCH_URL = (
    "https://www.fmkorea.com/search.php"
    f"?mid=stock&search_target=member_srl&search_keyword={MEMBER_SRL}"
)

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "fmkorea_state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MAX_MESSAGES_PER_RUN = 10


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_srl": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_message(p):
    cate = f"[{p['cate']}] " if p.get("cate") else ""
    date = f"\n🗓 {p['date']}" if p.get("date") else ""
    return (
        "📝 뽀삐햄 새 글!\n\n"
        f"{cate}{p['title']}"
        f"{date}\n"
        f"https://www.fmkorea.com/{p['srl']}"
    )


EXTRACT_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('a.hx').forEach(a => {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/(\d{9,})/) || href.match(/document_srl=(\d{9,})/);
    if (!m) return;
    const tr = a.closest('tr');
    const g = s => (tr && tr.querySelector(s)) ? tr.querySelector(s).textContent.trim().replace(/\s+/g,' ') : '';
    out.push({ srl: m[1], title: a.textContent.trim().replace(/\s+/g,' '),
               author: g('td.author'), date: g('td.time'), cate: g('td.cate') });
  });
  return out;
}
"""


def fetch_posts():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="ko-KR",
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"},
        )
        page = ctx.new_page()
        try:
            page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("a.hx", timeout=25000)
            except Exception:
                # Cloudflare 확인 화면일 수 있음 → 대기 후 재시도
                page.wait_for_timeout(8000)
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("a.hx", timeout=25000)
            posts = page.evaluate(EXTRACT_JS)
            # 차단 여부 판단용: 페이지 제목도 같이 기록
            title = page.title()
            print(f"[페이지 제목] {title} / 글 {len(posts)}개 발견")
        finally:
            browser.close()
        return posts


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없습니다.", file=sys.stderr)
        sys.exit(1)

    try:
        posts = fetch_posts()
    except Exception as e:
        print(f"[오류] 페이지를 읽지 못했습니다(차단 가능성): {e}", file=sys.stderr)
        sys.exit(1)

    posts = [p for p in posts if str(p.get("srl", "")).isdigit()]
    if not posts:
        print("글 목록을 찾지 못했습니다 — Cloudflare 차단 가능성이 높습니다.")
        sys.exit(1)

    posts.sort(key=lambda p: int(p["srl"]))
    newest = int(posts[-1]["srl"])

    state = load_state()
    last = int(state.get("last_srl", 0))

    if last == 0:
        save_state({"last_srl": newest})
        try:
            send_telegram(
                "✅ 뽀삐햄 글 알리미(클라우드판)가 시작됐어요.\n"
                "이제 컴퓨터를 꺼도 새 글 알림이 옵니다."
            )
        except Exception as e:
            print(f"시작 메시지 전송 실패: {e}", file=sys.stderr)
        print(f"첫 실행: 기준 글번호 {newest} 로 설정했습니다.")
        return

    new_posts = [p for p in posts if int(p["srl"]) > last]
    if not new_posts:
        print("새 글이 없습니다.")
        return

    trimmed = new_posts[-MAX_MESSAGES_PER_RUN:]
    sent = 0
    for p in trimmed:
        try:
            send_telegram(build_message(p))
            sent += 1
        except Exception as e:
            print(f"전송 실패: {e}", file=sys.stderr)
            save_state({"last_srl": last})
            sys.exit(1)

    save_state({"last_srl": newest})
    print(f"새 글 {sent}건 전송 완료. 기준 글번호 갱신: {newest}")


if __name__ == "__main__":
    main()
