#!/usr/bin/env python3
"""
게임 정보를 RAG 서버에 추가하는 스크립트
"""
import requests
import json

RAG_URL = "http://localhost:8100"

# 게임 팁 데이터
game_data = [
    {
        "title": "기사 스탯 가이드",
        "content": """
기사는 근접 탱커 직업입니다.
추천 스탯: 힘 70%, 체력 30%
초반에는 체력 위주로 찍다가 레벨 50 이후부터 힘을 올리는 것이 좋습니다.
무기는 한손검+방패 조합이 안정적입니다.
        """.strip(),
        "category": "직업가이드",
        "source_url": "https://example.com/knight"
    },
    {
        "title": "마법사 스탯 가이드",
        "content": """
마법사는 원거리 딜러 직업입니다.
추천 스탯: 지능 80%, 정신력 20%
지능을 최우선으로 올려야 마법 데미지가 강해집니다.
마나 관리를 위해 정신력도 적당히 투자하세요.
        """.strip(),
        "category": "직업가이드",
        "source_url": "https://example.com/mage"
    },
    {
        "title": "초보자 사냥터 추천 (1-20레벨)",
        "content": """
1-10레벨: 초보자 숲 - 슬라임, 토끼 사냥
10-15레벨: 늑대 동굴 - 늑대, 박쥐 사냥
15-20레벨: 고블린 마을 - 고블린 사냥
경험치 효율이 좋고 아이템 드랍률도 괜찮습니다.
        """.strip(),
        "category": "사냥터",
        "source_url": "https://example.com/hunting1"
    },
    {
        "title": "초보자 사냥터 추천 (21-40레벨)",
        "content": """
21-30레벨: 오크 요새 - 오크 전사 사냥
30-35레벨: 언데드 묘지 - 스켈레톤, 좀비 사냥
35-40레벨: 용암 동굴 - 화염 정령 사냥
파티 플레이를 추천합니다.
        """.strip(),
        "category": "사냥터",
        "source_url": "https://example.com/hunting2"
    },
    {
        "title": "장비 강화 팁",
        "content": """
강화석은 +5까지는 실패 확률이 낮으니 시도해보세요.
+6부터는 실패 시 파괴될 수 있으니 보호석을 사용하세요.
강화 확률 이벤트 기간에 도전하는 것이 유리합니다.
무기 > 방어구 순으로 강화하는 것을 추천합니다.
        """.strip(),
        "category": "아이템",
        "source_url": "https://example.com/enhance"
    },
    {
        "title": "골드 파밍 방법",
        "content": """
1. 데일리 퀘스트 완료 - 하루 10만 골드 보장
2. 보스 몬스터 사냥 - 희귀 아이템 판매
3. 채집/제작 - 포션, 장비 제작 후 판매
4. 경매장 시세차익 - 저가 구매 후 재판매
매일 꾸준히 하면 일주일에 100만 골드 모을 수 있습니다.
        """.strip(),
        "category": "재화",
        "source_url": "https://example.com/gold"
    },
    {
        "title": "파티 플레이 팁",
        "content": """
이상적인 파티 구성: 탱커 1명, 딜러 2명, 힐러 1명
탱커가 몬스터를 끌고, 딜러가 공격, 힐러가 회복하는 역할 분담이 중요합니다.
아이템 분배는 사전에 협의하세요 (주사위, 입찰, 분배 등)
보스 레이드는 최소 4인 이상 추천합니다.
        """.strip(),
        "category": "플레이",
        "source_url": "https://example.com/party"
    }
]

def add_document(doc):
    """문서를 RAG 서버에 추가"""
    try:
        response = requests.post(
            f"{RAG_URL}/add",
            json=doc,
            timeout=10
        )
        if response.status_code == 200:
            result = response.json()
            print(f"✅ 추가 완료: {doc['title']} (ID: {result['id']})")
            return True
        else:
            print(f"❌ 실패: {doc['title']} - {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ 오류: {doc['title']} - {e}")
        return False

def main():
    print("=" * 60)
    print("게임 정보 데이터 추가 시작")
    print("=" * 60)

    # 현재 문서 수 확인
    try:
        stats = requests.get(f"{RAG_URL}/stats").json()
        print(f"현재 저장된 문서 수: {stats['total_documents']}\n")
    except:
        print("⚠️  RAG 서버 상태 확인 실패\n")

    # 데이터 추가
    success_count = 0
    for doc in game_data:
        if add_document(doc):
            success_count += 1

    print("\n" + "=" * 60)
    print(f"완료: {success_count}/{len(game_data)} 문서 추가됨")
    print("=" * 60)

    # 최종 문서 수 확인
    try:
        stats = requests.get(f"{RAG_URL}/stats").json()
        print(f"최종 문서 수: {stats['total_documents']}")
    except:
        pass

if __name__ == "__main__":
    main()
