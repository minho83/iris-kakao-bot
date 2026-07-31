"""
iris-kakao-bot — 파티 전용 슬림 버전
- 닉네임/거래/검색/공지/RAG/대시보드 전부 제거
- 파티 수집 + !파티 조회 + !파티설정 관리자 명령만 유지
- !파티 는 외부 링크 대신 wikibot(party.db)을 직접 조회해서 응답
"""
import os
import re
import json
import time
import random
import logging
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Iris(redroid) reply 엔드포인트
IRIS_URL = os.getenv('IRIS_URL', 'http://localhost:3000')
# wikibot(Node) 주소 — 파티 API 제공
WIKIBOT_URL = os.getenv('WIKIBOT_URL', 'http://localhost:8214')
# wikibot 관리 API(파티방 추가/제거) 인증용 서비스 토큰 = wikibot의 ADMIN_PASSWORD
WIKIBOT_TOKEN = os.getenv('ADMIN_PASSWORD', '')

# milddok.cc 파티 매칭 게시판 봇 API (functions/api/match/bot/parties)
MATCH_API_URL = os.getenv('MATCH_API_URL', 'https://milddok.cc/api/match/bot')
MATCH_BOT_KEY = os.getenv('MATCH_BOT_KEY', '')
MATCH_DEFAULT_ZONE = os.getenv('MATCH_DEFAULT_ZONE', '나겔링')
MATCH_DEFAULT_SERVER = os.getenv('MATCH_DEFAULT_SERVER', 'seo')
MATCH_WEB_URL = 'https://milddok.cc/match/'

# 방별 기능 토글 — BOT_OWNER가 '!<기능>사용'/'!<기능>해제'로 방마다 켜고 끈다.
BOT_OWNER = os.getenv('BOT_OWNER', '밀떡밀떡')
FEATURES = ('파티봇', '현자', '업데이트', '도움말')
FEATURES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'room_features.json')
TOGGLE_RE = re.compile(r'^!(파티봇|현자|업데이트|도움말)\s*(사용|해제)$')

# wikibot 검색(/ask/*)은 자체 rate limit이 있어 호출 간격을 띄운다
WIKIBOT_ASK_DELAY = 3.5
_last_ask_time = 0.0

KST = timezone(timedelta(hours=9))
MATCH_JOB_LABEL = {'warrior': '전사', 'rogue': '도적', 'mage': '법사', 'cleric': '직자', 'taoist': '도가'}
MATCH_SERVER_LABEL = {'seo': '세오', 'shus': '셔스'}


def _admin_headers():
    return {"Authorization": f"Bearer {WIKIBOT_TOKEN}"} if WIKIBOT_TOKEN else {}


# ── 유틸 ─────────────────────────────────────────────────
def send_reply(chat_id, message):
    """Iris를 통해 채팅방에 메시지 전송"""
    try:
        payload = {"type": "text", "room": str(chat_id), "data": message}
        resp = requests.post(f"{IRIS_URL}/reply", json=payload, timeout=5)
        logger.info(f"Reply -> {chat_id}: {resp.status_code}")
    except Exception as e:
        logger.error(f"Reply 전송 오류: {e}")


