"""
iris-kakao-bot — 파티 전용 슬림 버전
- 닉네임/거래/검색/공지/RAG/대시보드 전부 제거
- 파티 수집 + !파티 조회 + !파티설정 관리자 명령만 유지
- !파티 는 외부 링크 대신 wikibot(party.db)을 직접 조회해서 응답
"""
import os
import sys
import re
import json
import time
import random
import logging
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify

import notices

# 대화 저장고(~/chat-archive). 없으면 조용히 끈다 — 저장이 안 된다고 봇이 죽으면 안 된다.
sys.path.insert(0, str(Path.home() / "chat-archive"))
try:
    import archive as chat_archive
except Exception:  # noqa: BLE001
    chat_archive = None

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
# milddok.cc 사이트 봇 API (functions/api/bot/*) — 퀘스트 동선·길찾기
# 응답의 text를 그대로 카톡에 뿌린다. 인증은 파티 API와 같은 MATCH_BOT_KEY.
SITE_API_URL = os.getenv('SITE_API_URL', 'https://milddok.cc/api/bot')

# 방 대화 질문응답(RAG). GB10에 GPU가 있어 거기서 돈다 — 테일스케일로 붙는다.
ASK_URL = os.getenv('ASK_URL', 'http://100.92.82.79:8899/ask')
ASK_KEY = os.getenv('ASK_KEY', '')
# 틀린 답 신고 — `!틀림`을 치면 그 방의 직전 !질문 질문·답을 사이트에 올린다 (X-Bot-Key).
ASK_REVIEW_URL = os.getenv('ASK_REVIEW_URL', 'https://milddok.cc/api/ask/review')
# !질문 기록 — 답을 낼 때마다 사이트에 한 줄. /ask-admin/ 의 "많이 묻는 질문 top 20"이 여기서 나온다.
ASK_LOG_URL = os.getenv('ASK_LOG_URL', 'https://milddok.cc/api/ask/log')
# 방마다 마지막 !질문 — {chat_id: {q, a, who, ts}}. 재시작하면 비지만 신고는 대개 몇 분 안에 온다.
LAST_ASK = {}
# 방 사람 닉네임 — {chat_id: {이름: 마지막 본 때}}. "쿠모삐"처럼 닉네임 한 낱말만 치면 GB10의 사람 질문 차단이
# 못 잡고(조사가 없다) 방 대화로 그 사람을 요약해 버렸다. 봇은 누가 방에 있는지 아니 여기서 먼저 막는다.
NAMES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'room_names.json')
try:
    with open(NAMES_FILE, encoding='utf-8') as _f:
        ROOM_NAMES = json.load(_f)
except (OSError, ValueError):
    ROOM_NAMES = {}
_NAME_STOP = {'뉴비', '초보', '복귀', '복귀뉴비', '오픈채팅봇', 'Iris'}


def _name_tokens(full):
    """'성은/최강법직/도박신고1336' → {'성은', 전체}. 닉네임은 첫 조각이다 — 뒤 조각은 직업·단수라
    '도가'·'15단' 같은 게임 낱말이 섞이고, 그것까지 사람으로 보면 '!질문 도가'를 거절하게 된다."""
    full = (full or '').strip()
    first = re.sub(r'\(.*?\)', '', full.split('/')[0]).strip()
    toks = set()
    for t in (first, full):
        if len(t) >= 2 and not re.fullmatch(r'[\d\s단렙]+', t) and t not in _NAME_STOP:
            toks.add(t)
    return toks


def note_name(chat_id, full):
    names = ROOM_NAMES.setdefault(str(chat_id), {})
    fresh = False
    for t in _name_tokens(full):
        if t not in names:
            fresh = True
        names[t] = int(time.time())
    if fresh:
        try:
            with open(NAMES_FILE, 'w', encoding='utf-8') as f:
                json.dump(ROOM_NAMES, f, ensure_ascii=False)
        except OSError as e:
            logger.error(f"닉네임 저장 오류: {e}")


