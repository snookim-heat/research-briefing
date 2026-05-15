"""
Daily research briefing -> Notion database.

매일 arXiv + Semantic Scholar에서 새 논문을 수집해서 OpenAI로 요약한 뒤,
Notion 데이터베이스에 한 행씩 추가한다. 중복은 DOI 또는 arXiv ID로 차단.

[2단계 필터링 구조]
  1. 1차 필터: 제목+초록 첫 200자만으로 빠른 점수 평가 (편당 ~0.5초, ~$0.0002)
  2. 통과한 논문만 풀 요약 (편당 ~3초, ~$0.003)
  → 시간/비용 약 70% 절감

필요한 환경변수 (GitHub Secrets로 등록):
  OPENAI_API_KEY     -- OpenAI API 키
  NOTION_TOKEN       -- Notion Connection의 Access token
  NOTION_DATABASE_ID -- 대상 Notion 데이터베이스 ID
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from openai import OpenAI

# ============================================================================
# CONFIG -- 마음대로 수정 가능
# ============================================================================

ARXIV_QUERIES = [
    'abs:"phase change material" AND abs:"thermal management"',
    'abs:"phase change material" AND abs:"electronics cooling"',
    'abs:"barocaloric"',
    'abs:"data center" AND abs:"cooling"',
    'abs:"chiplet" AND abs:"thermal"',
    'abs:"semiconductor packaging" AND abs:"thermal"',
    'abs:"microfluidic" AND abs:"hotspot"',
    'abs:"transient thermal" AND abs:"electronics"',
]

SEMANTIC_SCHOLAR_QUERIES = [
    "phase change material thermal management electronics",
    "phase change material cooling power electronics",
    "barocaloric cooling material",
    "data center cooling thermal management",
    "chiplet 2.5D 3D thermal packaging",
    "semiconductor packaging thermal interface",
    "microfluidic chip hotspot cooling",
    "transient thermal management electronics",
]

ARXIV_LOOKBACK_DAYS = 3
JOURNAL_LOOKBACK_DAYS = 30

# 1차 필터: 이 점수 이상이면 풀 요약으로 진행
PREFILTER_THRESHOLD = 50

# 2차(최종): 풀 요약 후 이 점수 이상이면 Notion 푸시
MIN_RELEVANCE_TO_PUSH = 55

# 최종 푸시 최대 편수 (관련성 점수 내림차순)
MAX_PAPERS = 25

# Semantic Scholar 쿼리당 최대 결과 수
S2_MAX_RESULTS_PER_QUERY = 100

# OpenAI 모델
OPENAI_MODEL_PREFILTER = "gpt-4o-mini"  # 1차 필터링용 (싸고 빠름)
OPENAI_MODEL_SUMMARY = "gpt-4o-mini"    # 풀 요약용

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


# ============================================================================
# arXiv 수집
# ============================================================================

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
            "source": "arXiv",
            "arxiv_id": arxiv_id,
            "doi": None,
            "title": " ".join(title.split()),
            "abstract": " ".join(summary.split()),
            "authors": authors,
            "venue": "arXiv",
            "published": published,
            "link": link_abs,
        })
    return entries


def collect_arxiv_papers() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=ARXIV_LOOKBACK_DAYS)
    seen = {}
    for q in ARXIV_QUERIES:
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
            if r["arxiv_id"] not in seen:
                seen[r["arxiv_id"]] = r
        time.sleep(3)
    papers = list(seen.values())
    print(f"[arxiv] {len(papers)} unique recent papers")
    return papers


# ============================================================================
# Semantic Scholar 수집
# ============================================================================

SS_API = "https://api.semanticscholar.org/graph/v1/paper/search"
SS_FIELDS = (
    "paperId,externalIds,title,abstract,authors,venue,"
    "publicationDate,year,openAccessPdf,url"
)


def fetch_semantic_scholar(query: str,
                           max_results: int = S2_MAX_RESULTS_PER_QUERY
                           ) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=JOURNAL_LOOKBACK_DAYS)
    date_filter = f"{start.isoformat()}:{today.isoformat()}"

    params = {
        "query": query,
        "limit": str(max_results),
        "fields": SS_FIELDS,
        "publicationDateOrYear": date_filter,
    }
    url = f"{SS_API}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "research-briefing-bot/1.0")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("[s2] rate limited, sleeping 5s", file=sys.stderr)
            time.sleep(5)
            return []
        raise

    out = []
    for item in data.get("data", []) or []:
        ext = item.get("externalIds") or {}
        arxiv_id = ext.get("ArXiv")
        doi = ext.get("DOI")
        abstract = item.get("abstract") or ""
        title = item.get("title") or ""
        if not title:
            continue
        if len(abstract) < 100:  # too short for meaningful summary
            continue

        authors = [a.get("name", "") for a in (item.get("authors") or [])]
        pub_date_raw = item.get("publicationDate")
        if pub_date_raw:
            published = pub_date_raw
        elif item.get("year"):
            published = f"{item['year']}-01-01"
        else:
            published = ""
        if published and "T" not in published:
            published = published + "T00:00:00Z"

        venue = item.get("venue") or ""
        source = "arXiv" if arxiv_id else "Journal"

        link = item.get("url") or (f"https://doi.org/{doi}" if doi else "")

        out.append({
            "source": source,
            "arxiv_id": arxiv_id,
            "doi": doi,
            "title": " ".join(title.split()),
            "abstract": " ".join(abstract.split()),
            "authors": authors,
            "venue": venue,
            "published": published,
            "link": link,
        })
    return out


def collect_semantic_scholar_papers() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=JOURNAL_LOOKBACK_DAYS)
    seen: dict[str, dict] = {}
    for q in SEMANTIC_SCHOLAR_QUERIES:
        try:
            print(f"[s2] querying: {q}")
            results = fetch_semantic_scholar(q)
        except Exception as e:
            print(f"[s2] ERROR on '{q}': {e}", file=sys.stderr)
            continue
        for r in results:
            try:
                pub_dt = datetime.fromisoformat(r["published"].replace("Z", "+00:00"))
            except Exception:
                continue
            if pub_dt < cutoff:
                continue
            key = r.get("doi") or r.get("arxiv_id") or r["title"][:80]
            if key not in seen:
                seen[key] = r
        time.sleep(1)
    papers = list(seen.values())
    print(f"[s2] {len(papers)} unique recent papers")
    return papers


def merge_sources(arxiv_papers: list[dict], s2_papers: list[dict]) -> list[dict]:
    by_arxiv = {}
    by_doi = {}
    merged = []

    for p in arxiv_papers:
        if p.get("arxiv_id"):
            by_arxiv[p["arxiv_id"]] = p
        merged.append(p)

    for p in s2_papers:
        aid = p.get("arxiv_id")
        doi = p.get("doi")
        if aid and aid in by_arxiv:
            continue
        if doi and doi in by_doi:
            continue
        if doi:
            by_doi[doi] = p
        merged.append(p)

    merged.sort(key=lambda p: p.get("published", ""), reverse=True)
    print(f"[merge] {len(merged)} after dedup "
          f"(arxiv {len(arxiv_papers)} + s2 {len(s2_papers)})")
    return merged


# ============================================================================
# 중복 제거 (Notion DB와 비교) — 1차 필터링 전에 먼저 적용해서 비용 절감
# ============================================================================

def filter_already_in_db(papers: list[dict],
                         existing_arxiv: set[str],
                         existing_doi: set[str]) -> list[dict]:
    out = []
    skipped = 0
    for p in papers:
        if p.get("arxiv_id") and p["arxiv_id"] in existing_arxiv:
            skipped += 1
            continue
        if p.get("doi") and p["doi"] in existing_doi:
            skipped += 1
            continue
        out.append(p)
    print(f"[dedup] {skipped} already in Notion DB, {len(out)} new candidates")
    return out


# ============================================================================
# 1차 필터링 (싸고 빠름)
# ============================================================================

PREFILTER_SYSTEM_PROMPT = f"""\
당신은 기계공학 박사후연구원의 논문 1차 필터다.

