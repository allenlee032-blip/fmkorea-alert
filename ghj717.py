#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
통합 알리미 (패드/Termux판)
---------------------------
1) fmkorea  : 등록된 고수들이 새 글을 쓰면 알림
2) tooja.me : 미국코인러의 실시간 포지션 변화(진입/추가/부분청산/청산) 알림

실행:
  python ghj717.py          한 번만 확인 (테스트)
  python ghj717.py watch    10분마다 계속 감시
"""

import gzip
import http.cookiejar
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse

# ===== 설정 ======================================================
BOT_TOKEN = "8992521880:AAHiMAWW0grsm4I89lzteNb_AfqWYgKkMmc"
CHAT_ID = "7979521679"
CHECK_INTERVAL_SEC = 600         # 감시 주기 (600 = 10분)

# fmkorea 감시 대상: (이름, 게시판, 회원번호)
MEMBERS = [
    ("뽀삐햄",     "stock", "7884592847"),
    ("역천신공",   "stock", "5120217388"),
    ("디깅온유",   "stock", "2970302224"),
    ("노라무",     "stock", "9112231649"),
    ("겜주",       "stock", "7399777861"),
    ("젤리14",     "stock", "9715010970"),
    ("미국코인러", "coin",  "4148159043"),
]

WATCH_TOOJA = True               # tooja 포지션 감시 켜기/끄기
MEMBER_GAP_SEC = 85              # 한 명 확인 후 다음 사람까지 간격(초)
                                 # 7명 x 85초 = 약 10분마다 각자 한 번씩 확인됨
TOOJA_INTERVAL_SEC = 600         # tooja 포지션 확인 주기 (600 = 10분)
MAX_MESSAGES_PER_MEMBER = 10
# ================================================================

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "allenlee_state.json")
COOKIE_FILE = os.path.join(BASE, "fmkorea_cookies.txt")

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

TOOJA_POSITIONS_URL = "https://tooja.me/api/positions"

_cj = http.cookiejar.LWPCookieJar(COOKIE_FILE)
try:
    _cj.load(ignore_discard=True, ignore_expires=True)
except Exception:
    pass
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))


# ---------- 공통 ----------
def fetch(url, headers=None, tries=2):
    """차단(429/430/503/403) 시 짧게 한 번만 재시도.
    오래 기다리지 않고 넘어가야 다른 사람 확인이 밀리지 않는다.
    실패해도 다음 순번(약 10분 뒤)에 자동으로 다시 시도된다."""
    last_err = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers=headers or HEADERS)
        try:
            with _opener.open(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            try:
                _cj.save(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
            return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 430, 503, 403):
                if attempt == tries - 1:
                    break
                wait = 20 + random.randint(0, 10)
                print(f"    (차단 {e.code} — {wait}초 뒤 한 번만 재시도)")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt == tries - 1:
                break
            time.sleep(5)
    raise last_err


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


# ---------- fmkorea ----------
TAG_RE = re.compile(r"<[^>]+>")

def strip_tags(s):
    return re.sub(r"\s+", " ", TAG_RE.sub("", s)).strip()


def member_url(mid, srl):
    return ("https://www.fmkorea.com/search.php"
            f"?mid={mid}&search_target=member_srl&search_keyword={srl}")


def parse_posts(html):
    posts = []
    for chunk in re.split(r"<tr\b", html):
        am = re.search(r'<a\b[^>]*\bclass="[^"]*\bhx\b[^"]*"[^>]*>(.*?)</a>', chunk, re.S)
        if not am:
            continue
        title = strip_tags(am.group(1))
        hm = re.search(r'href="([^"]+)"', am.group(0))
        if not hm:
            continue
        sm = re.search(r'(\d{9,})', hm.group(1))
        if not sm or not title:
            continue
        cate = re.search(r'class="cate"[^>]*>(.*?)</td>', chunk, re.S)
        date = re.search(r'class="time"[^>]*>(.*?)</td>', chunk, re.S)
        posts.append({
            "srl": sm.group(1), "title": title,
            "cate": strip_tags(cate.group(1)) if cate else "",
            "date": strip_tags(date.group(1)) if date else "",
        })
    return posts


def build_post_message(name, p):
    cate = f"[{p['cate']}] " if p.get("cate") else ""
    date = f"\n🗓 {p['date']}" if p.get("date") else ""
    return (f"📝 {name} 새 글!\n\n{cate}{p['title']}{date}\n"
            f"https://www.fmkorea.com/{p['srl']}")


def check_member(state, name, mid, srl, first_run_names):
    """한 명만 확인. 실패하면 조용히 넘어가고 다음 순번에 재시도."""
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
        first_run_names.append(name)
        print(f"[{name}] 감시 시작 (기준 {newest})")
        return

    new_posts = [p for p in posts if int(p["srl"]) > last]
    if not new_posts:
        print(f"[{name}] 새 글 없음")
        return

    sent, ok = 0, True
    for p in new_posts[-MAX_MESSAGES_PER_MEMBER:]:
        try:
            send_telegram(build_post_message(name, p))
            sent += 1
        except Exception as e:
            print(f"[{name}] 전송 실패: {e}")
            ok = False
            break
    if ok:
        state[key] = newest
        print(f"[{name}] 새 글 {sent}건 전송!")


# ---------- tooja 실시간 포지션 ----------
def num(s):
    """'3,000.00 NFLX' 또는 '-1,019.72' -> 숫자"""
    m = re.search(r"-?[\d,]+\.?\d*", str(s))
    return float(m.group(0).replace(",", "")) if m else 0.0


def sym_name(s):
    return str(s).replace("USDT", "")


def check_tooja(state):
    try:
        data = json.loads(fetch(TOOJA_POSITIONS_URL, headers={
            "User-Agent": HEADERS["User-Agent"], "Accept": "application/json"}))
    except Exception as e:
        print(f"[tooja] 오류: {e} — 다음 확인 때 재시도")
        return

    positions = data.get("positions", [])
    if not positions and "positions" not in data:
        print("[tooja] 응답 이상 — 다음 확인 때 재시도")
        return

    # 현재 포지션을 {심볼_방향: 수량} 으로 정리
    now = {}
    info = {}
    for p in positions:
        key = f"{p.get('symbol','')}_{p.get('side','')}"
        now[key] = num(p.get("size", 0))
        info[key] = p

    prev = state.get("tooja_positions")

    if prev is None:
        state["tooja_positions"] = now
        print(f"[tooja] 감시 시작 (현재 포지션 {len(now)}개)")
        return True   # 첫 실행 표시

    msgs = []
    # 신규 진입 / 추가매수 / 부분청산
    for key, size in now.items():
        p = info[key]
        side = "🟢 롱" if p.get("side") == "long" else "🔴 숏"
        sym = sym_name(p.get("symbol"))
        if key not in prev:
            msgs.append(f"🚨 미국코인러 신규 진입!\n\n{side} {sym}\n"
                        f"진입가: {p.get('entry')}\n수량: {p.get('size')}\n"
                        f"레버리지: {p.get('lev')}")
        else:
            before = prev[key]
            if size > before * 1.001:
                msgs.append(f"➕ 미국코인러 추가 진입\n\n{side} {sym}\n"
                            f"수량: {before:,.2f} → {size:,.2f}\n"
                            f"평균가: {p.get('entry')}")
            elif size < before * 0.999:
                msgs.append(f"➖ 미국코인러 부분 청산\n\n{side} {sym}\n"
                            f"수량: {before:,.2f} → {size:,.2f}\n"
                            f"현재가: {p.get('mark')}")
    # 완전 청산
    for key, before in prev.items():
        if key not in now:
            sym = sym_name(key.rsplit("_", 1)[0])
            side = "롱" if key.endswith("_long") else "숏"
            msgs.append(f"✅ 미국코인러 포지션 종료\n\n{side} {sym} 전량 청산\n"
                        f"(청산 전 수량: {before:,.2f})")

    if not msgs:
        print("[tooja] 포지션 변화 없음")
        state["tooja_positions"] = now
        return

    sent = 0
    for msg in msgs[:10]:
        try:
            send_telegram(msg)
            sent += 1
        except Exception as e:
            print(f"[tooja] 전송 실패: {e}")
            return   # 상태 갱신 안 함 → 다음에 재시도
    state["tooja_positions"] = now
    print(f"[tooja] 포지션 변화 {sent}건 전송!")


# ---------- 메인 ----------
def check_once():
    """전체 1회 점검 (테스트/최초 기준잡기용)."""
    state = load_state()
    first_run_names = []
    for i, (name, mid, srl) in enumerate(MEMBERS):
        if i > 0:
            time.sleep(MEMBER_GAP_SEC + random.randint(0, 15))
        check_member(state, name, mid, srl, first_run_names)
        save_state(state)
    tooja_first = False
    if WATCH_TOOJA:
        tooja_first = check_tooja(state) is True
    save_state(state)
    announce(first_run_names, tooja_first)


def announce(first_run_names, tooja_first):
    if not first_run_names and not tooja_first:
        return
    parts = []
    if first_run_names:
        parts.append("fmkorea: " + ", ".join(first_run_names))
    if tooja_first:
        parts.append("tooja: 미국코인러 실시간 포지션")
    try:
        send_telegram("✅ 통합 알리미 감시 시작!\n\n" + "\n".join(parts) +
                      "\n\n이제 새 글/새 매매가 생기면 알려드릴게요.")
    except Exception as e:
        print(f"시작 메시지 전송 실패: {e}")


def watch():
    """한 명씩 계속 돌아가며 확인 (몰아치지 않아 차단이 덜 남)."""
    total = len(MEMBERS)
    cycle_min = round(total * MEMBER_GAP_SEC / 60)
    print(f"감시 모드 시작 — {MEMBER_GAP_SEC}초마다 한 명씩 순환 "
          f"(각자 약 {cycle_min}분마다 확인) + tooja 포지션. Ctrl+C 로 중지")

    state = load_state()
    idx = 0
    last_tooja = 0.0

    while True:
        first_run_names = []
        name, mid, srl = MEMBERS[idx % total]
        idx += 1
        try:
            check_member(state, name, mid, srl, first_run_names)
        except Exception as e:
            print(f"[{name}] 예외: {e}")

        # tooja 는 시간 기준으로 별도 확인
        tooja_first = False
        if WATCH_TOOJA and (time.time() - last_tooja) >= TOOJA_INTERVAL_SEC:
            try:
                tooja_first = check_tooja(state) is True
            except Exception as e:
                print(f"[tooja] 예외: {e}")
            last_tooja = time.time()

        try:
            save_state(state)
        except Exception as e:
            print(f"상태 저장 실패: {e}")

        announce(first_run_names, tooja_first)
        time.sleep(MEMBER_GAP_SEC + random.randint(0, 15))


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "watch":
        watch()
    else:
        check_once()


if __name__ == "__main__":
    main()
