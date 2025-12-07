from flask import Flask, request, jsonify
import requests
import os
import logging
import time
from datetime import datetime

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

IRIS_URL = os.getenv('IRIS_URL', 'http://192.168.0.80:3000')
RAG_URL = os.getenv('RAG_URL', "http://localhost:8100")

# 요청 딜레이 관리를 위한 변수
last_request_time = 0
REQUEST_DELAY = 2  # 2초 딜레이

def ask_rag(query):
    """RAG 서버에 질문"""
    global last_request_time
    
    try:
        # 딜레이 처리
        current_time = time.time()
        time_since_last = current_time - last_request_time
        if time_since_last < REQUEST_DELAY:
            sleep_time = REQUEST_DELAY - time_since_last
            logger.info(f"딜레이 적용: {sleep_time:.1f}초 대기")
            time.sleep(sleep_time)
        
        last_request_time = time.time()
        
        logger.info(f"RAG 서버에 질문: {query}")
        response = requests.post(
            f"{RAG_URL}/ask",
            json={"query": query},
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"RAG 응답 받음: {result}")
            return result
        else:
            logger.error(f"RAG 서버 오류: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"RAG 서버 통신 오류: {e}")
        return None

def handle_rag_question(question, sender, command_type):
    """RAG 서버를 통해 질문에 대한 답변을 생성합니다."""
    try:
        logger.info(f"RAG {command_type} 처리: {question} from {sender}")
        
        # RAG 서버에 질문
        result = ask_rag(question)
        
        if result is None:
            return f"{sender}님, RAG 서버 연결에 실패했습니다. 잠시 후 다시 시도해주세요."
        
        answer = result.get("answer", "답변을 생성하지 못했습니다.")
        sources = result.get("sources", [])
        
        # 응답 구성
        response = f"💬 {sender}님의 {command_type}에 대한 답변:\n\n"
        response += f"{answer}\n"
        
        # 출처 정보 추가 (있는 경우)
        if sources:
            response += "\n📚 참고 자료:\n"
            for source in sources[:3]:  # 최대 3개까지
                title = source.get("title", "제목 없음")
                score = source.get("score", 0)
                response += f"• {title} (신뢰도: {score:.2f})\n"
        
        return response.strip()
        
    except Exception as e:
        logger.error(f"RAG 처리 오류: {e}")
        return "죄송합니다. 질문 처리 중 오류가 발생했습니다."

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy"})

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        
        # 전체 데이터 출력
        logger.info(f"받은 데이터: {data}")
        
        msg = data.get('msg', '')
        room = data.get('room', '')
        sender = data.get('sender', '')
        json_data = data.get('json', {})
        
        # chat_id 가져오기 (숫자 형태)
        chat_id = json_data.get('chat_id', room)
        
        logger.info(f"[{room}] {sender}: {msg}")
        logger.info(f"chat_id: {chat_id}")
        
        response_msg = None
        msg_lower = msg.lower().strip()
        
        if msg.startswith("!질문"):
            question = msg[3:].strip()
            if question:
                response_msg = handle_rag_question(question, sender, "질문")
            else:
                response_msg = "질문을 입력해주세요. 예: !질문 무한의탑탑?"
        elif msg.startswith("!검색"):
            query = msg[3:].strip()
            if query:
                response_msg = handle_rag_question(query, sender, "검색")
            else:
                response_msg = "검색어를 입력해주세요. 예: !검색 1서클 퀘스트트"
        elif msg_lower == "안녕":
            response_msg = f"안녕하세요 {sender}님!"
        elif msg_lower == "시간":
            response_msg = f"현재 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        elif msg_lower == "도움말":
            response_msg = "명령어: 안녕, 시간, 도움말, !질문 [질문내용], !검색 [검색어]"
        
        if response_msg:
            payload = {"type": "text", "room": str(chat_id), "data": response_msg}
            logger.info(f"전송 payload: {payload}")
            resp = requests.post(f"{IRIS_URL}/reply", json=payload, timeout=5)
            logger.info(f"Iris 응답: {resp.status_code} - {resp.text}")
        
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
