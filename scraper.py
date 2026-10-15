import csv
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()


def env(key, default=""):
    return os.getenv(key, default).strip()


def flag(key, default):
    return env(key, default).lower() in ("1", "true", "yes")


def items_of(key, default=""):
    return [x.strip() for x in env(key, default).split(",") if x.strip()]


QUERY = env("SEARCH_QUERY")
API_KEY = env("HACKCLUB_API_KEY")
ENGINE = env("SEARCH_ENGINE", "bing").lower()
MARKET = env("BING_MARKET", "en-US")
NUM_RESULTS = int(env("NUM_RESULTS", "5"))
MODEL = env("AI_MODEL", "llama-3.3-70b-versatile")
MODELS = [MODEL] + [
    m for m in items_of("AI_MODEL_FALLBACKS", "llama-3.3-70b-versatile,meta-llama/llama-3.1-8b-instruct,mistralai/mistral-7b-instruct") if m != MODEL
]
SMART_FILTER = flag("SMART_FILTER", "true")
EXPAND = flag("QUERY_EXPANSION", "false")
SUBQUERIES = int(env("NUM_SUBQUERIES", "6"))
PER_SUBQUERY = int(env("RESULTS_PER_SUBQUERY", "5"))
DEEP = flag("DEEP_SCRAPE", "false")
DEEP_QUERIES = int(env("DEEP_SCRAPE_QUERIES", "5"))
DEEP_PER_QUERY = int(env("DEEP_SCRAPE_RESULTS_PER_QUERY", "5"))
DEEP_RATIO = float(env("DEEP_SCRAPE_TRIGGER_RATIO", "0.3"))
CRAWL_URLS = items_of("CRAWL_URLS")
CRAWL_FILE = env("CRAWL_URLS_FILE")
EXCLUDE = [d.lower() for d in items_of("EXCLUDE_DOMAINS", "kbbi.kemdikbud.go.id,kbbi.web.id,kbbi.co.id,lirik.kapanlagi.com,liriklaguindonesia.net,youtube.com,music.youtube.com")]

CHAT_URL = "https://ai.hackclub.com/proxy/v1/chat/completions"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
RESULT_FIELDS = ["time", "run_id", "query", "status", "tier", "matched_query", "title", "url", "snippet", "ai_summary", "page_blocked", "skip_reason"]
LOG_FIELDS = ["time", "run_id", "level", "message"]
MAX_CHARS = 6000
JUNK = re.compile("just a moment|checking your browser|enable javascript|you are being redirected|please wait while|redirecting you to|attention required|ddos protection by|verifying you are human")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


class Fatal(Exception):
    pass


STATS = {"skipped": 0}
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")


def open_csv(name, fields):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    f = open(path, "a", newline="", encoding="utf-8-sig")
    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    if new:
        w.writeheader()
    return f, w


RESULTS_F, RESULTS_W = open_csv("results.csv", RESULT_FIELDS)
LOG_F, LOG_W = open_csv("log.csv", LOG_FIELDS)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    s = msg.lstrip()
    level = {"✗": "SKIP", "⚠": "WARN", "!": "ERROR", "✓": "OK"}.get(s[:1], "INFO")
    print(f"[{now()[11:]}] {msg}")
    LOG_W.writerow({"time": now(), "run_id": RUN_ID, "level": level, "message": s})
    LOG_F.flush()


def emit(row):
    RESULTS_W.writerow({"time": now(), "run_id": RUN_ID, "query": QUERY, "tier": 1, **row})
    RESULTS_F.flush()


def excluded(url):
    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in EXCLUDE)


def parse_results(entries, n, seen):
    out = []
    for title, url, snippet in entries:
        if not url.startswith("http") or url in seen or excluded(url):
            continue
        seen.add(url)
        out.append({"title": title, "url": url, "snippet": snippet})
        if len(out) >= n:
            break
    return out


def search_bing(query, n):
    try:
        SESSION.get("https://www.bing.com/", timeout=15)
    except requests.RequestException:
        pass
    SESSION.cookies.set("SRCHHPGUSR", "ADLT=OFF", domain=".bing.com")
    SESSION.cookies.set("SRCHD", "AF=OFF", domain=".bing.com")
    results, seen = [], set()
    for page in range(8):
        if len(results) >= n:
            break
        params = {"q": query, "first": page * 10 + 1, "mkt": MARKET, "setmkt": MARKET, "adlt": "off", "_": int(time.time() * 1000)}
        resp = SESSION.get("https://www.bing.com/search", params=params, timeout=20)
        resp.raise_for_status()
        entries = []
        for li in BeautifulSoup(resp.text, "html.parser").select("li.b_algo"):
            a = li.select_one("h2 a")
            if a:
                p = li.select_one(".b_caption p") or li.select_one("p")
                entries.append((a.get_text(strip=True), a.get("href", ""), p.get_text(strip=True) if p else ""))
        if not entries:
            break
        results += parse_results(entries, n - len(results), seen)
        time.sleep(0.3)
    return results