def is_member_name(chat_id, query):
    """질문이 이 방 누군가의 닉네임이거나, 질문의 낱말 하나가 닉네임이면 사람 질문이다.
    '쿠모삐'도, '쿠모삐 퀘스트'도 막는다 — 후자를 GB10에 보내면 '퀘스트'로 위키를 찾아 쿠모삐를 억지로 엮는다
    ("쿠모삐 퀘스트는 승급 무기업 퀘스트와 관련"). 낱말은 띄어쓰기 기준, 두 글자 이상."""
    names = ROOM_NAMES.get(str(chat_id), {})
    if not names:
        return False
    flat = {re.sub(r'\s+', '', n).lower() for n in names}
    q = (query or '').strip()
    whole = re.sub(r'\s+', '', q).lower()
    if len(whole) >= 2 and whole in flat:
        return True
    for w in re.split(r'\s+', q):
        w = re.sub(r'[?!.,~]+$', '', w).lower()
        if len(w) >= 2 and w in flat:
            return True
    return False

# 방별 기능 토글 — BOT_OWNER가 '!<기능>사용'/'!<기능>해제'로 방마다 켜고 끈다.
BOT_OWNER = os.getenv('BOT_OWNER', '밀떡밀떡')
FEATURES = ('파티봇', '현자', '업데이트', '퀘스트', '매크로', '질문', '도움말')
FEATURES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'room_features.json')
TOGGLE_RE = re.compile(r'^!(파티봇|현자|업데이트|퀘스트|매크로|질문|도움말)\s*(사용|해제)$')

# wikibot 검색(/ask/*)은 자체 rate limit이 있어 호출 간격을 띄운다
WIKIBOT_ASK_DELAY = 3.5
_last_ask_time = 0.0

KST = timezone(timedelta(hours=9))
MATCH_JOB_LABEL = {'warrior': '전사', 'rogue': '도적', 'mage': '법사', 'cleric': '직자', 'taoist': '도가'}
MATCH_SERVER_LABEL = {'seo': '세오', 'shus': '셔스'}


def _admin_headers():
    return {"Authorization": f"Bearer {WIKIBOT_TOKEN}"} if WIKIBOT_TOKEN else {}


# ── 유틸 ─────────────────────────────────────────────────
# ── 봇 테스트 (스튜디오 "봇 테스트" 탭) ─────────────────────
# 사이트가 테스트 요청을 쌓아 두면 여기서 몇 초마다 가져가 **실제 웹훅 코드**로 처리하고 답을 돌려준다.
# 테스트 방(chat_id 'test')의 답은 카톡으로 보내지 않고 모아 둔다. 방 기능은 전부 켜진 것으로 친다.
TEST_CHAT_ID = 'test'
BOT_TEST_URL = os.getenv('BOT_TEST_URL', 'https://milddok.cc/api/bot-test')
TEST_REPLIES = []


def send_reply(chat_id, message):
    """Iris를 통해 채팅방에 메시지 전송"""
    if str(chat_id) == TEST_CHAT_ID:
        TEST_REPLIES.append(str(message))
        return
    try:
        payload = {"type": "text", "room": str(chat_id), "data": message}
        resp = requests.post(f"{IRIS_URL}/reply", json=payload, timeout=5)
        logger.info(f"Reply -> {chat_id}: {resp.status_code}")
    except Exception as e:
        logger.error(f"Reply 전송 오류: {e}")


# ── 방 공지 (milddok.cc /notice-admin/) ─────────────────────
# 누가 들어올 때 / 정한 시각에 뿌리는 글. 목록은 사이트에서 1분마다 받아 온다 —
# 문구를 고치려고 여기 들어와 재시작하지 않는다. 인증은 파티 API와 같은 MATCH_BOT_KEY.
NOTICE_API_URL = os.getenv('NOTICE_API_URL', 'https://milddok.cc/api/notices')
NOTICE_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notice_state.json')
room_notices = notices.Notices(NOTICE_API_URL, MATCH_BOT_KEY, send_reply, NOTICE_STATE_FILE)


