import os
import sys
import json
import time
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
SEARCH_QUERY = os.gatenv("SEARCH_QUERY", "").strip()
HACKCLUB_API_KEY = os.getenv

load_dotenv()

SEARCH_QUERY = os.getenv("SEARCH_QUERY", "").strip()
HACKCLUB_API_KEY = os.getenv("HACKCLUB_API_KEY", "").strip()
NUM_RESULTS = int(os.getenv("NUM_RESULTS", "5"))
SMART_FILTER = os.getenv("SMART_FILTER", "true").strip().lower() in ("1", "true", "yes")
QUERY_EXPANSION = os.getenv("QUERY_EXPANSION", "false").strip().lower() in ("1", "true", "yes")
NUM_SUBQUERIES = int(os.getenv("NUM_SUBQUERIES", "6"))
RESULTS_PER_SUBQUERY = int(os.getenv("RESULTS_PER_SUBQUERY", "5"))
DEEP_SCRAPE = os.getenv("DEEP_SCRAPE", "false").strip().lower() in ("1", "true", "yes")
DEEP_SCRAPE_QUERIES = int(os.getenv("DEEP_SCRAPE_QUERIES", "5"))
DEEP_SCRAPE_RESULTS_PER_QUERY = int(os.getenv("DEEP_SCRAPE_RESULTS_PER_QUERY", "5"))
DEEP_SCRAPE_TRIGGER_RATIO = float(os.getenv("DEEP_SCRAPE_TRIGGER_RATIO", "0.3"))
AI_MODEL = os.getenv("AI_MODEL", "llama-3.3-70b-versatile")

FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "AI_MODEL_FALLBACKS",
        "llama-3.3-70b-versatile,meta-llama/llama-3.1-8b-instruct,mistralai/mistral-7b-instruct",
    ).split(",")
    if m.strip()
]

HACKCLUB_BASE_URL = "https://ai.hackclub.com/proxy/v1"
HACKCLUB_CHAT_URL = f"{HACKCLUB_BASE_URL}/chat/completions"

SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "bing").strip().lower()

BING_SEARCH_URL = "https://www.bing.com/search"
BING_MARKET = os.getenv("BING_MARKET", "en-US")

DDG_HTML_URL = "https://html.duckduckgo.com/html/"

EXCLUDE_DOMAINS = [
    d.strip().lower()
    for d in os.getenv(
        "EXCLUDE_DOMAINS",
        "kbbi.kemdikbud.go.id,kbbi.web.id,kbbi.co.id,lirik.kapanlagi.com,"
        "liriklaguindonesia.net,youtube.com,music.youtube.com",
    ).split(",")
    if d.strip()
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

DOH_ENABLED = os.getenv("USE_DOH", "true").strip().lower() in ("1", "true", "yes")
DOH_TARGET_HOSTS = {"www.bing.com", "bing.com", "html.duckduckgo.com", "duckduckgo.com"}
_doh_cache = {}
_original_getaddrinfo = socket.getaddrinfo


def _doh_resolve(hostname: str):
    if hostname in _doh_cache:
        return _doh_cache[hostname]
    try:
        r = requests.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": hostname, "type": "A"},
            headers={"accept": "application/dns-json"},
            timeout=10,
        )
        answers = r.json().get("Answer", [])
        ips = [a["data"] for a in answers if a.get("type") == 1]
        if ips:
            _doh_cache[hostname] = ips[0]
            log(f"  (DoH resolved {hostname} -> {ips[0]})")
            return ips[0]
    except Exception as e:
        log(f"  (DoH lookup for {hostname} failed: {e})")
    return None


def _patched_getaddrinfo(host, *args, **kwargs):
    if DOH_ENABLED and host in DOH_TARGET_HOSTS:
        ip = _doh_resolve(host)
        if ip:
            return _original_getaddrinfo(ip, *args, **kwargs)
    return _original_getaddrinfo(host, *args, **kwargs)