def ddg_href(href):
    if href.startswith("/l/"):
        href = "https://duckduckgo.com" + href
    elif href.startswith("//"):
        href = "https:" + href
    real = parse_qs(urlparse(href).query).get("uddg")
    return unquote(real[0]) if real else href


def search_ddg(query, n):
    resp = SESSION.post("https://html.duckduckgo.com/html/", data={"q": query}, timeout=20)
    resp.raise_for_status()
    entries = []
    for div in BeautifulSoup(resp.text, "html.parser").select("div.result"):
        a = div.select_one("a.result__a")
        if a:
            s = div.select_one(".result__snippet")
            entries.append((a.get_text(strip=True), ddg_href(a.get("href", "")), s.get_text(strip=True) if s else ""))
    return parse_results(entries, n, set())


def search(query, n):
    log(f"Searching {ENGINE} for: {query!r}")
    results = (search_ddg if ENGINE == "duckduckgo" else search_bing)(query, n)
    if not results:
        raise Fatal(f"No search results from {ENGINE} for {query!r}")
    log(f"Found {len(results)} result(s)")
    return results


def fetch_page(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"! could not fetch {url}: {e}")
        return "", ""
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.select_one("title")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True))
    return text[:MAX_CHARS], title.get_text(strip=True) if title else ""


def is_junk(text):
    return len(text.strip()) < 200 or bool(JUNK.search(text[:800].lower()))


def pick_models():
    global MODELS
    MODELS = [m for m in MODELS if ":batch" not in m]
    try:
        resp = requests.get(CHAT_URL.replace("/chat/completions", "/models"), headers={"Authorization": f"Bearer {API_KEY}"}, timeout=20)
        ids = [m["id"] for m in resp.json()["data"]]
    except Exception as e:
        log(f"⚠ could not list models ({e}), using configured ones")
        return
    usable = [i for i in ids if ":batch" not in i and not re.search("embed|whisper|image|tts|moderation", i)]
    MODELS = [m for m in MODELS if m in usable] or usable[:3]
    log(f"Using models: {', '.join(MODELS)}")


def ask_model(model, system, user):
    payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    error = "unknown error"
    for attempt in range(1, 4):
        try:
            resp = requests.post(CHAT_URL, json=payload, headers=headers, timeout=90)
        except requests.RequestException as e:
            error = f"network error: {e}"
            log(f"! attempt {attempt} failed ({error})")
            time.sleep(2 * attempt)
            continue
        if resp.status_code != 200:
            return None, f"{resp.status_code}: {resp.text[:200]}"
        try:
            return resp.json()["choices"][0]["message"]["content"].strip(), ""
        except (KeyError, IndexError, ValueError):
            return None, "unexpected response shape"
    return None, error


def ai(system, user):
    if not API_KEY:
        raise Fatal("HACKCLUB_API_KEY is missing from .env")
    for model in MODELS:
        text, error = ask_model(model, system, user)
        if text is not None:
            return text
        log(f"! Hack Club AI error with model {model!r}: {error}")
    raise Fatal("All AI models failed, check AI_MODEL in .env and the errors above")


def lines_of(text, n):
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\d+[.)]\s*", "", line.strip()).strip("-•* \t")
        if line:
            out.append(line)
    return out[:n]


def expand_query(query, n):
    system = (
        f"Turn one search query into exactly {n} diverse, more specific search queries covering the topic from different angles "
        f"(platforms, contexts, phrasings, narrower specifics). Reply with exactly one plain query per line, {n} lines, nothing else."
    )
    return lines_of(ai(system, f"Original query: {query}"), n) or [query]


def followup_queries(query, kept, n):
    joined = "\n\n".join(f"- {r['title']} ({r['url']}): {r['ai_summary']}" for r in kept)
    system = (
        "You are planning a second round of web research. Given the original query and what the first sources said "
        "(including blocked or empty ones), find what is still unanswered and turn each gap into one concrete search query. "
        f"Output exactly {n} queries, one per line, no numbering, no explanations."
    )
    return lines_of(ai(system, f"Original query: {query}\n\nFirst-round summaries:\n{joined}"), n)


def judge(query, results):
    verdicts = {}
    for start in range(0, len(results), 25):
        chunk = results[start:start + 25]
        listing = "\n".join(f"{i + 1}. Title: {r['title']} | URL: {r['url']} | Snippet: {r['snippet']}" for i, r in enumerate(chunk))
        system = (
            "Judge search-result relevance before pages are fetched. For EACH numbered result decide if it is actually ABOUT what the query "
            "looks for (a dictionary definition, unrelated forum page or generic homepage is not relevant). "
            f"Reply with exactly {len(chunk)} lines in order, formatted '<number>: YES: <short reason>' or '<number>: NO: <short reason>'. Nothing else."
        )
        for line in ai(system, f"Search query: {query}\n\nResults:\n{listing}").splitlines():
            m = re.match(r"^\**(\d+)\**\s*[:.]\s*(YES|NO)\s*:?\s*(.*)$", line.strip(), re.I)
            if m and 0 < int(m.group(1)) <= len(chunk):
                verdicts[start + int(m.group(1)) - 1] = (m.group(2).upper() == "YES", m.group(3).strip().strip("*"))
    return verdicts


