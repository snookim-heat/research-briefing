"""
Weekly research digest -> Notion page.

지난 7일간 Notion DB에 추가된 논문들을 모아서, 트렌드/핵심 3편/본인 연구
시너지 포인트를 종합한 페이지를 새로 생성한다.

매주 월요일 아침 한국시간 8시에 실행 (GitHub Actions cron).

필요한 환경변수 (GitHub Secrets로 등록 — 일일 빌드와 동일):
  OPENAI_API_KEY      -- OpenAI API 키
  NOTION_TOKEN        -- Notion Connection의 Access token
  NOTION_DATABASE_ID  -- 논문 DB ID (일일 빌드와 동일한 DB)
  NOTION_DIGEST_PAGE_ID  -- 다이제스트가 자식 페이지로 추가될 부모 페이지 ID
                            (선택: 없으면 DB 자체의 부모를 사용)
"""

import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

from openai import OpenAI

# ============================================================================
# CONFIG
# ============================================================================

LOOKBACK_DAYS = 7              # 지난 며칠을 종합할지
MAX_PAPERS_FOR_DIGEST = 30     # 너무 많으면 OpenAI 컨텍스트 초과; 상위 N편만
TOP_HIGHLIGHTS = 3             # 다이제스트 본문에서 강조할 핵심 논문 수

OPENAI_MODEL = "gpt-4o-mini"

YOUR_RESEARCH_CONTEXT = """
연구자는 기계공학 박사후연구원이며 다음 영역에서 활동:

[과거~현재 연구]
- 상변화 물질(PCM) 기반 전력전자 과도열관리
- 바로칼로릭 재료 기반 열관리

[앞으로 진행할 연구]
- 데이터센터 열관리
- 반도체 패키징 레벨 열관리 (2.5D/3D, chiplet, TIM, embedded cooling)
""".strip()


# ============================================================================
# Notion API
# ============================================================================

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def notion_request(method: str, path: str, body: dict | None = None) -> dict:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("ERROR: NOTION_TOKEN missing", file=sys.stderr)
        sys.exit(1)

    url = f"{NOTION_API}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Notion-Version", NOTION_VERSION)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"[notion] HTTP {e.code}: {err_body}", file=sys.stderr)
        raise


def fetch_recent_papers(database_id: str, days: int) -> list[dict]:
    """지난 N일간 DB에 추가된 페이지들을 가져옴."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    papers = []
    start_cursor = None
    while True:
        body = {
            "page_size": 100,
            "filter": {
                "timestamp": "created_time",
                "created_time": {"after": cutoff},
            },
            "sorts": [
                {"property": "Relevance", "direction": "descending"}
            ],
        }
        if start_cursor:
            body["start_cursor"] = start_cursor
        result = notion_request("POST", f"/databases/{database_id}/query", body)

        for page in result.get("results", []):
            props = page.get("properties", {})
            papers.append({
                "page_id": page["id"],
                "title": _get_title(props),
                "authors": _get_rich_text(props, "Authors"),
                "venue": _get_rich_text(props, "Venue"),
                "source": _get_select(props, "Source"),
                "tags": _get_multi_select(props, "Tags"),
                "relevance": _get_number(props, "Relevance"),
                "must_read": _get_checkbox(props, "Must read"),
                "doi": _get_rich_text(props, "DOI"),
                "arxiv_id": _get_rich_text(props, "arXiv ID"),
                "link": _get_url(props, "arXiv link"),
                "published": _get_date(props, "Published"),
            })

        if not result.get("has_more"):
            break
        start_cursor = result.get("next_cursor")

    print(f"[fetch] {len(papers)} papers in last {days} days")
    return papers


def _get_title(props):
    t = props.get("Title", {}).get("title", [])
    return t[0]["plain_text"] if t else ""

def _get_rich_text(props, key):
    rt = props.get(key, {}).get("rich_text", [])
    return "".join(part.get("plain_text", "") for part in rt)

def _get_select(props, key):
    sel = props.get(key, {}).get("select")
    return sel["name"] if sel else ""

def _get_multi_select(props, key):
    ms = props.get(key, {}).get("multi_select", [])
    return [item["name"] for item in ms]

def _get_number(props, key):
    return props.get(key, {}).get("number") or 0

def _get_checkbox(props, key):
    return bool(props.get(key, {}).get("checkbox"))

def _get_url(props, key):
    return props.get(key, {}).get("url") or ""

def _get_date(props, key):
    d = props.get(key, {}).get("date")
    return d["start"] if d else ""


def fetch_page_summary_text(page_id: str) -> str:
    """페이지 본문에서 메커니즘/한계/활용포인트 텍스트만 추출 (다이제스트용)."""
    try:
        result = notion_request("GET", f"/blocks/{page_id}/children?page_size=100")
    except Exception:
        return ""

    chunks = []
    for block in result.get("results", []):
        btype = block.get("type")
        if btype == "callout":
            rt = block["callout"].get("rich_text", [])
            text = "".join(p.get("plain_text", "") for p in rt)
            if text:
                chunks.append(text)
        elif btype == "toggle":
            rt = block["toggle"].get("rich_text", [])
            label = "".join(p.get("plain_text", "") for p in rt)
            chunks.append(f"[{label}]")
    return " | ".join(chunks)


# ============================================================================
# OpenAI 메타 요약
# ============================================================================

DIGEST_SYSTEM_PROMPT = f"""\
당신은 기계공학 박사후연구원을 위한 주간 논문 큐레이터다.

