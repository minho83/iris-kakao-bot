import json
import logging
import os
import subprocess
import time
from datetime import datetime

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Iris (redroid) reply 엔드포인트
IRIS_URL = os.getenv('IRIS_URL', 'http://192.168.0.80:3000')
# wikibot-kakao 서버 주소
WIKIBOT_URL = os.getenv('WIKIBOT_URL', 'http://localhost:8100')
# 배포 스크립트 경로
DEPLOY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy.sh")

# 요청 딜레이 관리
last_request_time = 0
REQUEST_DELAY = 2


# ── 유틸리티 ──────────────────────────────────────────────

def send_reply(chat_id, message):
    """Iris를 통해 채팅방에 메시지 전송"""
    try:
        payload = {"type": "text", "room": str(chat_id), "data": message}
        resp = requests.post(f"{IRIS_URL}/reply", json=payload, timeout=5)
        logger.info(f"Reply → {chat_id}: {resp.status_code}")
    except Exception as e:
        logger.error(f"Reply 전송 오류: {e}")


def ask_wikibot(endpoint, query="", max_length=500):
    """wikibot 엔드포인트 호출"""
    global last_request_time
    try:
        now = time.time()
        wait = REQUEST_DELAY - (now - last_request_time)
        if wait > 0:
            time.sleep(wait)
        last_request_time = time.time()

        resp = requests.post(
            f"{WIKIBOT_URL}{endpoint}",
            json={"query": query, "max_length": max_length},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"wikibot 통신 오류: {e}")
    return None


def format_search_result(result, sender):
    """wikibot 검색 결과를 메시지로 포맷"""
    if result is None:
        return f"{sender}님, 서버 연결에 실패했습니다."

    answer = result.get("answer", "검색 결과가 없습니다.")
    sources = result.get("sources", [])
    response = answer

    filtered = [s for s in sources if s.get("url")]
    if filtered:
        response += "\n\n📚 관련 링크:\n"
        for s in filtered[:2]:
            if s.get("url"):
                response += f"• {s.get('title', '링크')}\n  🔗 {s['url']}\n"

    return response.strip()


def multi_search(endpoint, query, sender):
    """& 구분자로 여러 검색어 동시 검색"""
    queries = [q.strip() for q in query.split("&") if q.strip()]
    if len(queries) <= 1:
        result = ask_wikibot(endpoint, query)
        return format_search_result(result, sender)

    parts = []
    for q in queries[:5]:
        result = ask_wikibot(endpoint, q, max_length=300)
        parts.append(f"【{q}】\n{format_search_result(result, sender)}")
    return "\n\n".join(parts)


# ── 닉네임/입퇴장 ────────────────────────────────────────

def check_nickname(sender_name, sender_id, room_id):
    """wikibot 닉네임 변경 체크"""
    try:
        resp = requests.post(
            f"{WIKIBOT_URL}/api/nickname/check",
            json={"sender_name": sender_name, "sender_id": sender_id, "room_id": room_id},
            timeout=5,
        )
        data = resp.json()
        if data.get("success") and data.get("notification"):
            return data["notification"]
    except Exception as e:
        logger.error(f"닉네임 체크 오류: {e}")
    return ""


def log_member_event(user_id, nickname, room_id, event_type):
    """wikibot 입퇴장 이벤트 기록"""
    try:
        resp = requests.post(
            f"{WIKIBOT_URL}/api/nickname/member-event",
            json={"user_id": user_id, "nickname": nickname, "room_id": room_id, "event_type": event_type},
            timeout=5,
        )
        data = resp.json()
        if data.get("success") and data.get("notification"):
            return data["notification"]
    except Exception as e:
        logger.error(f"입퇴장 이벤트 오류: {e}")
    return ""


# ── 거래 가격 ────────────────────────────────────────────

# 방 설정 캐시 (5분마다 갱신)
_room_cache = {}
_room_cache_time = 0
ROOM_CACHE_TTL = 300  # 5분