연구자 배경:
{YOUR_RESEARCH_CONTEXT}

논문의 제목과 초록 앞부분만 보고 0~100 점수를 매긴다.
점수 기준:
- 90~100: 본인 연구와 직접 연결, 반드시 읽어야 함
- 70~89: 본인 연구의 인접 분야, 인용/참고 가치 큼
- 50~69: 흥미롭지만 우선순위 낮음
- 30~49: 관련성 약함
- 0~29: 무관

JSON으로 정확히 다음 형식만 반환:
{{"score": 정수}}
"""


def prefilter_paper(client: OpenAI, paper: dict) -> int:
    """제목+초록 앞부분으로 빠른 점수 평가."""
    abstract_preview = paper.get("abstract", "")[:200]
    user_msg = (
        f"Title: {paper['title']}\n"
        f"Abstract preview: {abstract_preview}\n"
    )
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL_PREFILTER,
            messages=[
                {"role": "system", "content": PREFILTER_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=50,
        )
        data = json.loads(resp.choices[0].message.content)
        return int(data.get("score", 0))
    except Exception as e:
        key = paper.get("arxiv_id") or paper.get("doi") or paper["title"][:60]
        print(f"[prefilter] ERROR on {key}: {e}", file=sys.stderr)
        return 0  # 안전하게 0점 처리


def run_prefilter(client: OpenAI, papers: list[dict]) -> list[dict]:
    """1차 필터링 후 임계점 통과한 것만 반환."""
    print(f"[prefilter] scoring {len(papers)} papers...")
    passed = []
    for p in papers:
        score = prefilter_paper(client, p)
        p["prefilter_score"] = score
        if score >= PREFILTER_THRESHOLD:
            passed.append(p)
    passed.sort(key=lambda x: x["prefilter_score"], reverse=True)
    print(f"[prefilter] {len(passed)}/{len(papers)} passed "
          f"(threshold {PREFILTER_THRESHOLD})")
    return passed


# ============================================================================
# 2차: 풀 요약
# ============================================================================

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
    venue = paper.get("venue") or paper.get("source") or ""
    user_msg = (
        f"Title: {paper['title']}\n"
        f"Authors: {', '.join(paper.get('authors', []))}\n"
        f"Venue: {venue}\n"
        f"Published: {paper.get('published', '')}\n"
        f"DOI: {paper.get('doi') or 'N/A'}\n"
        f"arXiv: {paper.get('arxiv_id') or 'N/A'}\n\n"
        f"Abstract:\n{paper['abstract']}\n"
    )
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL_SUMMARY,
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        key = paper.get("arxiv_id") or paper.get("doi") or paper["title"][:60]
        print(f"[openai] ERROR on {key}: {e}", file=sys.stderr)
        return None


def run_full_summary(client: OpenAI, papers: list[dict]) -> list[dict]:
    enriched = []
    for p in papers:
        key = p.get("arxiv_id") or p.get("doi") or p["title"][:60]
        print(f"[summary] {key} -- {p['title'][:60]}")
        s = summarize_paper(client, p)
        if s is None:
            continue
        p["summary"] = s
        enriched.append(p)

    enriched.sort(key=lambda x: x["summary"].get("relevance_score", 0), reverse=True)
    return enriched[:MAX_PAPERS]


# ============================================================================
# Notion 푸시
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


def get_existing_keys(database_id: str) -> tuple[set[str], set[str]]:
    arxiv_ids = set()
    dois = set()
    start_cursor = None
    while True:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        result = notion_request("POST", f"/databases/{database_id}/query", body)
        for page in result.get("results", []):
            props = page.get("properties", {})

            aid_prop = props.get("arXiv ID", {}).get("rich_text", [])
            if aid_prop:
                aid = aid_prop[0].get("plain_text", "").strip()
                if aid:
                    arxiv_ids.add(aid)

            doi_prop = props.get("DOI", {}).get("rich_text", [])
            if doi_prop:
                doi = doi_prop[0].get("plain_text", "").strip()
                if doi:
                    dois.add(doi)

        if not result.get("has_more"):
            break
        start_cursor = result.get("next_cursor")
    print(f"[notion] {len(arxiv_ids)} arxiv + {len(dois)} doi already in DB")
    return arxiv_ids, dois


def chunk_text(text: str, max_len: int = 1900) -> list[str]:
    if len(text) <= max_len:
        return [text]
    return [text[i:i + max_len] for i in range(0, len(text), max_len)]


def rich_text_blocks(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": c}} for c in chunk_text(text)]


def build_page_children(paper: dict) -> list[dict]:
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
    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text_blocks(s.get("one_liner", "")),
            "icon": {"type": "emoji", "emoji": "💡"},
            "color": "blue_background",
        },
    })
    blocks.append(heading_toggle("⚙️", "메커니즘", [para(s.get("mechanism", ""))]))

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

    blocks.append(heading_toggle("⚠️", "한계", [para(s.get("limitations", ""))]))

    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text_blocks(s.get("for_you", "")),
            "icon": {"type": "emoji", "emoji": "🎯"},
            "color": "yellow_background",
        },
    })

    blocks.append(heading_toggle("📄", "원문 abstract",
                                 [para(paper.get("abstract", ""))]))
    return blocks


def push_to_notion(database_id: str, paper: dict) -> None:
    s = paper["summary"]
    authors_str = ", ".join(paper.get("authors", [])[:5])
    if len(paper.get("authors", [])) > 5:
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
        "Source": {
            "select": {"name": paper.get("source", "Unknown")}
        },
        "Venue": {
            "rich_text": rich_text_blocks(paper.get("venue", ""))
        },
        "arXiv link": {
            "url": paper.get("link") or None
        },
    }
    if pub_date:
        properties["Published"] = {"date": {"start": pub_date}}
    if paper.get("arxiv_id"):
        properties["arXiv ID"] = {
            "rich_text": [{"type": "text", "text": {"content": paper["arxiv_id"]}}]
        }
    if paper.get("doi"):
        properties["DOI"] = {
            "rich_text": [{"type": "text", "text": {"content": paper["doi"]}}]
        }

    body = {
        "parent": {"database_id": database_id},
        "properties": properties,
        "children": build_page_children(paper),
    }
    notion_request("POST", "/pages", body)
    key = paper.get("arxiv_id") or paper.get("doi") or paper["title"][:40]
    print(f"[notion] + {key} (score {s.get('relevance_score')}, "
          f"source {paper.get('source')})")


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

    # 1. 두 소스에서 수집
    arxiv_papers = collect_arxiv_papers()
    s2_papers = collect_semantic_scholar_papers()
    papers = merge_sources(arxiv_papers, s2_papers)
    if not papers:
        print("[done] no new papers in lookback window")
        return

    # 2. Notion DB에 이미 있는 건 미리 제거 (1차 필터링 비용 절감)
    existing_arxiv, existing_doi = get_existing_keys(database_id)
    papers = filter_already_in_db(papers, existing_arxiv, existing_doi)
    if not papers:
        print("[done] all candidates already in DB")
        return

    # 3. 1차 필터링 (싸고 빠름)
    candidates = run_prefilter(client, papers)
    if not candidates:
        print("[done] nothing passed prefilter")
        return

    # 4. 통과한 것만 풀 요약
    summarized = run_full_summary(client, candidates)
    if not summarized:
        print("[done] nothing survived summarization")
        return

    # 5. Notion 푸시
    pushed = 0
    skipped_low = 0
    for p in summarized:
        score = p["summary"].get("relevance_score", 0)
        if score < MIN_RELEVANCE_TO_PUSH:
            skipped_low += 1
            continue
        try:
            push_to_notion(database_id, p)
            pushed += 1
        except Exception as e:
            key = p.get("arxiv_id") or p.get("doi") or p["title"][:40]
            print(f"[notion] failed to push {key}: {e}", file=sys.stderr)

    print(f"[done] pushed={pushed} below_final_threshold={skipped_low}")


if __name__ == "__main__":
    main()
