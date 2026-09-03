"""
방 공지 — milddok.cc /notice-admin/ 에서 정한 글을 뿌린다.

두 가지:
  - join: 방에 누가 들어올 때 (카톡 피드 feedType 4)
  - time: 정한 시각(KST)에, 고른 요일에만

공지 목록은 사이트 `GET /api/notices`(X-Bot-Key)에서 1분마다 받아 온다.
문구를 고치려고 여기 들어와 재시작하지 않게 하는 것이 이 구조의 이유다.
사이트 쪽 계약은 lod_tools `tests/bot-notices.test.js` — 모양이 바뀌면 여기가 깨진다.

- `{이름}` → 들어온 사람, `{방}` → 방 이름 (관리 페이지에 적은 이름, 없으면 카톡 방 이름)
- 같은 사람이 30분 안에 들락날락하면 한 번만 인사한다 (한 사람이 하루 13번 들어온 적이 있다)
- 시간 공지는 "날짜|id|시각"을 파일에 적어 재시작해도 두 번 안 보낸다
- "지금 보내기"(send_now)는 보낸 뒤 `POST /api/notices {id}`로 되돌린다.
  되돌리기가 실패해도 이 프로세스 안에서는 다시 안 보낸다 — 실패했다고 방을 도배하면 안 된다.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
POLL_SEC = 60          # 목록을 다시 받는 간격
TICK_SEC = 15          # 시각을 확인하는 간격 (1분 안에 반드시 한 번은 본다)
JOIN_COOLDOWN_SEC = 30 * 60
ROOMS_PUSH_SEC = 5 * 60  # 방 목록을 사이트에 올리는 간격 (새 방은 바로)
STATE_KEEP_DAYS = 2


def render(text, name='', room=''):
    return (text or '').replace('{이름}', name or '').replace('{방}', room or '')


class Notices:
    def __init__(self, api_url, bot_key, send, state_file):
        self.api_url = api_url
        self.headers = {"X-Bot-Key": bot_key}
        self.send = send                    # send(chat_id, text)
        self.state_file = state_file
        self.items = []
        self.fetched_at = 0.0
        self.lock = threading.Lock()
        self.sent = self._load_state()      # {"YYYY-MM-DD|id|HH:MM": epoch}
        self.join_seen = {}                 # (chat_id, user_id) -> epoch
        self.now_done = set()               # 이 프로세스가 이미 보낸 send_now id
        # 봇이 본 방 — 관리 화면에서 고르게 하려고 사이트에 올린다. 17자리 ID를 외울 사람은 없다.
        self.rooms_file = os.path.join(os.path.dirname(state_file), 'rooms.json')
        self.rooms = self._load_rooms()     # {chat_id: {name, last_seen(ms), count}}
        self.rooms_dirty = True             # 시작하면 한 번은 올린다
        self.rooms_pushed_at = 0.0

    # ── 방 목록 ──────────────────────────────────────────
    def _load_rooms(self):
        try:
            with open(self.rooms_file, encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def note_room(self, chat_id, name):
        """웹훅마다 부른다. 새 방이면 다음 회차에 바로 올린다."""
        cid = str(chat_id or '')
        if not cid.isdigit():
            return
        with self.lock:
            r = self.rooms.get(cid)
            fresh = r is None
            if fresh:
                r = self.rooms[cid] = {'name': '', 'last_seen': 0, 'count': 0}
            if name:
                r['name'] = str(name)
            r['last_seen'] = int(time.time() * 1000)
            r['count'] = int(r.get('count') or 0) + 1
            if fresh:
                self.rooms_dirty = True

    def push_rooms(self):
        with self.lock:
            payload = [{'chat_id': cid, **r} for cid, r in self.rooms.items()]
        try:
            with open(self.rooms_file, 'w', encoding='utf-8') as f:
                json.dump(self.rooms, f, ensure_ascii=False, indent=1)
        except OSError as e:
            logger.error(f"방 목록 저장 오류: {e}")
        try:
            res = requests.post(f"{self.api_url}/rooms", json={'rooms': payload},
                                headers=self.headers, timeout=8)
            if not res.json().get('ok'):
                logger.error(f"방 목록 올리기 오류: {res.text[:200]}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"방 목록 올리기 실패: {e}")
            return
        self.rooms_dirty = False
        self.rooms_pushed_at = time.time()

    # ── 상태 파일 ────────────────────────────────────────
    def _load_state(self):
        try:
            with open(self.state_file, encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        cutoff = time.time() - STATE_KEEP_DAYS * 86400
        self.sent = {k: v for k, v in self.sent.items() if v >= cutoff}
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.sent, f, ensure_ascii=False, indent=1)
        except OSError as e:
            logger.error(f"공지 상태 저장 오류: {e}")

    # ── 목록 받기 ────────────────────────────────────────
    def refresh(self):
        try:
            res = requests.get(self.api_url, headers=self.headers, timeout=8)
            data = res.json()
        except Exception as e:  # noqa: BLE001
            logger.error(f"공지 목록 받기 실패: {e}")
            return False
        if not data.get('ok'):
            logger.error(f"공지 목록 오류: {data.get('error')}")
            return False
        items = [n for n in (data.get('notices') or []) if isinstance(n, dict)]
        with self.lock:
            self.items = items
            self.fetched_at = time.time()
            # 사이트가 send_now를 0으로 되돌린 것은 기억에서도 지운다 — 다음에 또 누르면 또 보내야 한다.
            live = {n['id'] for n in items if n.get('send_now')}
            self.now_done &= live
        return True

    def _items(self, chat_id=None, kind=None):
        with self.lock:
            items = list(self.items)
        return [n for n in items
                if (chat_id is None or str(n.get('chat_id')) == str(chat_id))
                and (kind is None or n.get('kind') == kind)]

    # ── 입장 ─────────────────────────────────────────────
    def on_join(self, chat_id, room, members):
        """카톡 피드 feedType 4. members = [{userId, nickName}]"""
        notices = self._items(chat_id, 'join')
        if not notices:
            return
        now = time.time()
        for m in members or []:
            uid = str((m or {}).get('userId') or '')
            name = str((m or {}).get('nickName') or '').strip()
            key = (str(chat_id), uid)
            if uid and now - self.join_seen.get(key, 0) < JOIN_COOLDOWN_SEC:
                logger.info(f"입장 공지 생략(쿨다운) {name} @ {chat_id}")
                continue
            self.join_seen[key] = now
            for n in notices:
                self.send(chat_id, render(n.get('text'), name, n.get('room_name') or room))
                logger.info(f"입장 공지 #{n.get('id')} -> {name} @ {chat_id}")
        # 오래된 기록은 버린다
        self.join_seen = {k: v for k, v in self.join_seen.items() if now - v < JOIN_COOLDOWN_SEC}

    # ── 시각 ─────────────────────────────────────────────
    def tick(self):
        now = datetime.now(KST)
        hhmm = now.strftime('%H:%M')
        today = now.strftime('%Y-%m-%d')
        weekday = now.weekday()  # 0=월 … 6=일 (사이트와 같은 규칙)
        dirty = False

        for n in self._items():
            nid = n.get('id')
            chat_id = str(n.get('chat_id') or '')
            if not chat_id:
                continue

            # "지금 보내기" — 문구 확인용. 한 프로세스에서 한 번만.
            if n.get('send_now') and nid not in self.now_done:
                self.now_done.add(nid)
                self.send(chat_id, render(n.get('text'), '', n.get('room_name')))
                logger.info(f"공지 지금 보내기 #{nid} -> {chat_id}")
                self._ack(nid)

            if n.get('kind') != 'time':
                continue
            days = n.get('days') or []
            if days and weekday not in days:
                continue
            if hhmm not in (n.get('times') or []):
                continue
            key = f"{today}|{nid}|{hhmm}"
            if key in self.sent:
                continue
            self.sent[key] = time.time()
            dirty = True
            self.send(chat_id, render(n.get('text'), '', n.get('room_name')))
            logger.info(f"시간 공지 #{nid} {hhmm} -> {chat_id}")

        if dirty:
            self._save_state()

    def _ack(self, nid):
        try:
            requests.post(self.api_url, json={"id": nid}, headers=self.headers, timeout=8)
        except Exception as e:  # noqa: BLE001
            logger.error(f"공지 send_now 되돌리기 실패 #{nid}: {e}")

    # ── 스레드 ───────────────────────────────────────────
    def loop(self):
        while True:
            try:
                if time.time() - self.fetched_at >= POLL_SEC:
                    self.refresh()
                if self.rooms_dirty or time.time() - self.rooms_pushed_at >= ROOMS_PUSH_SEC:
                    self.push_rooms()
                self.tick()
            except Exception as e:  # noqa: BLE001
                logger.error(f"공지 루프 오류: {e}")
            time.sleep(TICK_SEC)

    def start(self):
        t = threading.Thread(target=self.loop, name='notices', daemon=True)
        t.start()
        return t