def check_trade_room(chat_id):
    """방 설정 조회 (캐시). 반환: {'collect': bool} 또는 None"""
    global _room_cache, _room_cache_time
    now = time.time()
    if now - _room_cache_time > ROOM_CACHE_TTL:
        _room_cache = {}
        _room_cache_time = now

    if chat_id in _room_cache:
        return _room_cache[chat_id]

    try:
        resp = requests.post(
            f"{WIKIBOT_URL}/api/trade/room-check",
            json={"room_id": chat_id},
            timeout=5,
        )
        data = resp.json()
        if not data.get("success"):
            return None  # 서버 오류 시 캐시하지 않음
        room = data.get("room")
        _room_cache[chat_id] = room  # 성공 응답만 캐시 (None 포함 = 미등록 방)
        return room
    except Exception:
        return None  # 통신 오류 시 캐시하지 않음 → 다음 요청에서 재시도


def collect_trade_message(msg, sender, chat_id):
    """거래방 메시지를 wikibot에 전달하여 시세 수집"""
    try:
        # 발신자 정보 파싱
        sender_name = sender
        sender_level = None
        server = None

        parts = sender.split('/')
        if len(parts) >= 2:
            sender_name = parts[0].strip()
            for p in parts[1:]:
                p = p.strip()
                if p.isdigit():
                    sender_level = int(p)
                elif p in ('세오', '베라', '도가', '세오의서'):
                    server = p
        else:
            space_parts = sender.split()
            if len(space_parts) >= 2:
                sender_name = space_parts[0]
                for p in space_parts[1:]:
                    if p.isdigit():
                        sender_level = int(p)
                    elif p in ('세오', '베라', '도가'):
                        server = p

        today = datetime.now().strftime('%Y-%m-%d')
        requests.post(
            f"{WIKIBOT_URL}/api/trade/collect",
            json={
                "message": msg,
                "sender_name": sender_name,
                "sender_level": sender_level,
                "server": server,
                "trade_date": today,
            },
            timeout=5,
        )
    except Exception as e:
        logger.error(f"거래 수집 오류: {e}")


# ── 관리자 명령 ───────────────────────────────────────────

