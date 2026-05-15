"""
Daily research briefing -> Notion database.

매일 arXiv에서 새 논문을 수집해서 OpenAI로 요약한 뒤, Notion 데이터베이스에
한 행씩 추가한다. 같은 arXiv ID는 두 번 추가되지 않는다.

필요한 환경변수 (GitHub Secrets로 등록):
  OPENAI_API_KEY    -- OpenAI API 키
  NOTION_TOKEN      -- Notion Integration의 Internal Integration Secret
  NOTION_DATABASE_ID -- 대상 Notion 데이터베이스 ID (URL에서 추출)
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from openai import OpenAI

# ----------------------------------------------------------------------------
# CONFIG -- 마음대로 수정 가능
# ----------------------------------------------------------------------------

QUERIES = [
    'abs:"phase change material" AND abs:"thermal management"',
    'abs:"phase change material" AND abs:"electronics cooling"',
    'abs:"barocaloric"',
    'abs:"data center" AND abs:"cooling"',
    'abs:"chiplet" AND abs:"thermal"',
    'abs:"semiconductor packaging" AND abs:"thermal"',
    'abs:"microfluidic" AND abs:"hotspot"',
    'abs:"transient thermal" AND abs:"electronics"',
]

LOOKBACK_DAYS = 3
MAX_PAPERS = 8
MIN_RELEVANCE_TO_PUSH = 60  # 이 점수 미만은 Notion에 올리지 않음
OPENAI_MODEL = "gpt-4o-mini"

YOUR_RESEARCH_CONTEXT = """
연구자는 기계공학 박사후연구원이며 다음 영역에서 활동:

