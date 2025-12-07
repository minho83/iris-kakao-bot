좋아요! Claude Code에서 작업할 수 있도록 정리해드릴게요.

---

## 📋 카카오톡 봇 + RAG 서버 연동 정보

### 1. 시스템 구성

| 서비스 | 주소 | 역할 |
|--------|------|------|
| 카카오톡 봇 서버 | http://localhost:5000 | 메시지 수신/발신 |
| RAG 서버 | http://localhost:8100 | AI 질문/답변 |
| Qdrant | http://localhost:6333 | 벡터 데이터베이스 |
| Iris API | http://localhost:3000 | 카카오톡 메시지 전송 |

---

### 2. 수정할 파일

```
~/iris-kakao-bot/bot-server/app.py
```

---

### 3. RAG 서버 API

**질문하기:**
```
POST http://localhost:8100/ask
Content-Type: application/json

{"query": "질문 내용"}
```

**응답 형식:**
```json
{
  "answer": "AI가 생성한 답변",
  "sources": [
    {"title": "제목", "url": "원본링크", "score": 0.xx}
  ]
}
```

**데이터 추가:**
```
POST http://localhost:8100/add
Content-Type: application/json

{
  "title": "제목",
  "content": "내용",
  "category": "카테고리",
  "source_url": "원본링크"
}
```

---

### 4. 봇 로직 흐름

```
사용자 메시지 → 봇 서버(app.py) → RAG 서버(/ask) → AI 답변 → 카카오톡 응답
```

---

### 5. 구현 예시

```python
# app.py에 추가할 내용

RAG_URL = "http://localhost:8100"

def ask_rag(query):
    """RAG 서버에 질문"""
    try:
        response = requests.post(
            f"{RAG_URL}/ask",
            json={"query": query},
            timeout=30
        )
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        logger.error(f"RAG error: {e}")
        return None

# 메시지 처리에서 사용
if msg.startswith("질문 ") or msg.startswith("검색 "):
    query = msg.split(" ", 1)[1]
    result = ask_rag(query)
    if result:
        answer = result["answer"]
        sources = result.get("sources", [])
        # 응답 구성
```

---

### 6. 적용 후 재시작

```bash
docker restart iris-bot-server
```

---

### 7. 테스트 명령어 예시

카카오톡에서:
```
질문 기사 스탯 어떻게 찍어?
검색 초보 사냥터 추천
```

---

**이 정보로 Claude Code에서 작업하면 됩니다!** 🚀

추가로 필요한 정보 있으면 알려주세요!