def handle_admin_command(msg, sender_id, room_id=None):
    """관리자 명령 처리. 응답 메시지 반환."""
    global _room_cache, _room_cache_time

    if msg.startswith("!관리자등록"):
        try:
            resp = requests.post(
                f"{WIKIBOT_URL}/api/nickname/admin/register",
                json={"admin_id": sender_id},
                timeout=5,
            )
            return resp.json().get("message", "처리 완료")
        except Exception as e:
            logger.error(f"관리자 등록 오류: {e}")
            return "관리자 등록 중 오류가 발생했습니다."

    if msg.startswith("!닉변감지 추가"):
        parts = msg.split()
        if len(parts) < 3:
            return "사용법: !닉변감지 추가 [room_id] [room_name(선택)]"
        target_room = parts[2]
        room_name = " ".join(parts[3:]) if len(parts) > 3 else ""
        try:
            resp = requests.post(
                f"{WIKIBOT_URL}/api/nickname/admin/rooms",
                json={"admin_id": sender_id, "room_id": target_room, "room_name": room_name},
                timeout=5,
            )
            return resp.json().get("message", "처리 완료")
        except Exception as e:
            logger.error(f"채팅방 추가 오류: {e}")
            return "채팅방 추가 중 오류가 발생했습니다."

    if msg.startswith("!닉변감지 제거"):
        parts = msg.split()
        if len(parts) < 3:
            return "사용법: !닉변감지 제거 [room_id]"
        target_room = parts[2]
        try:
            resp = requests.delete(
                f"{WIKIBOT_URL}/api/nickname/admin/rooms/{target_room}",
                json={"admin_id": sender_id},
                timeout=5,
            )
            return resp.json().get("message", "처리 완료")
        except Exception as e:
            logger.error(f"채팅방 제거 오류: {e}")
            return "채팅방 제거 중 오류가 발생했습니다."

    if msg.startswith("!닉변감지 목록"):
        try:
            resp = requests.get(
                f"{WIKIBOT_URL}/api/nickname/admin/rooms",
                params={"admin_id": sender_id},
                timeout=5,
            )
            data = resp.json()
            if not data.get("success"):
                return data.get("message", "조회 실패")
            rooms = data.get("rooms", [])
            if not rooms:
                return "감시 중인 채팅방이 없습니다."
            lines = ["[감시 채팅방 목록]"]
            for r in rooms:
                status = "활성" if r.get("enabled") else "비활성"
                name = r.get("room_name") or r.get("room_id")
                lines.append(f"- {name} ({r.get('room_id')}) [{status}]")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"채팅방 목록 오류: {e}")
            return "채팅방 목록 조회 중 오류가 발생했습니다."

    if msg.startswith("!닉변이력"):
        parts = msg.split()
        if len(parts) < 2:
            return "사용법: !닉변이력 [room_id]"
        target_room = parts[1]
        try:
            resp = requests.get(
                f"{WIKIBOT_URL}/api/nickname/history/{target_room}",
                params={"admin_id": sender_id},
                timeout=5,
            )
            data = resp.json()
            if not data.get("success"):
                return data.get("message", "조회 실패")
            history = data.get("history", [])
            if not history:
                return "닉네임 변경 이력이 없습니다."
            lines = [f"[닉네임 이력 - {target_room}]"]
            for h in history[:30]:
                lines.append(f"- [{h.get('detected_at')}] {h.get('sender_name')}")
            if len(history) > 30:
                lines.append(f"... 외 {len(history) - 30}건")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"이력 조회 오류: {e}")
            return "이력 조회 중 오류가 발생했습니다."

    # ── 가격 방 설정 ──
    if msg.startswith("!가격설정 추가") or msg.startswith("!가격설정 수집"):
        is_collect = msg.startswith("!가격설정 수집")
        parts = msg.split()
        if len(parts) < 3:
            return "사용법: !가격설정 추가 [room_id] [방이름(선택)]\n!가격설정 수집 [room_id] [방이름(선택)]\n\n추가: 가격 조회만 가능\n수집: 시세 수집 + 가격 조회"
        target_room = parts[2]
        room_name = " ".join(parts[3:]) if len(parts) > 3 else ""
        try:
            resp = requests.post(
                f"{WIKIBOT_URL}/api/trade/rooms",
                json={"admin_id": sender_id, "room_id": target_room, "room_name": room_name, "collect": is_collect},
                timeout=5,
            )
            data = resp.json()
            # 캐시 초기화
            _room_cache.clear()
            _room_cache_time = 0
            return data.get("message", "처리 완료")
        except Exception as e:
            logger.error(f"가격 방 추가 오류: {e}")
            return "가격 방 추가 중 오류가 발생했습니다."

    if msg.startswith("!가격설정 제거"):
        parts = msg.split()
        if len(parts) < 3:
            return "사용법: !가격설정 제거 [room_id]"
        target_room = parts[2]
        try:
            resp = requests.delete(
                f"{WIKIBOT_URL}/api/trade/rooms/{target_room}",
                json={"admin_id": sender_id},
                timeout=5,
            )
            data = resp.json()
            # 캐시 초기화
            _room_cache.clear()
            return data.get("message", "처리 완료")
        except Exception as e:
            logger.error(f"가격 방 제거 오류: {e}")
            return "가격 방 제거 중 오류가 발생했습니다."

    if msg.startswith("!가격설정 목록"):
        try:
            resp = requests.get(
                f"{WIKIBOT_URL}/api/trade/rooms",
                params={"admin_id": sender_id},
                timeout=5,
            )
            data = resp.json()
            if not data.get("success"):
                return data.get("message", "조회 실패")
            rooms = data.get("rooms", [])
            if not rooms:
                return "설정된 가격 방이 없습니다."
            lines = ["[가격 방 목록]"]
            for r in rooms:
                mode = "수집+조회" if r.get("collect") else "조회만"
                name = r.get("room_name") or r.get("room_id")
                lines.append(f"- {name} ({r.get('room_id')}) [{mode}]")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"가격 방 목록 오류: {e}")
            return "가격 방 목록 조회 중 오류가 발생했습니다."

    if msg.startswith("!가격설정"):
        return "사용법:\n!가격설정 추가 [room_id] [방이름] - 조회만\n!가격설정 수집 [room_id] [방이름] - 수집+조회\n!가격설정 제거 [room_id]\n!가격설정 목록"

    # ── 별칭(줄임말) 관리 ──
    if msg.startswith("!별칭 추가") or msg.startswith("!별칭추가"):
        parts = msg.split()
        if len(parts) < 4:
            return "사용법: !별칭 추가 [줄임말] [정식명]\n예: !별칭 추가 강세 강화된세피어링"
        alias_name = parts[2]
        canonical = parts[3]
        try:
            resp = requests.post(
                f"{WIKIBOT_URL}/api/trade/alias",
                json={"alias": alias_name, "canonical_name": canonical},
                timeout=5,
            )
            data = resp.json()
            if data.get("success"):
                return f"별칭 등록 완료: {alias_name} → {canonical}"
            return data.get("message", "별칭 등록 실패")
        except Exception as e:
            logger.error(f"별칭 추가 오류: {e}")
            return "별칭 추가 중 오류가 발생했습니다."

    if msg.startswith("!별칭 삭제") or msg.startswith("!별칭삭제"):
        parts = msg.split()
        if len(parts) < 3:
            return "사용법: !별칭 삭제 [줄임말]"
        alias_name = parts[2]
        try:
            resp = requests.delete(
                f"{WIKIBOT_URL}/api/trade/alias/{alias_name}",
                timeout=5,
            )
            data = resp.json()
            return data.get("message", "처리 완료")
        except Exception as e:
            logger.error(f"별칭 삭제 오류: {e}")
            return "별칭 삭제 중 오류가 발생했습니다."

    if msg.startswith("!별칭 목록") or msg.startswith("!별칭목록"):
        try:
            resp = requests.get(
                f"{WIKIBOT_URL}/api/trade/alias",
                timeout=5,
            )
            data = resp.json()
            if not data.get("success"):
                return "별칭 목록 조회 실패"
            aliases = data.get("aliases", [])
            if not aliases:
                return "등록된 별칭이 없습니다."
            # 정식명별로 그룹화
            groups = {}
            for a in aliases:
                cn = a.get("canonical_name", "")
                if cn not in groups:
                    groups[cn] = []
                groups[cn].append(a.get("alias", ""))
            lines = ["[별칭 목록]"]
            for cn, alias_list in sorted(groups.items()):
                lines.append(f"· {cn}: {', '.join(alias_list)}")
            if len(lines) > 30:
                lines = lines[:30]
                lines.append(f"... 외 {len(groups) - 29}개")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"별칭 목록 오류: {e}")
            return "별칭 목록 조회 중 오류가 발생했습니다."

    if msg.startswith("!별칭"):
        return "사용법:\n!별칭 추가 [줄임말] [정식명]\n!별칭 삭제 [줄임말]\n!별칭 목록"

    # ── 가격 데이터 정리 ──
    if msg.startswith("!시세정리"):
        parts = msg.split()
        since_date = parts[1] if len(parts) >= 2 else None
        try:
            payload = {}
            if since_date:
                payload["since_date"] = since_date
            resp = requests.post(
                f"{WIKIBOT_URL}/api/trade/cleanup",
                json=payload,
                timeout=30,
            )
            data = resp.json()
            if data.get("success"):
                lines = [
                    "[가격 데이터 정리 완료]",
                    f"· 제거: {data.get('removed', 0)}개 항목",
                    f"· 유지: {data.get('kept', 0)}개 항목",
                ]
                examples = data.get("examples", [])
                if examples:
                    lines.append(f"\n제거된 항목:")
                    for ex in examples[:10]:
                        lines.append(f"  - {ex}")
                    if len(examples) > 10:
                        lines.append(f"  ... 외 {len(examples) - 10}개")
                return "\n".join(lines)
            return data.get("message", "정리 실패")
        except Exception as e:
            logger.error(f"가격 정리 오류: {e}")
            return "가격 데이터 정리 중 오류가 발생했습니다."

    if msg.startswith("!서버재시작"):
        try:
            resp = requests.post(
                f"{WIKIBOT_URL}/api/nickname/admin/verify",
                json={"admin_id": sender_id},
                timeout=5,
            )
            data = resp.json()
            if not data.get("success"):
                return data.get("message", "권한이 없습니다.")
        except Exception:
            return "권한 확인 중 오류가 발생했습니다."

        try:
            subprocess.Popen(
                ["bash", DEPLOY_SCRIPT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"서버 재시작 실행 (by {sender_id})")
            return "서버 재시작을 시작합니다. (git pull → 빌드 → 재시작)"
        except Exception as e:
            logger.error(f"서버 재시작 오류: {e}")
            return f"서버 재시작 실패: {e}"

    return None


# ── 시스템 메시지 처리 ────────────────────────────────────

def handle_system_message(data, chat_id):
    """type 0 시스템 메시지 처리 (입퇴장)"""
    try:
        msg_text = data.get('msg', '')
        json_info = data.get('json', {})
        user_id = str(json_info.get('user_id', ''))

        feed = json.loads(msg_text)
        feed_type = feed.get('feedType')
        member = feed.get('member', {})
        nickname = member.get('nickName', '')
        member_user_id = str(member.get('userId', user_id))

        if feed_type == 1:
            event_type = 'join'
        elif feed_type == 2:
            event_type = 'leave'
        else:
            return

        notification = log_member_event(member_user_id, nickname, chat_id, event_type)
        if notification:
            send_reply(chat_id, notification)

    except (json.JSONDecodeError, KeyError):
        pass
    except Exception as e:
        logger.error(f"시스템 메시지 처리 오류: {e}")


# ── 웹훅 엔드포인트 ──────────────────────────────────────

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
        is_group = data.get('isGroupChat', True)
        json_info = data.get('json', {})
        msg_type = str(json_info.get('type', '1'))
        chat_id = str(json_info.get('chat_id', room))
        user_id = str(json_info.get('user_id', ''))

        # ── 시스템 메시지 (입퇴장) ──
        if msg_type == '0':
            handle_system_message(data, chat_id)
            return jsonify({"status": "ok"})

        # sender 없으면 무시
        if not sender:
            return jsonify({"status": "ok"})

        # 봇 자신의 메시지 무시
        if sender == 'Iris':
            return jsonify({"status": "ok"})

        logger.info(f"[{room}] {sender}: {msg}")

        # ── 방 설정 조회 ──
        msg_stripped = msg.strip()
        trade_room = check_trade_room(chat_id)
        is_collect_room = trade_room and trade_room.get('collect')
        is_price_room = trade_room is not None  # 수집방 또는 조회방

        # ── 닉네임 변경 체크 (수집방 제외) ──
        if not is_collect_room and user_id and chat_id:
            notification = check_nickname(sender, user_id, chat_id)
            if notification:
                send_reply(chat_id, notification)

        # ── 수집방: 자동 수집 + !가격만 응답 ──
        if is_collect_room:
            if not msg_stripped.startswith('!'):
                collect_trade_message(msg, sender, chat_id)
                return jsonify({"status": "ok"})

            # 수집방에서도 관리자 명령 허용
            if msg_stripped.startswith("!가격설정") or msg_stripped.startswith("!시세정리") or msg_stripped.startswith("!별칭"):
                result = handle_admin_command(msg_stripped, user_id, room_id=chat_id)
                if result:
                    send_reply(chat_id, result)
                return jsonify({"status": "ok"})

            # 수집방에서는 !가격만 허용
            if msg_stripped.startswith("!가격"):
                query = msg_stripped[3:].strip()
                if query:
                    result = ask_wikibot("/api/trade/query", query)
                    if result:
                        send_reply(chat_id, result.get("answer", "가격 정보가 없습니다."))
                    else:
                        send_reply(chat_id, "가격 조회에 실패했습니다.")
                else:
                    send_reply(chat_id, "사용법: !가격 [아이템명]\n예: !가격 암목\n예: !가격 5강 나겔반지")
            return jsonify({"status": "ok"})

        # ── 명령어 처리 (일반 방) ──
        response_msg = None

        # 방 확인
        if msg_stripped == "!방확인":
            response_msg = f"[방 정보]\nroom: {room}\nchat_id: {chat_id}\nsender: {sender}\nuser_id: {user_id}"

        # 관리자 명령 (DM 또는 그룹)
        elif msg_stripped.startswith("!관리자등록") or msg_stripped.startswith("!닉변감지") or msg_stripped.startswith("!닉변이력") or msg_stripped.startswith("!가격설정") or msg_stripped.startswith("!별칭") or msg_stripped.startswith("!시세정리"):
            result = handle_admin_command(msg_stripped, user_id, room_id=chat_id)
            if result:
                response_msg = result

        # 서버 재시작
        elif msg_stripped.startswith("!서버재시작"):
            result = handle_admin_command(msg_stripped, user_id, room_id=chat_id)
            if result:
                response_msg = result

        # 아이템 검색
        elif msg_stripped.startswith("!아이템"):
            query = msg_stripped[4:].strip()
            if query:
                response_msg = multi_search("/ask/item", query, sender)
            else:
                response_msg = "검색어를 입력해주세요. 예: !아이템 오리하르콘"

        # 스킬/마법 검색
        elif msg_stripped.startswith("!스킬") or msg_stripped.startswith("!마법"):
            query = msg_stripped[3:].strip()
            if query:
                response_msg = multi_search("/ask/skill", query, sender)
            else:
                response_msg = "검색어를 입력해주세요. 예: !스킬 메테오"

        # 게시판 검색
        elif msg_stripped.startswith("!현자"):
            query = msg_stripped[4:].strip()
            if query:
                result = ask_wikibot("/ask/community", query)
                response_msg = format_search_result(result, sender)
            else:
                response_msg = "검색어를 입력해주세요. 예: !현자 발록"

        # 공지사항
        elif msg_stripped.startswith("!공지"):
            query = msg_stripped[3:].strip()
            result = ask_wikibot("/ask/notice", query)
            response_msg = format_search_result(result, sender)

        # 업데이트
        elif msg_stripped.startswith("!업데이트"):
            query = msg_stripped[5:].strip()
            result = ask_wikibot("/ask/update", query)
            response_msg = format_search_result(result, sender)

        # 가격 조회 (설정된 방에서만)
        elif msg_stripped.startswith("!가격") and not msg_stripped.startswith("!가격설정"):
            if is_price_room:
                query = msg_stripped[3:].strip()
                if query:
                    result = ask_wikibot("/api/trade/query", query)
                    if result:
                        response_msg = result.get("answer", "가격 정보가 없습니다.")
                    else:
                        response_msg = "가격 조회에 실패했습니다."
                else:
                    response_msg = "사용법: !가격 [아이템명]\n예: !가격 암목\n예: !가격 5강 나겔반지"

        # 통합 검색
        elif msg_stripped.startswith("!검색") or msg_stripped.startswith("!질문"):
            query = msg_stripped[3:].strip()
            if query:
                result = ask_wikibot("/ask", query)
                response_msg = format_search_result(result, sender)
            else:
                response_msg = "검색어를 입력해주세요. 예: !검색 메테오"

        # 도움말
        elif msg_stripped == "!도움말" or msg_stripped == "도움말":
            lines = [
                "📋 명령어 안내",
                "!아이템 [이름] - 아이템 검색",
                "!스킬 [이름] - 스킬/마법 검색",
                "!현자 [키워드] - 현자게시판[세오]내 글 재목 검색",
                "!검색 [키워드] - 통합 검색",
                "!공지 [날짜] - 공지사항 (예: !공지 2/5)",
                "!업데이트 [날짜] - 업데이트 내역",
            ]
            if is_price_room:
                lines.append("!가격 [아이템명] - 거래 시세 조회")
            lines.append("")
            lines.append("💡 &로 여러 개 동시 검색 가능")
            lines.append("예: !아이템 오리하르콘 & 미스릴")
            response_msg = "\n".join(lines)

        # 관리자 도움말
        elif msg_stripped == "!관리자":
            response_msg = """🔧 관리자 명령어

[가격 시세 조회]
!가격 [아이템명] - 강화별 시세 요약
  (판매/구매 평균 분리 표시)
!가격 5강 [아이템명] - 특정 강화 상세
  예: !가격 암목, !가격 5강 나겔반지

[가격 방 설정]
!가격설정 수집 [방ID] [방이름]
  → 거래방 (시세 자동 수집 + 조회)
!가격설정 추가 [방ID] [방이름]
  → 조회 전용방 (조회만 가능)
!가격설정 제거 [방ID]
!가격설정 목록

[별칭/줄임말 관리]
!별칭 추가 [줄임말] [정식명]
  예: !별칭 추가 강세 강화된세피어링
!별칭 삭제 [줄임말]
!별칭 목록

[데이터 관리]
!시세정리 - 전체 데이터 정리 (LOD_DB 검증)
!시세정리 [날짜] - 특정일 이후 정리
  예: !시세정리 2026-02-03
  (매일 04:00 자동 실행)

[닉네임 감시]
!닉변감지 추가/제거/목록
!닉변이력 [방ID]

[시스템]
!관리자등록 - 최초 관리자 등록
!서버재시작 - 서버 재배포
!방확인 - 현재 방 ID 확인"""

        # 응답 전송
        if response_msg:
            send_reply(chat_id, response_msg)

        return jsonify({"status": "ok"})

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