if DOH_ENABLED:
    socket.getaddrinfo = _patched_getaddrinfo

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
JSON_OUTPUT_PATH = os.path.join(OUTPUT_DIR, "results.json")
HTML_OUTPUT_PATH = os.path.join(OUTPUT_DIR, "index.html")

MAX_PAGE_CHARS = 6000


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def fail(msg: str) -> None:
    print(f"\n✗ {msg}\n", file=sys.stderr)
    sys.exit(1)


DEBUG_URL = os.getenv("DEBUG_URL", "").strip()


def _warm_up_bing_session() -> None:
    try:
        SESSION.get("https://www.bing.com/", timeout=15)
    except requests.RequestException as e:
        print(f"(warm-up request to bing.com failed: {e})")
    SESSION.cookies.set("SRCHHPGUSR", "ADLT=OFF", domain=".bing.com")
    SESSION.cookies.set("SRCHD", "AF=OFF", domain=".bing.com")


def debug_fetch(url: str) -> None:
    print("=" * 60)
    print(" DEBUG MODE — fetching exact URL")
    print("=" * 60)
    print(f"URL: {url}\n")

    _warm_up_bing_session()

    resp = SESSION.get(url, timeout=20)
    print(f"Status code : {resp.status_code}")
    print(f"Content len : {len(resp.text)} chars")
    print(f"Final URL   : {resp.url}")
    print(f"History     : {[r.status_code for r in resp.history]} (redirects)")

    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select("li.b_algo")
    print(f"li.b_algo found: {len(items)}")

    title_tag = soup.select_one("title")
    print(f"<title>: {title_tag.get_text(strip=True) if title_tag else '(none)'}")

    print("\n--- result titles found ---")
    for item in items[:10]:
        t = item.select_one("h2 a")
        if t:
            print(f"  - {t.get_text(strip=True)}")

    print("\n--- first 1500 chars of response ---")
    print(resp.text[:1500])
    print("--- end snippet ---")


def is_excluded(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in EXCLUDE_DOMAINS)


def _clean_ddg_href(href: str) -> str:
    from urllib.parse import parse_qs, unquote

    if href.startswith("//duckduckgo.com/l/") or href.startswith("/l/"):
        href = "https://duckduckgo.com" + href if href.startswith("/l/") else "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        real = qs.get("uddg")
        if real:
            return unquote(real[0])
    return href


