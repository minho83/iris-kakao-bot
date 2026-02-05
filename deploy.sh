#!/bin/bash
# wikibot-kakao 서버 배포 스크립트
# 사용법: ./deploy.sh
# 환경변수 WIKIBOT_CONTAINER로 컨테이너 이름 변경 가능

CONTAINER_NAME="${WIKIBOT_CONTAINER:-wikibot-kakao}"
REPO_DIR="/home/user/wikibot-kakao"

echo "[deploy] 배포 시작: $(date)"

cd "$REPO_DIR" || { echo "[deploy] 디렉토리 이동 실패: $REPO_DIR"; exit 1; }

echo "[deploy] git pull..."
git pull origin master || { echo "[deploy] git pull 실패"; exit 1; }

echo "[deploy] Docker 빌드..."
docker build -t "$CONTAINER_NAME" . || { echo "[deploy] Docker 빌드 실패"; exit 1; }

echo "[deploy] 기존 컨테이너 중지 및 제거..."
docker stop "$CONTAINER_NAME" 2>/dev/null
docker rm "$CONTAINER_NAME" 2>/dev/null

echo "[deploy] 새 컨테이너 시작..."
docker run -d \
  --name "$CONTAINER_NAME" \
  -p 3000:3000 \
  --restart unless-stopped \
  "$CONTAINER_NAME"

echo "[deploy] 배포 완료: $(date)"
