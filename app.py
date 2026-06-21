"""
iris-kakao-bot — 파티 전용 슬림 버전
- 닉네임/거래/검색/공지/RAG/대시보드 전부 제거
- 파티 수집 + !파티 조회 + !파티설정 관리자 명령만 유지
- !파티 는 외부 링크 대신 wikibot(party.db)을 직접 조회해서 응답
"""
import os
import time
import logging

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


# ── 유틸 ─────────────────────────────────────────────────
def send_reply(chat_id, message):
    """Iris를 통해 채팅방에 메시지 전송"""
    try:
        payload = {"type": "text", "room": str(chat_id), "data": message}
        resp = requests.post(f"{IRIS_URL}/reply", json=payload, timeout=5)
        logger.info(f"Reply -> {chat_id}: {resp.status_code}")
    except Exception as e:
        logger.error(f"Reply 전송 오류: {e}")


# ── 파티방 설정 캐시 ──────────────────────────────────────
_party_room_cache = {}
_party_room_cache_time = 0
ROOM_CACHE_TTL = 300  # 5분


def check_party_room(chat_id):
    """파티방 설정 조회(캐시). 반환: {'collect': bool} 또는 None(미등록)"""
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
            json={"room_id": chat_id}, timeout=5)
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


def query_party(msg_stripped):
    """!파티 [날짜] [직업] 파싱 후 wikibot 조회 → 응답 문자열"""
    args = msg_stripped[3:].strip()  # '!파티' 다음
    date_arg = None
    job_arg = None
    if args:
        job_keywords = ['전사', '데빌', '도적', '법사', '직자', '도가']
        for part in args.split():
            if any(j in part for j in job_keywords):
                job_arg = part
            elif part in ['오늘', '내일'] or '/' in part or '월' in part:
                date_arg = part
    payload = {}
    if date_arg:
        payload["date"] = date_arg
    if job_arg:
        payload["job"] = job_arg
    try:
        resp = requests.post(
            f"{WIKIBOT_URL}/api/party/query", json=payload, timeout=10)
        return resp.json().get("answer", "파티 정보가 없습니다.")
    except Exception as e:
        logger.error(f"파티 조회 오류: {e}")
        return "파티 조회에 실패했습니다."


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
                      "room_name": room_name, "collect": is_collect}, timeout=5)
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
                json={"admin_id": sender_id}, timeout=5)
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

        # 시스템 메시지 / 빈 발신자 / 봇 자신 무시
        if msg_type == '0' or not sender or sender == 'Iris':
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

        # 파티방 설정 조회
        party_room = check_party_room(chat_id)
        is_party_collect_room = bool(party_room and party_room.get('collect'))
        is_party_room = party_room is not None

        # 수집방: 일반 메시지(명령어 아님) 자동 수집
        if is_party_collect_room and not msg_stripped.startswith('!'):
            collect_party_message(msg, sender, chat_id)
            return jsonify({"status": "ok"})

        # !파티 조회 (등록된 방에서만)
        if msg_stripped.startswith("!파티"):
            if is_party_room:
                send_reply(chat_id, query_party(msg_stripped))
            else:
                send_reply(chat_id,
                           "파티 조회가 활성화된 방이 아닙니다.\n(관리자: !파티설정 수집 [room_id])")
            return jsonify({"status": "ok"})

        return jsonify({"status": "ok"})

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
