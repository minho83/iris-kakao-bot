from flask import Flask, request, jsonify
import requests
import os
import logging
import json
import numpy as np
import faiss
from sentence_transformers import SentenceTransformer
from datetime import datetime

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

IRIS_URL = os.getenv('IRIS_URL', 'http://192.168.0.80:3000')

# RAG 시스템 초기화
model = None
index = None
game_data = []

def initialize_rag_system():
    """RAG 시스템을 초기화합니다."""
    global model, index, game_data
    
    try:
        logger.info("RAG 시스템 초기화 중...")
        
        # SentenceTransformer 모델 로드
        model = SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')
        
        # 게임 데이터 로드
        with open('game_data.json', 'r', encoding='utf-8') as f:
            game_data = json.load(f)
        
        # 게임 정보를 임베딩으로 변환
        texts = [f"{game['title']} {game['genre']} {game['content']}" for game in game_data]
        embeddings = model.encode(texts)
        
        # FAISS 인덱스 생성
        dimension = embeddings.shape[1]
        index = faiss.IndexFlatIP(dimension)  # Inner Product (코사인 유사도)
        
        # 임베딩을 정규화하여 코사인 유사도로 사용
        embeddings = embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)
        index.add(embeddings.astype('float32'))
        
        logger.info(f"RAG 시스템 초기화 완료! 게임 데이터 {len(game_data)}개 로드됨")
        
    except Exception as e:
        logger.error(f"RAG 시스템 초기화 실패: {e}")

def search_game_info(query, top_k=2):
    """질문과 관련된 게임 정보를 검색합니다."""
    try:
        if model is None or index is None:
            return []
        
        # 질문을 임베딩으로 변환
        query_embedding = model.encode([query])
        query_embedding = query_embedding / np.linalg.norm(query_embedding, axis=1, keepdims=True)
        
        # 유사도 검색
        scores, indices = index.search(query_embedding.astype('float32'), top_k)
        
        results = []
        for i, (score, idx) in enumerate(zip(scores[0], indices[0])):
            if idx < len(game_data):
                game = game_data[idx]
                results.append({
                    'game': game,
                    'score': float(score)
                })
        
        return results
        
    except Exception as e:
        logger.error(f"게임 정보 검색 오류: {e}")
        return []

def handle_rag_question(question, sender):
    """RAG 시스템을 통해 질문에 대한 답변을 생성합니다."""
    try:
        logger.info(f"RAG 질문 처리: {question} from {sender}")
        
        # RAG 시스템이 초기화되지 않은 경우
        if model is None or index is None:
            return "RAG 시스템이 아직 초기화되지 않았습니다. 잠시 후 다시 시도해주세요."
        
        # 관련 게임 정보 검색
        search_results = search_game_info(question, top_k=2)
        
        if not search_results:
            return f"{sender}님, 질문과 관련된 게임 정보를 찾을 수 없습니다. 다른 질문을 시도해보세요."
        
        # 검색 결과를 바탕으로 답변 생성
        response = f"{sender}님의 질문에 대한 답변입니다:\n\n"
        
        for i, result in enumerate(search_results):
            game = result['game']
            score = result['score']
            
            # 유사도가 충분히 높은 경우에만 포함 (0.3 이상)
            if score > 0.3:
                response += f"📱 {game['title']} ({game['genre']})\n"
                response += f"{game['content']}\n\n"
        
        # 관련 정보가 없는 경우
        if len([r for r in search_results if r['score'] > 0.3]) == 0:
            response = f"{sender}님, 질문과 관련된 게임 정보를 찾을 수 없습니다. 다른 질문을 시도해보세요."
        
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
                response_msg = handle_rag_question(question, sender)
            else:
                response_msg = "질문을 입력해주세요. 예: !질문 게임 조작법이 뭐야?"
        elif msg_lower == "안녕":
            response_msg = f"안녕하세요 {sender}님!"
        elif msg_lower == "시간":
            response_msg = f"현재 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        elif msg_lower == "도움말":
            response_msg = "명령어: 안녕, 시간, 도움말, !질문 [질문내용]"
        
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
    initialize_rag_system()
    app.run(host='0.0.0.0', port=5000)