def search_duckduckgo(query: str, n: int) -> list:
    log(f"Searching DuckDuckGo for: {query!r}")

    try:
        SESSION.get("https://duckduckgo.com/", timeout=15)
    except requests.RequestException as e:
        log(f"  (warm-up request to duckduckgo.com failed: {e})")

    resp = SESSION.post(
        DDG_HTML_URL,
        data={"q": query},
        timeout=20,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    skipped_excluded = 0
    for result in soup.select("div.result"):
        link_tag = result.select_one("a.result__a")
        if not link_tag:
            continue
        title = link_tag.get_text(strip=True)
        url = _clean_ddg_href(link_tag.get("href", ""))
        if not url or not url.startswith("http"):
            continue
        if is_excluded(url):
            skipped_excluded += 1
            continue

        snippet_tag = result.select_one(".result__snippet")
        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= n:
            break

    if skipped_excluded:
        log(f"  (skipped {skipped_excluded} result(s) from excluded domains)")

    if not results:
        fail(
            "No search results found on DuckDuckGo. Its markup may have "
            "changed, the request may be blocked, or the network can't "
            "reach duckduckgo.com from this environment."
        )

    log(f"Found {len(results)} result(s)")
    return results


def search_bing(query: str, n: int) -> list:
    log(f"Searching Bing for: {query!r}")

    _warm_up_bing_session()

    results = []
    seen_urls = set()
    skipped_excluded = 0
    page = 0

    while len(results) < n and page < 8:
        params = {
            "q": query,
            "first": str(page * 10 + 1),
            "mkt": BING_MARKET,
            "setmkt": BING_MARKET,
            "adlt": "off",
            "_": str(int(time.time() * 1000)),
        }
        prepared = requests.Request(
            "GET", BING_SEARCH_URL, params=params, headers=HEADERS
        ).prepare()
        if page == 0:
            log(f"  URL: {prepared.url}")

        resp = SESSION.get(BING_SEARCH_URL, params=params, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        items = soup.select("li.b_algo")
        if not items:
            break

        for item in items:
            link_tag = item.select_one("h2 a")
            if not link_tag:
                continue
            title = link_tag.get_text(strip=True)
            url = link_tag.get("href", "")
            if not url or not url.startswith("http") or url in seen_urls:
                continue
            if is_excluded(url):
                seen_urls.add(url)
                skipped_excluded += 1
                continue

            snippet_tag = item.select_one(".b_caption p") or item.select_one("p")
            snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

            seen_urls.add(url)
            results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= n:
                break

        page += 1
        time.sleep(0.3)

    if skipped_excluded:
        log(f"  (skipped {skipped_excluded} result(s) from excluded domains)")

    if not results:
        fail(
            "No search results found. Bing may have changed its markup, "
            "blocked this request, filtered everything via EXCLUDE_DOMAINS, "
            "or the network can't reach bing.com from this environment."
        )

    log(f"Found {len(results)} result(s)")
    return results


def run_search(query: str, n: int) -> list:
    if SEARCH_ENGINE == "duckduckgo":
        return search_duckduckgo(query, n)
    return search_bing(query, n)


def scrape_page_text(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"  ! could not fetch {url}: {e}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:MAX_PAGE_CHARS]


def _hackclub_request(model: str, system_prompt: str, user_prompt: str, retries: int = 2):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {HACKCLUB_API_KEY}",
        "Content-Type": "application/json",
    }

    last_error = "unknown error"
    for attempt in range(1, retries + 2): 
        try:
            resp = requests.post(
                HACKCLUB_CHAT_URL, json=payload, headers=headers, timeout=90
            )
        except requests.RequestException as e:
            last_error = f"network error: {e}"
            log(f"  ! attempt {attempt} failed ({last_error}), retrying..." if attempt <= retries else f"  ! attempt {attempt} failed ({last_error})")
            time.sleep(2 * attempt)
            continue

        if resp.status_code != 200:
            return False, f"{resp.status_code}: {resp.text[:200]}"

        data = resp.json()
        try:
            return True, data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            return False, "unexpected response shape"

    return False, last_error


def call_hackclub_ai(system_prompt: str, user_prompt: str) -> str:
    if not HACKCLUB_API_KEY:
        fail(
            "HACKCLUB_API_KEY is missing from .env.\n"
            "  Get one from your Hack Club AI dashboard, then add it to .env:\n"
            "  HACKCLUB_API_KEY=sk-..."
        )

    models_to_try = [AI_MODEL] + [m for m in FALLBACK_MODELS if m != AI_MODEL]

    for model in models_to_try:
        ok, result = _hackclub_request(model, system_prompt, user_prompt)
        if ok:
            return result
        log(f"  ! Hack Club AI error with model '{model}': {result}")

    return "(AI summary unavailable — all models failed, see log above)"


def expand_query(query: str, n: int) -> list:
    system_prompt = (
        f"You turn one search query into exactly {n} diverse, more specific "
        "search queries that together cover the topic from different "
        "angles (e.g. different platforms, contexts, phrasings, related "
        "terms, or narrower specifics). Each must be a plain search-engine "
        "query on its own — no explanations, no numbering, no quotes "
        "around them unless the original had them. Reply with EXACTLY one "
        f"query per line, {n} lines total, nothing else."
    )
    user_prompt = f"Original query: {query}"
    response = call_hackclub_ai(system_prompt, user_prompt)

    queries = []
    for line in response.splitlines():
        line = line.strip()
        line = re.sub(r"^[\d]+[\.\)]\s*", "", line)
        line = line.strip("-•* \t")
        if line:
            queries.append(line)

    if not queries:
        return [query]
    return queries[:n]


def batch_check_relevance(query: str, results: list) -> dict:
    CHUNK = 25
    verdicts = {}

    for start in range(0, len(results), CHUNK):
        chunk = results[start : start + CHUNK]
        listing = "\n".join(
            f"{i + 1}. Title: {r['title']} | URL: {r['url']} | Snippet: {r['snippet']}"
            for i, r in enumerate(chunk)
        )
        system_prompt = (
            "You judge search-result relevance before a scraper spends time "
            "fetching full pages. For EACH numbered result below, decide if "
            "it is actually likely to be ABOUT what the query is looking for "
            "(not just a page that happens to contain a matching word — e.g. "
            "a dictionary definition, an unrelated help/forum page, or a "
            "generic homepage is NOT relevant unless it's clearly on-topic).\n"
            "Reply with EXACTLY one line per result, in the same order, in "
            "this format:\n"
            "<number>: YES: <short reason>\n"
            "or\n"
            "<number>: NO: <short reason>\n"
            f"You must output exactly {len(chunk)} lines, numbered {1} to "
            f"{len(chunk)}. No other text, no preamble."
        )
        user_prompt = f"Search query: {query}\n\nResults:\n{listing}"
        response = call_hackclub_ai(system_prompt, user_prompt)

        for line in response.splitlines():
            line = line.strip()
            m = re.match(r"^\**(\d+)\**\s*[:.]\s*(YES|NO)\s*:?\s*(.*)$", line, re.IGNORECASE)
            if not m:
                continue
            local_idx = int(m.group(1)) - 1
            if not (0 <= local_idx < len(chunk)):
                continue
            is_yes = m.group(2).upper() == "YES"
            reason = m.group(3).strip().strip("*")
            verdicts[start + local_idx] = (is_yes, reason)

    return verdicts


def summarize_result(query: str, result: dict, page_text: str) -> str:
    system_prompt = (
        "You are a research assistant. Given raw scraped webpage text and a "
        "user's search query, write a concise, factual summary (3-5 sentences) "
        "of how this page is relevant to the query. If the page text is empty "
        "or irrelevant, say so plainly. Do not invent facts not in the text."
    )
    user_prompt = (
        f"Search query: {query}\n\n"
        f"Page title: {result['title']}\n"
        f"Page URL: {result['url']}\n\n"
        f"Page text (truncated):\n{page_text if page_text else '[no text extracted]'}"
    )
    return call_hackclub_ai(system_prompt, user_prompt)


JUNK_PAGE_PATTERNS = [
    r"just a moment",
    r"checking your browser",
    r"enable javascript",
    r"you are being redirected",
    r"please wait while",
    r"redirecting you to",
    r"attention required",
    r"ddos protection by",
    r"verifying you are human",
]


def is_junk_page(text: str) -> bool:
    if not text or len(text.strip()) < 200:
        return True
    lower = text.lower()
    return any(re.search(p, lower) for p in JUNK_PAGE_PATTERNS)


def generate_followup_queries(query: str, tier1_results: list, n: int) -> list:
    joined = "\n\n".join(
        f"- {it['title']} ({it['url']}): {it['ai_summary']}" for it in tier1_results
    )
    system_prompt = (
        "You are planning a second round of web research. Given the original "
        "query and what the first round of sources actually said (including "
        "cases where sources had no usable content, e.g. blocked by "
        "anti-bot/redirect pages), identify the specific sub-topics, terms, "
        "named laws/procedures/entities that are STILL unanswered or "
        "unconfirmed. Turn each gap into one concrete, specific search "
        f"query likely to find that exact information elsewhere. Output "
        f"exactly {n} queries, one per line, no numbering, no explanations."
    )
    user_prompt = (
        f"Original query: {query}\n\nFirst-round source summaries:\n{joined}"
    )
    response = call_hackclub_ai(system_prompt, user_prompt)

    queries = []
    for line in response.splitlines():
        line = line.strip()
        line = re.sub(r"^[\d]+[\.\)]\s*", "", line)
        line = line.strip("-•* \t")
        if line:
            queries.append(line)
    return queries[:n]


def build_overall_summary(query: str, items: list) -> str:
    system_prompt = (
        "You are a research assistant. You are given several per-source "
        "summaries produced from a web search. Write a short overview "
        "(4-6 sentences) synthesizing the key findings across all sources "
        "relevant to the user's query. Note any disagreement between sources."
    )
    joined = "\n\n".join(
        f"- {it['title']} ({it['url']}): {it['ai_summary']}" for it in items
    )
    user_prompt = f"Search query: {query}\n\nPer-source summaries:\n{joined}"
    return call_hackclub_ai(system_prompt, user_prompt)

def write_json(data: dict) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(JSON_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log(f"Wrote JSON -> {JSON_OUTPUT_PATH}")


def esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_html(data: dict) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    subqueries_html = ""
    if data.get("subqueries"):
        chips = "".join(f'<span class="chip">{esc(sq)}</span>' for sq in data["subqueries"])
        subqueries_html = f'<div class="subqueries">{chips}</div>'

    cards = []
    for it in data["results"]:
        matched = it.get("matched_query")
        matched_html = f'<div class="matched">via: {esc(matched)}</div>' if matched else ""
        cards.append(
            f"""
        <article class="card">
          <h2><a href="{esc(it['url'])}" target="_blank" rel="noopener">{esc(it['title']) or esc(it['url'])}</a></h2>
          <div class="url">{esc(it['url'])}</div>
          {matched_html}
          <p class="snippet">{esc(it['snippet'])}</p>
          <div class="ai-summary">
            <span class="tag">AI summary</span>
            <p>{esc(it['ai_summary'])}</p>
          </div>
        </article>"""
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scrape results: {esc(data['query'])}</title>
<style>
  :root {{
    --bg: #0f1115;
    --card: #171a21;
    --border: #262a33;
    --text: #e7e9ee;
    --muted: #9aa3b2;
    --accent: #7c9eff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }}
  header {{
    padding: 32px 20px 20px;
    max-width: 860px;
    margin: 0 auto;
  }}
  header h1 {{
    margin: 0 0 6px;
    font-size: 1.6rem;
  }}
  header .meta {{
    color: var(--muted);
    font-size: 0.9rem;
  }}
  .overview {{
    max-width: 860px;
    margin: 0 auto 28px;
    padding: 18px 20px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
  }}
  .overview h3 {{
    margin: 0 0 8px;
    font-size: 1rem;
    color: var(--accent);
  }}
  main {{
    max-width: 860px;
    margin: 0 auto;
    padding: 0 20px 60px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px 20px;
  }}
  .card h2 {{
    margin: 0 0 4px;
    font-size: 1.1rem;
  }}
  .card h2 a {{
    color: var(--text);
    text-decoration: none;
  }}
  .card h2 a:hover {{
    color: var(--accent);
    text-decoration: underline;
  }}
  .url {{
    color: var(--muted);
    font-size: 0.78rem;
    word-break: break-all;
    margin-bottom: 10px;
  }}
  .snippet {{
    color: var(--muted);
    font-size: 0.92rem;
    margin: 0 0 12px;
  }}
  .ai-summary {{
    border-top: 1px dashed var(--border);
    padding-top: 10px;
  }}
  .ai-summary p {{
    margin: 6px 0 0;
    font-size: 0.94rem;
  }}
  .tag {{
    display: inline-block;
    font-size: 0.68rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--accent);
    background: rgba(124,158,255,0.12);
    border: 1px solid rgba(124,158,255,0.3);
    padding: 2px 8px;
    border-radius: 999px;
  }}
  .subqueries {{
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px dashed var(--border);
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }}
  .chip {{
    font-size: 0.75rem;
    color: var(--muted);
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    padding: 3px 10px;
    border-radius: 999px;
  }}
  .matched {{
    font-size: 0.72rem;
    color: var(--accent);
    margin-bottom: 8px;
  }}
  footer {{
    text-align: center;
    color: var(--muted);
    font-size: 0.8rem;
    padding-bottom: 40px;
  }}
</style>
</head>
<body>
<header>
  <h1>🔎 {esc(data['query'])}</h1>
  <div class="meta">{len(data['results'])} sources · generated {esc(data['generated_at'])} · model {esc(data['model'])}</div>
</header>

<section class="overview">
  <h3>AI overview</h3>
  <p>{esc(data['overall_summary'])}</p>
  {subqueries_html}
</section>

<main>
{"".join(cards)}
</main>

<footer>Generated by the terminal AI scraper (Hack Club AI).</footer>
</body>
</html>
"""
    with open(HTML_OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Wrote HTML -> {HTML_OUTPUT_PATH}")

def main():
    if DEBUG_URL:
        debug_fetch(DEBUG_URL)
        return

    print("=" * 60)
    print(" AI-Powered Terminal Scraper (Hack Club AI)")
    print("=" * 60)
    log(f"Search engine: {SEARCH_ENGINE}")

    if not SEARCH_QUERY:
        fail(
            "SEARCH_QUERY is missing from .env.\n"
            "  Copy .env.example to .env and set:\n"
            "  SEARCH_QUERY=your search terms here"
        )

    subqueries = []
    if QUERY_EXPANSION:
        log(f"Expanding query into {NUM_SUBQUERIES} search angles via AI...")
        subqueries = expand_query(SEARCH_QUERY, NUM_SUBQUERIES)
        for sq in subqueries:
            log(f"  → {sq}")

        search_results = []
        seen = set()
        for i, sq in enumerate(subqueries, start=1):
            log(f"[query {i}/{len(subqueries)}] searching: {sq!r}")
            try:
                sub_results = run_search(sq, RESULTS_PER_SUBQUERY)
            except SystemExit:
                log(f"  (no results for this angle, continuing)")
                continue
            for r in sub_results:
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                search_results.append({**r, "matched_query": sq})
            time.sleep(0.5)

        if not search_results:
            fail("None of the expanded queries returned any results.")
        log(f"Combined {len(search_results)} unique result(s) across {len(subqueries)} queries")
    else:
        search_results = run_search(SEARCH_QUERY, NUM_RESULTS)

    verdicts = {}
    if SMART_FILTER:
        log(f"Running batch AI relevance check on all {len(search_results)} result(s)...")
        verdicts = batch_check_relevance(SEARCH_QUERY, search_results)
        n_green = sum(1 for v in verdicts.values() if v[0])
        log(f"  → {n_green} green-flagged, {len(search_results) - n_green} red-flagged")

    enriched = []
    skipped = []
    for i, r in enumerate(search_results, start=1):
        log(f"[{i}/{len(search_results)}] {r['title'][:70]}")

        if SMART_FILTER:
            relevant, reason = verdicts.get(
                i - 1, (True, "no verdict parsed — including by default")
            )
            if not relevant:
                log(f"  ✗ skipped (AI: not relevant — {reason})")
                skipped.append({**r, "skip_reason": reason})
                continue
            log(f"  ✓ relevant (AI: {reason}) — scraping page...")

        page_text = scrape_page_text(r["url"])
        junk = is_junk_page(page_text)
        if junk:
            log(f"  ⚠ page looks like a redirect/anti-bot interstitial, not real content")
        ai_summary = summarize_result(SEARCH_QUERY, r, page_text)
        enriched.append({**r, "ai_summary": ai_summary, "page_blocked": junk})
        time.sleep(0.5)

    if SMART_FILTER:
        log(f"Smart filter kept {len(enriched)}/{len(search_results)} result(s)")

    if not enriched:
        fail(
            "The smart filter marked every result as not relevant, so there's "
            "nothing left to summarize. Try a broader SEARCH_QUERY, increase "
            "NUM_RESULTS, or set SMART_FILTER=false in .env to disable filtering."
        )

    if DEEP_SCRAPE:
        junk_count = sum(1 for it in enriched if it.get("page_blocked"))
        junk_ratio = junk_count / len(enriched) if enriched else 0
        if junk_ratio >= DEEP_SCRAPE_TRIGGER_RATIO:
            log(
                f"Tier 1 had {junk_count}/{len(enriched)} blocked/junk page(s) "
                f"({junk_ratio:.0%}) — starting deep-scrape tier 2..."
            )
            followups = generate_followup_queries(SEARCH_QUERY, enriched, DEEP_SCRAPE_QUERIES)
            for fq in followups:
                log(f"  → follow-up: {fq}")

            already_scraped_urls = {it["url"] for it in enriched}
            tier2_raw = []
            for i, fq in enumerate(followups, start=1):
                log(f"[deep-dive {i}/{len(followups)}] searching: {fq!r}")
                try:
                    sub_results = run_search(fq, DEEP_SCRAPE_RESULTS_PER_QUERY)
                except SystemExit:
                    log("  (no results for this follow-up, continuing)")
                    continue
                for r in sub_results:
                    if r["url"] in already_scraped_urls:
                        continue
                    already_scraped_urls.add(r["url"])
                    tier2_raw.append({**r, "matched_query": fq, "tier": 2})
                time.sleep(0.5)

            if tier2_raw:
                log(f"Deep-scrape tier 2: {len(tier2_raw)} new unique result(s) found")
                tier2_verdicts = {}
                if SMART_FILTER:
                    log(f"Running batch AI relevance check on {len(tier2_raw)} tier-2 result(s)...")
                    tier2_verdicts = batch_check_relevance(SEARCH_QUERY, tier2_raw)

                for i, r in enumerate(tier2_raw, start=1):
                    log(f"[tier2 {i}/{len(tier2_raw)}] {r['title'][:70]}")
                    if SMART_FILTER:
                        relevant, reason = tier2_verdicts.get(
                            i - 1, (True, "no verdict parsed — including by default")
                        )
                        if not relevant:
                            log(f"  ✗ skipped (AI: not relevant — {reason})")
                            skipped.append({**r, "skip_reason": reason})
                            continue
                        log(f"  ✓ relevant (AI: {reason}) — scraping page...")
                    page_text = scrape_page_text(r["url"])
                    junk2 = is_junk_page(page_text)
                    if junk2:
                        log("  ⚠ page looks like a redirect/anti-bot interstitial, not real content")
                    ai_summary = summarize_result(SEARCH_QUERY, r, page_text)
                    enriched.append({**r, "ai_summary": ai_summary, "page_blocked": junk2})
                    time.sleep(0.5)
            else:
                log("Deep-scrape tier 2: no new unique results found from follow-up queries")
        else:
            log(
                f"Tier 1 blocked ratio {junk_ratio:.0%} is below "
                f"DEEP_SCRAPE_TRIGGER_RATIO — skipping deep-scrape tier 2"
            )

    log("Building overall AI overview...")
    overall_summary = build_overall_summary(SEARCH_QUERY, enriched)

    data = {
        "query": SEARCH_QUERY,
        "query_expansion_used": QUERY_EXPANSION,
        "subqueries": subqueries if QUERY_EXPANSION else [],
        "skipped_by_smart_filter": skipped,
        "model": AI_MODEL,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "overall_summary": overall_summary,
        "results": enriched,
    }

    write_json(data)
    write_html(data)

    print("\n✓ Done.")
    print(f"  JSON:    {JSON_OUTPUT_PATH}")
    print(f"  Website: {HTML_OUTPUT_PATH}  (open it in your browser)")


if __name__ == "__main__":
    main()