# ── 방별 기능 토글 ────────────────────────────────────────
def _load_features():
    try:
        with open(FEATURES_FILE, encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_features(data):
    try:
        with open(FEATURES_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error(f"기능 설정 저장 오류: {e}")


def feature_enabled(chat_id, feature):
    return bool(_load_features().get(str(chat_id), {}).get(feature))


def handle_feature_toggle(feature, action, chat_id):
    """'!<기능>사용'/'!<기능>해제' 처리 (호출 전 BOT_OWNER 확인 필수)."""
    data = _load_features()
    room = data.setdefault(str(chat_id), {})
    room[feature] = (action == '사용')
    _save_features(data)
    return f"[{feature}] 기능을 {'켰습니다' if room[feature] else '껐습니다'}."


def handle_feature_status(chat_id):
    """'!기능' — 이 방의 토글 상태 (BOT_OWNER 전용)."""
    room = _load_features().get(str(chat_id), {})
    status = "\n".join(f"- {f}: {'켜짐' if room.get(f) else '꺼짐'}" for f in FEATURES)
    return f"[이 방의 기능 설정]\n{status}\n\n켜기: !현자사용 · 끄기: !현자해제"


# ── wikibot 검색 (!현자 / !업데이트) ──────────────────────
def ask_wikibot(endpoint, query='', max_length=500):
    """wikibot /ask/* 호출 (rate limit 간격 유지)"""
    global _last_ask_time
    try:
        wait = WIKIBOT_ASK_DELAY - (time.time() - _last_ask_time)
        if wait > 0:
            time.sleep(wait)
        _last_ask_time = time.time()
        resp = requests.post(f"{WIKIBOT_URL}{endpoint}",
                             json={"query": query, "max_length": max_length}, timeout=30)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"wikibot 통신 오류: {e}")
    return None


def format_wiki_answer(result, empty_msg="검색 결과가 없습니다."):
    """wikibot 응답({success, data:{title,date,content,link}} 또는 {answer})을 메시지로"""
    if result is None:
        return "서버 연결에 실패했습니다. 잠시 후 다시 시도하세요."
    if not result.get('success'):
        return result.get('answer') or result.get('message') or empty_msg
    d = result.get('data') or {}
    lines = []
    if d.get('title'):
        head = f"[{d['title']}]"
        if d.get('date'):
            head += f" ({d['date']})"
        lines.append(head)
    content = (d.get('content') or '').strip()
    if content:
        if len(content) > 800:
            content = content[:800] + '…'
        lines.append(content)
    if d.get('link'):
        lines.append(d['link'])
    if not lines:
        return result.get('answer') or empty_msg
    return "\n\n".join(lines)


def handle_hyunja(msg):
    query = msg[len('!현자'):].strip()
    if not query:
        return "검색어를 입력해주세요. 예: !현자 발록"
    return format_wiki_answer(ask_wikibot('/ask/community', query))


def handle_update(msg):
    query = msg[len('!업데이트'):].strip()
    return format_wiki_answer(ask_wikibot('/ask/update', query), "업데이트 정보가 없습니다.")


def handle_help(chat_id):
    room = _load_features().get(str(chat_id), {})
    lines = ["[밀떡봇 도움말]"]
    if room.get('파티봇'):
        lines.append("!파티결성 [파티명] [인원] — 파티 게시판에 등록")
        lines.append("!파티확인 — 모집 중인 파티 목록")
    if room.get('현자'):
        lines.append("!현자 [검색어] — 현자 게시판 검색")
    if room.get('업데이트'):
        lines.append("!업데이트 — 최신 업데이트 확인")
    if len(lines) == 1:
        return "이 방에서 사용할 수 있는 기능이 없습니다."
    lines.append(f"\n파티 게시판: {MATCH_WEB_URL}")
    return "\n".join(lines)


# ── 파티 매칭 게시판(milddok.cc/match) 연동 ───────────────
def _match_headers():
    return {"X-Bot-Key": MATCH_BOT_KEY}


def _fmt_party_time(ms):
    """epoch ms → '오늘 21:00' / '내일 09:00' / '8/3 21:00' (KST)"""
    dt = datetime.fromtimestamp(ms / 1000, KST)
    today = datetime.now(KST).date()
    if dt.date() == today:
        day = "오늘"
    elif dt.date() == today + timedelta(days=1):
        day = "내일"
    else:
        day = f"{dt.month}/{dt.day}"
    return f"{day} {dt:%H:%M}"


def _fmt_duration(minutes):
    return f"{minutes // 60}시간" if minutes % 60 == 0 else f"{minutes}분"


def handle_match_create(msg, sender):
    """!파티결성 [파티명] [인원] → 게시판에 파티 등록.

    구역/서버/시작시간은 기본값을 쓴다:
    구역 MATCH_DEFAULT_ZONE · 서버 MATCH_DEFAULT_SERVER · 시작 다음 정각(최소 1시간 뒤) · 1시간 사냥.
    직업별 인원은 파티장(전사) 1 + 나머지를 도적→법사→직자→도가→전사 순환 배분.
    """
    usage = ("사용법: !파티결성 [파티명] [인원]\n"
             "예) !파티결성 발록가실분 4\n"
             f"기본값: {MATCH_DEFAULT_ZONE} · {MATCH_SERVER_LABEL.get(MATCH_DEFAULT_SERVER)} · 다음 정각 시작 · 1시간")
    parts = msg.split()[1:]
    if not parts or not parts[-1].isdigit():
        return usage
    total = int(parts[-1])
    if not 2 <= total <= 12:
        return "인원은 2~12명 사이로 입력하세요."
    title = " ".join(parts[:-1])[:40]

    nick = (sender.split('/')[0].strip() if '/' in sender else sender).strip()[:16] or "익명"

    # 시작시간: 지금+1시간을 다음 정각으로 올림 (예: 12:10 → 14:00, 12:00 → 13:00)
    now = datetime.now(KST)
    base = now.replace(minute=0, second=0, microsecond=0)
    start = base + timedelta(hours=1 if now == base else 2)
    party_at = int(start.timestamp() * 1000)

    # 직업 배분: 파티장(전사) 1자리 + 나머지 순환
    caps = {"warrior": 1}
    for i, _ in enumerate(range(total - 1)):
        job = ["rogue", "mage", "cleric", "taoist", "warrior"][i % 5]
        caps[job] = caps.get(job, 0) + 1

    password = f"{random.randint(0, 9999):04d}"
    payload = {
        "zone_name": MATCH_DEFAULT_ZONE,
        "server": MATCH_DEFAULT_SERVER,
        "creator_nick": nick,
        "creator_job": "warrior",
        "title": title or None,
        "party_at": party_at,
        "duration_min": 60,
        "password": password,
        "caps": caps,
    }
    try:
        resp = requests.post(f"{MATCH_API_URL}/parties", json=payload,
                             headers=_match_headers(), timeout=10)
        data = resp.json()
    except Exception as e:
        logger.error(f"파티 등록 API 오류: {e}")
        return "파티 등록 중 오류가 발생했습니다. 잠시 후 다시 시도하세요."
    if not data.get("ok"):
        return f"파티 등록 실패: {data.get('error', '알 수 없는 오류')}"

    jobs_txt = " ".join(f"{MATCH_JOB_LABEL[j]}{c}" for j, c in caps.items())
    return ("[파티 등록 완료]\n"
            f"구역: {data.get('zone', MATCH_DEFAULT_ZONE)} ({MATCH_SERVER_LABEL.get(MATCH_DEFAULT_SERVER)})\n"
            + (f"파티명: {title}\n" if title else "")
            + f"파티장: {nick}(전사)\n"
            f"시작: {_fmt_party_time(party_at)} · 1시간\n"
            f"모집: {jobs_txt} (총 {total}명)\n"
            f"수정/삭제 암호: {password}\n"
            f"{MATCH_WEB_URL}")


def handle_match_check():
    """!파티확인 → 게시판의 현재 파티 목록 요약."""
    try:
        resp = requests.get(f"{MATCH_API_URL}/parties",
                            params={"server": MATCH_DEFAULT_SERVER},
                            headers=_match_headers(), timeout=10)
        data = resp.json()
    except Exception as e:
        logger.error(f"파티 목록 API 오류: {e}")
        return "파티 목록 조회 중 오류가 발생했습니다. 잠시 후 다시 시도하세요."
    if not data.get("ok"):
        return f"파티 목록 조회 실패: {data.get('error', '알 수 없는 오류')}"

    parties = data.get("parties", [])
    if not parties:
        return f"현재 모집 중인 파티가 없습니다.\n{MATCH_WEB_URL}"

    lines = [f"[파티 목록 - {MATCH_SERVER_LABEL.get(MATCH_DEFAULT_SERVER)}] {len(parties)}건"]
    for i, p in enumerate(parties, 1):
        title = p.get("title") or "(파티명 없음)"
        lines.append(f"\n{i}. {p['zone']} | {title}")
        lines.append(f"   {_fmt_party_time(p['party_at'])} · {_fmt_duration(p['duration_min'])}"
                     f" · 파티장 {p['creator_nick']}({MATCH_JOB_LABEL.get(p['creator_job'], '?')})")
        open_seats = []
        for jb in p.get("jobs", []):
            remain = jb["capacity"] - len(jb.get("applicants", []))
            if remain > 0:
                open_seats.append(f"{MATCH_JOB_LABEL.get(jb['job'], '?')}{remain}")
        lines.append(f"   빈자리: {' '.join(open_seats) if open_seats else '없음 (마감)'}")
    lines.append(f"\n{MATCH_WEB_URL}")
    return "\n".join(lines)


# ── 파티방 설정 캐시 ──────────────────────────────────────
_party_room_cache = {}
_party_room_cache_time = 0
ROOM_CACHE_TTL = 300  # 5분


def check_party_room(chat_id, msg='', sender=''):
    """파티방 설정 조회(캐시). 반환: {'collect': bool} 또는 None(미등록)

    캐시 미스 시에만 room-check 호출 → 미등록 방 자동발견용 샘플(msg/sender)을
    함께 전달한다(5분당 1회). wikibot은 미등록 방만 seen_rooms에 기록한다.
    """
    global _party_room_cache, _party_room_cache_time
    now = time.time()
    if now - _party_room_cache_time > ROOM_CACHE_TTL:
        _party_room_cache = {}
        _party_room_cache_time = now
    if chat_id in _party_room_cache:
        return _party_room_cache[chat_id]
    try:
        resp = requests.post(
            f"{WIKIBOT_URL}/api/party/room-check",
            json={"room_id": chat_id, "msg": msg, "sender": sender}, timeout=5)
        data = resp.json()
        if not data.get("success"):
            return None
        room = data.get("room")
        _party_room_cache[chat_id] = room
        return room
    except Exception:
        return None


def collect_party_message(msg, sender, chat_id):
    """파티방 일반 메시지를 wikibot에 전달하여 수집"""
    try:
        sender_name = sender.split('/')[0].strip() if '/' in sender else sender
        requests.post(
            f"{WIKIBOT_URL}/api/party/collect",
            json={"message": msg, "sender_name": sender_name, "room_id": chat_id},
            timeout=5)
    except Exception as e:
        logger.error(f"파티 수집 오류: {e}")


def handle_party_setting(msg, sender_id):
    """!파티설정 추가/수집/제거/목록 (관리자)"""
    global _party_room_cache
    if msg.startswith("!파티설정 추가") or msg.startswith("!파티설정 수집"):
        is_collect = msg.startswith("!파티설정 수집")
        parts = msg.split()
        if len(parts) < 3:
            return "사용법: !파티설정 추가/수집 [room_id] [방이름(선택)]\n\n추가: 조회만 / 수집: 자동수집+조회"
        target_room = parts[2]
        room_name = " ".join(parts[3:]) if len(parts) > 3 else ""
        try:
            resp = requests.post(
                f"{WIKIBOT_URL}/api/party/rooms",
                json={"admin_id": sender_id, "room_id": target_room,
                      "room_name": room_name, "collect": is_collect},
                headers=_admin_headers(), timeout=5)
            _party_room_cache.clear()
            return resp.json().get("message", "처리 완료")
        except Exception as e:
            logger.error(f"파티 방 추가 오류: {e}")
            return "파티 방 추가 중 오류가 발생했습니다."

    if msg.startswith("!파티설정 제거"):
        parts = msg.split()
        if len(parts) < 3:
            return "사용법: !파티설정 제거 [room_id]"
        try:
            resp = requests.delete(
                f"{WIKIBOT_URL}/api/party/rooms/{parts[2]}",
                json={"admin_id": sender_id},
                headers=_admin_headers(), timeout=5)
            _party_room_cache.clear()
            return resp.json().get("message", "처리 완료")
        except Exception as e:
            logger.error(f"파티 방 제거 오류: {e}")
            return "파티 방 제거 중 오류가 발생했습니다."

    if msg.startswith("!파티설정 목록"):
        try:
            resp = requests.get(
                f"{WIKIBOT_URL}/api/party/rooms",
                params={"admin_id": sender_id}, timeout=5)
            rooms = resp.json().get("rooms", [])
            if not rooms:
                return "설정된 파티 방이 없습니다."
            lines = ["[파티 방 목록]"]
            for r in rooms:
                mode = "수집+조회" if r.get("collect") else "조회만"
                name = r.get("room_name") or r.get("room_id")
                lines.append(f"- {name} ({r.get('room_id')}) [{mode}]")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"파티 방 목록 오류: {e}")
            return "파티 방 목록 조회 중 오류가 발생했습니다."

    return "사용법:\n!파티설정 추가/수집 [room_id] [방이름]\n!파티설정 제거 [room_id]\n!파티설정 목록"