연구자 배경:
{YOUR_RESEARCH_CONTEXT}

지난 일주일간 수집된 논문 목록을 보고, 다음을 한국어 JSON으로 반환:

{{
  "headline": "이번 주를 한 문장으로 (60자 이내)",
  "trend_summary": "이번 주 모인 논문들에서 보이는 트렌드와 큰 흐름 3~5문장.
                    어떤 주제가 활발한지, 어떤 방향이 부상하는지.",
  "synergy_for_you": "본인 연구(과거·미래)와의 시너지 포인트를 4~5문장.
                      활용 가능한 협업/인용/실험 매핑 등 구체적으로.",
  "highlights": [
    {{
      "title": "핵심 논문 제목 그대로",
      "why_important": "왜 이번 주 핵심인지 2~3문장"
    }},
    ... (정확히 {TOP_HIGHLIGHTS}개)
  ],
  "tag_distribution_note": "태그 분포에서 읽히는 인사이트 1~2문장"
}}

스타일:
- 문장 시작 "이", "이것은" 금지
- 콜론(:) 금지
- "최적화" 대신 "조정/튜닝/탐색"
- 단정조 피하고 정직한 톤
- JSON 객체 하나만 반환. 마크다운 코드 펜스 금지.
"""


def generate_digest(client: OpenAI, papers: list[dict]) -> dict | None:
    """OpenAI에 전체 논문 리스트를 던지고 메타 요약을 받음."""
    # 상위 N편만 (관련성 내림차순 — 이미 정렬되어 있음)
    top_papers = papers[:MAX_PAPERS_FOR_DIGEST]

    lines = []
    for i, p in enumerate(top_papers, 1):
        tags = ", ".join(p["tags"]) if p["tags"] else "(no tags)"
        venue = p["venue"] or p["source"] or "?"
        must = " ★" if p["must_read"] else ""
        lines.append(
            f"{i}. [{p['relevance']}/100{must}] {p['title']} "
            f"({venue}) — tags: {tags}"
        )
    paper_list_text = "\n".join(lines)

    user_msg = (
        f"지난 {LOOKBACK_DAYS}일간 수집된 논문 {len(top_papers)}편 "
        f"(전체 {len(papers)}편 중 관련성 상위):\n\n"
        f"{paper_list_text}"
    )

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.4,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[openai] ERROR: {e}", file=sys.stderr)
        return None


# ============================================================================
# 다이제스트 페이지 생성
# ============================================================================

def chunk_text(text: str, max_len: int = 1900) -> list[str]:
    if len(text) <= max_len:
        return [text]
    return [text[i:i + max_len] for i in range(0, len(text), max_len)]


def rich_text_blocks(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": c}} for c in chunk_text(text)]


def heading(level: int, text: str) -> dict:
    key = f"heading_{level}"
    return {
        "object": "block",
        "type": key,
        key: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text_blocks(text)},
    }


def callout(text: str, emoji: str, color: str = "blue_background") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text_blocks(text),
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color,
        },
    }


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def bullet(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rich_text_blocks(text)},
    }


def build_digest_blocks(digest: dict, papers: list[dict]) -> list[dict]:
    """다이제스트 페이지의 본문 블록 구성."""
    blocks = []

    # 1. Headline (callout)
    blocks.append(callout(
        digest.get("headline", ""),
        emoji="📰",
        color="blue_background",
    ))

    # 2. Trend
    blocks.append(heading(2, "🌊 이번 주 트렌드"))
    blocks.append(paragraph(digest.get("trend_summary", "")))

    # 3. Synergy
    blocks.append(heading(2, "🎯 본인 연구와의 시너지"))
    blocks.append(callout(
        digest.get("synergy_for_you", ""),
        emoji="🎯",
        color="yellow_background",
    ))

    # 4. Highlights
    blocks.append(heading(2, f"⭐ 핵심 {TOP_HIGHLIGHTS}편"))
    for h in (digest.get("highlights") or [])[:TOP_HIGHLIGHTS]:
        blocks.append({
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": rich_text_blocks(f"★ {h.get('title', '')}"),
                "children": [paragraph(h.get("why_important", ""))],
            },
        })

    # 5. Tag distribution
    blocks.append(heading(2, "🏷 태그 분포"))
    tag_counts = {}
    for p in papers:
        for t in p["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    if tag_counts:
        sorted_tags = sorted(tag_counts.items(), key=lambda x: -x[1])
        for tag, count in sorted_tags:
            blocks.append(bullet(f"{tag} — {count}편"))
    blocks.append(paragraph(digest.get("tag_distribution_note", "")))

    blocks.append(divider())

    # 6. Full paper list (collapsed in toggle)
    blocks.append(heading(2, f"📚 전체 논문 {len(papers)}편"))
    must_read_papers = [p for p in papers if p["must_read"]]
    other_papers = [p for p in papers if not p["must_read"]]

    if must_read_papers:
        blocks.append(heading(3, f"Must-read ({len(must_read_papers)}편)"))
        for p in must_read_papers:
            blocks.append(_paper_bullet(p))

    if other_papers:
        blocks.append({
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": rich_text_blocks(
                    f"Other papers ({len(other_papers)}편) — 펼쳐서 보기"
                ),
                "children": [_paper_bullet(p) for p in other_papers[:50]],
            },
        })

    return blocks


def _paper_bullet(p: dict) -> dict:
    venue = p["venue"] or p["source"] or ""
    tags = " · ".join(p["tags"][:3])
    score = p["relevance"]
    title = p["title"]
    suffix_parts = []
    if venue:
        suffix_parts.append(venue)
    if tags:
        suffix_parts.append(tags)
    suffix = f" ({' · '.join(suffix_parts)})" if suffix_parts else ""
    text = f"[{score}] {title}{suffix}"
    link = p.get("link", "")

    if link:
        rt = [
            {"type": "text", "text": {"content": text, "link": {"url": link}}},
        ]
    else:
        rt = rich_text_blocks(text)

    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rt},
    }


def create_digest_page(digest: dict, papers: list[dict]) -> str:
    """다이제스트 페이지를 생성하고 URL을 반환."""
    parent_page_id = os.environ.get("NOTION_DIGEST_PAGE_ID")
    if not parent_page_id:
        # 폴백: 논문 DB와 같은 부모에 만들 수 없으므로, DB 자체에 행으로 추가
        print("[warn] NOTION_DIGEST_PAGE_ID 미설정 — 페이지로 만들 수 없음.",
              file=sys.stderr)
        print("[warn] 이 스크립트를 사용하려면 NOTION_DIGEST_PAGE_ID 환경변수가 필요.",
              file=sys.stderr)
        sys.exit(1)

    now_kst = datetime.now(timezone(timedelta(hours=9)))
    title = f"📅 Weekly digest — {now_kst.strftime('%Y-%m-%d (%a)')}"

    body = {
        "parent": {"page_id": parent_page_id},
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": title}}]
            }
        },
        "children": build_digest_blocks(digest, papers),
    }
    result = notion_request("POST", "/pages", body)
    page_url = result.get("url", "")
    print(f"[notion] created digest page: {page_url}")
    return page_url


# ============================================================================
# Main
# ============================================================================

def main():
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if not database_id:
        print("ERROR: NOTION_DATABASE_ID missing", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY missing", file=sys.stderr)
        sys.exit(1)
    client = OpenAI(api_key=api_key)

    # 1. 지난 7일간 DB에 추가된 논문들 가져오기
    papers = fetch_recent_papers(database_id, LOOKBACK_DAYS)
    if not papers:
        print("[done] no papers in last week — skipping digest")
        return

    # 2. OpenAI에 메타 요약 요청
    digest = generate_digest(client, papers)
    if digest is None:
        print("[done] failed to generate digest")
        sys.exit(1)

    # 3. Notion에 다이제스트 페이지 생성
    create_digest_page(digest, papers)


if __name__ == "__main__":
    main()