def _parse_feed(msg):
    """type 0(피드) 메시지의 본문은 JSON이다: {"feedType":4,"members":[...]} 등"""
    try:
        data = json.loads(msg) if isinstance(msg, str) else {}
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


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


def room_features(chat_id):
    """이 방에 켜진 기능 dict. 테스트 방(스튜디오 '봇 테스트')은 전부 켜진 것으로 본다."""
    if str(chat_id) == TEST_CHAT_ID:
        return {f: True for f in FEATURES}
    return _load_features().get(str(chat_id), {})


def feature_enabled(chat_id, feature):
    return bool(room_features(chat_id).get(feature))


def handle_feature_toggle(feature, action, chat_id):
    """'!<기능>사용'/'!<기능>해제' 처리 (호출 전 BOT_OWNER 확인 필수)."""
    data = _load_features()
    room = data.setdefault(str(chat_id), {})
    room[feature] = (action == '사용')
    _save_features(data)
    return f"[{feature}] 기능을 {'켰습니다' if room[feature] else '껐습니다'}."


def handle_feature_status(chat_id):
    """'!기능' — 이 방의 토글 상태 (BOT_OWNER 전용)."""
    room = room_features(chat_id)
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


LOD_BASE_URL = 'https://lod.nexon.com'
# 넥슨 게시판 글 링크를 짧게 — milddok.cc/g/<글번호> 가 넥슨으로 리다이렉트한다
SHORT_LINK_BASE = 'https://milddok.cc/g/'
_POST_ID_RE = re.compile(r'/game/(\d+)')


def _short_link(link):
    """넥슨 게시판 URL → milddok.cc/g/<id> 단축 링크 (실패 시 원본 유지)"""
    m = _POST_ID_RE.search(link or '')
    return f"{SHORT_LINK_BASE}{m.group(1)}" if m else link


def _excerpt(text, limit=200):
    """본문 발췌 — 빈 줄 정리 후 줄 단위로 limit자까지"""
    if not text:
        return ''
    lines = [l.strip() for l in str(text).strip().split('\n') if l.strip()]
    out, total = [], 0
    for line in lines:
        if total + len(line) > limit:
            remain = limit - total
            if remain > 15:
                out.append(line[:remain].rstrip() + '…')
            break
        out.append(line)
        total += len(line)
    return '\n'.join(out)


def handle_hyunja(msg):
    """!현자 [검색어] — 위키(노션) + 넥슨 게시판 통합 검색
    1번 결과에는 본문 발췌를 붙이고 나머지는 제목+링크만 회신한다."""
    query = msg[len('!현자'):].strip()
    if not query:
        return "검색어를 입력해주세요. 예: !현자 발록"
    result = ask_wikibot('/ask/community', query)
    if result is None:
        return "서버 연결에 실패했습니다. 잠시 후 다시 시도하세요."
    if not result.get('success'):
        return result.get('answer') or result.get('message') or "검색 결과가 없습니다."

    d = result.get('data') or {}
    wiki = result.get('wiki') or []

    # 게시판 결과
    items = []
    if d.get('title'):
        items.append({'title': d['title'], 'date': d.get('date'), 'link': d.get('link')})
    for r in (d.get('otherResults') or []):
        link = r.get('link') or ''
        if link.startswith('/'):
            link = LOD_BASE_URL + link
        items.append({'title': r.get('title'), 'date': r.get('date'), 'link': link})

    if not wiki and not items:
        return "검색 결과가 없습니다."

    blocks = [f"[현자 검색: {query}]"]

    # 위키 우선 노출
    if wiki:
        blocks.append(f"📖 위키 {len(wiki)}건")
        for i, w in enumerate(wiki[:3], 1):
            head = f"{i}. {w.get('title') or '(제목 없음)'}"
            page = w.get('page')
            if page and page != w.get('title'):
                head += f" — {page}"
            block = head
            # 1번 결과만 본문 발췌. 외부 링크 항목은 링크가 곧 답이라 발췌를 붙이지 않는다.
            if i == 1 and not w.get('isLink'):
                excerpt = _excerpt(w.get('snippet'), 180)
                if excerpt:
                    block += f"\n{excerpt}"
            if w.get('url'):
                block += f"\n{w['url']}"
            blocks.append(block)

    if items:
        blocks.append(f"📋 게시판 {len(items)}건")
        for i, it in enumerate(items[:5], 1):
            head = f"{i}. {it['title']}"
            if it.get('date'):
                head += f" ({it['date']})"
            block = head
            # 1번 결과만 본문 발췌 (wikibot이 첫 글 본문을 이미 긁어온다)
            if i == 1:
                excerpt = _excerpt(d.get('content'), 200)
                if excerpt:
                    block += f"\n{excerpt}"
            if it.get('link'):
                block += f"\n{_short_link(it['link'])}"
            blocks.append(block)
    elif result.get('message'):
        # 게시판만 실패(레이트리밋 등) — 위키 결과는 이미 위에 붙었다
        blocks.append(result['message'])

    return "\n\n".join(blocks)