[과거~현재 연구]
- 상변화 물질(PCM) 기반 전력전자 과도열관리
  · dynPCM-integrated liquid cooling, FEM, pulsed heat load
  · pressure-enhanced close-contact melting
  · composite PCM for GaN devices (Field's metal 등 금속 PCM 포함)
  · 1D finite difference PCM slab model, melt front tracking
- 바로칼로릭 재료 기반 열관리
  · pressure-tunable thermal energy storage module, HVAC 응용
  · coaxial tube testbed, LabVIEW DAQ

[앞으로 진행할 연구]
- 데이터센터 열관리
- 반도체 패키징 레벨 열관리 (2.5D/3D, chiplet, TIM, embedded cooling)

논문을 평가할 때 이 두 갈래(과거 경험 / 미래 방향)와 어떻게 연결되는지,
어디서 시너지/차별화가 가능한지를 구체적으로 짚어줄 것.
""".strip()

# ----------------------------------------------------------------------------
# arXiv 수집
# ----------------------------------------------------------------------------

ARXIV_API = "http://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def fetch_arxiv(query: str, max_results: int = 30) -> list[dict]:
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": str(max_results),
    }
    url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        xml_text = resp.read().decode("utf-8")

    root = ET.fromstring(xml_text)
    entries = []
    for entry in root.findall("atom:entry", ATOM_NS):
        arxiv_id_full = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        arxiv_id = arxiv_id_full.rsplit("/", 1)[-1].split("v")[0]
        published = entry.findtext("atom:published", default="", namespaces=ATOM_NS)
        title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
        summary = (entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").strip()
        authors = [
            (a.findtext("atom:name", default="", namespaces=ATOM_NS) or "").strip()
            for a in entry.findall("atom:author", ATOM_NS)
        ]
        link_abs = arxiv_id_full
        for link in entry.findall("atom:link", ATOM_NS):
            if link.attrib.get("rel") == "alternate":
                link_abs = link.attrib.get("href", link_abs)
        entries.append({
            "id": arxiv_id,
            "title": " ".join(title.split()),
            "abstract": " ".join(summary.split()),
            "authors": authors,
            "published": published,
            "link": link_abs,
        })
    return entries


def collect_recent_papers() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    seen = {}
    for q in QUERIES:
        try:
            print(f"[arxiv] querying: {q}")
            results = fetch_arxiv(q)
        except Exception as e:
            print(f"[arxiv] ERROR on '{q}': {e}", file=sys.stderr)
            continue
        for r in results:
            try:
                pub_dt = datetime.fromisoformat(r["published"].replace("Z", "+00:00"))
            except Exception:
                continue
            if pub_dt < cutoff:
                continue
            if r["id"] not in seen:
                seen[r["id"]] = r
        time.sleep(3)
    papers = list(seen.values())
    papers.sort(key=lambda p: p["published"], reverse=True)
    print(f"[arxiv] {len(papers)} unique recent papers")
    return papers


# ----------------------------------------------------------------------------
# OpenAI 요약
# ----------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = f"""\
당신은 기계공학(열전달, 상변화 물질, 데이터센터/반도체 패키징 열관리)
박사후연구원을 위한 일일 논문 큐레이터다.

연구자 배경:
{YOUR_RESEARCH_CONTEXT}

각 논문에 대해 다음을 JSON으로 반환:
- "relevance_score": 0~100 정수
- "must_read": relevance_score >= 90 이면 true
- "tags": ["PCM", "Barocaloric", "Data center", "Packaging", "Transient",
   "Chiplet", "Hotspot", "Review", "Optimization", "3D IC"] 중 1~3개
- "one_liner": 1문장(80자 이내) 핵심 기여
- "mechanism": 메커니즘/접근법 2~3문장
- "key_numbers": [{{"label": "...", "value": "..."}}] 3~5개
- "limitations": 한계 2~3문장
- "for_you": 본인 연구와의 연결고리 및 활용 방안 3~4문장

스타일:
- 문장 시작 "이", "이것은" 금지
- 콜론(:) 금지
- "최적화" 대신 "조정/튜닝/탐색"
- 단정조 피하고 정직한 톤

JSON 객체 하나만 반환. 마크다운 코드 펜스 금지.
"""


def summarize_paper(client: OpenAI, paper: dict) -> dict | None:
    user_msg = (
        f"Title: {paper['title']}\n"
        f"Authors: {', '.join(paper['authors'])}\n"
        f"Published: {paper['published']}\n"
        f"arXiv: {paper['id']}\n\n"
        f"Abstract:\n{paper['abstract']}\n"
    )
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[openai] ERROR on {paper['id']}: {e}", file=sys.stderr)
        return None


def score_and_summarize(papers: list[dict]) -> list[dict]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY missing", file=sys.stderr)
        sys.exit(1)
    client = OpenAI(api_key=api_key)

    enriched = []
    for p in papers:
        print(f"[openai] summarizing {p['id']} -- {p['title'][:60]}")
        s = summarize_paper(client, p)
        if s is None:
            continue
        p["summary"] = s
        enriched.append(p)

    enriched.sort(key=lambda x: x["summary"].get("relevance_score", 0), reverse=True)
    return enriched[:MAX_PAPERS]


# ----------------------------------------------------------------------------
# Notion 푸시
# ----------------------------------------------------------------------------

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def notion_request(method: str, path: str, body: dict | None = None) -> dict:
    """Minimal Notion API client using urllib (no extra dependency)."""
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


def get_existing_arxiv_ids(database_id: str) -> set[str]:
    """이미 DB에 있는 arXiv ID 집합을 가져온다 (중복 방지)."""
    existing = set()
    start_cursor = None
    while True:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        result = notion_request("POST", f"/databases/{database_id}/query", body)
        for page in result.get("results", []):
            props = page.get("properties", {})
            arxiv_prop = props.get("arXiv ID", {}).get("rich_text", [])
            if arxiv_prop:
                aid = arxiv_prop[0].get("plain_text", "").strip()
                if aid:
                    existing.add(aid)
        if not result.get("has_more"):
            break
        start_cursor = result.get("next_cursor")
    print(f"[notion] {len(existing)} papers already in DB")
    return existing


def chunk_text(text: str, max_len: int = 1900) -> list[str]:
    """Notion rich_text의 한 블록은 2000자 한도. 안전하게 잘라준다."""
    if len(text) <= max_len:
        return [text]
    return [text[i:i + max_len] for i in range(0, len(text), max_len)]


def rich_text_blocks(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": c}} for c in chunk_text(text)]


def build_page_children(paper: dict) -> list[dict]:
    """페이지 본문 — 메커니즘 / 핵심 수치 / 한계 / 활용 포인트를 토글로."""
    s = paper["summary"]

    def heading_toggle(emoji: str, label: str, content_blocks: list[dict]) -> dict:
        return {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [{"type": "text", "text": {"content": f"{emoji} {label}"}}],
                "children": content_blocks,
            },
        }

    def para(text: str) -> dict:
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text_blocks(text)},
        }

    blocks = []

    # 한 줄 요약을 callout으로
    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text_blocks(s.get("one_liner", "")),
            "icon": {"type": "emoji", "emoji": "💡"},
            "color": "blue_background",
        },
    })

    # 메커니즘
    blocks.append(heading_toggle("⚙️", "메커니즘", [para(s.get("mechanism", ""))]))

    # 핵심 수치 — bullet list로
    kn = s.get("key_numbers", []) or []
    if kn:
        kn_children = [
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": rich_text_blocks(
                        f"{item.get('label', '')} — {item.get('value', '')}"
                    ),
                },
            }
            for item in kn
        ]
        blocks.append(heading_toggle("📊", "핵심 수치", kn_children))

    # 한계
    blocks.append(heading_toggle("⚠️", "한계", [para(s.get("limitations", ""))]))

    # 본인 연구 활용 포인트 — 가장 중요한 부분이라 펼쳐서 보여줌
    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text_blocks(s.get("for_you", "")),
            "icon": {"type": "emoji", "emoji": "🎯"},
            "color": "yellow_background",
        },
    })

    # 초록 원문 (참고용, 토글)
    blocks.append(heading_toggle("📄", "원문 abstract",
                                 [para(paper.get("abstract", ""))]))

    return blocks


def push_to_notion(database_id: str, paper: dict) -> None:
    s = paper["summary"]
    authors_str = ", ".join(paper["authors"][:5])
    if len(paper["authors"]) > 5:
        authors_str += " et al."

    try:
        pub_dt = datetime.fromisoformat(paper["published"].replace("Z", "+00:00"))
        pub_date = pub_dt.strftime("%Y-%m-%d")
    except Exception:
        pub_date = None

    tags = s.get("tags", []) or []
    properties = {
        "Title": {
            "title": [{"type": "text", "text": {"content": paper["title"][:1900]}}]
        },
        "arXiv ID": {
            "rich_text": [{"type": "text", "text": {"content": paper["id"]}}]
        },
        "Authors": {
            "rich_text": rich_text_blocks(authors_str)
        },
        "Relevance": {
            "number": int(s.get("relevance_score", 0))
        },
        "Must read": {
            "checkbox": bool(s.get("must_read", False))
        },
        "Tags": {
            "multi_select": [{"name": t} for t in tags]
        },
        "Status": {
            "select": {"name": "To read"}
        },
        "arXiv link": {
            "url": paper.get("link") or f"https://arxiv.org/abs/{paper['id']}"
        },
    }
    if pub_date:
        properties["Published"] = {"date": {"start": pub_date}}

    body = {
        "parent": {"database_id": database_id},
        "properties": properties,
        "children": build_page_children(paper),
    }
    notion_request("POST", "/pages", body)
    print(f"[notion] + {paper['id']} (score {s.get('relevance_score')})")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if not database_id:
        print("ERROR: NOTION_DATABASE_ID missing", file=sys.stderr)
        sys.exit(1)

    papers = collect_recent_papers()
    if not papers:
        print("[done] no new papers in lookback window")
        return

    papers = score_and_summarize(papers)
    if not papers:
        print("[done] nothing survived summarization")
        return

    existing = get_existing_arxiv_ids(database_id)

    pushed = 0
    skipped_dup = 0
    skipped_low = 0
    for p in papers:
        score = p["summary"].get("relevance_score", 0)
        if score < MIN_RELEVANCE_TO_PUSH:
            skipped_low += 1
            print(f"[skip] {p['id']} score={score} below {MIN_RELEVANCE_TO_PUSH}")
            continue
        if p["id"] in existing:
            skipped_dup += 1
            print(f"[skip] {p['id']} already in DB")
            continue
        try:
            push_to_notion(database_id, p)
            pushed += 1
        except Exception as e:
            print(f"[notion] failed to push {p['id']}: {e}", file=sys.stderr)

    print(
        f"[done] pushed={pushed} duplicates={skipped_dup} "
        f"below_threshold={skipped_low}"
    )


if __name__ == "__main__":
    main()
