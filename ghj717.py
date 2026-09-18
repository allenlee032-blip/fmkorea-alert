#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fmkorea 고수 새 글 알리미 (패드/Termux판) — 간단 버전
- 크롬 위장(curl_cffi) + 우선순위(뽀삐햄·노라무 자주)
준비물:  pip install curl_cffi
실행:    python ghj717.py          한 번 확인(테스트)
         python ghj717.py watch     계속 감시
"""

import json
import os
import random
import re
import sys
import time
import urllib.request
import urllib.parse

try:
    from curl_cffi import requests as cffi
except ImportError:
    print("먼저 curl_cffi 를 설치해주세요:  pip install curl_cffi")
    sys.exit(1)

# ===== 설정 ======================================================
BOT_TOKEN = "8992521880:AAHiMAWW0grsm4I89lzteNb_AfqWYgKkMmc"
CHAT_ID = "7979521679"

# 감시할 유저: (이름, 게시판, 회원번호)
MEMBERS = [
    ("뽀삐햄",     "stock", "7884592847"),
    ("역천신공",   "stock", "5120217388"),
    ("디깅온유",   "stock", "2970302224"),
    ("노라무",     "stock", "9112231649"),
    ("겜주",       "stock", "7399777861"),
    ("젤리14",     "stock", "9715010970"),
    ("디에알",     "stock", "9164519700"),
    ("직장인3",    "stock", "8928718846"),
    ("손흥민",     "stock", "224241"),
    ("개천재님",   "stock", "7011935566"),
    ("달빛속삭임", "stock", "6904116317"),
    ("저매수",     "stock", "10098792964"),
    ("짭란드",     "stock", "2714339135"),
    ("블로거",     "stock", "7862798450"),
    ("강퇴",       "stock", "3902132645"),
    ("제로콜라",   "stock", "9927537878"),
    ("Roony햄",    "stock", "10167681602"),
    ("아기티큐",   "stock", "3366170283"),
    ("삼전하닉피보나치햄", "stock", "105788011"),
    ("HA2햄",      "stock", "3158413881"),
]

PRIORITY = {"뽀삐햄", "노라무"}     # 더 자주 확인
PRIORITY_WEIGHT = 3
MEMBER_GAP_SEC = 50                # 한 명 확인 후 다음까지 간격(초)
MAX_MESSAGES_PER_MEMBER = 10
IMPERSONATE = "chrome"
# ================================================================

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "ghj717_state.json")

_session = cffi.Session(impersonate=IMPERSONATE)
EXTRA_HEADERS = {
    "Referer": "https://www.fmkorea.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(s):
    return re.sub(r"\s+", " ", TAG_RE.sub("", s)).strip()


def member_url(mid, srl):
    return ("https://www.fmkorea.com/search.php"
            f"?mid={mid}&search_target=member_srl&search_keyword={srl}")


def fetch(url, tries=2):
    last = None
    for attempt in range(tries):
        try:
            r = _session.get(url, headers=EXTRA_HEADERS, timeout=30)
            if r.status_code in (429, 430, 503, 403):
                if attempt == tries - 1:
                    raise RuntimeError(f"HTTP {r.status_code} (차단)")
                wait = 20 + random.randint(0, 15)
                print(f"    (차단 {r.status_code} — {wait}초 뒤 재시도)")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.text
        except Exception as e:
            last = e
            if attempt == tries - 1:
                break
            time.sleep(6)
    raise last


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
        "chat_id": CHAT_ID, "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_posts(html):
    out = []
    for chunk in re.split(r"<tr\b", html):
        am = re.search(r'<a\b[^>]*\bclass="[^"]*\bhx\b[^"]*"[^>]*>(.*?)</a>', chunk, re.S)
        if not am:
            continue
        hm = re.search(r'href="([^"]+)"', am.group(0))
        if not hm:
            continue
        href = hm.group(1)
        # 글번호(document_srl) 정확히 추출 (회원번호를 착각하지 않도록)
        sm = (re.search(r'document_srl=(\d{6,})', href)
              or re.search(r'/(\d{9,})(?:[/?#]|$)', href))
        if not sm:
            continue
        cate = re.search(r'class="cate"[^>]*>(.*?)</td>', chunk, re.S)
        date = re.search(r'class="time"[^>]*>(.*?)</td>', chunk, re.S)
        out.append({
            "srl": sm.group(1),
            "title": strip_tags(am.group(1)),
            "cate": strip_tags(cate.group(1)) if cate else "",
            "date": strip_tags(date.group(1)) if date else "",
        })
    return out


def build_message(name, p):
    cate = f"[{p['cate']}] " if p.get("cate") else ""
    date = f"\n🗓 {p['date']}" if p.get("date") else ""
    return (f"📝 {name} 새 글!\n\n{cate}{p['title']}{date}\n"
            f"https://www.fmkorea.com/{p['srl']}")


def check_member(state, name, mid, srl):
    key = "fm_" + srl
    try:
        posts = parse_posts(fetch(member_url(mid, srl)))
    except Exception as e:
        print(f"[{name}] 실패({e}) — 다음 순번에 재시도")
        return
    if not posts:
        print(f"[{name}] 글 목록 못 찾음 — 다음 순번에 재시도")
        return

    posts.sort(key=lambda p: int(p["srl"]))
    newest = int(posts[-1]["srl"])
    last = int(state.get(key, 0))

    if last == 0:
        state[key] = newest
        print(f"[{name}] 감시 시작 (기준 {newest})")
        return

    new_posts = [p for p in posts if int(p["srl"]) > last]
    if not new_posts:
        print(f"[{name}] 새 글 없음")
        return

    sent, ok = 0, True
    for p in new_posts[-MAX_MESSAGES_PER_MEMBER:]:
        try:
            send_telegram(build_message(name, p))
            sent += 1
        except Exception as e:
            print(f"[{name}] 전송 실패: {e}")
            ok = False
            break
    if ok:
        state[key] = newest
        print(f"[{name}] 새 글 {sent}건 전송!")


def build_schedule():
    normals = [m for m in MEMBERS if m[0] not in PRIORITY]
    prios = [m for m in MEMBERS if m[0] in PRIORITY]
    slots = []
    n = max(1, len(normals))
    for i, m in enumerate(normals):
        slots.append(((i + 0.5) / n, m))
    for j, m in enumerate(prios):
        for k in range(PRIORITY_WEIGHT):
            pos = (k + (j + 0.5) / max(1, len(prios))) / PRIORITY_WEIGHT
            slots.append((pos % 1.0, m))
    slots.sort(key=lambda x: x[0])
    return [m for _, m in slots]


def check_once():
    state = load_state()
    for i, (name, mid, srl) in enumerate(MEMBERS):
        if i > 0:
            time.sleep(MEMBER_GAP_SEC + random.randint(0, 15))
        check_member(state, name, mid, srl)
        save_state(state)


def watch():
    schedule = build_schedule()
    prio_gap = round(len(schedule) / PRIORITY_WEIGHT * (MEMBER_GAP_SEC + 7) / 60, 1)
    normal_gap = round(len(schedule) * (MEMBER_GAP_SEC + 7) / 60)
    print(f"감시 모드 시작 — 한 바퀴 {len(schedule)}칸 "
          f"(우선순위 약 {prio_gap}분마다 / 일반 약 {normal_gap}분마다). Ctrl+C 로 중지")
    try:
        send_telegram(f"✅ fmkorea 알리미 감시 시작! (총 {len(MEMBERS)}명 감시)\n"
                      "새 글이 올라오면 알려드릴게요.")
    except Exception as e:
        print(f"시작 메시지 전송 실패: {e}")

    state = load_state()
    idx = 0
    while True:
        name, mid, srl = schedule[idx % len(schedule)]
        idx += 1
        try:
            check_member(state, name, mid, srl)
            save_state(state)
        except Exception as e:
            print(f"[{name}] 예외: {e}")
        time.sleep(MEMBER_GAP_SEC + random.randint(0, 15))


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "watch":
        watch()
    else:
        check_once()


if __name__ == "__main__":
    main()