def handle_update(msg):
    query = msg[len('!업데이트'):].strip()
    return format_wiki_answer(ask_wikibot('/ask/update', query), "업데이트 정보가 없습니다.")


def handle_help(chat_id):
    room = room_features(chat_id)
    lines = ["[밀떡봇 도움말]"]
    if room.get('파티봇'):
        lines.append("!파티결성 파티명 인원 — 파티 게시판에 등록")
        lines.append("  예) !파티결성 발록 5")
        lines.append("!파티확인 — 모집 중인 파티 목록")
    if room.get('현자'):
        lines.append("!현자 검색어 — 위키·게시판 검색")
        lines.append("  예) !현자 발록")
    if room.get('업데이트'):
        lines.append("!업데이트 — 최신 업데이트 확인")
    if room.get('퀘스트'):
        lines.append("!퀘스트 이름 — 동선·필요한 것·보상")
        lines.append("  예) !퀘스트 구피의부탁1")
        lines.append("!20단 — 그 단 필요 경험치·풀경험치 한 번 획득량 (!8단 20단 은 표)")
        lines.append("!단수 체력 300만 마력 150만 — 지금 몇 단인지")
    if room.get('질문'):
        lines.append("!질문 궁금한 것 — 방에 쌓인 대화에서 찾아 답합니다")
        lines.append("  예) !질문 초보자 뭐부터 해야해요")
        lines.append("!틀림 — 방금 !질문 답이 틀렸으면 이렇게 알려주세요 (운영자가 확인)")
        lines.append("  예) !틀림 이벤트 얘긴데 낚시가 나옴")
    if room.get('매크로'):
        lines.append("!매크로 — 키셋팅 안내 (목록 보기)")
        lines.append("  예) !매크로 사냥")
    if len(lines) == 1:
        return "이 방에서 사용할 수 있는 기능이 없습니다."
    custom = room_notices.commands_for(chat_id)
    if custom:
        lines.append("\n[이 방의 명령어]")
        lines.extend(f"- {c}" for c in custom)
    lines.append(f"\n파티 게시판: {MATCH_WEB_URL}")
    return "\n".join(lines)


# ── 퀘스트·길찾기 (milddok.cc/api/bot) ────────────────────
def _site_get(path, params):
    """사이트 봇 API 호출.

    응답의 `text`는 카톡에 **그대로 뿌리도록** 서버가 만들어 준 글이다.
    여기서 다시 조립하지 않는다 — 그러면 사이트와 봇의 답이 어긋나기 시작한다.
    """
    try:
        res = requests.get(f"{SITE_API_URL}{path}", params=params,
                           headers=_match_headers(), timeout=8)
        data = res.json()
    except Exception as e:
        logger.error(f"site api {path} 실패: {e}")
        return "사이트를 불러오지 못했습니다. 잠시 뒤 다시 시도해주세요."
    if not data.get('ok'):
        return data.get('error') or "요청을 처리하지 못했습니다."
    return data.get('text') or "결과가 없습니다."


