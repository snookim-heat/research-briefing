"""
Weekly research digest -> HTML dashboard + Notion page with link.

매주 월요일 아침:
  1. Notion DB에서 지난 7일 논문 조회
  2. OpenAI로 메타 요약 생성
  3. HTML 대시보드 파일을 site/ 폴더에 생성
     (GitHub Actions가 이 폴더를 GitHub Pages에 배포)
  4. Notion에 다이제스트 페이지 생성 + HTML 링크 임베드

필요한 환경변수:
  OPENAI_API_KEY           -- OpenAI API 키
  NOTION_TOKEN             -- Notion Connection의 Access token
  NOTION_DATABASE_ID       -- 논문 DB ID
  NOTION_DIGEST_PAGE_ID    -- 다이제스트 페이지가 자식으로 추가될 부모 페이지 ID
  PAGES_BASE_URL           -- Pages 베이스 URL
                              (예: https://snookim-heat.github.io/research-briefing)
"""

import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from html import escape

from openai import OpenAI

# ============================================================================
# CONFIG
# ============================================================================

LOOKBACK_DAYS = 7
MAX_PAPERS_FOR_DIGEST = 30
TOP_HIGHLIGHTS = 3
OPENAI_MODEL = "gpt-4o-mini"

# 대시보드에 노출될 본인 정보 (일부만)
RESEARCHER_NAME = "Soonwook Kim"
RESEARCHER_AFFILIATION = "Mechanical Engineering"

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
# Notion API (read)
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


def fetch_recent_papers(database_id: str, days: int) -> list[dict]:
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
            "sorts": [{"property": "Relevance", "direction": "descending"}],
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


def fetch_page_body_summary(page_id: str) -> dict:
    """페이지 본문에서 one_liner / for_you를 추출 (HTML 대시보드 카드용)."""
    try:
        result = notion_request("GET", f"/blocks/{page_id}/children?page_size=50")
    except Exception:
        return {"one_liner": "", "for_you": ""}

    callouts = []
    for block in result.get("results", []):
        if block.get("type") == "callout":
            rt = block["callout"].get("rich_text", [])
            text = "".join(p.get("plain_text", "") for p in rt)
            if text:
                callouts.append(text)
    # 첫 번째 callout = one_liner, 두 번째 = for_you (페이지 생성 시 순서)
    return {
        "one_liner": callouts[0] if len(callouts) >= 1 else "",
        "for_you":   callouts[1] if len(callouts) >= 2 else "",
    }


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
  "trend_summary": "이번 주 모인 논문들에서 보이는 트렌드와 큰 흐름 3~5문장.",
  "synergy_for_you": "본인 연구(과거·미래)와의 시너지 포인트 4~5문장. 구체적으로.",
  "highlights": [
    {{
      "title": "핵심 논문 제목 그대로 (원문 일치)",
      "why_important": "왜 이번 주 핵심인지 2~3문장"
    }},
    ... (정확히 {TOP_HIGHLIGHTS}개)
  ],
  "tag_distribution_note": "태그 분포 인사이트 1~2문장"
}}

