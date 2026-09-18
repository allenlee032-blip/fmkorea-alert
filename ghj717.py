#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FM코리아 20명 알리미. Python 3.10+, Termux curl 또는 기존 curl_cffi.

실행: python ghj717.py (기존 watch 명령도 지원). 종료: Ctrl+C.
최초 기록 이전·비밀 설정·실행 보조 파일은 프로그램이 준비한다.
HTTP 430/CAPTCHA를 우회하거나 Telegram exactly-once를 보장하지 않는다.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import getpass
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

VERSION = "2.1.1"
SCHEMA = 1
HA2_MEMBER = ("HA2햄", "stock", "3158413881")
MEMBERS = [
    ("뽀삐햄", "stock", "7884592847"),
    ("역천신공", "stock", "5120217388"),
    ("디깅온유", "stock", "2970302224"),
    ("노라무", "stock", "9112231649"),
    ("겜주", "stock", "7399777861"),
    ("젤리14", "stock", "9715010970"),
    ("디에알", "stock", "9164519700"),
    ("직장인3", "stock", "8928718846"),
    ("손흥민", "stock", "224241"),
    ("개천재님", "stock", "7011935566"),
    ("달빛속삭임", "stock", "6904116317"),
    ("저매수", "stock", "10098792964"),
    ("짭란드", "stock", "2714339135"),
    ("블로거", "stock", "7862798450"),
    ("강퇴", "stock", "3902132645"),
    ("제로콜라", "stock", "9927537878"),
    ("Roony햄", "stock", "10167681602"),
    ("아기티큐", "stock", "3366170283"),
    ("삼전하닉피보나치햄", "stock", "105788011"),
    HA2_MEMBER,
]
PREVIOUS_MEMBERS = MEMBERS[:-1]
PRIORITY = {"뽀삐햄", "노라무"}
PRIORITY_WEIGHT = 3
DEFAULTS = {
    "transport": "auto", "impersonate": "chrome", "request_timeout": 20,
    "member_gap_sec": 50, "jitter_sec": 15, "max_pages": 20,
    "priority_stale_sec": 1500, "normal_stale_sec": 3600,
    "pending_stale_sec": 1800, "heartbeat_sec": 60,
    "backup_sec": 21600, "backup_keep": 12,
    "bot_token": "", "chat_id": "", "hc_process_url": "", "hc_health_url": "",
}
LOG = logging.getLogger("ghj717")


class SafeError(Exception):
    """메시지에 원격 응답 본문/비밀 URL을 넣지 않는다."""


class FetchError(SafeError):
    def __init__(self, code, blocked=False):
        super().__init__(code)
        self.code, self.blocked = code, blocked


class SendError(SafeError):
    def __init__(self, code, retry_after=60, permanent=False, ambiguous=False):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.permanent = permanent
        self.ambiguous = ambiguous


def data_dir():
    return Path(os.environ.get("GHJ717_DATA_DIR", "~/.local/share/ghj717")).expanduser().resolve()


def config_path():
    return Path(os.environ.get("GHJ717_CONFIG", "~/.config/ghj717/config.json")).expanduser().resolve()


def sync_dir(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "wb") as f:
        os.chmod(temp, 0o600)
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    sync_dir(path.parent)


def load_config(path=None):
    path = Path(path) if path else config_path()
    cfg = dict(DEFAULTS)
    if path.exists():
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise SafeError("설정 파일 권한을 chmod 600으로 변경하세요.")
        try:
            extra = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise SafeError("설정 JSON을 읽을 수 없습니다.") from None
        if not isinstance(extra, dict) or set(extra) - set(DEFAULTS):
            raise SafeError("설정 키를 확인하세요. config.example.json 참조.")
        cfg.update(extra)
    for key, env in (("bot_token", "TELEGRAM_BOT_TOKEN"), ("chat_id", "TELEGRAM_CHAT_ID"),
                     ("hc_process_url", "GHJ717_HC_PROCESS_URL"), ("hc_health_url", "GHJ717_HC_HEALTH_URL")):
        cfg[key] = os.environ.get(env, str(cfg[key])).strip()
    for key in DEFAULTS:
        if isinstance(DEFAULTS[key], int):
            if type(cfg[key]) is not int or cfg[key] < (0 if key == "jitter_sec" else 1):
                raise SafeError("숫자 설정값의 범위/형식이 잘못되었습니다: " + key)
    if not 5 <= cfg["request_timeout"] <= 60 or cfg["member_gap_sec"] < 30:
        raise SafeError("request_timeout은 5~60초, member_gap_sec는 30초 이상이어야 합니다.")
    if cfg["heartbeat_sec"] < 30:
        raise SafeError("heartbeat_sec는 30초 이상이어야 합니다.")
    if cfg["transport"] not in ("auto", "curl", "curl_cffi"):
        raise SafeError("transport는 auto, curl, curl_cffi 중 하나여야 합니다.")
    for key in ("hc_process_url", "hc_health_url"):
        if cfg[key] and not re.fullmatch(r"https://hc-ping\.com/[0-9a-fA-F-]{36}", cfg[key]):
            raise SafeError("Healthchecks UUID 기본 HTTPS ping URL을 입력하세요: " + key)
    if cfg["hc_process_url"] and cfg["hc_process_url"] == cfg["hc_health_url"]:
        raise SafeError("실행 감시와 수집 감시는 서로 다른 Check URL이어야 합니다.")
    return cfg


def require_telegram(cfg):
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", cfg["bot_token"]) or not cfg["chat_id"]:
        raise SafeError("configure로 Telegram 토큰과 chat_id를 설정하세요.")


class Redact(logging.Filter):
    def __init__(self, cfg):
        super().__init__()
        self.secrets = [cfg[k] for k in ("bot_token", "chat_id", "hc_process_url", "hc_health_url") if cfg[k]]

    def filter(self, record):
        msg = record.getMessage()
        for value in self.secrets:
            msg = msg.replace(value, "<redacted>")
        record.msg = re.sub(r"\b\d{5,15}:[A-Za-z0-9_-]{25,}", "<redacted>", msg)
        record.args = ()
        return True


def setup_logging(root, cfg):
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOG.setLevel(logging.INFO)
    for old_handler in list(LOG.handlers):
        LOG.removeHandler(old_handler)
        old_handler.close()
    for h in (RotatingFileHandler(logs / "ghj717.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"),
              logging.StreamHandler()):
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S%z"))
        h.addFilter(Redact(cfg))
        LOG.addHandler(h)


class FileLock:
    def __init__(self, root):
        self.root, self.f = root, None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.f = open(self.root / "writer.lock", "a+b")
        self.f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                if self.f.read(1) == b"":
                    self.f.write(b"0")
                    self.f.flush()
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            self.f = None
            raise SafeError("이미 실행 중입니다. 서비스 중지 후 다시 실행하세요.") from None
        return self

    def __exit__(self, *args):
        if self.f:
            self.f.close()


