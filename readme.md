좋습니다! 요약해드릴게요! 😊

---

## 📋 Claude Code로 봇 개발 요약

### 1. 봇 코드 파일 위치

```
~/iris-kakao-bot/bot-server/app.py
```

---

### 2. 수정 후 적용 방법

```bash
# 코드 수정 후 이 명령어 실행
docker restart iris-bot-server
```

---

### 3. 현재 봇 구조

```python
# 메시지 수신 → webhook 함수가 처리
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    msg = data.get('msg', '')        # 메시지 내용
    room = data.get('room', '')      # 채팅방 이름
    sender = data.get('sender', '')  # 발신자 이름
    chat_id = data.get('json', {}).get('chat_id', room)  # 채팅방 ID (숫자)
```

---

### 4. 응답 보내는 방법

```python
# Iris API로 메시지 전송
payload = {
    "type": "text",           # 텍스트 메시지
    "room": str(chat_id),     # 채팅방 ID (문자열)
    "data": "보낼 메시지"      # 메시지 내용
}
requests.post("http://192.168.0.80:3000/reply", json=payload)
```

---

### 5. 이미지 전송

```python
import base64

# 이미지를 base64로 인코딩
with open("image.png", "rb") as f:
    image_data = base64.b64encode(f.read()).decode()

payload = {
    "type": "image",
    "room": str(chat_id),
    "data": image_data
}
requests.post("http://192.168.0.80:3000/reply", json=payload)
```

---

### 6. 로그 확인

```bash
docker logs -f iris-bot-server
```

---