# ── 엔드포인트 ───────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy"})


@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        logger.info(f"받은 데이터: {data}")

        msg = data.get('msg', '')
        room = data.get('room', '')
        sender = data.get('sender', '')
        json_info = data.get('json', {})
        msg_type = str(json_info.get('type', '1'))
        chat_id = str(json_info.get('chat_id', room))
        user_id = str(json_info.get('user_id', ''))

        # 원본 행의 v(JSON 문자열) 파싱 → 봇 자기메시지 판별용 isMine 추출
        v_raw = json_info.get('v')
        try:
            v = json.loads(v_raw) if isinstance(v_raw, str) else (v_raw or {})
        except (ValueError, TypeError):
            v = {}

        # 시스템 메시지(type 0) / 빈 발신자 / 봇 자신(isMine) 무시
        if msg_type == '0' or not sender or v.get('isMine'):
            return jsonify({"status": "ok"})

        msg_stripped = msg.strip()

        # 방 ID 확인용 (수집방 등록 시 필요)
        if msg_stripped == "!방확인":
            send_reply(chat_id, f"[방 정보]\nroom: {room}\nchat_id: {chat_id}\nsender: {sender}")
            return jsonify({"status": "ok"})

        # 파티방 설정 (관리자)
        if msg_stripped.startswith("!파티설정"):
            send_reply(chat_id, handle_party_setting(msg_stripped, user_id))
            return jsonify({"status": "ok"})

        sender_name = sender.split('/')[0].strip() if '/' in sender else sender.strip()

        # 방별 기능 토글 (BOT_OWNER 전용, 다른 사용자는 무응답)
        toggle_match = TOGGLE_RE.match(msg_stripped)
        if toggle_match:
            if sender_name == BOT_OWNER:
                send_reply(chat_id, handle_feature_toggle(
                    toggle_match.group(1), toggle_match.group(2), chat_id))
            return jsonify({"status": "ok"})
        if msg_stripped == "!기능":
            if sender_name == BOT_OWNER:
                send_reply(chat_id, handle_feature_status(chat_id))
            return jsonify({"status": "ok"})

        # 기능이 켜진 방에서만 동작하는 명령들
        if msg_stripped.startswith("!파티결성") and feature_enabled(chat_id, '파티봇'):
            send_reply(chat_id, handle_match_create(msg_stripped, sender))
            return jsonify({"status": "ok"})
        if (msg_stripped == "!파티확인" or msg_stripped.startswith("!파티확인 ")) \
                and feature_enabled(chat_id, '파티봇'):
            send_reply(chat_id, handle_match_check())
            return jsonify({"status": "ok"})
        if msg_stripped.startswith("!현자") and feature_enabled(chat_id, '현자'):
            send_reply(chat_id, handle_hyunja(msg_stripped))
            return jsonify({"status": "ok"})
        if msg_stripped.startswith("!업데이트") and feature_enabled(chat_id, '업데이트'):
            send_reply(chat_id, handle_update(msg_stripped))
            return jsonify({"status": "ok"})
        if msg_stripped == "!도움말" and feature_enabled(chat_id, '도움말'):
            send_reply(chat_id, handle_help(chat_id))
            return jsonify({"status": "ok"})

        # 파티방 설정 조회 (수집 여부만) + 미등록 방 자동발견용 샘플 전달
        party_room = check_party_room(chat_id, msg, sender)
        is_party_collect_room = bool(party_room and party_room.get('collect'))

        # 수집방: 일반 메시지(명령어 아님) 자동 수집
        # 조회는 카톡 명령(!파티) 없이 웹 뷰어(party.milddok.cc)로만 제공.
        if is_party_collect_room and not msg_stripped.startswith('!'):
            collect_party_message(msg, sender, chat_id)
            return jsonify({"status": "ok"})

        return jsonify({"status": "ok"})

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