def handle_ask(msg, chat_id, who='', room=''):
    """!질문 [궁금한 것] — 방에 쌓인 대화에서 찾아 답한다.

    **지어내지 않는다.** 비슷한 대화가 없으면 없다고 답한다 — 게임 정보는
    틀리면 사람이 헤매니 그럴듯한 답이 가장 나쁘다.
    """
    query = msg[len('!질문'):].strip()
    if not query:
        return "무엇이 궁금한지 같이 적어주세요.\n예) !질문 초보자 뭐부터 해야해요"
    if is_member_name(chat_id, query):
        return "사람에 대한 것은 답하지 않습니다. 게임에 대해 물어봐 주세요."

    # 30B 모델이라 10초를 넘기기도 한다. 그동안 아무 말이 없으면 죽은 줄 안다.
    send_reply(chat_id, "찾아보는 중입니다…")
    try:
        res = requests.get(ASK_URL, params={'q': query, 'chat_id': str(chat_id), 'who': who},
                           headers={'X-Ask-Key': ASK_KEY}, timeout=90)
        data = res.json()
        text = data.get('text') or "답을 만들지 못했습니다."
        LAST_ASK[str(chat_id)] = {'q': query, 'a': text, 'who': who, 'room': room, 'ts': int(time.time() * 1000)}
        _log_ask(chat_id, room, who, query, text, data)
        return text
    except Exception as e:
        logger.error(f"질문 실패: {e}")
        return "지금은 답할 수 없습니다. 잠시 뒤 다시 시도해주세요."

def _log_ask(chat_id, room, who, query, text, data):
    """질문·답 한 줄을 사이트에 남긴다. 기록이 안 된다고 답을 잃으면 안 된다 — 조용히 넘긴다."""
    if str(chat_id) == TEST_CHAT_ID:
        return   # 스튜디오 '봇 테스트'는 top 20에 섞지 않는다
    try:
        requests.post(ASK_LOG_URL, json={
            'chat_id': str(chat_id), 'room': room, 'who': who, 'question': query, 'answer': text,
            'source': str(data.get('source') or ''), 'tool': str(data.get('tool') or ''),
            'found': bool(data.get('found')),
        }, headers=_match_headers(), timeout=5)
    except Exception as e:  # noqa: BLE001
        logger.error(f"질문 기록 실패: {e}")


def handle_wrong(msg, chat_id, who=''):
    """!틀림 [한마디] — 이 방의 직전 !질문 답이 틀렸다고 사이트에 알린다.

    봇은 스스로 고치지 않는다. 운영자가 /ask-admin/ 에서 보고 위키·데이터·규칙 중 뭘 고칠지 정한다.
    """
    last = LAST_ASK.get(str(chat_id))
    if not last:
        return "최근에 답한 질문이 없습니다. !질문 뒤에 !틀림 을 쳐 주세요."
    note = msg[len('!틀림'):].strip()
    try:
        res = requests.post(ASK_REVIEW_URL, json={
            'chat_id': str(chat_id), 'room': last.get('room', ''), 'question': last['q'], 'answer': last['a'],
            'reporter': who, 'note': note, 'asked_at': last['ts'],
        }, headers=_match_headers(), timeout=8)
        data = res.json()
    except Exception as e:  # noqa: BLE001
        logger.error(f"틀림 신고 실패: {e}")
        return "지금은 접수하지 못했습니다. 잠시 뒤 다시 시도해주세요."
    if not data.get('ok'):
        return data.get('error') or "접수하지 못했습니다."
    n = data.get('reports') or 1
    tail = f" (지금까지 {n}명)" if n > 1 else ""
    return f"알려주셔서 고맙습니다. 운영자가 확인합니다.{tail}\n\n\"{last['q'][:40]}\" 에 대한 답이 틀린 것으로 접수됐습니다."


def handle_macro(msg):
    """!매크로 [사냥/이동/…] — 키셋팅을 글로 안내

    **파일이나 붙여넣기용 데이터는 주지 않는다.** 넥슨 고객센터가 키셋팅
    파일 공유형 서비스는 지양하고 텍스트 템플릿을 검토하라고 했다(2026-08-08).
    어느 키에 무슨 동작을 걸면 되는지 읽을 글만 전달한다.
    """
    query = msg[len('!매크로'):].strip()
    return _site_get('/macro', {'q': query})

