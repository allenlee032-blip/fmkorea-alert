#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fmkorea 고수 새 글 알리미 — 안드로이드 패드(Termux)판
-----------------------------------------------------
등록된 유저들이 fmkorea에 새 글을 쓰면 텔레그램으로 알립니다.
브라우저 없이 가볍게 확인 (일반 IP 통과 확인됨).

실행:
  python fmkorea_pad_alert.py          한 번만 확인 (테스트)
  python fmkorea_pad_alert.py watch    10분마다 계속 감시
"""

import gzip
import io
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse

# ===== 설정 ======================================================
BOT_TOKEN = "8992521880:AAHiMAWW0grsm4I89lzteNb_AfqWYgKkMmc"
CHAT_ID = "7979521679"
CHECK_INTERVAL_SEC = 600         # watch 모드 확인 간격 (600 = 10분)

# 감시할 유저 목록: (이름, 게시판, 회원번호)
MEMBERS = [
    ("뽀삐햄",     "stock", "7884592847"),
    ("역천신공",   "stock", "5120217388"),
    ("디깅온유",   "stock", "2970302224"),
    ("노라무",     "stock", "9112231649"),
    ("겜주",       "stock", "7399777861"),
    ("젤리14",     "stock", "9715010970"),
    ("미국코인러", "coin",  "4148159043"),
]
# ================================================================

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip",
    "Referer": "https://www.fmkorea.com/",
    "Connection": "keep-alive",
}

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "fmkorea_state.json")

MAX_MESSAGES_PER_MEMBER = 10
DELAY_BETWEEN_MEMBERS_SEC = 3    # 유저 간 요청 간격 (사이트 배려)


def member_url(mid, srl):
    return ("https://www.fmkorea.com/search.php"
            f"?mid={mid}&search_target=member_srl&search_keyword={srl}")


def fetch_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw.decode("utf-8", errors="replace")


TAG_RE = re.compile(r"<[^>]+>")

def strip_tags(s):
    return re.sub(r"\s+", " ", TAG_RE.sub("", s)).strip()


def parse_posts(html):
    """<tr> 단위로 잘라 a.hx(글 링크)가 있는 행에서 글번호/제목/날짜/게시판 추출."""
    posts = []
    for chunk in re.split(r"<tr\b", html):
        am = re.search(r'<a\b[^>]*\bclass="[^"]*\bhx\b[^"]*"[^>]*>(.*?)</a>', chunk, re.S)
        if not am:
            continue
        tag = am.group(0)
        title = strip_tags(am.group(1))
        hm = re.search(r'href="([^"]+)"', tag)
        if not hm:
            continue
        sm = re.search(r'(\d{9,})', hm.group(1))
        if not sm or not title:
            continue
        cate = re.search(r'class="cate"[^>]*>(.*?)</td>', chunk, re.S)
        date = re.search(r'class="time"[^>]*>(.*?)</td>', chunk, re.S)
        posts.append({
            "srl": sm.group(1),
            "title": title,
            "cate": strip_tags(cate.group(1)) if cate else "",
            "date": strip_tags(date.group(1)) if date else "",
        })
    return posts


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


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


def build_message(name, p):
    cate = f"[{p['cate']}] " if p.get("cate") else ""
    date = f"\n🗓 {p['date']}" if p.get("date") else ""
    return (
        f"📝 {name} 새 글!\n\n"
        f"{cate}{p['title']}"
        f"{date}\n"
        f"https://www.fmkorea.com/{p['srl']}"
    )


def check_once():
    state = load_state()
    newly_watching = []

    for i, (name, mid, srl) in enumerate(MEMBERS):
        if i > 0:
            time.sleep(DELAY_BETWEEN_MEMBERS_SEC)
        try:
            html = fetch_html(member_url(mid, srl))
            posts = parse_posts(html)
        except Exception as e:
            print(f"[{name}] 오류: {e} — 다음 확인 때 재시도")
            continue

        if not posts:
            print(f"[{name}] 글 목록을 찾지 못함 — 다음 확인 때 재시도")
            continue

        posts.sort(key=lambda p: int(p["srl"]))
        newest = int(posts[-1]["srl"])
        last = int(state.get(srl, 0))

        if last == 0:
            # 이 유저는 처음 감시 시작: 기준점만 잡고 과거 글은 안 보냄
            state[srl] = newest
            newly_watching.append(name)
            print(f"[{name}] 감시 시작 (기준 글번호 {newest})")
            continue

        new_posts = [p for p in posts if int(p["srl"]) > last]
        if not new_posts:
            print(f"[{name}] 새 글 없음")
            continue

        sent = 0
        ok = True
        for p in new_posts[-MAX_MESSAGES_PER_MEMBER:]:
            try:
                send_telegram(build_message(name, p))
                sent += 1
            except Exception as e:
                print(f"[{name}] 전송 실패: {e} — 다음 확인 때 재시도")
                ok = False
                break
        if ok:
            state[srl] = newest
            print(f"[{name}] 새 글 {sent}건 전송!")

    save_state(state)

    if newly_watching:
        try:
            send_telegram(
                "✅ fmkorea 알리미 감시 시작!\n"
                "대상: " + ", ".join(newly_watching) + "\n"
                "이제 새 글이 올라오면 알려드릴게요."
            )
        except Exception as e:
            print(f"시작 메시지 전송 실패: {e}")


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "watch":
        print(f"감시 모드 시작 — {len(MEMBERS)}명을 10분마다 확인합니다. (Ctrl+C 로 중지)")
        while True:
            try:
                check_once()
            except Exception as e:
                print(f"[오류] {e} — 다음 확인 때 재시도")
            time.sleep(CHECK_INTERVAL_SEC)
    else:
        check_once()


if __name__ == "__main__":
    main()