SQL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE members(
 sid TEXT PRIMARY KEY, name TEXT NOT NULL, mid TEXT NOT NULL,
 cursor INTEGER NOT NULL, scan_page INTEGER NOT NULL DEFAULT 0,
 scan_head INTEGER NOT NULL DEFAULT 0, fingerprint TEXT NOT NULL DEFAULT '',
 last_attempt REAL, last_page REAL, last_complete REAL,
 failures INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', gap TEXT NOT NULL DEFAULT '');
CREATE TABLE deliveries(
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL,
 due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
 message_id INTEGER, sent_at REAL, error TEXT NOT NULL DEFAULT '');
CREATE TABLE posts(
 sid TEXT NOT NULL REFERENCES members(sid), pid INTEGER NOT NULL,
 title TEXT NOT NULL, cate TEXT NOT NULL, date TEXT NOT NULL, discovered REAL NOT NULL,
 delivery_id INTEGER REFERENCES deliveries(id), sent_at REAL,
 PRIMARY KEY(sid,pid));
CREATE INDEX pending_posts ON posts(sent_at,delivery_id,pid);
"""


def connect_db(path, readonly=False, allow_previous_roster=False):
    if not path.is_file():
        raise SafeError("상태 DB가 없습니다. 기존 기록 이관 또는 백업 복구가 필요합니다. 자동 초기화하지 않습니다.")
    db = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SafeError("DB 무결성 검사 실패. restore로 검증된 백업을 복구하세요.")
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
            raise SafeError("지원하지 않는 상태 DB 버전입니다.")
        rows = db.execute("SELECT sid,name,mid,cursor,scan_page FROM members").fetchall()
        roster = {(r["name"], r["mid"], r["sid"]) for r in rows}
        if roster != set(MEMBERS) and not (allow_previous_roster and roster == set(PREVIOUS_MEMBERS)):
            raise SafeError("DB의 감시 명단이 코드와 다릅니다. 기록을 자동 초기화하지 않습니다.")
        if any(r["cursor"] < 0 or r["scan_page"] < 0 for r in rows):
            raise SafeError("DB 기준점이 잘못되었습니다.")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SafeError("DB 참조 무결성 검사 실패.")
        if not db.execute("SELECT value FROM meta WHERE key='created_at'").fetchone():
            raise SafeError("DB 생성 정보가 없습니다.")
        if not readonly:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
        return db
    except BaseException:
        db.close()
        raise


def meta(db, key, default="0"):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def put_meta(db, key, value):
    db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def initialize(root, legacy=None, fresh=False, baseline_missing=False):
    path = root / "ghj717.sqlite3"
    if path.exists() or (root / "initialized.json").exists():
        raise SafeError("이미 초기화된 저장소입니다. init으로 덮어쓰거나 재이관하지 않습니다.")
    if not legacy and not fresh:
        raise SafeError("--legacy-state PATH를 지정하세요. 기록 없는 새 설치에만 --fresh 사용.")
    values, raw = {}, None
    if legacy:
        try:
            raw = Path(legacy).read_bytes()
            values = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, OSError):
            raise SafeError("기존 JSON 손상/경로 오류. 백업을 찾으세요. 빈 기록으로 대체하지 않습니다.") from None
        if not isinstance(values, dict):
            raise SafeError("기존 JSON은 fm_회원번호: 글번호 형식이어야 합니다.")
    cursors, missing, unexpected_missing = {}, [], []
    for name, _, sid in MEMBERS:
        value = values.get("fm_" + sid, 0)
        if isinstance(value, bool) or not re.fullmatch(r"\d{1,18}", str(value)):
            raise SafeError("기존 기준점 형식 오류: " + name)
        cursors[sid] = int(value)
        if not cursors[sid]:
            missing.append(name)
            # 이번에 요청받아 추가한 HA2만 새 기준을 허용한다. 기존 19명 누락은 계속 차단한다.
            if not (legacy and sid == HA2_MEMBER[2] and "fm_" + sid not in values):
                unexpected_missing.append(name)
    if unexpected_missing and not (fresh or baseline_missing):
        raise SafeError("기존 기준점 없는 회원: " + ", ".join(unexpected_missing) + ". 의도한 경우에만 --baseline-missing 사용.")
    temp = root / "init.sqlite3.tmp"
    if temp.exists():
        raise SafeError("이전 초기화 임시 DB가 남았습니다. 보존 후 확인이 필요합니다.")
    db = sqlite3.connect(temp)
    try:
        db.executescript(SQL)
        db.execute(f"PRAGMA user_version={SCHEMA}")
        with db:
            for name, mid, sid in MEMBERS:
                db.execute("INSERT INTO members(sid,name,mid,cursor) VALUES(?,?,?,?)", (sid,name,mid,cursors[sid]))
            put_meta(db, "created_at", time.time())
            put_meta(db, "version", VERSION)
            put_meta(db, "legacy_sha256", hashlib.sha256(raw).hexdigest() if raw else "fresh")
    finally:
        db.close()
    os.chmod(temp, 0o600)
    if raw:
        atomic_write(root / "backups" / "legacy-import.json", raw)
    # 마커를 먼저 남겨 중간 중단도 자동 재초기화로 이어지지 않게 한다.
    atomic_write(root / "initialized.json", json.dumps({"schema": SCHEMA, "created": time.time()}).encode())
    os.replace(temp, path)
    sync_dir(root)
    print(f"이관 완료: 기존 기준 {len(MEMBERS)-len(missing)}명, 새 기준 예정 {len(missing)}명. 원본 JSON 보존.")


def backup_db(db, root, keep=12):
    folder = root / "backups"
    folder.mkdir(exist_ok=True, mode=0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = folder / ("state-" + stamp + ".sqlite3")
    temp = path.with_suffix(".tmp")
    dest = sqlite3.connect(temp)
    try:
        db.backup(dest)
        if dest.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SafeError("백업 무결성 검사 실패")
    finally:
        dest.close()
    os.chmod(temp, 0o600)
    with open(temp, "r+b") as f:
        os.fsync(f.fileno())
    os.replace(temp, path)
    sync_dir(folder)
    for old in sorted(folder.glob("state-*.sqlite3"))[:-keep]:
        old.unlink()
    return path


def restore_db(root, source):
    source = Path(source).expanduser().resolve()
    with contextlib.closing(connect_db(source, readonly=True, allow_previous_roster=True)) as db:
        temp = root / "restore.sqlite3.tmp"
        if temp.exists():
            raise SafeError("이전 복구 임시 DB가 남았습니다. 확인 후 보존/이동하세요.")
        dest = sqlite3.connect(temp)
        try:
            db.backup(dest)
        finally:
            dest.close()
    # 19명 시절 백업도 복구 가능하다. 검증된 복사본에만 HA2를 추가한다.
    with contextlib.closing(connect_db(temp, allow_previous_roster=True)) as restored:
        add_ha2_member(restored)
        restored.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with contextlib.closing(connect_db(temp, readonly=True)):
        pass
    archive = root / "recovery" / str(time.time_ns())
    archive.mkdir(parents=True, mode=0o700)
    for suffix in ("", "-wal", "-shm"):
        p = root / ("ghj717.sqlite3" + suffix)
        if p.exists():
            os.replace(p, archive / p.name)
    os.chmod(temp, 0o600)
    os.replace(temp, root / "ghj717.sqlite3")
    atomic_write(root / "initialized.json", json.dumps({"schema": SCHEMA, "restored": time.time()}).encode())
    print("복구 완료. 기존 DB/WAL/SHM은 recovery에 보존. 백업 이후 전송분은 중복될 수 있습니다.")


def member_url(mid, sid, page=1):
    return "https://www.fmkorea.com/search.php?" + urllib.parse.urlencode({
        "mid": mid, "search_target": "member_srl", "search_keyword": sid, "page": page})


def post_id(href):
    u = urllib.parse.urlsplit(urllib.parse.urljoin("https://www.fmkorea.com/", href))
    if u.hostname not in ("www.fmkorea.com", "fmkorea.com"):
        return None
    q = urllib.parse.parse_qs(u.query)
    v = q.get("document_srl", [""])[0]
    if re.fullmatch(r"\d{6,18}", v):
        return int(v)
    match = re.fullmatch(r"/(?:[A-Za-z_]+/)?(\d{9,18})/?", u.path)
    return int(match.group(1)) if match else None


class ListingParser(HTMLParser):
    def __init__(self, sid):
        super().__init__(convert_charrefs=True)
        self.sid, self.context = sid, False
        self.row, self.capture, self.parts, self.posts = None, None, [], []
        self.anchors = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = a.get("class", "").split()
        if tag == "input" and a.get("name") == "search_keyword" and a.get("value") == self.sid:
            self.context = True
        if tag == "a":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(a.get("href", "")).query)
            if q.get("search_keyword") == [self.sid] and q.get("search_target") == ["member_srl"]:
                self.context = True
        if tag == "tr":
            self.row = {"notice": any("notice" in c for c in classes), "title": "", "cate": "", "date": ""}
            self.capture = None
        if self.row is None:
            return
        if tag == "a" and "hx" in classes:
            self.row["pid"] = post_id(a.get("href", ""))
            self.capture, self.parts = ("a", "title"), []
        elif tag == "td" and "cate" in classes:
            self.capture, self.parts = ("td", "cate"), []
        elif tag == "td" and "time" in classes:
            self.capture, self.parts = ("td", "date"), []
        # 서버가 다른 회원 행을 반환한 경우 성공으로 인정하지 않는다.
        for c in classes:
            match = re.fullmatch(r"member_(\d+)", c)
            if match:
                self.row["author"] = match.group(1)

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if self.capture and self.capture[0] == tag:
            self.row[self.capture[1]] = " ".join("".join(self.parts).split())
            self.capture = None
        if tag == "tr" and self.row is not None:
            r = self.row
            if r.get("pid") and r["title"] and not r["notice"]:
                if r.get("author", self.sid) != self.sid:
                    raise FetchError("wrong_member")
                self.posts.append({k: r[k] for k in ("pid", "title", "cate", "date")})
            self.row, self.capture = None, None


def parse_page(html, sid, final_url):
    u = urllib.parse.urlsplit(final_url)
    q = urllib.parse.parse_qs(u.query)
    if u.scheme != "https" or u.hostname not in ("www.fmkorea.com", "fmkorea.com"):
        raise FetchError("unexpected_redirect", True)
    if q.get("search_keyword") != [sid] or q.get("search_target") != ["member_srl"]:
        raise FetchError("search_context_redirect")
    p = ListingParser(sid)
    p.feed(html)
    if not p.posts:
        if re.search(r"cf-chl-|g-recaptcha|hcaptcha|자동\s*접속|비정상적인\s*접근|captcha", html, re.I):
            raise FetchError("challenge_page", True)
        raise FetchError("empty_or_layout_changed")
    if not p.context:
        raise FetchError("member_context_missing")
    return sorted({r["pid"]: r for r in p.posts}.values(), key=lambda r: r["pid"])


class Fetcher:
    def __init__(self, cfg):
        self.cfg, self.session, self.mode = cfg, None, cfg["transport"]
        if self.mode in ("auto", "curl_cffi"):
            try:
                from curl_cffi import requests
                self.session = requests.Session(impersonate=cfg["impersonate"])
                self.mode = "curl_cffi"
            except (ImportError, OSError):
                if self.mode == "curl_cffi":
                    raise SafeError("curl_cffi를 불러올 수 없습니다. 기존 설치 확인 또는 transport=curl 설정.") from None
                self.mode = "curl"
        if self.mode == "curl" and not shutil.which("curl"):
            raise SafeError("Termux에서 pkg install curl을 실행하세요.")

    def fetch(self, mid, sid, page):
        url = member_url(mid, sid, page)
        timeout = self.cfg["request_timeout"]
        try:
            if self.session:
                r = self.session.get(url, timeout=timeout, allow_redirects=True,
                    headers={"Referer": "https://www.fmkorea.com/", "Accept-Language": "ko-KR,ko;q=0.9"})
                status, html, final = r.status_code, r.text, str(r.url)
            else:
                r = subprocess.run(["curl", "--silent", "--show-error", "--location", "--max-redirs", "3",
                    "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "8", "--max-time", str(timeout),
                    "--max-filesize", "5000000", "--header", "Accept-Language: ko-KR,ko;q=0.9",
                    "--referer", "https://www.fmkorea.com/", "--user-agent", "Mozilla/5.0",
                    "--write-out", "\n__GHJ717__%{http_code} %{url_effective}", url],
                    capture_output=True, timeout=timeout+5, check=False)
                if r.returncode:
                    raise FetchError("curl_network_" + str(r.returncode))
                html, trailer = r.stdout.decode("utf-8", errors="replace").rsplit("\n__GHJ717__", 1)
                code, final = trailer.split(" ", 1)
                status = int(code)
            if status != 200:
                raise FetchError("http_" + str(status), status in (403, 429, 430, 503))
            return parse_page(html, sid, final)
        except FetchError:
            raise
        except Exception as e:
            raise FetchError("network_" + type(e).__name__) from None


def build_schedule():
    normals = [m for m in MEMBERS if m[0] not in PRIORITY]
    prios = [m for m in MEMBERS if m[0] in PRIORITY]
    slots = [((i + .5) / len(normals), m) for i, m in enumerate(normals)]
    for j, m in enumerate(prios):
        for k in range(PRIORITY_WEIGHT):
            slots.append(((k + (j + .5) / len(prios)) / PRIORITY_WEIGHT, m))
    return [m for _, m in sorted(slots, key=lambda x: x[0])]


def ingest(db, sid, posts, now, max_pages):
    """글 저장과 기준점 변경은 같은 트랜잭션. 경계 발견 전에는 기준점 유지."""
    if not posts:
        raise FetchError("empty_or_layout_changed")
    r = db.execute("SELECT * FROM members WHERE sid=?", (sid,)).fetchone()
    page = r["scan_page"] or 1
    newest = max(p["pid"] for p in posts)
    fingerprint = hashlib.sha256(",".join(str(p["pid"]) for p in sorted(posts, key=lambda p: p["pid"])).encode()).hexdigest()
    with db:
        if r["cursor"] == 0:  # 명시적 새 설치/누락 승인 또는 새로 추가한 HA2의 최초 기준
            db.execute("UPDATE members SET cursor=?,last_attempt=?,last_page=?,last_complete=?,failures=0,error='' WHERE sid=?",
                       (newest, now, now, now, sid))
            return "baseline"
        for p in posts:
            if p["pid"] > r["cursor"]:
                db.execute("INSERT OR IGNORE INTO posts(sid,pid,title,cate,date,discovered) VALUES(?,?,?,?,?,?)",
                           (sid,p["pid"],p["title"][:700],p["cate"][:100],p["date"][:100],now))
        head = max(r["cursor"], r["scan_head"], newest)
        if min(p["pid"] for p in posts) <= r["cursor"]:
            db.execute("UPDATE members SET cursor=?,scan_page=0,scan_head=0,fingerprint='',last_attempt=?,last_page=?,last_complete=?,failures=0,error='',gap='' WHERE sid=?",
                       (head, now, now, now, sid))
            return "complete"
        gap = "repeated_page" if page > 1 and fingerprint == r["fingerprint"] else ""
        next_page = page if gap else page + 1
        if next_page > max_pages:
            gap = "page_limit"
        db.execute("UPDATE members SET scan_page=?,scan_head=?,fingerprint=?,last_attempt=?,last_page=?,failures=0,error='',gap=? WHERE sid=?",
                   (next_page,head,fingerprint,now,now,gap,sid))
        return "gap:" + gap if gap else "backfill"


def collect_one(db, fetcher, member, cfg, now):
    name, mid, sid = member
    r = db.execute("SELECT * FROM members WHERE sid=?", (sid,)).fetchone()
    page = r["scan_page"] or 1
    if page > cfg["max_pages"] or r["gap"] == "repeated_page":
        if page > cfg["max_pages"]:
            with db:
                db.execute("UPDATE members SET gap='page_limit' WHERE sid=?", (sid,))
        LOG.warning("수집 보류 member=%s gap=%s page=%s cursor=%s", name, r["gap"], page, r["cursor"])
        return
    try:
        posts = fetcher.fetch(mid, sid, page)
        now = time.time()
        result = ingest(db, sid, posts, now, cfg["max_pages"])
        with db:
            put_meta(db, "site_strikes", 0)
            put_meta(db, "site_until", 0)
        LOG.info("수집 member=%s page=%s posts=%s result=%s", name, page, len(posts), result)
    except FetchError as e:
        strikes = min(int(meta(db, "site_strikes")) + 1, 8)
        pause = min((120 if e.blocked else 60) * 2 ** (strikes-1), 3600 if e.blocked else 900)
        until = time.time() + pause + random.randint(0, 15)
        with db:
            db.execute("UPDATE members SET last_attempt=?,failures=failures+1,error=? WHERE sid=?", (now,e.code,sid))
            put_meta(db, "site_strikes", strikes)
            put_meta(db, "site_until", until)
        LOG.warning("수집 실패 member=%s page=%s reason=%s 사이트 전체 휴식=%ss", name, page, e.code, pause)


def utf16len(s):
    return len(s.encode("utf-16-le")) // 2


def prepare_delivery(db, now):
    # 이미 묶은 메시지는 재시도/재시작 때 그대로 유지한다.
    if db.execute("SELECT 1 FROM deliveries WHERE status IN ('pending','blocked') LIMIT 1").fetchone():
        return
    first = db.execute("SELECT p.sid,m.name FROM posts p JOIN members m USING(sid) WHERE p.sent_at IS NULL AND p.delivery_id IS NULL AND m.scan_page=0 ORDER BY p.pid LIMIT 1").fetchone()
    if not first:
        return
    rows = db.execute("SELECT * FROM posts WHERE sid=? AND sent_at IS NULL AND delivery_id IS NULL ORDER BY pid LIMIT 10", (first["sid"],)).fetchall()
    body, chosen = "📝 " + first["name"] + " 새 글\n", []
    for p in rows:
        item = f"\n[{p['cate']}] {p['title']}\n{p['date']}\nhttps://www.fmkorea.com/{p['pid']}\n"
        if utf16len(body + item) > 3500:
            break
        body += item
        chosen.append(p["pid"])
    if not chosen:
        raise SafeError("전송 메시지 길이 처리 오류")
    with db:
        did = db.execute("INSERT INTO deliveries(kind,body,created) VALUES('posts',?,?)", (body,now)).lastrowid
        for pid in chosen:
            db.execute("UPDATE posts SET delivery_id=? WHERE sid=? AND pid=?", (did,first["sid"],pid))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = urllib.request.build_opener(NoRedirect())


def telegram_send(cfg, body):
    payload = json.dumps({"chat_id": cfg["chat_id"], "text": body,
                          "link_preview_options": {"is_disabled": True}}, ensure_ascii=False).encode()
    req = urllib.request.Request("https://api.telegram.org/bot" + cfg["bot_token"] + "/sendMessage",
                                 data=payload, headers={"Content-Type": "application/json"})
    status = 200
    try:
        with HTTP.open(req, timeout=cfg["request_timeout"]) as resp:
            raw = resp.read(100_000)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(100_000)
    except Exception:
        raise SendError("transport_outcome_unknown", ambiguous=True) from None
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SendError("response_outcome_unknown", ambiguous=True) from None
    if not isinstance(result, dict):
        raise SendError("response_outcome_unknown", ambiguous=True)
    mid = result.get("result", {})
    if status == 200 and result.get("ok") is True and isinstance(mid, dict) and type(mid.get("message_id")) is int:
        return mid["message_id"]
    code = result.get("error_code", status)
    params = result.get("parameters", {})
    retry = params.get("retry_after", 60) if isinstance(params, dict) else 60
    retry = retry if type(retry) is int and retry > 0 else 60
    if code == 429:
        raise SendError("telegram_429", retry_after=retry)
    if code in (400, 401, 403, 404):
        raise SendError("telegram_" + str(code), permanent=True)
    raise SendError("telegram_response_unknown", ambiguous=True)


def send_one(db, cfg, now, sender=None):
    if meta(db, "telegram_block", "") or now < float(meta(db, "telegram_until")):
        return False
    prepare_delivery(db, now)
    r = db.execute("SELECT * FROM deliveries WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
    if not r or r["due"] > now:
        return False
    # 송신 직전 시도 사실 저장. 이 구간에서 죽으면 결과 불명으로 재시도한다.
    with db:
        db.execute("UPDATE deliveries SET attempts=attempts+1,due=?,error='outcome_unknown' WHERE id=?", (now+60,r["id"]))
    try:
        message_id = (sender or (lambda body: telegram_send(cfg, body)))(r["body"])
    except SendError as e:
        delay = max(e.retry_after, min(60 * 2 ** min(r["attempts"], 6), 3600))
        with db:
            db.execute("UPDATE deliveries SET status=?,due=?,error=? WHERE id=?",
                       ("blocked" if e.permanent else "pending",now+delay,e.code,r["id"]))
            put_meta(db, "telegram_until", now+delay)
            put_meta(db, "telegram_error", e.code)
            if e.permanent:
                put_meta(db, "telegram_block", e.code)
        LOG.warning("Telegram 실패 delivery=%s reason=%s ambiguous=%s retry=%ss", r["id"], e.code, e.ambiguous, delay)
        return False
    with db:
        db.execute("UPDATE deliveries SET status='sent',message_id=?,sent_at=?,error='' WHERE id=?", (message_id,now,r["id"]))
        db.execute("UPDATE posts SET sent_at=? WHERE delivery_id=?", (now,r["id"]))
        put_meta(db, "telegram_until", now+1.2)
        put_meta(db, "telegram_error", "")
        put_meta(db, "last_delivery", now)
    LOG.info("Telegram 전송 확인 delivery=%s message_id=%s", r["id"], message_id)
    return True


def health(db, cfg, now):
    issues, warming = [], []
    created = float(meta(db, "created_at"))
    for r in db.execute("SELECT * FROM members ORDER BY sid"):
        threshold = cfg["priority_stale_sec"] if r["name"] in PRIORITY else cfg["normal_stale_sec"]
        if r["gap"]:
            issues.append(r["name"] + ":확인공백(" + r["gap"] + ")")
        if r["last_complete"] is None and now-created <= threshold:
            warming.append(r["name"])
        elif r["last_complete"] is None or now-r["last_complete"] > threshold:
            issues.append(r["name"] + ":수집지연")
    pending = db.execute("SELECT COUNT(*),MIN(discovered) FROM posts WHERE sent_at IS NULL").fetchone()
    if pending[0] and now-pending[1] > cfg["pending_stale_sec"]:
        issues.append("미전송글:30분이상" if cfg["pending_stale_sec"] == 1800 else "미전송글:기준초과")
    if meta(db, "telegram_block", ""):
        issues.append("Telegram:설정수정필요")
    elif meta(db, "telegram_error", ""):
        issues.append("Telegram:전송실패")
    return issues, warming, pending[0]


def health_event(db, issues, now):
    signature = "|".join(sorted(issues))
    previous = meta(db, "health_signature", "")
    last = float(meta(db, "health_notice_at"))
    changed = bool(signature) != bool(previous)
    if not changed and (not signature or now-last < 21600):
        return
    body = "⚠️ FM코리아 감시 이상\n" + "\n".join(issues) if issues else "✅ FM코리아 감시가 정상 상태로 돌아왔습니다."
    with db:
        # 늦게 도착하는 미발송 경보/복구 메시지를 현재 상태로 대체한다.
        db.execute("UPDATE deliveries SET status='cancelled' WHERE kind='health' AND status IN ('pending','blocked')")
        db.execute("INSERT INTO deliveries(kind,body,created) VALUES('health',?,?)", (body,now))
        put_meta(db, "health_signature", signature)
        put_meta(db, "health_notice_at", now)


def ping(url, fail=False):
    if not url:
        return False
    try:
        with HTTP.open(urllib.request.Request(url + ("/fail" if fail else ""), data=b""), timeout=8) as r:
            return r.status == 200 and r.read(100).strip() == b"OK"
    except Exception:
        return False


def status_data(db, cfg, now):
    issues, warming, pending = health(db, cfg, now)
    return {"version": VERSION, "member_count": len(MEMBERS), "schedule_slots": len(build_schedule()),
            "process_last_tick": float(meta(db,"last_tick")), "pending_posts": pending,
            "healthy": not issues and not warming, "issues": issues, "warming": warming,
            "site_pause_until": float(meta(db,"site_until")),
            "telegram_block": meta(db,"telegram_block", ""),
            "external_monitor_configured": bool(cfg["hc_process_url"] and cfg["hc_health_url"]),
            "members": [dict(r) for r in db.execute("SELECT * FROM members ORDER BY rowid")]}


def run_watch(db, root, cfg):
    require_telegram(cfg)
    fetcher = Fetcher(cfg)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    schedule = build_schedule()
    with db:
        put_meta(db, "runtime_version", VERSION)
    LOG.info("시작 version=%s members=%s slots=%s transport=%s", VERSION, len(MEMBERS), len(build_schedule()), fetcher.mode)
    if not (cfg["hc_process_url"] and cfg["hc_health_url"]):
        LOG.warning("외부 감시 미설정: 패드 전원 종료를 원격으로 감지할 수 없습니다.")
    next_heartbeat = 0.0
    while not stop.is_set():
        now = time.time()
        if now >= next_heartbeat:
            with db:
                put_meta(db, "last_tick", now)
            issues, warming, pending = health(db, cfg, now)
            health_event(db, issues, now)
            for key, failed in (("hc_process_url", False), ("hc_health_url", bool(issues or warming))):
                if cfg[key] and not ping(cfg[key], failed):
                    LOG.warning("외부 감시 ping 실패 channel=%s", key)
            LOG.info("상태 pending=%s issues=%s warming=%s", pending, len(issues), len(warming))
            next_heartbeat = time.time() + cfg["heartbeat_sec"]
        if stop.is_set():
            break
        if now-float(meta(db,"last_backup")) >= cfg["backup_sec"]:
            path = backup_db(db, root, cfg["backup_keep"])
            with db:
                put_meta(db,"last_backup",time.time())
            LOG.info("검증 백업 생성 file=%s", path.name)
        if now >= max(float(meta(db,"site_until")),float(meta(db,"next_collect"))):
            idx = int(meta(db,"schedule_index")) % len(schedule)
            collect_one(db, fetcher, schedule[idx], cfg, now)
            with db:
                put_meta(db,"schedule_index",(idx+1) % len(schedule))
                put_meta(db,"next_collect",time.time()+cfg["member_gap_sec"]+random.randint(0,cfg["jitter_sec"]))
        if stop.is_set():
            break
        send_one(db, cfg, time.time())
        stop.wait(1.25)
    LOG.info("정상 종료: 상태/대기열 유지")


def configure():
    cfg = load_config()
    if not sys.stdin.isatty():
        raise SafeError("configure는 패드의 대화형 터미널에서 실행하세요.")
    for key, label in (("bot_token", "새 Telegram 봇 토큰"), ("chat_id", "Telegram chat_id"),
                       ("hc_process_url", "Healthchecks 실행 감시 ping URL"),
                       ("hc_health_url", "Healthchecks 수집 감시 ping URL")):
        value = getpass.getpass(label + " (빈 입력=유지, -=지우기): ").strip()
        if value:
            cfg[key] = "" if value == "-" else value
    path = config_path()
    temp = path.with_name("config.validate.json")
    atomic_write(temp, json.dumps(cfg, ensure_ascii=False, indent=2).encode())
    try:
        load_config(temp)
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        if temp.exists():
            temp.unlink()
    print("기기 내부 설정 저장 완료 (권한 600). 실행 중 서비스에는 재시작 후 적용됩니다.")


def selected_member(value):
    for member in MEMBERS:
        if value in (member[0], member[2]):
            return member
    raise SafeError("명단에서 이름 또는 회원번호를 확인하세요.")


def find_legacy_file(source, home, current):
    candidates = []
    for folder in (Path(source).parent, Path(home), Path(current), Path(home)/"fmkorea-alert"):
        path = (folder / "ghj717_state.json").resolve()
        if path.is_file() and path not in candidates:
            candidates.append(path)
    if not candidates:
        raise SafeError("이전 감시 기록을 찾지 못했습니다. 기록을 새로 만들지 않고 멈췄습니다. 이 안내를 알려주세요.")
    # 다른 기록이 있으면 수정 시각만으로 어느 것이 실사용 기록인지 추측하지 않는다.
    digests = {hashlib.sha256(p.read_bytes()).digest() for p in candidates}
    if len(digests) > 1:
        raise SafeError("서로 다른 감시 기록이 둘 이상 있습니다. 잘못 이어받지 않도록 멈췄습니다. 이 안내를 알려주세요.")
    return candidates[0]


def add_ha2_member(db):
    """검증된 기존 19명 DB에 새 회원만 추가한다. 기존 행과 대기열은 건드리지 않는다."""
    if db.execute("SELECT 1 FROM members WHERE sid=?", (HA2_MEMBER[2],)).fetchone():
        return False
    name, mid, sid = HA2_MEMBER
    with db:
        db.execute("INSERT INTO members(sid,name,mid,cursor) VALUES(?,?,?,0)", (sid, name, mid))
        put_meta(db, "version", VERSION)
    return True


def easy_state(root, source, home, current):
    if (root / "ghj717.sqlite3").exists():
        with FileLock(root), contextlib.closing(connect_db(
                root / "ghj717.sqlite3", allow_previous_roster=True)) as db:
            if not db.execute("SELECT 1 FROM members WHERE sid=?", (HA2_MEMBER[2],)).fetchone():
                backup_db(db, root)
                add_ha2_member(db)
                print("기존 19명 기록과 전송 대기열을 보존하고 HA2햄을 추가했습니다.")
        return False
    if (root / "initialized.json").exists():
        raise SafeError("사용하던 기록 파일이 사라졌습니다. 새 기록으로 바꾸지 않고 멈췄습니다.")
    legacy = find_legacy_file(source, home, current)
    with FileLock(root):
        initialize(root, legacy)
    return True


def telegram_setup_request(token, method):
    # 이 함수는 수신자 연결 점검 전용이다. 응답/토큰/URL은 출력하지 않는다.
    if method not in ("getMe", "getUpdates"):
        raise SafeError("설정 요청 종류 오류")
    data = b'{"limit":100,"timeout":0}' if method == "getUpdates" else b'{}'
    req = urllib.request.Request("https://api.telegram.org/bot" + token + "/" + method,
                                 data=data, headers={"Content-Type":"application/json"})
    try:
        with HTTP.open(req, timeout=20) as response:
            result = json.loads(response.read(1_000_000))
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError()
        return result["result"]
    except Exception:
        raise SafeError("Telegram에 연결하지 못했습니다. 인터넷·봇 토큰을 확인한 뒤 같은 실행 명령을 다시 입력하세요.") from None


def chat_from_confirmation(updates, phrase):
    ids = set()
    if not isinstance(updates, list):
        return None
    for update in updates:
        if not isinstance(update, dict):
            continue
        msg = update.get("message", {})
        if not isinstance(msg, dict) or not isinstance(msg.get("text"), str) or msg["text"].strip() != phrase:
            continue
        chat = msg.get("chat", {})
        if isinstance(chat, dict) and type(chat.get("id")) is int:
            ids.add(str(chat["id"]))
    if len(ids) > 1:
        raise SafeError("확인 문장을 서로 다른 방에 보내셨습니다. 다시 실행하고 알림받을 방 한 곳에만 보내주세요.")
    return next(iter(ids)) if ids else None


def easy_credentials(cfg):
    cfg = dict(cfg)
    if cfg["bot_token"] and cfg["chat_id"]:
        require_telegram(cfg)
        return cfg
    if not sys.stdin.isatty():
        raise SafeError("처음 한 번은 Termux 화면에서 직접 실행해 Telegram을 연결해주세요.")
    print("처음 한 번만 Telegram을 연결합니다. 다음부터는 다시 입력하지 않습니다.")
    if not cfg["bot_token"]:
        print("BotFather에서 받은 새 봇 토큰을 붙여넣으세요. 입력 글자가 안 보이는 것은 정상입니다.")
        cfg["bot_token"] = getpass.getpass("봇 토큰: ").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", cfg["bot_token"]):
            raise SafeError("봇 토큰 형식이 맞지 않습니다. 코드를 수정하지 말고 다시 실행해주세요.")
    info = telegram_setup_request(cfg["bot_token"], "getMe")
    if not isinstance(info, dict) or not info.get("is_bot"):
        raise SafeError("Telegram 봇을 확인하지 못했습니다.")
    atomic_write(config_path(), json.dumps(cfg, ensure_ascii=False, indent=2).encode())
    if not cfg["chat_id"]:
        phrase = "/ghj717_" + secrets.token_hex(4)
        username = info.get("username", "")
        if re.fullmatch(r"[A-Za-z0-9_]+", username):
            print("알림받을 Telegram 봇: @" + username)
        print("그 봇 대화방에 아래 한 줄을 보내주세요. 수신방 번호는 직접 찾지 않아도 됩니다.")
        print(phrase)
        for _ in range(3):
            input("보냈으면 여기로 돌아와 Enter: ")
            cfg["chat_id"] = chat_from_confirmation(telegram_setup_request(cfg["bot_token"], "getUpdates"), phrase) or ""
            if cfg["chat_id"]:
                break
            print("아직 확인 문장을 찾지 못했습니다. 위 봇에 정확히 보냈는지 확인해주세요.")
        if not cfg["chat_id"]:
            raise SafeError("알림받을 방을 확인하지 못해 멈췄습니다. 기록은 유지됩니다.")
    atomic_write(config_path(), json.dumps(cfg, ensure_ascii=False, indent=2).encode())
    print("Telegram 연결 정보를 패드 안에 저장했습니다.")
    return cfg


def easy_command(args, root, timeout=60, required=True):
    """보조 도구의 출력을 설정 로그에만 기록. 명령에 비밀값을 전달하지 않는다."""
    try:
        result = subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        if required:
            raise SafeError("실행 준비가 끝나지 않았습니다. 기록은 유지됩니다. 화면 안내를 알려주세요.") from None
        return None
    if result.returncode and required:
        atomic_write(root / "logs" / "setup-error.log", result.stdout + result.stderr)
        raise SafeError("실행 준비 중 문제가 생겼습니다. 기록은 유지됩니다. 화면 안내를 알려주세요.")
    return result


def service_running(prefix, service, root):
    if not service.exists() or not (prefix / "bin/sv").exists():
        return False
    result = easy_command([prefix / "bin/sv", "status", service], root, required=False)
    return bool(result and result.returncode == 0 and result.stdout.startswith(b"run:"))


def service_control(prefix, service, action, root):
    return easy_command([prefix / "bin/sv", "-w", "65", action, service], root, timeout=70)


def stop_service(prefix, service, root):
    if service.exists():
        atomic_write(service / "down", b"")
        # 재시작 대기 중에도 runsv의 희망 상태를 down으로 바꿔야 다시 살아나지 않는다.
        if (prefix / "bin/sv").exists() and (service / "supervise/ok").exists():
            service_control(prefix, service, "down", root)


def ensure_legacy_stopped(source, target, home):
    # 정확히 해당 알리미 파일을 실행하는 다른 Python만 찾으며, 임의로 종료하지 않는다.
    paths = {Path(source).resolve(), Path(target).resolve(), (Path(home)/"ghj717.py").resolve()}
    for proc in Path("/proc").glob("[0-9]*"):
        if proc.name == str(os.getpid()):
            continue
        try:
            args = (proc / "cmdline").read_bytes().decode(errors="replace").split("\0")
            if not args or not Path(args[0]).name.startswith("python"):
                continue
            cwd = (proc / "cwd").resolve(strict=True)
            for arg in args[1:]:
                if arg.endswith(".py") and (cwd / arg).resolve() in paths:
                    raise SafeError("기존 알리미가 다른 화면에서 실행 중입니다. 그 화면에서 Ctrl+C를 누른 뒤 다시 실행해주세요.")
        except (OSError, RuntimeError):
            continue


def easy_prepare_service(prefix, service, target, root):
    q = shlex.quote
    shell = prefix / "bin/sh"
    # 시작 명령은 Python 하나. 기존 버전의 watch와도 호환되어 코드 복구가 가능하다.
    runner = f"#!{shell}\nexec 2>&1\numask 077\n"
    runner += f"export HOME={q(str(Path.home()))}\nexport PREFIX={q(str(prefix))}\n"
    runner += f"export GHJ717_DATA_DIR={q(str(root))}\nexport GHJ717_CONFIG={q(str(config_path()))}\nexport GHJ717_SERVICE=1\n"
    runner += f"cd {q(str(target.parent))}\nexec {q(sys.executable)} -u {q(str(target))} watch\n"
    stage = service if service.exists() else prefix / "var" / (".ghj717-" + str(time.time_ns()))
    atomic_write(stage / "down", b"")
    atomic_write(stage / "run", runner.encode())
    atomic_write(stage / "finish", f"#!{shell}\nsleep 15\n".encode())
    logdir = root / "service-log"
    atomic_write(logdir / "config", b"s1000000\nn5\n")
    atomic_write(stage / "log/run", f"#!{shell}\nexec {q(str(prefix/'bin/svlogd'))} -tt {q(str(logdir))}\n".encode())
    for path in (stage / "run", stage / "finish", stage / "log/run"):
        path.chmod(0o700)
    if stage != service:
        service.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, service)
    boot = f"#!{shell}\nexport PREFIX={q(str(prefix))}\n{q(str(prefix/'bin/termux-wake-lock'))}\n. {q(str(prefix/'etc/profile.d/start-services.sh'))}\n"
    boot_path = Path.home() / ".termux/boot/ghj717-services"
    atomic_write(boot_path, boot.encode())
    boot_path.chmod(0o700)


def easy_dependencies(prefix, root):
    required = [prefix / "bin/sv", prefix / "bin/svlogd", prefix / "bin/curl",
                prefix / "etc/profile.d/start-services.sh"]
    if not all(p.exists() for p in required):
        print("자동 재시작에 필요한 도구를 준비합니다. 별도 명령을 입력하지 않아도 됩니다.")
        easy_command([prefix / "bin/pkg", "install", "-y", "curl", "termux-services"], root, timeout=300)
    if not all(p.exists() for p in required):
        raise SafeError("자동 재시작 도구 설치가 완료되지 않았습니다. 이 안내를 알려주세요.")
    easy_command([prefix / "bin/sh", "-c", '. "$PREFIX/etc/profile.d/start-services.sh"'], root)


def easy_start_service(prefix, service, root):
    # runsvdir의 디렉터리 재조회가 끝나기 전에 sv를 호출하지 않는다.
    for _ in range(40):
        if (service / "supervise/ok").exists():
            break
        time.sleep(.25)
    else:
        raise SafeError("실행 준비가 아직 끝나지 않았습니다. Termux를 다시 연 뒤 같은 명령을 입력해주세요.")
    (service / "down").unlink(missing_ok=True)
    service_control(prefix, service, "up", root)


def runtime_started(root, since):
    with contextlib.closing(connect_db(root / "ghj717.sqlite3", readonly=True)) as db:
        return float(meta(db, "last_tick")) >= since


def easy_show_log(root):
    path = root / "logs/ghj717.log"
    offset, inode = 0, None
    if path.exists():
        stat = path.stat()
        offset, inode = stat.st_size, stat.st_ino
    print("알리미가 실행 중입니다. 이전과 같이 Ctrl+C를 누르면 감시가 멈춥니다.")
    while True:
        try:
            stat = path.stat()
            if inode != stat.st_ino or stat.st_size < offset:
                offset = 0
            inode = stat.st_ino
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read(100_000)
                offset = stream.tell()
            if chunk:
                print(chunk.decode("utf-8", errors="replace"), end="", flush=True)
        except FileNotFoundError:
            pass
        time.sleep(1)


def easy_main():
    source, home, root = Path(__file__).resolve(), Path.home(), data_dir()
    prefix = Path(os.environ.get("PREFIX", "/not-termux"))
    if sys.platform != "linux" or not (prefix / "bin/pkg").is_file():
        raise SafeError("이 파일을 기존처럼 패드의 Termux에서 실행해주세요. 컴퓨터에서는 감시를 시작하지 않습니다.")
    if root != (home / ".local/share/ghj717").resolve() or config_path() != (home / ".config/ghj717/config.json").resolve():
        raise SafeError("기록 저장 경로가 별도로 설정돼 있습니다. 기존 기록을 보호하려고 멈췄습니다.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = home / ".local/opt/ghj717/ghj717.py"
    service = prefix / "var/service/ghj717"
    with FileLock(root / "launcher"):
        # 이미 설치된 v2의 자동 실행 작업도 먼저 중지한다.
        stop_service(prefix, service, root)
        ensure_legacy_stopped(source, target, home)
        easy_state(root, source, home, Path.cwd())
        cfg = easy_credentials(load_config())
        setup_logging(root, cfg)
        easy_dependencies(prefix, root)
        with FileLock(root), contextlib.closing(connect_db(root / "ghj717.sqlite3")) as db:
            backup_db(db, root, cfg["backup_keep"])
            # 설정 성공 메시지는 한 번만 대기열에 넣는다.
            with db:
                if not meta(db, "easy_welcome", ""):
                    db.execute("INSERT INTO deliveries(kind,body,created) VALUES('test',?,?)",
                               (f"✅ 알리미가 켜졌습니다. 감시 대상 {len(MEMBERS)}명(HA2햄 포함). 기존 기록을 이어서 감시합니다. 새 글 확인 결과는 별도로 알려드립니다.", time.time()))
                    put_meta(db, "easy_welcome", 1)
        current_bytes = target.read_bytes() if target.exists() else None
        incoming = source.read_bytes()
        if current_bytes is not None and current_bytes != incoming:
            atomic_write(target.parent / "previous.py", current_bytes)
        if current_bytes != incoming:
            atomic_write(target, incoming)
        easy_prepare_service(prefix, service, target, root)
        # 기존 Termux:Boot 앱이 있으면 새 스크립트를 사용한다. 앱 설치를 완료했다고 가정하지 않는다.
        easy_command([prefix / "bin/termux-wake-lock"], root, required=False)
        started = time.time()
        active = False
        try:
            active = True
            easy_start_service(prefix, service, root)
            for _ in range(30):
                if runtime_started(root, started):
                    break
                time.sleep(1)
            else:
                raise SafeError("알리미가 정상적으로 시작하지 못했습니다. 기록은 유지됩니다. 화면 안내를 알려주세요.")
            print(f"간편판 {VERSION} · {len(MEMBERS)}명 감시 · HA2햄 포함 · 뽀삐햄·노라무 우선순위 유지")
            print("기록 보존·로그·프로그램 자동 재시작이 준비됐습니다.")
            if not cfg["hc_process_url"] or not cfg["hc_health_url"]:
                print("패드 전원 종료를 알려주는 외부 경보는 아직 연결되지 않았습니다.")
            print("재부팅 후 자동 시작에는 Termux:Boot와 Android 배터리 설정이 필요합니다.")
            easy_show_log(root)
        except KeyboardInterrupt:
            print("\n알리미를 멈춥니다. 잠시 기다려주세요.")
        except Exception:
            stop_service(prefix, service, root)
            active = False
            if current_bytes is not None and current_bytes != incoming:
                atomic_write(target, current_bytes)
                print("이전 실행 파일을 보존한 상태로 되돌렸습니다. 감시는 중지 상태입니다.")
            raise
        finally:
            if active:
                stop_service(prefix, service, root)
        print("감시를 멈췄습니다. 다음에도 같은 명령으로 실행하면 됩니다.")
    return 0


def main(argv=None):
    os.umask(0o077)
    incoming = list(sys.argv[1:] if argv is None else argv)
    if incoming in ([], ["watch"]) and os.environ.get("GHJ717_SERVICE") != "1":
        return easy_main()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("watch", "status", "configure", "doctor", "backup", "test-telegram", "retry-telegram"):
        sub.add_parser(name)
    p = sub.add_parser("init")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--legacy-state", type=Path)
    group.add_argument("--fresh", action="store_true")
    p.add_argument("--baseline-missing", action="store_true")
    p = sub.add_parser("restore")
    p.add_argument("backup", type=Path)
    p.add_argument("--accept-possible-duplicates", action="store_true", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--member", default="뽀삐햄")
    p = sub.add_parser("retry-scan")
    p.add_argument("--member", required=True)
    p = sub.add_parser("accept-gap")
    p.add_argument("--member", required=True)
    p.add_argument("--accept-unrecoverable-gap", action="store_true", required=True)
    args = ap.parse_args(argv)
    root = data_dir()
    if args.command == "configure":
        configure()
        return 0
    cfg = load_config()
    setup_logging(root, cfg)
    if args.command == "doctor":
        fetcher = Fetcher(cfg)
        print(json.dumps({"version":VERSION,"python":sys.version.split()[0],"transport":fetcher.mode,
            "members":len(MEMBERS),"priority":sorted(PRIORITY),"slots":len(build_schedule()),
            "data_directory":str(root),"db_exists":(root/"ghj717.sqlite3").exists(),
            "telegram_configured":bool(cfg["bot_token"] and cfg["chat_id"]),
            "process_monitor_configured":bool(cfg["hc_process_url"]),
            "health_monitor_configured":bool(cfg["hc_health_url"])}, ensure_ascii=False,indent=2))
        return 0
    if args.command in ("status", "backup"):
        with contextlib.closing(connect_db(root/"ghj717.sqlite3", readonly=True)) as db:
            if args.command == "status":
                print(json.dumps(status_data(db,cfg,time.time()), ensure_ascii=False,indent=2))
            else:
                print(backup_db(db,root,cfg["backup_keep"]))
        return 0
    with FileLock(root):
        if args.command == "init":
            initialize(root,args.legacy_state,args.fresh,args.baseline_missing)
            with contextlib.closing(connect_db(root/"ghj717.sqlite3")) as db:
                print("백업:",backup_db(db,root,cfg["backup_keep"]))
            return 0
        if args.command == "restore":
            restore_db(root,args.backup)
            return 0
        if args.command == "probe":
            name, mid, sid = selected_member(args.member)
            fetcher = Fetcher(cfg)
            posts = fetcher.fetch(mid,sid,1)
            print(f"실제 목록 확인: {name}, {len(posts)}건, transport={fetcher.mode}. 상태/대기열/Telegram 변경 없음.")
            return 0
        with contextlib.closing(connect_db(root/"ghj717.sqlite3")) as db:
            if args.command == "watch":
                run_watch(db,root,cfg)
            elif args.command == "test-telegram":
                require_telegram(cfg)
                with db:
                    db.execute("INSERT INTO deliveries(kind,body,created) VALUES('test',?,?)",
                               (f"✅ ghj717 {VERSION} 전송 점검 · {len(MEMBERS)}명 / 뽀삐햄·노라무 우선순위 3",time.time()))
                print("점검 메시지를 저장했습니다. watch 실행 시 실제 전송됩니다.")
            elif args.command == "retry-telegram":
                with db:
                    put_meta(db,"telegram_block","")
                    put_meta(db,"telegram_error","")
                    put_meta(db,"telegram_until",0)
                    db.execute("UPDATE deliveries SET status='pending',due=0 WHERE status='blocked'")
                print("전송 차단 해제. watch 실행 시 보관 중인 메시지를 재시도합니다.")
            elif args.command in ("retry-scan", "accept-gap"):
                _, _, sid = selected_member(args.member)
                if args.command == "accept-gap":
                    print("⚠ 확인하지 못한 구간을 승인했습니다. 이미 발견한 글은 보존/전송합니다.")
                with db:
                    if args.command == "accept-gap":
                        db.execute("UPDATE members SET cursor=MAX(cursor,scan_head) WHERE sid=?", (sid,))
                        put_meta(db,"accepted_gap_"+sid,time.time())
                    # 재수집 명령은 경계를 아직 확인하지 않았으므로 미전송 글을 계속 보류한다.
                    page = 0 if args.command == "accept-gap" else 1
                    db.execute("UPDATE members SET scan_page=?,scan_head=0,fingerprint='',gap='' WHERE sid=?",(page,sid))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        # 예외의 원문에는 토큰 포함 URL이 있을 수 있으므로 외부 예외는 종류만 출력.
        message = str(exc) if isinstance(exc, SafeError) else type(exc).__name__
        LOG.error("중단: %s", message)
        if not LOG.handlers:
            print("중단: " + message, file=sys.stderr)
        raise SystemExit(1)