def handle_quest(msg):
    """!퀘스트 [이름] — 동선·필요한 것·보상"""
    query = msg[len('!퀘스트'):].strip()
    if not query:
        return "퀘스트 이름을 입력해주세요.\n예) !퀘스트 구피의부탁1"
    return _site_get('/quest', {'q': query})


# `!20단` / `!8단 20단` / `!8단~20단`. 숫자 뒤 '단'은 두 번째는 생략해도 된다(!8단 20).
DANSU_RE = re.compile(r'^!(\d{1,2})단(?:\s*[~\-]?\s*(\d{1,2})단?)?$')
# `!단수 체력 300만 마력 150만` — 만·억 단위와 쉼표를 받는다.
DANSU_STAT_RE = re.compile(r'^!단수\s*(?:체력|체|hp)\s*([\d,만억.]+)\s*(?:마력|마|mp)\s*([\d,만억.]+)$', re.I)


def _korean_number(s):
    """'300만' '1.5억' '3,000,000' → 정수. 못 읽으면 None."""
    s = s.replace(',', '').strip()
    m = re.fullmatch(r'(\d+(?:\.\d+)?)(만|억)?', s)
    if not m:
        return None
    n = float(m.group(1)) * {'만': 10000, '억': 100000000, None: 1}[m.group(2)]
    return int(round(n))


def handle_dansu(msg):
    """단수 — 라르 계산기의 단수표·풀경험치표 그대로. 계산은 사이트가 한다."""
    m = DANSU_STAT_RE.match(msg)
    if m:
        hp, mp = _korean_number(m.group(1)), _korean_number(m.group(2))
        if hp is None or mp is None:
            return "체력·마력을 숫자로 적어주세요.\n예) !단수 체력 300만 마력 150만"
        return _site_get('/dansu', {'hp': hp, 'mp': mp})
    m = DANSU_RE.match(msg)
    if not m:
        return "예) !20단 / !8단 20단 / !단수 체력 300만 마력 150만"
    if m.group(2):
        return _site_get('/dansu', {'from': int(m.group(1)), 'to': int(m.group(2))})
    return _site_get('/dansu', {'n': int(m.group(1))})