스타일:
- 문장 시작 "이", "이것은" 금지
- 콜론(:) 금지
- "최적화" 대신 "조정/튜닝/탐색"
- 단정조 피하고 정직한 톤
- JSON 객체 하나만. 마크다운 코드 펜스 금지.
"""


def generate_digest(client: OpenAI, papers: list[dict]) -> dict | None:
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
        f"(전체 {len(papers)}편 중 관련성 상위):\n\n{paper_list_text}"
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
# HTML 대시보드 생성
# ============================================================================

TAG_COLORS = {
    "PCM": ("#EEEDFE", "#26215C"),
    "Barocaloric": ("#FAECE7", "#4A1B0C"),
    "Data center": ("#E1F5EE", "#04342C"),
    "Packaging": ("#E1F5EE", "#04342C"),
    "Transient": ("#FAECE7", "#4A1B0C"),
    "Chiplet": ("#E1F5EE", "#04342C"),
    "Hotspot": ("#FBEAF0", "#4B1528"),
    "Review": ("#F1EFE8", "#2C2C2A"),
    "Optimization": ("#F1EFE8", "#2C2C2A"),
    "3D IC": ("#E1F5EE", "#04342C"),
    "arXiv": ("#FAEEDA", "#412402"),
    "Journal": ("#E6F1FB", "#042C53"),
}


def render_tag(tag: str) -> str:
    bg, fg = TAG_COLORS.get(tag, ("#F1EFE8", "#2C2C2A"))
    return (
        f'<span class="tag" style="background:{bg};color:{fg};">'
        f"{escape(tag)}</span>"
    )


def render_paper_card(paper: dict, is_highlight: bool = False) -> str:
    tags_html = "".join(render_tag(t) for t in paper["tags"])
    if paper["source"]:
        tags_html += render_tag(paper["source"])

    score = int(paper["relevance"])
    must_badge = (
        '<span class="tag" style="background:#E6F1FB;color:#042C53;font-weight:600;">★ MUST READ</span>'
        if paper["must_read"] else ""
    )

    authors = paper["authors"] or ""
    venue = paper["venue"] or paper["source"] or ""
    meta_parts = [v for v in [venue, paper["published"]] if v]
    meta = " · ".join(meta_parts)

    border_style = (
        "border:2px solid #2B7FB8;"
        if (paper["must_read"] or is_highlight)
        else "border:0.5px solid #D5D3CC;"
    )

    one_liner_html = ""
    if paper.get("one_liner"):
        one_liner_html = (
            f'<p class="one-liner">{escape(paper["one_liner"])}</p>'
        )

    for_you_html = ""
    if paper.get("for_you"):
        for_you_html = (
            '<div class="for-you">'
            '<div class="for-you-label">↳ 본인 연구 활용 포인트</div>'
            f'<p>{escape(paper["for_you"])}</p>'
            '</div>'
        )

    link = paper.get("link") or (
        f"https://doi.org/{paper['doi']}" if paper.get("doi") else ""
    )
    link_html = (
        f'<a href="{escape(link)}" target="_blank" rel="noopener">Open ↗</a>'
        if link else ""
    )

    return f"""
<article class="card" style="{border_style}">
  <div class="card-head">
    {must_badge}{tags_html}
    <span class="score">{score}/100<span class="score-bar"><span style="width:{score}%;"></span></span></span>
  </div>
  <h3 class="card-title">{escape(paper["title"])}</h3>
  <p class="meta">{escape(authors)}{(' · ' + escape(meta)) if meta else ''}</p>
  {one_liner_html}
  {for_you_html}
  <div class="links">{link_html}</div>
</article>
"""


def render_html(digest: dict, papers: list[dict], week_label: str) -> str:
    n_papers = len(papers)
    n_must = sum(1 for p in papers if p["must_read"])
    avg_score = round(sum(p["relevance"] for p in papers) / n_papers) if n_papers else 0
    n_journal = sum(1 for p in papers if p["source"] == "Journal")
    n_arxiv = sum(1 for p in papers if p["source"] == "arXiv")

    # tag distribution
    tag_counts = {}
    for p in papers:
        for t in p["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    tag_dist_html = ""
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        tag_dist_html += (
            f'<div class="tag-bar"><span class="tag-name">{escape(tag)}</span>'
            f'<span class="tag-count">{count}</span></div>'
        )

    # highlights — title 매칭으로 paper 찾기
    highlights_html = ""
    highlighted_titles = set()
    for h in (digest.get("highlights") or [])[:TOP_HIGHLIGHTS]:
        h_title = h.get("title", "").strip()
        matching = next(
            (p for p in papers if p["title"].strip().lower() == h_title.lower()),
            None,
        )
        if matching is None:
            # fuzzy: contains
            for p in papers:
                if h_title.lower() in p["title"].lower() or p["title"].lower() in h_title.lower():
                    matching = p
                    break
        if matching:
            highlighted_titles.add(matching["title"])
            highlights_html += f"""
<div class="highlight">
  <div class="highlight-why">
    <div class="highlight-label">왜 이번 주 핵심인가</div>
    <p>{escape(h.get('why_important', ''))}</p>
  </div>
  {render_paper_card(matching, is_highlight=True)}