def summarize(r, text):
    system = (
        "You are a research assistant. Given scraped webpage text and a search query, write a concise factual summary (3-5 sentences) "
        "of how this page is relevant to the query. If the text is empty or irrelevant, say so. Do not invent facts."
    )
    user = f"Search query: {QUERY}\n\nPage title: {r['title']}\nPage URL: {r['url']}\n\nPage text:\n{text or '[no text extracted]'}"
    return ai(system, user)


def overview(kept):
    joined = "\n\n".join(f"- {r['title']} ({r['url']}): {r['ai_summary']}" for r in kept)
    system = (
        "You are a research assistant. Given per-source summaries from a web search, write a short overview (4-6 sentences) "
        "of the key findings relevant to the query. Note any disagreement between sources."
    )
    return ai(system, f"Search query: {QUERY}\n\nPer-source summaries:\n{joined}")


def process(items, tier, smart, kept):
    verdicts = judge(QUERY, items) if smart else {}
    if smart:
        yes = sum(1 for v in verdicts.values() if v[0])
        log(f"AI flags: {yes} green, {len(items) - yes} red")
    for i, r in enumerate(items):
        log(f"[{i + 1}/{len(items)}] {(r.get('title') or r['url'])[:70]}")
        ok, reason = verdicts.get(i, (True, "no verdict parsed"))
        if smart and not ok:
            log(f"✗ skipped ({reason})")
            STATS["skipped"] += 1
            emit({**r, "status": "skipped", "tier": tier, "skip_reason": reason})
            continue
        text, title = fetch_page(r["url"])
        r = {**r, "title": r.get("title") or title}
        junk = is_junk(text)
        if junk:
            log(f"⚠ blocked or empty page ({len(text)} chars): {text[:100]!r}")
        item = {**r, "ai_summary": "(page blocked or empty)" if junk else summarize(r, text), "page_blocked": junk, "tier": tier}
        kept.append(item)
        emit({**item, "status": "kept"})
        time.sleep(0.5)


def gather(queries, per_query, seen, label):
    found = []
    for i, q in enumerate(queries, 1):
        log(f"[{label} {i}/{len(queries)}] {q!r}")
        try:
            results = search(q, per_query)
        except Fatal:
            log("⚠ no results for this query, continuing")
            continue
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                found.append({**r, "matched_query": q})
        time.sleep(0.5)
    return found


def crawl_list():
    urls = list(CRAWL_URLS)
    if CRAWL_FILE:
        try:
            with open(CRAWL_FILE, encoding="utf-8") as f:
                urls += [l.strip() for l in f if l.strip() and not l.startswith("#")]
        except OSError as e:
            raise Fatal(f"Could not read CRAWL_URLS_FILE {CRAWL_FILE!r}: {e}")
    return list(dict.fromkeys(urls))


def run():
    if not QUERY:
        raise Fatal("SEARCH_QUERY is empty in .env")
    if not API_KEY:
        raise Fatal("HACKCLUB_API_KEY is missing from .env")
    pick_models()
    kept, seen = [], set()
    urls = crawl_list()
    if urls:
        log(f"Crawl mode: {len(urls)} fixed URL(s), search and filter skipped")
        items, smart = [{"title": "", "url": u, "snippet": ""} for u in urls], False
    elif EXPAND:
        queries = expand_query(QUERY, SUBQUERIES)
        for q in queries:
            log(f"→ {q}")
        items, smart = gather(queries, PER_SUBQUERY, seen, "query"), SMART_FILTER
        if not items:
            raise Fatal("None of the expanded queries returned results")
    else:
        items, smart = search(QUERY, NUM_RESULTS), SMART_FILTER
    seen.update(r["url"] for r in items)

    process(items, 1, smart, kept)
    if not kept:
        raise Fatal("Every result was filtered out. Try a broader query, more results, or SMART_FILTER=false")

    if DEEP:
        blocked = sum(1 for r in kept if r["page_blocked"])
        if blocked / len(kept) >= DEEP_RATIO:
            log(f"{blocked}/{len(kept)} pages blocked or junk, starting deep-scrape tier 2")
            queries = followup_queries(QUERY, kept, DEEP_QUERIES)
            for q in queries:
                log(f"→ follow-up: {q}")
            extra = gather(queries, DEEP_PER_QUERY, seen, "deep-dive")
            if extra:
                process(extra, 2, SMART_FILTER, kept)
            else:
                log("No new results from follow-up queries")
        else:
            log("Blocked ratio below threshold, skipping tier 2")

    log("Building overall AI overview...")
    emit({"status": "overview", "tier": "", "title": "AI overview", "ai_summary": overview(kept)})
    print(f"\n✓ Done. kept {len(kept)}, skipped {STATS['skipped']}\n  {os.path.join(OUT_DIR, 'results.csv')}\n  {os.path.join(OUT_DIR, 'log.csv')}")


if __name__ == "__main__":
    try:
        run()
    except Fatal as e:
        log(f"! {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log("⚠ stopped by user, rows so far are saved in the CSV")
        sys.exit(130)
    finally:
        RESULTS_F.close()
        LOG_F.close()