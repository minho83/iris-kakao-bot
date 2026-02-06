import json
import logging
import os
import time
from datetime import datetime

import requests
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Iris (redroid) reply 엔드포인트
IRIS_URL = os.getenv('IRIS_URL', 'http://192.168.0.80:3000')
# wikibot-kakao 서버 주소 (Docker host 네트워크 → localhost 직접 통신)
WIKIBOT_URL = 'http://localhost:8214'
# 배포 트리거 파일 (호스트의 cron이 이 파일 감지 후 deploy.sh 실행)
DEPLOY_TRIGGER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".deploy_trigger")

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

# 파티방 설정 캐시
_party_room_cache = {}
_party_room_cache_time = 0


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


def check_party_room(chat_id):
    """파티방 설정 조회 (캐시). 반환: {'collect': bool} 또는 None"""
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
            json={"room_id": chat_id},
            timeout=5,
        )
        data = resp.json()
        if not data.get("success"):
            return None
        room = data.get("room")
        _party_room_cache[chat_id] = room
        return room
    except Exception:
        return None


def collect_party_message(msg, sender, chat_id):
    """파티방 메시지를 wikibot에 전달하여 파티 수집"""
    try:
        sender_name = sender.split('/')[0].strip() if '/' in sender else sender
        requests.post(
            f"{WIKIBOT_URL}/api/party/collect",
            json={
                "message": msg,
                "sender_name": sender_name,
                "room_id": chat_id,
            },
            timeout=5,
        )
    except Exception as e:
        logger.error(f"파티 수집 오류: {e}")


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
            lines = ["[닉네임 변경 이력]"]
            for h in history:
                changes = h.get('changes', '')
                last_changed = h.get('last_changed', '')[:16]  # 초 제외
                lines.append(f"• {changes}")
                lines.append(f"  ({last_changed})")
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

    # ── 파티방 설정 ──
    if msg.startswith("!파티설정 추가") or msg.startswith("!파티설정 수집"):
        is_collect = msg.startswith("!파티설정 수집")
        parts = msg.split()
        if len(parts) < 3:
            return "사용법: !파티설정 추가 [room_id] [방이름(선택)]\n!파티설정 수집 [room_id] [방이름(선택)]\n\n추가: 파티 조회만 가능\n수집: 파티 수집 + 조회"
        target_room = parts[2]
        room_name = " ".join(parts[3:]) if len(parts) > 3 else ""
        try:
            resp = requests.post(
                f"{WIKIBOT_URL}/api/party/rooms",
                json={"admin_id": sender_id, "room_id": target_room, "room_name": room_name, "collect": is_collect},
                timeout=5,
            )
            data = resp.json()
            # 캐시 초기화
            _party_room_cache.clear()
            return data.get("message", "처리 완료")
        except Exception as e:
            logger.error(f"파티 방 추가 오류: {e}")
            return "파티 방 추가 중 오류가 발생했습니다."

    if msg.startswith("!파티설정 제거"):
        parts = msg.split()
        if len(parts) < 3:
            return "사용법: !파티설정 제거 [room_id]"
        target_room = parts[2]
        try:
            resp = requests.delete(
                f"{WIKIBOT_URL}/api/party/rooms/{target_room}",
                json={"admin_id": sender_id},
                timeout=5,
            )
            data = resp.json()
            _party_room_cache.clear()
            return data.get("message", "처리 완료")
        except Exception as e:
            logger.error(f"파티 방 제거 오류: {e}")
            return "파티 방 제거 중 오류가 발생했습니다."

    if msg.startswith("!파티설정 목록"):
        try:
            resp = requests.get(
                f"{WIKIBOT_URL}/api/party/rooms",
                params={"admin_id": sender_id},
                timeout=5,
            )
            data = resp.json()
            if not data.get("success"):
                return data.get("message", "조회 실패")
            rooms = data.get("rooms", [])
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

    if msg.startswith("!파티설정"):
        return "사용법:\n!파티설정 추가 [room_id] [방이름] - 조회만\n!파티설정 수집 [room_id] [방이름] - 수집+조회\n!파티설정 제거 [room_id]\n!파티설정 목록"

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
            # 재시작 완료 알림을 보낼 방 저장
            if room_id:
                save_restart_room(room_id)

            # 배포 트리거 파일 생성 (호스트의 cron이 감지 후 deploy.sh 실행)
            with open(DEPLOY_TRIGGER_FILE, 'w') as f:
                f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            logger.info(f"서버 재시작 요청 (by {sender_id}) in room {room_id}")
            return "서버 재시작을 시작합니다. (최대 1분 내 실행)"
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


# ── 대시보드 ──────────────────────────────────────────────

DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WikiBot Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
            padding: 20px;
        }
        h1 { text-align: center; margin-bottom: 30px; color: #00d9ff; }
        .container { max-width: 1200px; margin: 0 auto; }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: #16213e;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }
        .stat-card h3 {
            color: #00d9ff;
            margin-bottom: 15px;
            font-size: 1.1em;
        }
        .stat-value {
            font-size: 2em;
            font-weight: bold;
            color: #fff;
        }
        .stat-unit { font-size: 0.5em; color: #888; }
        .stat-detail { color: #888; font-size: 0.9em; margin-top: 10px; }
        .chart-container {
            background: #16213e;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
        }
        .chart-title {
            color: #00d9ff;
            margin-bottom: 15px;
            font-size: 1.2em;
        }
        .status-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: #16213e;
            border-radius: 8px;
            padding: 15px 20px;
            margin-bottom: 20px;
        }
        .status-item { display: flex; align-items: center; gap: 8px; }
        .status-dot {
            width: 10px; height: 10px;
            border-radius: 50%;
            background: #00ff88;
        }
        .status-dot.warning { background: #ffaa00; }
        .status-dot.error { background: #ff4444; }
        .refresh-btn {
            background: #00d9ff;
            color: #1a1a2e;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: bold;
        }
        .refresh-btn:hover { background: #00b8d9; }
        .last-update { color: #666; font-size: 0.9em; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 WikiBot Dashboard</h1>

        <div class="status-bar">
            <div class="status-item">
                <span class="status-dot" id="statusDot"></span>
                <span id="statusText">연결 중...</span>
            </div>
            <div>
                <span class="last-update" id="lastUpdate"></span>
                <button class="refresh-btn" onclick="loadStats()">새로고침</button>
            </div>
        </div>

        <div class="stats-grid" id="statsGrid">
            <!-- DB 카드들이 여기에 동적으로 추가됨 -->
        </div>

        <div class="chart-container">
            <h3 class="chart-title">📈 DB 용량 추이 (24시간)</h3>
            <canvas id="dbChart" height="100"></canvas>
        </div>
    </div>

    <script>
        let chart = null;
        // 브라우저에서 접속한 호스트 기준으로 wikibot URL 설정
        const WIKIBOT_URL = 'http://' + window.location.hostname + ':8100';

        async function loadStats() {
            try {
                const resp = await fetch(WIKIBOT_URL + '/api/db/stats');
                const data = await resp.json();

                if (data.success) {
                    updateStatusBar(true, data.uptime);
                    renderStats(data.databases);
                    document.getElementById('lastUpdate').textContent =
                        '마지막 업데이트: ' + new Date().toLocaleTimeString('ko-KR');
                }
            } catch (e) {
                updateStatusBar(false);
                console.error('Stats load error:', e);
            }

            // 히스토리도 로드
            loadHistory();
        }

        async function loadHistory() {
            try {
                const resp = await fetch(WIKIBOT_URL + '/api/db/history');
                const data = await resp.json();
                if (data.success && data.history.length > 0) {
                    renderChart(data.history);
                }
            } catch (e) {
                console.error('History load error:', e);
            }
        }

        function updateStatusBar(connected, uptime) {
            const dot = document.getElementById('statusDot');
            const text = document.getElementById('statusText');

            if (connected) {
                dot.className = 'status-dot';
                const hours = Math.floor(uptime / 3600);
                const mins = Math.floor((uptime % 3600) / 60);
                text.textContent = `서버 정상 (가동시간: ${hours}시간 ${mins}분)`;
            } else {
                dot.className = 'status-dot error';
                text.textContent = '서버 연결 실패';
            }
        }

        function renderStats(databases) {
            const grid = document.getElementById('statsGrid');
            const dbNames = {
                'trade.db': { icon: '💰', name: '거래 시세' },
                'party.db': { icon: '🎉', name: '파티 모집' },
                'nickname.db': { icon: '👤', name: '닉네임 감시' },
                'notice.db': { icon: '📢', name: '공지사항' }
            };

            let html = '';
            for (const [db, info] of Object.entries(databases)) {
                const meta = dbNames[db] || { icon: '📁', name: db };
                const sizeMb = parseFloat(info.size_mb);
                const sizeClass = sizeMb > 50 ? 'warning' : '';

                let detail = '';
                if (info.records !== undefined) {
                    detail = `${info.records.toLocaleString()} 레코드`;
                } else if (info.rooms !== undefined) {
                    detail = `${info.rooms} 감시방`;
                }

                html += `
                    <div class="stat-card">
                        <h3>${meta.icon} ${meta.name}</h3>
                        <div class="stat-value ${sizeClass}">
                            ${info.size_mb}<span class="stat-unit"> MB</span>
                        </div>
                        <div class="stat-detail">${detail}</div>
                    </div>
                `;
            }
            grid.innerHTML = html;
        }

        function renderChart(history) {
            const ctx = document.getElementById('dbChart').getContext('2d');

            const labels = history.map(h => {
                const d = new Date(h.timestamp);
                return d.getHours() + ':' + String(d.getMinutes()).padStart(2, '0');
            });

            const toMB = (bytes) => (bytes / 1024 / 1024).toFixed(2);

            const datasets = [
                {
                    label: 'trade.db',
                    data: history.map(h => toMB(h['trade.db'] || 0)),
                    borderColor: '#ff6384',
                    backgroundColor: 'rgba(255, 99, 132, 0.1)',
                    fill: true,
                    tension: 0.3
                },
                {
                    label: 'party.db',
                    data: history.map(h => toMB(h['party.db'] || 0)),
                    borderColor: '#36a2eb',
                    backgroundColor: 'rgba(54, 162, 235, 0.1)',
                    fill: true,
                    tension: 0.3
                },
                {
                    label: 'nickname.db',
                    data: history.map(h => toMB(h['nickname.db'] || 0)),
                    borderColor: '#ffce56',
                    backgroundColor: 'rgba(255, 206, 86, 0.1)',
                    fill: true,
                    tension: 0.3
                }
            ];

            if (chart) {
                chart.data.labels = labels;
                chart.data.datasets = datasets;
                chart.update();
            } else {
                chart = new Chart(ctx, {
                    type: 'line',
                    data: { labels, datasets },
                    options: {
                        responsive: true,
                        plugins: {
                            legend: {
                                labels: { color: '#eee' }
                            }
                        },
                        scales: {
                            x: {
                                ticks: { color: '#888' },
                                grid: { color: '#333' }
                            },
                            y: {
                                ticks: {
                                    color: '#888',
                                    callback: (v) => v + ' MB'
                                },
                                grid: { color: '#333' }
                            }
                        }
                    }
                });
            }
        }

        // 초기 로드
        loadStats();

        // 1분마다 자동 새로고침
        setInterval(loadStats, 60000);
    </script>
</body>
</html>
'''


@app.route('/dashboard', methods=['GET'])
def dashboard():
    return render_template_string(DASHBOARD_HTML, wikibot_url=WIKIBOT_URL)


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

        # 파티방 설정 조회
        party_room = check_party_room(chat_id)
        is_party_collect_room = party_room and party_room.get('collect')
        is_party_room = party_room is not None

        # ── 닉네임 변경 체크 (수집방 제외) ──
        if not is_collect_room and user_id and chat_id:
            notification = check_nickname(sender, user_id, chat_id)
            if notification:
                send_reply(chat_id, notification)

        # ── 파티 수집방: 자동 수집 + !파티만 응답 ──
        if is_party_collect_room:
            if not msg_stripped.startswith('!'):
                collect_party_message(msg, sender, chat_id)
                return jsonify({"status": "ok"})

            # 파티 수집방에서도 관리자 명령 허용
            if msg_stripped.startswith("!파티설정"):
                result = handle_admin_command(msg_stripped, user_id, room_id=chat_id)
                if result:
                    send_reply(chat_id, result)
                return jsonify({"status": "ok"})

            # 파티 수집방에서 !파티만 입력 → 웹사이트 안내
            if msg_stripped == "!파티":
                send_reply(chat_id, "📋 파티 빈자리 현황\n\n아래 링크에서 실시간 파티 빈자리를 확인하세요!\n👉 https://party.milddok.cc/\n\n* 어둠의전설 나겔파티 오픈톡 데이터 기반\n* 수집상태에 따라 오차가 있을 수 있습니다.")
                return jsonify({"status": "ok"})

            # 파티 수집방에서는 !파티 [인자]로 조회
            if msg_stripped.startswith("!파티"):
                # !파티 [날짜] [직업] 파싱
                args = msg_stripped[3:].strip()
                date_arg = None
                job_arg = None

                if args:
                    job_keywords = ['전사', '데빌', '도적', '법사', '직자', '도가']
                    parts = args.split()
                    for part in parts:
                        if any(job in part for job in job_keywords):
                            job_arg = part
                        elif part in ['오늘', '내일'] or '/' in part or '월' in part:
                            date_arg = part

                try:
                    payload = {}
                    if date_arg:
                        payload["date"] = date_arg
                    if job_arg:
                        payload["job"] = job_arg

                    resp = requests.post(
                        f"{WIKIBOT_URL}/api/party/query",
                        json=payload,
                        timeout=10,
                    )
                    data = resp.json()
                    send_reply(chat_id, data.get("answer", "파티 정보가 없습니다."))
                except Exception as e:
                    logger.error(f"파티 조회 오류: {e}")
                    send_reply(chat_id, "파티 조회에 실패했습니다.")
            return jsonify({"status": "ok"})

        # ── 거래 수집방: 자동 수집 + !가격만 응답 ──
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
        elif msg_stripped.startswith("!관리자등록") or msg_stripped.startswith("!닉변감지") or msg_stripped.startswith("!닉변이력") or msg_stripped.startswith("!가격설정") or msg_stripped.startswith("!별칭") or msg_stripped.startswith("!시세정리") or msg_stripped.startswith("!파티설정"):
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

        # 파티 빈자리 안내 (방 제한 없음)
        elif msg_stripped == "!파티":
            response_msg = "📋 파티 빈자리 현황\n\n아래 링크에서 실시간 파티 빈자리를 확인하세요!\n👉 https://party.milddok.cc/\n\n* 어둠의전설 나겔파티 오픈톡 데이터 기반\n* 수집상태에 따라 오차가 있을 수 있습니다."

        # 파티 조회 (설정된 방에서만)
        elif msg_stripped.startswith("!파티") and not msg_stripped.startswith("!파티설정"):
            if is_party_room:
                # !파티 [날짜] [직업] 파싱
                args = msg_stripped[3:].strip()
                date_arg = None
                job_arg = None

                if args:
                    # 직업 키워드 체크
                    job_keywords = ['전사', '데빌', '도적', '법사', '직자', '도가']
                    parts = args.split()
                    for part in parts:
                        if any(job in part for job in job_keywords):
                            job_arg = part
                        elif part in ['오늘', '내일'] or '/' in part or '월' in part:
                            date_arg = part

                try:
                    payload = {}
                    if date_arg:
                        payload["date"] = date_arg
                    if job_arg:
                        payload["job"] = job_arg

                    resp = requests.post(
                        f"{WIKIBOT_URL}/api/party/query",
                        json=payload,
                        timeout=10,
                    )
                    data = resp.json()
                    response_msg = data.get("answer", "파티 정보가 없습니다.")
                except Exception as e:
                    logger.error(f"파티 조회 오류: {e}")
                    response_msg = "파티 조회에 실패했습니다."
            else:
                response_msg = "파티 조회가 활성화된 방에서만 사용 가능합니다.\n(관리자: !파티설정 추가/수집 [room_id])"

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
            if is_party_room:
                lines.append("!파티 [날짜] [직업] - 빈자리 파티 조회")
            lines.append("")
            lines.append("💡 &로 여러 개 동시 검색 가능")
            lines.append("예: !아이템 오리하르콘 & 미스릴")
            response_msg = "\n".join(lines)

        # 관리자 도움말
        elif msg_stripped == "!관리자":
            response_msg = """🔧 관리자 명령어

[가격]
!가격 [아이템명] - 시세 조회
!가격설정 수집/추가/제거/목록

[파티]
!파티 [날짜] [직업] - 빈자리 조회
!파티설정 수집/추가/제거/목록

[기타]
!별칭 추가/삭제/목록
!시세정리 - 가격 데이터 정리
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


# 재시작 요청 저장 파일
RESTART_REQUEST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".restart_room")


def save_restart_room(room_id):
    """재시작 요청한 방 ID 저장"""
    try:
        with open(RESTART_REQUEST_FILE, 'w') as f:
            f.write(room_id)
    except Exception as e:
        logger.error(f"Failed to save restart room: {e}")


def send_startup_notification():
    """서버 시작 시 재시작 요청한 방에 알림 전송"""
    import threading

    def notify():
        # wikibot이 준비될 때까지 대기
        time.sleep(10)

        try:
            # 재시작 요청한 방 확인
            if not os.path.exists(RESTART_REQUEST_FILE):
                return

            with open(RESTART_REQUEST_FILE, 'r') as f:
                room_id = f.read().strip()

            if room_id:
                startup_msg = "🤖 서버 재시작 완료\n" + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                send_reply(room_id, startup_msg)
                logger.info(f"Restart notification sent to room: {room_id}")

            # 파일 삭제
            os.remove(RESTART_REQUEST_FILE)

        except Exception as e:
            logger.error(f"Startup notification error: {e}")

    # 백그라운드 스레드로 실행
    thread = threading.Thread(target=notify, daemon=True)
    thread.start()


if __name__ == '__main__':
    send_startup_notification()
    app.run(host='0.0.0.0', port=5000)