</div>
"""

    # remaining papers
    must_read_papers = [p for p in papers if p["must_read"] and p["title"] not in highlighted_titles]
    other_papers = [p for p in papers if not p["must_read"] and p["title"] not in highlighted_titles]

    must_read_html = "".join(render_paper_card(p) for p in must_read_papers)
    other_html = "".join(render_paper_card(p) for p in other_papers)

    now_kst = datetime.now(timezone(timedelta(hours=9)))

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Weekly research digest · {escape(week_label)}</title>
<style>
  :root {{
    --bg: #FAF9F5;
    --card: #FFFFFF;
    --text: #1A1915;
    --text-2: #6B6960;
    --border: #E5E2D9;
    --accent: #2B7FB8;
    --accent-bg: #E6F1FB;
    --accent-text: #042C53;
    --highlight: #FAF6EC;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui,
                 "Noto Sans KR", sans-serif;
    line-height: 1.55;
  }}
  .container {{ max-width: 920px; margin: 0 auto; padding: 40px 20px 80px; }}
  header h1 {{ margin: 0 0 4px; font-size: 28px; font-weight: 500; letter-spacing: -0.01em; }}
  header .sub {{ font-size: 14px; color: var(--text-2); }}
  header .byline {{
    font-size: 12px; color: var(--text-2); margin-top: 8px;
    padding-top: 8px; border-top: 0.5px solid var(--border);
  }}
  .metrics {{
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 10px; margin: 24px 0;
  }}
  .metric {{
    background: var(--card); border: 0.5px solid var(--border);
    border-radius: 10px; padding: 12px 14px;
  }}
  .metric .label {{ font-size: 12px; color: var(--text-2); }}
  .metric .value {{ font-size: 22px; font-weight: 500; margin-top: 2px; }}
  .signal {{
    background: var(--accent-bg); color: var(--accent-text);
    border-radius: 12px; padding: 16px 20px; margin-bottom: 28px;
  }}
  .signal .label {{
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
    margin-bottom: 6px; font-weight: 600;
  }}
  .signal .headline {{ font-size: 16px; font-weight: 500; }}
  h2 {{ font-size: 20px; font-weight: 500; margin: 36px 0 14px; }}
  h3 {{ font-size: 17px; font-weight: 500; }}
  .section-text {{ font-size: 14.5px; line-height: 1.7; color: var(--text); margin: 0; }}
  .synergy {{
    background: var(--highlight); border-radius: 12px; padding: 16px 20px;
    margin-bottom: 8px;
  }}
  .synergy .label {{
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
    margin-bottom: 6px; font-weight: 600; color: var(--text-2);
  }}
  .card {{
    background: var(--card); border-radius: 14px;
    padding: 20px; margin-bottom: 12px;
  }}
  .card-head {{
    display: flex; align-items: center; gap: 6px;
    flex-wrap: wrap; margin-bottom: 10px;
  }}
  .tag {{
    font-size: 11px; padding: 3px 9px; border-radius: 8px; font-weight: 500;
  }}
  .score {{
    margin-left: auto; font-size: 12px; color: var(--text-2);
    display: inline-flex; align-items: center; gap: 8px;
  }}
  .score-bar {{
    display: inline-block; width: 56px; height: 4px;
    background: #EEECE3; border-radius: 2px; overflow: hidden;
  }}
  .score-bar > span {{ display: block; height: 100%; background: var(--accent); }}
  .card-title {{ margin: 0 0 4px; font-size: 16px; line-height: 1.4; font-weight: 500; }}
  .meta {{ margin: 0 0 10px; font-size: 12.5px; color: var(--text-2); }}
  .one-liner {{
    margin: 0 0 10px; font-size: 13.5px; padding: 8px 12px;
    background: #F7F5EE; border-radius: 8px;
  }}
  .for-you {{
    background: var(--highlight); border-radius: 8px;
    padding: 10px 12px; margin-top: 10px;
  }}
  .for-you-label {{
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--text-2); margin-bottom: 4px; font-weight: 600;
  }}
  .for-you p {{ margin: 0; font-size: 13px; line-height: 1.6; }}
  .links {{ margin-top: 12px; }}
  .links a {{ font-size: 12.5px; color: var(--accent); text-decoration: none; }}
  .links a:hover {{ text-decoration: underline; }}
  .highlight {{ margin-bottom: 16px; }}
  .highlight-why {{
    background: var(--accent-bg); color: var(--accent-text);
    padding: 12px 16px; border-radius: 12px 12px 0 0;
    margin-bottom: -10px;
  }}
  .highlight-why .highlight-label {{
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
    margin-bottom: 4px; font-weight: 600;
  }}
  .highlight-why p {{ margin: 0; font-size: 13px; line-height: 1.55; }}
  .highlight .card {{ border-radius: 0 0 12px 12px; }}
  .tag-bar {{
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--card); border: 0.5px solid var(--border);
    padding: 4px 10px; border-radius: 999px; margin: 0 6px 6px 0;
    font-size: 12.5px;
  }}
  .tag-count {{
    background: var(--accent-bg); color: var(--accent-text);
    padding: 1px 7px; border-radius: 999px; font-weight: 600;
  }}
  footer {{
    margin-top: 50px; padding-top: 20px; border-top: 0.5px solid var(--border);
    font-size: 12px; color: var(--text-2);
  }}
  @media (max-width: 600px) {{
    .metrics {{ grid-template-columns: repeat(2, 1fr); }}
    .container {{ padding: 24px 14px 60px; }}
  }}
</style>
</head>
<body>
<div class="container">

  <header>
    <h1>Weekly research digest</h1>
    <div class="sub">{escape(week_label)} · PCM · barocaloric · data center · packaging</div>
    <div class="byline">{escape(RESEARCHER_NAME)} · {escape(RESEARCHER_AFFILIATION)}</div>
  </header>

  <section class="metrics">
    <div class="metric"><div class="label">Papers</div><div class="value">{n_papers}</div></div>
    <div class="metric"><div class="label">Must read</div><div class="value">{n_must}</div></div>
    <div class="metric"><div class="label">Avg score</div><div class="value">{avg_score}</div></div>
    <div class="metric"><div class="label">Journal / arXiv</div><div class="value" style="font-size:14px;padding-top:6px;">{n_journal} / {n_arxiv}</div></div>
  </section>

  <div class="signal">
    <div class="label">This week's signal</div>
    <div class="headline">{escape(digest.get("headline", ""))}</div>
  </div>

  <h2>🌊 Trend</h2>
  <p class="section-text">{escape(digest.get("trend_summary", ""))}</p>

  <h2>🎯 본인 연구와의 시너지</h2>
  <div class="synergy">
    <div class="label">Synergy</div>
    <p class="section-text">{escape(digest.get("synergy_for_you", ""))}</p>
  </div>

  <h2>⭐ Top {TOP_HIGHLIGHTS} Highlights</h2>
  {highlights_html}

  <h2>🏷 Tag distribution</h2>
  <div style="margin-bottom:10px;">{tag_dist_html}</div>
  <p class="section-text" style="font-size:13px;color:var(--text-2);">{escape(digest.get("tag_distribution_note", ""))}</p>

  {('<h2>🔥 Other must-read</h2>' + must_read_html) if must_read_papers else ''}

  {('<h2>📚 All other papers</h2>' + other_html) if other_papers else ''}

  <footer>
    Generated {now_kst.strftime("%Y-%m-%d %H:%M")} KST · powered by arXiv + Semantic Scholar + OpenAI
  </footer>

</div>
</body>
</html>
"""