def handle_route(msg):
    """!길찾기 [출발] [도착] — 포탈·대륙지도를 아우른 최단 경로

    맵 이름에 띄어쓰기가 든 경우가 있어 '>'나 쉼표로도 나눌 수 있게 둔다.
    """
    body = msg[len('!길찾기'):].strip()
    for sep in ('>', ','):
        if sep in body:
            a, _, b = body.partition(sep)
            a, b = a.strip(), b.strip()
            break
    else:
        parts = body.split(None, 1)
        a, b = (parts + ['', ''])[:2]
        a, b = a.strip(), b.strip()
    if not a or not b:
        return "출발 맵과 도착 맵을 입력해주세요.\n예) !길찾기 밀레스마을 나겔링마을"
    return _site_get('/route', {'from': a, 'to': b})

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

        # 봇이 본 방 목록 — 관리 화면(/notice-admin/)에서 방을 고르게 하려고 사이트에 올린다.
        room_notices.note_room(chat_id, room)

        # 방에 누가 들어옴(피드 feedType 4) → 입장 공지. 초대한 사람이 없으면(링크 입장) sender가 비므로
        # 아래 "빈 발신자 무시"보다 먼저 본다. 4=들어옴, 2=나감.
        if msg_type == '0':
            feed = _parse_feed(msg)
            if feed.get('feedType') == 4:
                for m in feed.get('members') or []:
                    note_name(chat_id, (m or {}).get('nickName'))
                room_notices.on_join(chat_id, room, feed.get('members') or [])
            return jsonify({"status": "ok"})

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
        note_name(chat_id, sender)

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

        # 방마다 정해 둔 명령어(/notice-admin/ '명령어를 치면') — 정확히 그 말일 때만 답한다.
        # 봇에 원래 있는 명령과 겹치는 말은 사이트가 저장 때 막는다.
        if room_notices.on_command(chat_id, msg_stripped, sender_name, room):
            return jsonify({"status": "ok"})

        # 대화 저장 — '질문'을 켠 방만. 명령어와 봇 자기 말은 넣지 않는다.
        # 무엇을 더 버릴지는 색인을 만들 때 정한다(원본은 그대로 쌓는다).
        if chat_archive and feature_enabled(chat_id, '질문') \
                and not msg_stripped.startswith('!'):
            chat_archive.save(
                msg_id=str(json_info.get('id') or ''),
                chat_id=chat_id, room=room,
                sender=sender_name, text=msg)

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
        if (DANSU_RE.match(msg_stripped) or msg_stripped.startswith("!단수")) and feature_enabled(chat_id, '퀘스트'):
            send_reply(chat_id, handle_dansu(msg_stripped))
            return jsonify({"status": "ok"})
        if msg_stripped.startswith("!퀘스트") and feature_enabled(chat_id, '퀘스트'):
            send_reply(chat_id, handle_quest(msg_stripped))
            return jsonify({"status": "ok"})
        # !길찾기는 2026-09-08 뺐다 — 게임 안 길찾기가 잘 된다(운영자). handle_route는 남겨 둔다.
        if msg_stripped.startswith("!질문") and feature_enabled(chat_id, '질문'):
            send_reply(chat_id, handle_ask(msg_stripped, chat_id, sender_name, room))
            return jsonify({"status": "ok"})
        if (msg_stripped == "!틀림" or msg_stripped.startswith("!틀림 ")) and feature_enabled(chat_id, '질문'):
            send_reply(chat_id, handle_wrong(msg_stripped, chat_id, sender_name))
            return jsonify({"status": "ok"})
        if msg_stripped.startswith("!매크로") and feature_enabled(chat_id, '매크로'):
            send_reply(chat_id, handle_macro(msg_stripped))
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


def _run_bot_test(test):
    """테스트 하나를 웹훅과 같은 길로 돌린다. Flask test client라 실제 요청과 같은 코드가 돈다."""
    TEST_REPLIES.clear()
    payload = {
        'msg': test.get('text', ''), 'room': '봇 테스트', 'sender': f"{test.get('sender') or '운영자'}/테스트",
        'json': {'type': '1', 'chat_id': TEST_CHAT_ID, 'user_id': 'test', 'id': str(test.get('id')),
                 'v': '{"isMine":false}'},
    }
    try:
        with app.test_client() as c:
            c.post('/webhook', json=payload)
    except Exception as e:  # noqa: BLE001
        TEST_REPLIES.append(f"(봇 오류: {e})")
    reply = '\n\n'.join(TEST_REPLIES)
    try:
        requests.post(f"{BOT_TEST_URL}/{test.get('id')}", json={'reply': reply}, headers=_match_headers(), timeout=8)
    except Exception as e:  # noqa: BLE001
        logger.error(f"봇 테스트 결과 전송 실패: {e}")


def bot_test_loop():
    last_seen = 0.0
    while True:
        try:
            res = requests.get(f"{BOT_TEST_URL}/next", headers=_match_headers(), timeout=8)
            test = res.json().get('test') if res.ok else None
            if test:
                last_seen = time.time()
                logger.info(f"봇 테스트 #{test.get('id')}: {str(test.get('text'))[:60]}")
                _run_bot_test(test)
                continue          # 하나 끝났으면 바로 다음 것을 본다
        except Exception as e:  # noqa: BLE001
            logger.error(f"봇 테스트 폴링 실패: {e}")
        time.sleep(3 if time.time() - last_seen < 600 else 30)


if __name__ == '__main__':
    threading.Thread(target=bot_test_loop, name='bot-test', daemon=True).start()
    room_notices.start()
    app.run(host='0.0.0.0', port=5000)
