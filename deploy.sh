#!/bin/bash
# wikibot-kakao 자동 배포 스크립트
# 사용법: ./deploy.sh [--force]
#   --force: 변경 감지 없이 강제 배포 (GitHub Actions용)
#   기본: cron으로 주기적 실행, 변경 있을 때만 빌드/재시작

export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

REPO_DIR="$HOME/wikibot-kakao"
IMAGE_NAME="wikibot-kakao"
CONTAINER_NAME="wikibot-server"
DATA_DIR="$HOME/wikibot-data"
ENV_FILE="$DATA_DIR/.env"
LOG_FILE="$REPO_DIR/deploy.log"
FORCE_DEPLOY=false

# --force 옵션 체크
if [ "$1" = "--force" ]; then
    FORCE_DEPLOY=true
fi

# DB 파일은 repo 밖 data 디렉토리에 저장 (git 영향 방지)
DB_DIR="$DATA_DIR"

cd "$REPO_DIR" || exit 1

# 최신 변경사항 가져오기
git fetch origin master 2>/dev/null

# 로컬과 원격 비교 (--force가 아닐 때만)
if [ "$FORCE_DEPLOY" = false ]; then
    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse origin/master)

    if [ "$LOCAL" = "$REMOTE" ]; then
        exit 0
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 변경 감지: $LOCAL -> $REMOTE" >> "$LOG_FILE"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 강제 배포 시작 (--force)" >> "$LOG_FILE"
fi

# data 디렉토리 보장
mkdir -p "$DB_DIR"

# 기존 repo 내 DB 파일 → data 디렉토리로 마이그레이션 (1회성)
for db in nickname.db notice.db trade.db party.db; do
    if [ -f "$REPO_DIR/$db" ] && [ ! -f "$DB_DIR/$db" ]; then
        cp "$REPO_DIR/$db" "$DB_DIR/$db"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] DB 마이그레이션: $db → $DB_DIR/" >> "$LOG_FILE"
    fi
done

# pull + submodule update
git pull origin master >> "$LOG_FILE" 2>&1
git submodule update --init --recursive >> "$LOG_FILE" 2>&1

# Docker 이미지 빌드
docker build -t "$IMAGE_NAME" . >> "$LOG_FILE" 2>&1

if [ $? -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 빌드 실패" >> "$LOG_FILE"
    exit 1
fi

# DB 파일이 없으면 생성 (디렉토리가 아닌 파일로)
for db in nickname.db notice.db trade.db party.db; do
    if [ ! -f "$DB_DIR/$db" ]; then
        touch "$DB_DIR/$db"
    fi
done

# 배포 전 DB 백업 (0바이트가 아닌 경우만)
BACKUP_DIR="$DB_DIR/backup"
mkdir -p "$BACKUP_DIR"
for db in nickname.db notice.db trade.db party.db; do
    if [ -s "$DB_DIR/$db" ]; then
        cp "$DB_DIR/$db" "$BACKUP_DIR/${db}.bak"
    fi
done

# wikibot 컨테이너 재시작 (DB + LOD_DB 볼륨 마운트)
docker stop "$CONTAINER_NAME" 2>/dev/null
sleep 3  # SIGTERM 처리 대기
docker rm "$CONTAINER_NAME" 2>/dev/null
docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    -p 8100:3000 \
    -v "$DB_DIR/nickname.db:/app/nickname.db" \
    -v "$DB_DIR/notice.db:/app/notice.db" \
    -v "$DB_DIR/trade.db:/app/trade.db" \
    -v "$DB_DIR/party.db:/app/party.db" \
    -v "$REPO_DIR/LOD_DB:/app/LOD_DB" \
    ${ENV_FILE:+--env-file "$ENV_FILE"} \
    "$IMAGE_NAME" >> "$LOG_FILE" 2>&1

# iris-bot 코드 동기화 + pycache 삭제 + 재시작
IRIS_DIR="$HOME/iris-kakao-bot"
if [ -d "$IRIS_DIR" ]; then
    cp "$REPO_DIR/iris-kakao-bot/app.py" "$IRIS_DIR/bot-server/app.py" 2>/dev/null
    docker exec iris-bot-server rm -rf /app/__pycache__ 2>/dev/null
    docker restart iris-bot-server >> "$LOG_FILE" 2>&1
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] iris-bot 동기화 완료" >> "$LOG_FILE"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 배포 완료: $(git rev-parse --short HEAD)" >> "$LOG_FILE"