# ============================================================================
# HTML 파일 쓰기 (GitHub Pages용 site/ 폴더에)
# ============================================================================

def write_html_files(html: str, week_slug: str) -> tuple[str, str]:
    """site/<week_slug>/index.html 과 site/index.html (최신 redirect)을 생성.
    또한 site/robots.txt와 site/archive.html을 갱신.
    Returns: (week_url_path, latest_url_path)
    """
    site_dir = "site"
    os.makedirs(site_dir, exist_ok=True)

    # 1. 이번 주 다이제스트 페이지
    week_dir = os.path.join(site_dir, week_slug)
    os.makedirs(week_dir, exist_ok=True)
    week_path = os.path.join(week_dir, "index.html")
    with open(week_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 2. site/index.html — 가장 최신 다이제스트로 자동 이동
    redirect_html = f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<meta http-equiv="refresh" content="0;url={week_slug}/">
<title>Latest digest</title>
</head><body>
<p>Redirecting to <a href="{week_slug}/">latest digest</a>...</p>
</body></html>
"""
    with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(redirect_html)

    # 3. robots.txt — 검색 차단
    with open(os.path.join(site_dir, "robots.txt"), "w") as f:
        f.write("User-agent: *\nDisallow: /\n")

    # 4. archive.html — 과거 다이제스트 목록
    archive_entries = []
    for entry in sorted(os.listdir(site_dir), reverse=True):
        full = os.path.join(site_dir, entry)
        if os.path.isdir(full) and not entry.startswith("."):
            archive_entries.append(entry)
    archive_links = "\n".join(
        f'<li><a href="{e}/">{e}</a></li>' for e in archive_entries
    )
    archive_html = f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<title>Digest archive</title>
<style>
body {{ font-family: system-ui, -apple-system, sans-serif;
       max-width: 600px; margin: 60px auto; padding: 0 20px; }}
h1 {{ font-weight: 500; }} li {{ margin: 8px 0; }}
a {{ color: #2B7FB8; }}
</style>
</head><body>
<h1>Weekly digest archive</h1>
<ul>{archive_links}</ul>
</body></html>
"""
    with open(os.path.join(site_dir, "archive.html"), "w", encoding="utf-8") as f:
        f.write(archive_html)

    return f"{week_slug}/", "/"


# ============================================================================
# Notion 페이지 생성 (HTML 링크 포함)
# ============================================================================

def chunk_text(text: str, max_len: int = 1900) -> list[str]:
    if len(text) <= max_len:
        return [text]
    return [text[i:i + max_len] for i in range(0, len(text), max_len)]


def rich_text_blocks(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": c}} for c in chunk_text(text)]


def create_notion_digest_page(digest: dict, papers: list[dict],
                              dashboard_url: str, week_label: str) -> str:
    parent_page_id = os.environ.get("NOTION_DIGEST_PAGE_ID")
    if not parent_page_id:
        print("ERROR: NOTION_DIGEST_PAGE_ID missing", file=sys.stderr)
        sys.exit(1)

    title = f"📅 Weekly digest — {week_label}"

    n_must = sum(1 for p in papers if p["must_read"])
    n_papers = len(papers)

    blocks = []

    # 1. 큰 링크 callout — 가장 위에 두어 클릭 유도
    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [
                {"type": "text", "text": {"content": "📊 Open full HTML dashboard →",
                                          "link": {"url": dashboard_url}},
                 "annotations": {"bold": True}},
            ],
            "icon": {"type": "emoji", "emoji": "📊"},
            "color": "blue_background",
        },
    })

    # 2. 헤드라인
    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text_blocks(digest.get("headline", "")),
            "icon": {"type": "emoji", "emoji": "📰"},
            "color": "default",
        },
    })

    # 3. 기본 메트릭 한 줄
    blocks.append({
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text_blocks(
            f"📚 {n_papers}편 · ⭐ Must-read {n_must}편 · 지난 {LOOKBACK_DAYS}일"
        )},
    })

    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # 4. Trend
    blocks.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text",
                                      "text": {"content": "🌊 이번 주 트렌드"}}]},
    })
    blocks.append({
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text_blocks(digest.get("trend_summary", ""))},
    })

    # 5. Synergy
    blocks.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text",
                                      "text": {"content": "🎯 본인 연구와의 시너지"}}]},
    })
    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text_blocks(digest.get("synergy_for_you", "")),
            "icon": {"type": "emoji", "emoji": "🎯"},
            "color": "yellow_background",
        },
    })

    # 6. Highlights — 3편만 간단히
    blocks.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text",
                                      "text": {"content": f"⭐ 핵심 {TOP_HIGHLIGHTS}편"}}]},
    })
    for h in (digest.get("highlights") or [])[:TOP_HIGHLIGHTS]:
        blocks.append({
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": rich_text_blocks(f"★ {h.get('title', '')}"),
                "children": [{
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": rich_text_blocks(h.get("why_important", ""))},
                }],
            },
        })

    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # 7. 다시 한 번 링크 강조 (스크롤 끝)
    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [
                {"type": "text", "text": {"content": "전체 논문 카드와 시각적 대시보드는 여기서 → ",
                                          "link": None}},
                {"type": "text", "text": {"content": "Open dashboard",
                                          "link": {"url": dashboard_url}},
                 "annotations": {"bold": True, "underline": True}},
            ],
            "icon": {"type": "emoji", "emoji": "🔗"},
            "color": "gray_background",
        },
    })

    body = {
        "parent": {"page_id": parent_page_id},
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": title}}]}
        },
        "children": blocks,
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

    pages_base = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
    if not pages_base:
        print("ERROR: PAGES_BASE_URL missing", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY missing", file=sys.stderr)
        sys.exit(1)
    client = OpenAI(api_key=api_key)

    now_kst = datetime.now(timezone(timedelta(hours=9)))
    week_slug = now_kst.strftime("%Y-%m-%d")
    week_label = now_kst.strftime("%Y-%m-%d (%a)")

    # 1. 지난 7일 논문 가져오기
    papers = fetch_recent_papers(database_id, LOOKBACK_DAYS)
    if not papers:
        print("[done] no papers in last week — skipping digest")
        return

    # 2. 각 페이지에서 one_liner / for_you 보강 (카드 풍성하게)
    print("[fetch] augmenting paper bodies...")
    for p in papers[:MAX_PAPERS_FOR_DIGEST]:
        body = fetch_page_body_summary(p["page_id"])
        p["one_liner"] = body["one_liner"]
        p["for_you"] = body["for_you"]

    # 3. OpenAI 메타 요약
    digest = generate_digest(client, papers)
    if digest is None:
        print("[done] failed to generate digest")
        sys.exit(1)

    # 4. HTML 생성
    html = render_html(digest, papers[:MAX_PAPERS_FOR_DIGEST], week_label)
    week_path, _ = write_html_files(html, week_slug)
    dashboard_url = f"{pages_base}/{week_path}"
    print(f"[html] dashboard URL: {dashboard_url}")

    # 5. Notion 페이지 생성
    create_notion_digest_page(digest, papers, dashboard_url, week_label)


if __name__ == "__main__":
    main()
